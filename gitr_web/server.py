#!/usr/bin/env python3
"""gitr-serve: the gitr diff view and review in the browser, for many repos and
their worktrees.

Run:  gitr-serve [--port 4487] [repo ...]     (from a checkout: python3 gitr_web/server.py)
With no repo arguments the project list is read from
~/.config/gitr/projects.json:  {"projects": ["~/dev/a", {"path": "~/dev/b", "name": "b"}]}

The browser gets the whole diff as one document (the same idea as gitr's
single Text widget) plus per-line metadata; word-diff ranges are fetched
separately in line windows so the first paint never waits for them.
"""

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if not __package__:
    # Run as a script from a checkout: gitr.py sits one level up. Installed,
    # both are on the path already.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gitr  # noqa: E402
from gitr import (COMMENT_KINDS, ReviewStore, _diff_side,  # noqa: E402
                  _ExportItem, _format_export, _match_anchor,
                  _pair_lines_for_word_diff, _read_text_safe,
                  _resolve_anchors, _word_matcher, parse_diff)

STATIC_DIR = Path(__file__).resolve().parent / 'static'
CONFIG_PATH = Path.home() / '.config' / 'gitr' / 'projects.json'
SETTINGS_PATH = (Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state')
                 / 'gitr' / 'web.json')
# Where the prototype (`gitr serve`) kept them; read until web.json exists.
OLD_SETTINGS_PATH = SETTINGS_PATH.with_name('serve.json')
MIN_FILE_LINES = 4  # see the padding in Doc._add_file
MAX_DOCS = 4  # a huge diff's document is tens of MB
UNTRACKED_MAX_BYTES = 2 * 1024 * 1024
TOKEN_RE = re.compile(r'\w+|[^\w\s]|\s+')

# Line kinds in the combined document, one char per line so the browser gets
# a compact string instead of a list.
K_FILE, K_HUNK, K_ADD, K_REM, K_CTX, K_META = 'f', 'h', 'a', 'r', 'c', 'm'
# A removed + added pair that differs only in whitespace, shown once as
# "~<new text>" (gitr's "collapse re-indented").
K_REIND = 'i'


# --git helpers ---------------------------------------------------------------

class GitError(Exception):
    pass


def git(cwd: str, *args: str, ok_codes: tuple[int, ...] = (0,)) -> str:
    try:
        p = subprocess.run(['git', '-c', 'core.quotePath=false', *args], cwd=cwd,
                           capture_output=True)
    except FileNotFoundError:
        raise GitError('git not found in PATH')
    if p.returncode not in ok_codes:
        raise GitError(p.stderr.decode('utf-8', 'replace').strip() or f'git {args[0]} failed')
    return p.stdout.decode('utf-8', 'replace')


def list_worktrees(repo: str) -> list[dict]:
    out = git(repo, 'worktree', 'list', '--porcelain')
    wts: list[dict] = []
    cur: dict = {}
    for line in out.splitlines() + ['']:
        if not line:
            if cur:
                wts.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(' ')
        if key == 'worktree':
            cur = {'path': val, 'branch': None, 'head': None, 'detached': False}
        elif key == 'HEAD':
            cur['head'] = val[:10]
        elif key == 'branch':
            cur['branch'] = val.removeprefix('refs/heads/')
        elif key == 'detached':
            cur['detached'] = True
        elif key in ('bare', 'prunable'):
            cur[key] = True
    return [w for w in wts if not w.get('bare') and not w.get('prunable')]


def default_base(repo: str) -> str:
    for ref in ('main', 'master'):
        if subprocess.call(['git', 'rev-parse', '--verify', '-q', f'refs/heads/{ref}'],
                           cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
            return ref
    try:
        return git(repo, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD').strip()
    except GitError:
        return 'HEAD'


def untracked_diff(wt: str) -> str:
    """Diff text for untracked files, synthesised here rather than one
    `git diff --no-index` process per file, which is slow for agents that
    create hundreds of files."""
    out = git(wt, 'ls-files', '--others', '--exclude-standard', '-z')
    parts: list[str] = []
    for rel in filter(None, out.split('\0')):
        # gitr's own review state lives in the worktree but is not under review.
        if rel == '.gitr' or rel.startswith('.gitr/'):
            continue
        p = Path(wt) / rel
        head = f'diff --git a/{rel} b/{rel}\nnew file mode 100644\n'
        try:
            if p.is_symlink() or not p.is_file():
                continue
            size = p.stat().st_size
            if size > UNTRACKED_MAX_BYTES:
                parts.append(head + f'Untracked file too large to show ({size} bytes)\n')
                continue
            data = p.read_bytes()
        except OSError:
            continue
        if b'\0' in data[:8192]:
            parts.append(head + f'Binary files /dev/null and b/{rel} differ\n')
            continue
        lines = data.decode('utf-8', 'replace').splitlines()
        if not lines:
            parts.append(head)
            continue
        body = ''.join(f'+{l}\n' for l in lines)
        parts.append(head + f'--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1,{len(lines)} @@\n' + body)
    return ''.join(parts)


# --combined document ---------------------------------------------------------

class Doc:
    """The whole diff as one text plus parallel per-line arrays (1-based
    line numbers in the browser map to index line-1 here)."""

    def __init__(self, diff_files, reindent: bool = False) -> None:
        self.reindent = reindent
        self.lines: list[str] = []
        self.kinds: list[str] = []
        self.old_no: list[int] = []
        self.new_no: list[int] = []
        self.files: list[dict] = []
        # (removed line numbers, added line numbers) per run of removed lines
        # followed by added lines; the word-diff pass works on these.
        self.blocks: list[tuple[list[int], list[int]]] = []
        # Post-image line a comment on this line is stored under (a removed
        # line uses the position it sits at); 0 where no comment can go.
        self.anchor_no: list[int] = []
        # Collapsed line -> (the removed line's text, its anchor): a comment
        # stored for either side of the pair belongs on the one shown line.
        self.reindent_src: dict[int, tuple[str, int]] = {}
        for df in diff_files:
            self._add_file(df)

    def _push(self, text: str, kind: str, old: 'int | None' = None, new: 'int | None' = None,
              anchor: 'int | None' = None) -> int:
        self.lines.append(text)
        self.anchor_no.append(anchor or 0)
        self.kinds.append(kind)
        self.old_no.append(old or 0)
        self.new_no.append(new or 0)
        return len(self.lines)

    def _add_file(self, df) -> None:
        title = f'{df.old_path} -> {df.path}' if df.status in 'RC' and df.old_path else df.path
        similarity = next((dl.text.rsplit(' ', 1)[-1] for dl in df.lines if dl.kind == 'fileheader'
                           and dl.text.startswith('similarity index ')), None)
        if similarity:
            title += f'  ({similarity} similar)'
        # The status letter leads the header so the browser's tokenizer can
        # tell a header from a +/-/space/@ line by its first character.
        start = self._push(f'{df.status} {title}', K_FILE)
        if not any(dl.kind == 'hunk' for dl in df.lines):
            # Every header is two lines, like file + hunk: the minimap's file
            # label needs the room, and headers look alike throughout.
            self._push('', K_HUNK)
        body_start = len(self.lines)
        adds = dels = 0
        # The current run of removed and added lines (with any "\ No newline
        # at end of file" between them), emitted as a whole by flush().
        run: list = []

        def flush() -> None:
            if run:
                self._emit_run(run)
                run.clear()

        has_hunk = False
        for dl in df.lines:
            k = dl.kind
            if k == 'fileheader':
                continue
            if k == 'hunk':
                flush()
                has_hunk = True
                self._push(dl.text, K_HUNK)
            elif k == 'removed':
                if any(d.kind == 'added' for d in run):
                    flush()
                run.append(dl)
                dels += 1
            elif k == 'added':
                run.append(dl)
                adds += 1
            elif dl.text.startswith('\\') and has_hunk:
                run.append(dl)
            elif not has_hunk:
                # The body of a hunkless file ("Binary files ... differ").
                flush()
                self._push(dl.text, K_META)
            else:
                flush()
                self._push(dl.text, K_CTX, old=dl.old_line_no, new=dl.new_line_no,
                           anchor=dl.new_line_no)
        flush()
        # git prints only header lines for an empty new or deleted file, a
        # pure rename or copy, or a mode change; without a note the bare
        # header looks like something is missing.
        if len(self.lines) == body_start:
            note = _header_only_note(df)
            if note:
                self._push(note, K_META)
        # The minimap labels each file with its name in a box ~6 lines tall
        # (8px font x 1.5 at 2px per line, app.js); a shorter file would have
        # the next file's label drawn over its own. 4 lines clear the text
        # itself and give up only the box's bottom margin.
        while len(self.lines) - start + 1 < MIN_FILE_LINES:
            self._push('', K_META)
        self.files.append({'path': df.path, 'oldPath': df.old_path, 'status': df.status,
                           'add': adds, 'del': dels, 'start': start,
                           'end': len(self.lines)})

    def _emit_run(self, run: list) -> None:
        rems = [d for d in run if d.kind == 'removed']
        adds = [d for d in run if d.kind == 'added']
        actions = None
        if self.reindent and rems and adds and _may_be_reindented(rems, adds):
            actions = _pair_lines_for_word_diff([d.text[1:] for d in rems],
                                                [d.text[1:] for d in adds])
            if not any(a[0] == 'pair' and a[3] for a in actions):
                actions = None
        if actions is None:
            rem_lns: list[int] = []
            add_lns: list[int] = []
            for d in run:
                if d.kind == 'removed':
                    rem_lns.append(self._push(d.text, K_REM, old=d.old_line_no, anchor=d.new_line_no))
                elif d.kind == 'added':
                    add_lns.append(self._push(d.text, K_ADD, new=d.new_line_no, anchor=d.new_line_no))
                else:
                    self._push(d.text, K_META)
            if rem_lns and add_lns:
                self.blocks.append((rem_lns, add_lns))
            return
        # gitr's order: the pairing's, each changed pair as - then + with its
        # own word-diff block, a whitespace-only pair as one "~" line.
        ri, ai = iter(rems), iter(adds)
        for act in actions:
            if act[0] == 'pair':
                old, new = next(ri), next(ai)
                if act[3]:
                    ln = self._push('~' + new.text[1:], K_REIND, old=old.old_line_no,
                                    new=new.new_line_no, anchor=new.new_line_no)
                    self.reindent_src[ln] = (old.text, old.new_line_no)
                    continue
                r = self._push(old.text, K_REM, old=old.old_line_no, anchor=old.new_line_no)
                a = self._push(new.text, K_ADD, new=new.new_line_no, anchor=new.new_line_no)
                self.blocks.append(([r], [a]))
            elif act[0] == 'rem':
                d = next(ri)
                self._push(d.text, K_REM, old=d.old_line_no, anchor=d.new_line_no)
            else:
                d = next(ai)
                self._push(d.text, K_ADD, new=d.new_line_no, anchor=d.new_line_no)
        for d in run:
            if d.kind not in ('removed', 'added'):
                self._push(d.text, K_META)

    def file_at(self, line: int) -> dict:
        lo, hi = 0, len(self.files)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.files[mid]['start'] <= line:
                lo = mid + 1
            else:
                hi = mid
        return self.files[lo - 1]

    def word_ranges(self, first: int, last: int) -> list[int]:
        """Changed-token column ranges for the blocks starting in
        [first, last], flattened as line, startCol, endCol (Monaco 1-based,
        endCol exclusive)."""
        out: list[int] = []
        for rem_lines, add_lines in self.blocks:
            if rem_lines[0] < first:
                continue
            if rem_lines[0] > last:
                break
            actions = _pair_lines_for_word_diff([self.lines[i - 1][1:] for i in rem_lines],
                                                [self.lines[i - 1][1:] for i in add_lines])
            ri = ai = 0
            for act in actions:
                if act[0] == 'pair':
                    self._token_ranges(rem_lines[ri], add_lines[ai], out)
                    ri += 1
                    ai += 1
                elif act[0] == 'rem':
                    ri += 1
                else:
                    ai += 1
        return out

    def _token_ranges(self, line_old: int, line_new: int, out: list[int]) -> None:
        tok_old = TOKEN_RE.findall(self.lines[line_old - 1][1:]) or ['']
        tok_new = TOKEN_RE.findall(self.lines[line_new - 1][1:]) or ['']
        col_old = col_new = 2  # column 1 is the +/- marker
        for op, i1, i2, j1, j2 in _word_matcher(tok_old, tok_new).get_opcodes():
            t_old = ''.join(tok_old[i1:i2])
            t_new = ''.join(tok_new[j1:j2])
            if op != 'equal':
                if t_old and not t_old.isspace():
                    out += (line_old, col_old, col_old + _u16len(t_old))
                if t_new and not t_new.isspace():
                    out += (line_new, col_new, col_new + _u16len(t_new))
            col_old += _u16len(t_old)
            col_new += _u16len(t_new)


def _header_only_note(df) -> str:
    modes = {dl.text[:8]: dl.text[9:] for dl in df.lines
             if dl.kind == 'fileheader' and dl.text.startswith(('old mode ', 'new mode '))}
    if len(modes) == 2:
        return f"(mode {modes['old mode']} -> {modes['new mode']})"
    if df.status in ('A', 'D'):
        return '(empty file)'
    if df.status == 'R':
        return '(renamed, no content changes)'
    if df.status == 'C':
        return '(copied, no content changes)'
    return ''


def _nws_tokens(text: str) -> tuple:
    return tuple(t for t in TOKEN_RE.findall(text) if not t.isspace())


def _may_be_reindented(rems: list, adds: list) -> bool:
    """Whether some removed and added line have the same non-space tokens.
    The pairing that finds re-indented lines is quadratic per run; most runs
    have no candidate and skip it."""
    old = {_nws_tokens(d.text[1:]) for d in rems}
    return any(_nws_tokens(d.text[1:]) in old for d in adds)


def _u16len(s: str) -> int:
    # Monaco columns count UTF-16 code units: characters outside the BMP
    # (most emoji) take two.
    return len(s) if s.isascii() else len(s.encode('utf-16-le')) // 2


# --state -----------------------------------------------------------------------

class State:
    def __init__(self, repos: list[dict]) -> None:
        self.repos = repos
        self._lock = threading.Lock()
        # review.json is read, changed and written back as a whole
        self._review_lock = threading.Lock()
        self._settings_lock = threading.Lock()
        # Last document per (worktree, base): word-diff windows and the
        # change check are served from it without recomputing the diff.
        # Bounded by count, not by worktree, so another tab (or another base on
        # the same worktree) keeps its document while this one reloads.
        self._docs: OrderedDict[str, Doc] = OrderedDict()

    def projects(self) -> list[dict]:
        out = []
        for r in self.repos:
            entry = {'name': r['name'], 'path': r['path']}
            try:
                entry['worktrees'] = list_worktrees(r['path'])
                entry['defaultBase'] = default_base(r['path'])
                with self._settings_lock:
                    saved = self._load_settings()
                entry['settings'] = saved['projects'].get(r['path'])
                for w in entry['worktrees']:
                    real = os.path.realpath(w['path'])
                    w['settings'] = saved['worktrees'].get(real)
                    w['position'] = saved['positions'].get(real)
            except GitError as e:
                entry['error'] = str(e)
                entry['worktrees'] = []
            out.append(entry)
        return out

    def check_worktree(self, wt: str) -> str:
        """Only worktrees of configured projects may be read, so the server
        cannot be pointed at an arbitrary directory."""
        return self._find_worktree(wt)[0]

    def _find_worktree(self, wt: str) -> 'tuple[str, str]':
        wt = os.path.realpath(wt)
        for r in self.repos:
            try:
                if any(os.path.realpath(w['path']) == wt for w in list_worktrees(r['path'])):
                    return wt, r['path']
            except GitError:
                continue
        raise GitError(f'not a configured worktree: {wt}')

    # --view settings: base and options per worktree, and the last ones used
    # in each project as the start for a worktree seen for the first time.

    def _load_settings(self) -> dict:
        try:
            path = SETTINGS_PATH if SETTINGS_PATH.exists() else OLD_SETTINGS_PATH
            obj = json.loads(path.read_text())
        except (OSError, ValueError):
            obj = {}
        if not isinstance(obj, dict):
            obj = {}
        return {'worktrees': dict(obj.get('worktrees') or {}),
                'projects': dict(obj.get('projects') or {}),
                'positions': dict(obj.get('positions') or {})}

    def save_settings(self, req: dict) -> dict:
        wt, repo = self._find_worktree(str(req['wt']))
        settings = {'base': str(req['base']), 'untracked': bool(req.get('untracked')),
                    'wrap': bool(req.get('wrap')), 'reindent': bool(req.get('reindent'))}
        with self._settings_lock:
            saved = self._load_settings()
            saved['worktrees'][wt] = settings
            saved['projects'][repo] = settings
            self._write_settings(saved)
        return settings

    def save_position(self, req: dict) -> dict:
        """Where the view was in a worktree's diff: a file and a line in it.
        Kept apart from the settings: a new worktree takes its project's last
        settings, but a position only means something in its own diff."""
        wt = self.check_worktree(str(req['wt']))
        pos = req.get('pos')
        pos = {'file': str(pos['file']), 'at': int(pos.get('at') or 0),
               'off': int(pos.get('off') or 0)} if isinstance(pos, dict) else None
        with self._settings_lock:
            saved = self._load_settings()
            if pos:
                saved['positions'][wt] = pos
            else:
                saved['positions'].pop(wt, None)
            self._write_settings(saved)
        return {'ok': True}

    @staticmethod
    def _write_settings(saved: dict) -> None:
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = SETTINGS_PATH.with_name(f'.{SETTINGS_PATH.name}.tmp')
            tmp.write_text(json.dumps(saved, indent=2))
            tmp.replace(SETTINGS_PATH)
        except OSError as e:
            raise GitError(f'cannot save settings to {SETTINGS_PATH}: {e}')

    def diff(self, wt: str, base: str, untracked: bool, reindent: bool = False) -> dict:
        wt = self.check_worktree(wt)
        t0 = time.perf_counter()
        mb = merge_base(wt, base)
        # Taken before the diff: an edit landing while the diff runs then
        # shows up as a change instead of hiding in the baseline.
        fp = fingerprint(wt, mb, untracked)
        # parse_diff finds paths by the a/ b/ prefixes, which diff.noprefix
        # or diff.mnemonicPrefix in the user's config would change.
        text = git(wt, 'diff', '--no-color', '--no-ext-diff',
                   '--src-prefix=a/', '--dst-prefix=b/', mb)
        if untracked:
            text += untracked_diff(wt)
        t_git = time.perf_counter()
        doc = Doc(parse_diff(text), reindent)
        t_doc = time.perf_counter()
        doc_id = hashlib.sha1(f'{wt}\0{base}\0{time.time()}'.encode()).hexdigest()[:16]
        with self._lock:
            self._docs[f'{wt}\0{doc_id}'] = doc
            while len(self._docs) > MAX_DOCS:
                self._docs.popitem(last=False)
        return {
            'id': doc_id, 'worktree': wt, 'base': base, 'mergeBase': mb[:10],
            'fingerprint': fp,
            'text': '\n'.join(doc.lines), 'kinds': ''.join(doc.kinds),
            'oldNo': doc.old_no, 'newNo': doc.new_no, 'files': doc.files,
            'timing': {'git_ms': round((t_git - t0) * 1000),
                       'doc_ms': round((t_doc - t_git) * 1000)},
        }

    def _doc(self, wt: str, doc_id: str) -> 'Doc':
        with self._lock:
            key = f'{os.path.realpath(wt)}\0{doc_id}'
            doc = self._docs.get(key)
            if doc is not None:
                self._docs.move_to_end(key)
        if doc is None:
            raise GitError('stale document, reload the diff')
        return doc

    # --comments: gitr's .gitr/review.json in each worktree, so a review can
    # move between the web page and a gitr window.

    def comments(self, wt: str, doc_id: str) -> dict:
        wt = self.check_worktree(wt)
        doc = self._doc(wt, doc_id)
        with self._review_lock:
            store = ReviewStore(Path(wt) / '.gitr')
            out, items = resolve_comments(wt, doc, store)
            # A gitr window edits review.json without knowing about review.md.
            md, js = Path(wt) / '.gitr' / 'review.md', Path(wt) / '.gitr' / 'review.json'
            if js.exists() and (not md.exists() or md.stat().st_mtime_ns < js.stat().st_mtime_ns):
                write_review_md(wt, items)
        return out

    def save_comment(self, wt: str, doc_id: str, req: dict) -> dict:
        wt = self.check_worktree(wt)
        doc = self._doc(wt, doc_id)
        text = str(req.get('text') or '').strip('\n')
        kind = req.get('kind') if req.get('kind') in COMMENT_KINDS else 'note'
        with self._review_lock:
            store = ReviewStore(Path(wt) / '.gitr')
            if req.get('key'):
                file = str(req['file'])
                snap, line_no, side = req['key']
                entry = next((e for e in store._data.get(file, [])
                              if e.get('snapshot') == snap and e.get('line_no') == line_no
                              and e.get('side') == side), None)
                if entry is None:
                    raise GitError('comment no longer exists, reload')
                if text:
                    store.add(file, snap, line_no, side, entry.get('line_text', ''), text, kind)
                else:
                    store.delete(file, snap, line_no, side)
            elif text:
                line = int(req['line'])
                if not 1 <= line <= len(doc.lines) or not doc.anchor_no[line - 1]:
                    raise GitError('no comment can go on this line')
                file = doc.file_at(line)['path']
                anchor, side, line_text = line_sides(doc, line)[0]
                current = _read_text_safe(Path(wt) / file)
                if current is None:
                    raise GitError(f'{file} cannot be read, so the comment cannot be anchored')
                # The snapshot is the file as it is now; line numbers from an
                # older diff would anchor the comment to the wrong line.
                if line_text[0] != '-':
                    cur = current.splitlines()
                    if anchor > len(cur) or cur[anchor - 1] != line_text[1:]:
                        raise GitError(f'{file} changed since the diff was loaded, reload first')
                snap = store.write_snapshot(current)
                if snap is None:
                    raise GitError(f'cannot write a snapshot to {wt}/.gitr')
                store.add(file, snap, anchor, side, line_text, text, kind)
            out, items = resolve_comments(wt, doc, store)
            write_review_md(wt, items)
        return out

    def clear_comments(self, wt: str, doc_id: str) -> dict:
        wt = self.check_worktree(wt)
        doc = self._doc(wt, doc_id)
        with self._review_lock:
            store = ReviewStore(Path(wt) / '.gitr')
            if not store.clear(store.backup_path()):
                raise GitError('could not back up review.json, nothing cleared')
            out, items = resolve_comments(wt, doc, store)
            write_review_md(wt, items)
        return out

    def export(self, wt: str, doc_id: str) -> dict:
        wt = self.check_worktree(wt)
        doc = self._doc(wt, doc_id)
        with self._review_lock:
            _, items = resolve_comments(wt, doc, ReviewStore(Path(wt) / '.gitr'))
        return {'text': _format_export(items, list(COMMENT_KINDS)), 'count': len(items)}

    def word_ranges(self, wt: str, doc_id: str, first: int, last: int) -> list[int]:
        return self._doc(wt, doc_id).word_ranges(first, last)


KIND_NAMES = {K_ADD: 'added', K_REM: 'removed', K_CTX: 'context', K_REIND: 'added'}


def line_sides(doc: 'Doc', ln: int) -> 'list[tuple[int, str, str]]':
    """(anchor, side, diff line text) a comment on this document line can be
    stored under; a collapsed line stands for both its removed and added
    line, the added one first."""
    no = doc.anchor_no[ln - 1]
    if not no:
        return []
    kind, text = doc.kinds[ln - 1], doc.lines[ln - 1]
    if kind == K_REIND:
        old_text, old_anchor = doc.reindent_src[ln]
        return [(no, '+', '+' + text[1:]), (old_anchor, '-', old_text)]
    return [(no, _diff_side(KIND_NAMES[kind]), text)]


def review_stamp(wt: str) -> int:
    try:
        return os.stat(os.path.join(wt, '.gitr', 'review.json')).st_mtime_ns
    except OSError:
        return 0


def resolve_comments(wt: str, doc: 'Doc', store: ReviewStore) -> 'tuple[dict, list[_ExportItem]]':
    """Stored comments placed on the document's lines the way a gitr window
    places them: a comment on a line no longer in the diff goes to the end
    of its file, marked orphaned. Also returns the agent export items in the
    same order (files not in this diff last)."""
    anchors = _resolve_anchors(store, lambda f: _read_text_safe(Path(wt) / f))
    shown: list[dict] = []
    items: list[_ExportItem] = []

    def add(a, line: int, line_no: 'int | None', orphan: bool, elsewhere: bool = False) -> None:
        shown.append({'line': line, 'file': a.file, 'key': [a.snapshot, a.snap_line_no, a.side],
                      'kind': a.kind, 'text': a.comment, 'moved': a.moved, 'orphan': orphan,
                      'elsewhere': elsewhere, 'lineNo': line_no, 'lineText': a.line_text})
        items.append(_ExportItem(a.file, line_no, a.side, a.kind, a.line_text,
                                 a.comment, a.moved, orphan))

    for f in doc.files:
        alist = anchors.pop(f['path'], None)
        if not alist:
            continue
        for ln in range(f['start'], f['end'] + 1):
            for no, side, text in line_sides(doc, ln):
                a = _match_anchor(alist, no, side, text)
                if a is not None:
                    add(a, ln, no, False)
        for a in alist:
            if not a.matched:
                add(a, f['end'], a.target_line_no, True)
    # Comments on files not in this diff go in a section after the last line.
    elsewhere = 0
    for file in sorted(anchors):
        for a in anchors[file]:
            elsewhere += 1
            add(a, len(doc.lines), a.target_line_no, True, True)
    return {'comments': shown, 'elsewhere': elsewhere, 'stamp': review_stamp(wt)}, items


def write_review_md(wt: str, items: 'list[_ExportItem]') -> None:
    """The agent export as a file, so an agent can be told to read
    .gitr/review.md instead of being handed the text."""
    path = Path(wt) / '.gitr' / 'review.md'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name('.review.md.tmp')
        tmp.write_text(_format_export(items, list(COMMENT_KINDS)) + '\n')
        tmp.replace(path)
    except OSError as e:
        print(f'gitr-serve: cannot write {path}: {e}', file=sys.stderr)


def merge_base(wt: str, base: str) -> str:
    # The base comes from the browser; a leading dash would be read as an option.
    if not base or base.startswith('-'):
        raise GitError(f'invalid base: {base!r}')
    return git(wt, 'merge-base', base, 'HEAD').strip()


def fingerprint(wt: str, merge_base: str, untracked: bool) -> str:
    """Cheap change detector: HEAD, the status listing and the size/mtime of
    every listed path. Agents editing a file change its mtime even when the
    status line stays the same."""
    head = git(wt, 'rev-parse', 'HEAD').strip()
    # Without untracked files in the diff, a new scratch file an agent drops
    # must not raise the "changed" banner.
    status = git(wt, 'status', '--porcelain=v1', '-z',
                 '--untracked-files=' + ('all' if untracked else 'no'))
    h = hashlib.sha1(f'{head}\0{merge_base}\0'.encode())
    for entry in filter(None, status.split('\0')):
        rel = entry[3:] if len(entry) > 3 and entry[2] == ' ' else entry
        # gitr's review state is left out of the diff, so writing it is no change.
        if rel == '.gitr' or rel.startswith('.gitr/'):
            continue
        h.update(f'{entry}\0'.encode())
        try:
            st = os.stat(os.path.join(wt, rel))
            h.update(f'{rel}\0{st.st_size}\0{st.st_mtime_ns}\0'.encode())
        except OSError:
            pass
    return h.hexdigest()[:16]


# --http --------------------------------------------------------------------------

APP_NAME = 'gitr - ' + socket.gethostname().split('.')[0]

ALLOWED_HOSTS = {'localhost', '127.0.0.1', '::1'}

CONTENT_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                 '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.ttf': 'font/ttf',
                 '.webmanifest': 'application/manifest+json'}


class Handler(BaseHTTPRequestHandler):
    state: State  # set on the class by main()

    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get('GITR_SERVE_LOG'):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, ctype: str, etag: str | None = None) -> None:
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        if etag:
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('ETag', etag)
        else:
            self.send_header('Cache-Control', 'no-store')
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The page went away mid-request (reload, worktree switch, a
            # poll cut off); nobody is left to answer.
            self.close_connection = True

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, separators=(',', ':')).encode(), 'application/json')

    def _host_ok(self) -> bool:
        # Binding to localhost does not stop DNS rebinding: a page from another
        # site can resolve its own name to 127.0.0.1 and read the API. The Host
        # header still carries that name, so only loopback names are served,
        # plus the names given with --allow-host.
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]').lower()
        return host in ALLOWED_HOSTS

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._send(HTTPStatus.FORBIDDEN, b'forbidden host', 'text/plain')
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == '/api/projects':
                return self._json(self.state.projects())
            if url.path == '/api/diff':
                return self._json(self.state.diff(q['wt'], q['base'], q.get('untracked') == '1',
                                                  q.get('reindent') == '1'))
            if url.path == '/api/words':
                return self._json(self.state.word_ranges(q['wt'], q['id'],
                                                         int(q['from']), int(q['to'])))
            if url.path == '/api/fingerprint':
                wt = self.state.check_worktree(q['wt'])
                mb = merge_base(wt, q['base'])
                return self._json({'fingerprint': fingerprint(wt, mb, q.get('untracked') == '1'),
                                   'review': review_stamp(wt)})
            if url.path == '/api/comments':
                return self._json(self.state.comments(q['wt'], q['id']))
            if url.path == '/api/export':
                return self._json(self.state.export(q['wt'], q['id']))
        except KeyError as e:
            return self._json({'error': f'missing parameter {e}'}, 400)
        except ValueError as e:
            return self._json({'error': f'bad parameter: {e}'}, 400)
        except GitError as e:
            return self._json({'error': str(e)}, 400)
        self._static(url.path)

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._send(HTTPStatus.FORBIDDEN, b'forbidden host', 'text/plain')
        # A form on another site can post here (the Host is ours then), but
        # only with a "simple" content type and its own Origin; a JSON body
        # needs a CORS preflight, which this server never grants.
        origin = urlparse(self.headers.get('Origin') or '').hostname
        if (self.headers.get('Content-Type') != 'application/json'
                or (origin is not None and origin not in ALLOWED_HOSTS)):
            return self._send(HTTPStatus.FORBIDDEN, b'forbidden', 'text/plain')
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n > 1 << 20:
                return self._json({'error': 'request too large'}, 413)
            req = json.loads(self.rfile.read(n) or b'{}')
            path = urlparse(self.path).path
            if path == '/api/comment':
                return self._json(self.state.save_comment(req['wt'], req['id'], req))
            if path == '/api/position':
                return self._json(self.state.save_position(req))
            if path == '/api/settings':
                return self._json(self.state.save_settings(req))
            if path == '/api/comments/clear':
                return self._json(self.state.clear_comments(req['wt'], req['id']))
        except KeyError as e:
            return self._json({'error': f'missing parameter {e}'}, 400)
        except (ValueError, TypeError) as e:
            return self._json({'error': f'bad request: {e}'}, 400)
        except GitError as e:
            return self._json({'error': str(e)}, 400)
        self._send(HTTPStatus.NOT_FOUND, b'not found', 'text/plain')

    def _static(self, path: str) -> None:
        rel = 'index.html' if path in ('', '/') else path.lstrip('/')
        p = (STATIC_DIR / rel).resolve()
        if STATIC_DIR not in p.parents or not p.is_file():
            return self._send(HTTPStatus.NOT_FOUND, b'not found', 'text/plain')
        # Revalidated on every load, so an edited page file shows on refresh,
        # but the 4 MB Monaco bundle is not sent again through the tunnel.
        st = p.stat()
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        if self.headers.get('If-None-Match') == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header('ETag', etag)
            self.end_headers()
            return
        body = p.read_bytes()
        # The machine's name goes into the window title and the installed
        # app's name: apps installed from two machines (one through a
        # tunnel) must be told apart in the dock and the app switcher.
        if rel == 'index.html':
            body = body.replace(b'<title>gitr</title>', f'<title>{APP_NAME}</title>'.encode())
        elif rel == 'manifest.webmanifest':
            manifest = json.loads(body)
            manifest['name'] = manifest['short_name'] = APP_NAME
            body = json.dumps(manifest, indent=2).encode()
        self._send(200, body, CONTENT_TYPES.get(p.suffix, 'application/octet-stream'), etag)


def read_config() -> dict:
    return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}


def load_repos(args_repos: list[str]) -> list[dict]:
    items: list = list(args_repos) or read_config().get('projects', [])
    repos = []
    for it in items:
        path = it if isinstance(it, str) else it['path']
        path = os.path.realpath(os.path.expanduser(path))
        name = it.get('name') if isinstance(it, dict) else None
        repos.append({'path': path, 'name': name or os.path.basename(path)})
    return repos


RELOAD_CHILD_ENV = 'GITR_SERVE_CHILD'


def supervise() -> None:
    """--reload: run the server in a child process and start a new one when
    the Python source changes. A separate process rather than re-exec'ing
    itself, so a save with a syntax error leaves the supervisor waiting for
    the next save instead of ending it. The page files are read from disk on
    every request and need no restart."""
    here = Path(__file__).resolve()
    watched = [here, Path(gitr.__file__).resolve()]

    def stamp() -> tuple:
        out = []
        for p in watched:
            try:
                out.append(p.stat().st_mtime_ns)
            except OSError:
                out.append(0)
        return tuple(out)

    cmd = [sys.executable, str(here), *sys.argv[1:]]
    env = dict(os.environ, **{RELOAD_CHILD_ENV: '1'})
    # A plain `kill` of the supervisor must not leave the server running.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    child = None
    try:
        while True:
            seen = stamp()
            child = subprocess.Popen(cmd, env=env)
            while stamp() == seen:
                time.sleep(0.5)
            print('gitr-serve: source changed, restarting', file=sys.stderr)
            child.terminate()
            child.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if child is not None:
            if child.poll() is None:
                child.terminate()
            child.wait()


def tilde(path: str) -> str:
    path = os.path.realpath(os.path.expanduser(path))
    home = os.path.expanduser('~')
    return '~' + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def config_template(args_repos: list[str]) -> str:
    """projects.json for the repos named, else the one the cwd is in, else
    an example. Paths under the home directory are written with ~."""
    paths = [tilde(p) for p in args_repos]
    if not paths:
        try:
            paths = [tilde(git(os.getcwd(), 'rev-parse', '--show-toplevel').strip())]
        except GitError:
            pass
    projects: list = paths or ['~/dev/repo-a', {'path': '~/dev/repo-b', 'name': 'b'}]
    return json.dumps({'projects': projects, 'allow_hosts': tailscale_names()}, indent=2)


def tailscale_names() -> list[str]:
    """This machine's name on the tailnet, the one `tailscale serve` is
    reached by, if Tailscale runs here."""
    try:
        out = subprocess.run(['tailscale', 'status', '--json'], capture_output=True,
                             text=True, timeout=3).stdout
        name = json.loads(out)['Self']['DNSName'].rstrip('.')
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return []
    return [name] if name else []


def main() -> None:
    ap = argparse.ArgumentParser(prog='gitr-serve', description='gitr diff view and review in the browser')
    ap.add_argument('repos', nargs='*', help=f'repositories (default: {CONFIG_PATH})')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=4487)  # "gitr" on a phone keypad
    ap.add_argument('--allow-host', action='append', default=[], metavar='NAME',
                    help='also answer to this host name, e.g. the machine\'s name under '
                         '`tailscale serve` (repeatable; added to "allow_hosts" in '
                         f'{CONFIG_PATH.name}); the server still listens on --host')
    ap.add_argument('--reload', action='store_true',
                    help='restart when server.py or gitr.py changes (for development)')
    ap.add_argument('--print-config', action='store_true',
                    help=f'print a {CONFIG_PATH.name} for the given repos (or the current one) '
                         f'to stdout, to save as {CONFIG_PATH}')
    a = ap.parse_args()
    if a.print_config:
        print(config_template(a.repos))
        # stderr, so a redirect to the config file gets the JSON alone
        print(f'gitr-serve: save as {CONFIG_PATH}, e.g. with: '
              f'gitr-serve --print-config ... > {CONFIG_PATH}', file=sys.stderr)
        return
    if a.reload and not os.environ.get(RELOAD_CHILD_ENV):
        return supervise()
    repos = load_repos(a.repos)
    if not repos:
        sys.exit(f'gitr-serve: no repositories given and none in {CONFIG_PATH}\n'
                 f'  create it with: gitr-serve --print-config [repo ...] > {CONFIG_PATH}')
    Handler.state = State(repos)
    ALLOWED_HOSTS.add(a.host.lower())
    # Machine settings: they apply also when the repos come from the command line.
    allow = [*read_config().get('allow_hosts', []), *a.allow_host]
    ALLOWED_HOSTS.update(h.lower().rstrip('.') for h in allow)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f'gitr-serve: http://{a.host}:{a.port}/', file=sys.stderr)
    for h in allow:
        print(f'  also answering as {h}', file=sys.stderr)
    width = max(len(r['name']) for r in repos)
    for r in repos:
        print(f"  {r['name']:<{width}}  {tilde(r['path'])}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
