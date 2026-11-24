#!/usr/bin/env python3
"""gitr-serve: the gitr diff view and review in the browser, for many repos and
their worktrees.

Run:  gitr-serve [--port 4487] [repo ...]     (from a checkout: python3 gitr_web/server.py)
With no repo arguments the project list is read from
~/.config/gitr/projects.json:  {"projects": ["~/dev/a", {"path": "~/dev/b", "name": "b"}]}
A directory of checkouts: {"path": "~/dev/ws", "multi": true, "depth": 1}

The browser gets the whole diff as one document (the same idea as gitr's
single Text widget) plus per-line metadata; word-diff ranges are fetched
separately in line windows so the first paint never waits for them.
"""

import argparse
import bisect
import difflib
import hashlib
import heapq
import json
import os
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import zlib
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.client import HTTPConnection
from typing import Iterator
from urllib.parse import parse_qs, urlencode, urlparse

if not __package__:
    # Run as a script from a checkout: gitr.py sits one level up. Installed,
    # both are on the path already.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gitr  # noqa: E402
from gitr import (COMMENT_KINDS, ReviewStore, ViewedStore, _diff_side,  # noqa: E402
                  _ExportItem, _format_export, _match_anchor,
                  _pair_lines_for_word_diff, _read_text_safe,
                  _resolve_anchors, _word_matcher, parse_diff)

STATIC_DIR = Path(__file__).resolve().parent / 'static'
STARTED = str(time.time_ns())
CONFIG_PATH = Path.home() / '.config' / 'gitr' / 'projects.json'
SETTINGS_PATH = (Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state')
                 / 'gitr' / 'web.json')
# Where the prototype (`gitr serve`) kept them; read until web.json exists.
OLD_SETTINGS_PATH = SETTINGS_PATH.with_name('serve.json')
MIN_FILE_LINES = 4  # see the padding in Doc._add_file
# Documents kept besides each worktree's last one (which the page keeps
# to show again on switching back), and a bound on all of them together: a
# huge diff's document is tens of MB.
MAX_DOCS = 4
MAX_DOC_LINES = 1_000_000
# Past review diffs a reload is compared with, as hashes of their + and -
# lines (see baseline()), so many more of them than documents.
MAX_BASELINES = 64
UNTRACKED_MAX_BYTES = 2 * 1024 * 1024
TOKEN_RE = re.compile(r'\w+|[^\w\s]|\s+')

# Line kinds in the combined document, one char per line so the browser gets
# a compact string instead of a list.
K_FILE, K_HUNK, K_ADD, K_REM, K_CTX, K_META = 'f', 'h', 'a', 'r', 'c', 'm'
# A removed + added pair that differs only in whitespace, shown once as
# "~<new text>" (gitr's "collapse re-indented").
K_REIND = 'i'
K_COMMIT = 'k'  # the commit header and message before a single commit's diff
EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
MAX_COMMITS = 1000
MAX_OLDER = 1000  # commits before the base the commits pane may ask for
UNCOMMITTED = 'uncommitted'  # the commits pane's row for the working tree
UNTRACKED = 'untracked'  # ... for the untracked files, above it
ALL = 'all'  # ... for the whole review, as the older end of a range


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
            # The blob id as git prints it, so that a changed binary file
            # differs in its header (file_sig) though its diff text does not.
            blob = hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()
            parts.append(head + f'index 0000000..{blob[:7]}\n'
                         f'Binary files /dev/null and b/{rel} differ\n')
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

    def __init__(self, diff_files, reindent: bool = False, prefix: 'list[str]' = ()) -> None:
        self.reindent = reindent
        # How comments find "the file as it is now": the worktree, or for a
        # single commit's diff the file in that commit. None: the worktree.
        self.read_file = None
        # A single commit's (or the uncommitted changes') diff instead of
        # the whole review; the agent export still comes from the whole one.
        self.commit: 'str | None' = None
        self.lines: list[str] = []
        self.kinds: list[str] = []
        self.old_no: list[int] = []
        self.new_no: list[int] = []
        self.files: list[dict] = []
        # (removed line numbers, added line numbers) per run of removed lines
        # followed by added lines; the word-diff pass works on these.
        self.blocks: list[tuple[list[int], list[int]]] = []
        # The re-indent pass's unpaired lines in runs of both removed and
        # added lines (word_ranges finds the other runs' itself).
        self.lone: list[int] = []
        # Post-image line a comment on this line is stored under (a removed
        # line uses the position it sits at); 0 where no comment can go.
        self.anchor_no: list[int] = []
        # Collapsed line -> (the removed line's text, its anchor): a comment
        # stored for either side of the pair belongs on the one shown line.
        self.reindent_src: dict[int, tuple[str, int]] = {}
        # Per file, the document line of each of its + and - lines in diff
        # order (changed_lines' order); a collapsed pair maps both to its ~ line.
        self.change_lines: dict[str, list[int]] = {}
        self.file_sigs: dict[str, str] = {}  # file_sig() per path
        # [first line, last line, hunk_key()] per hunk, in document order; a
        # file without hunks (binary, empty, a pure rename) is one.
        self.hunks: list[list] = []
        self._line_of: dict[int, int] = {}
        for line in prefix:
            self._push(line, K_COMMIT)
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
        first_hunk = len(self.hunks)
        hunk_lines: list[str] = []  # the + and - lines of the current hunk
        seen: dict[str, int] = {}

        def close_hunk() -> None:
            if len(self.hunks) > first_hunk:
                self.hunks[-1][1] = len(self.lines)
                self.hunks[-1][2] = hunk_key(df.path, hunk_lines, seen)
                hunk_lines.clear()

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
                close_hunk()
                has_hunk = True
                self.hunks.append([self._push(dl.text, K_HUNK), 0, ''])
            elif k == 'removed':
                if any(d.kind == 'added' for d in run):
                    flush()
                run.append(dl)
                hunk_lines.append(dl.text)
                dels += 1
            elif k == 'added':
                run.append(dl)
                hunk_lines.append(dl.text)
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
        close_hunk()
        self.change_lines[df.path] = [self._line_of[id(dl)] for dl in df.lines
                                      if dl.kind in ('added', 'removed')]
        self._line_of.clear()
        self.file_sigs[df.path] = file_sig(df)
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
        if has_hunk:
            self.hunks[-1][1] = len(self.lines)
        else:
            body = [dl.text for dl in df.lines if dl.kind != 'fileheader']
            self.hunks.append([start + 1, len(self.lines),
                               hunk_key(df.path, [file_sig(df), *body], seen)])
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
                    self._line_of[id(d)] = rem_lns[-1]
                elif d.kind == 'added':
                    add_lns.append(self._push(d.text, K_ADD, new=d.new_line_no, anchor=d.new_line_no))
                    self._line_of[id(d)] = add_lns[-1]
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
                    self._line_of[id(old)] = self._line_of[id(new)] = ln
                    continue
                r = self._push(old.text, K_REM, old=old.old_line_no, anchor=old.new_line_no)
                a = self._push(new.text, K_ADD, new=new.new_line_no, anchor=new.new_line_no)
                self._line_of[id(old)], self._line_of[id(new)] = r, a
                self.blocks.append(([r], [a]))
            elif act[0] == 'rem':
                d = next(ri)
                self._line_of[id(d)] = self._push(d.text, K_REM, old=d.old_line_no, anchor=d.new_line_no)
                self.lone.append(self._line_of[id(d)])
            else:
                d = next(ai)
                self._line_of[id(d)] = self._push(d.text, K_ADD, new=d.new_line_no, anchor=d.new_line_no)
                self.lone.append(self._line_of[id(d)])
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
        endCol exclusive). A line of such a block left unpaired, added or
        removed whole, comes as line, 0, 0: the page tints it as one, not as
        an edited line."""
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
                    out += (rem_lines[ri], 0, 0)
                    ri += 1
                else:
                    out += (add_lines[ai], 0, 0)
                    ai += 1
        for ln in self.lone[bisect.bisect_left(self.lone, first):]:
            if ln > last:
                break
            out += (ln, 0, 0)
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
        self._entries = repos
        self._lock = threading.Lock()
        # review.json is read, changed and written back as a whole
        self._review_lock = threading.Lock()
        self._settings_lock = threading.Lock()
        self._repos_lock = threading.Lock()
        self._repos: 'tuple[float, list[dict]] | None' = None  # (when, expand_repos())
        # Documents by (worktree, id), least recently used first: word-diff
        # windows, comments and the change check are served from them without
        # recomputing the diff. See _evict_docs.
        self._docs: OrderedDict[str, Doc] = OrderedDict()
        # (worktree, doc id) -> (base, baseline()), see new_since
        self._baselines: OrderedDict[str, tuple] = OrderedDict()
        self._md_dropped: set[str] = set()  # worktrees drop_review_md has run for

    @property
    def repos(self) -> list[dict]:
        # Rescanned every few seconds, so a checkout added to a multi-repo
        # directory shows up without a restart; not on every request, as each
        # one checks its worktree against these.
        with self._repos_lock:
            now = time.monotonic()
            if self._repos is None or now - self._repos[0] > REPOS_S:
                self._repos = (now, expand_repos(self._entries))
            return self._repos[1]

    def projects(self) -> list[dict]:
        out = []
        with self._settings_lock:
            saved = self._load_settings()
        for r in self.repos:
            entry = {'name': r['name'], 'path': r['path']}
            if 'group' in r:
                entry['group'] = r['group']
            if 'error' in r:
                out.append({**entry, 'error': r['error'], 'worktrees': []})
                continue
            try:
                entry['worktrees'] = list_worktrees(r['path'])
                entry['defaultBase'] = default_base(r['path'])
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
        # wrap and collapse re-indent are kept per browser, the untracked
        # files are a row of the commits pane: only the base is left here
        settings = {'base': str(req['base'])}
        # A new worktree starts from its project's last settings. A base that
        # is a branch or tag carries over; a commit (a hash, HEAD~3) belongs
        # to the worktree it was picked in, so the project keeps its base.
        is_ref = not settings['base'].startswith('-') and bool(
            git(wt, 'rev-parse', '--verify', '--quiet', '--symbolic-full-name', settings['base'],
                ok_codes=(0, 1)).strip())
        with self._settings_lock:
            saved = self._load_settings()
            saved['worktrees'][wt] = settings
            project = dict(settings)
            if not is_ref:
                old = saved['projects'].get(repo, {}).get('base')
                if old:
                    project['base'] = old
                else:
                    del project['base']
            saved['projects'][repo] = project
            self._write_settings(saved)
        return {'settings': settings, 'project': project}

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

    def diff(self, wt: str, base: str, reindent: bool = False,
             commit: 'str | None' = None, since: 'str | None' = None) -> dict:
        """The review's diff (merge base to the working tree, the tracked
        files), or with `commit` one commit's diff against its first parent,
        as git show has it, the uncommitted changes or the untracked files
        alone, or a range (commit_range). `since` names the diff a reload
        replaces, whose lines the new one's are compared with."""
        wt = self.check_worktree(wt)
        t0 = time.perf_counter()
        mb = merge_base(wt, base)
        # Taken before the diff: an edit landing while the diff runs then
        # shows up as a change instead of hiding in the baseline.
        head = git(wt, 'rev-parse', 'HEAD').strip()
        # A view with the untracked files changes when they do.
        with_untracked = shows_untracked(commit)
        fps = fingerprint(wt, mb, with_untracked)
        prefix: list[str] = []
        shown = None
        newer = None
        if not commit:
            text = diff_text(wt, mb, False)
        elif commit == UNCOMMITTED:
            text = diff_text(wt, 'HEAD', False)
            shown = {'hash': UNCOMMITTED}
        elif commit == UNTRACKED:
            text = untracked_diff(wt)
            shown = {'hash': UNTRACKED}
            newer = UNTRACKED
        elif '..' in commit:
            text, prefix, shown, newer = commit_range(wt, commit, mb)
        else:
            sha = verify_commit(wt, commit)
            parents = git(wt, 'rev-list', '--parents', '-n', '1', sha).split()[1:]
            # A merge shows what it brought into the branch: its first parent's
            # side. git show's combined diff is a format parse_diff does not read.
            text = git(wt, 'diff', '--no-color', '--no-ext-diff', '--src-prefix=a/',
                       '--dst-prefix=b/', parents[0] if parents else EMPTY_TREE, sha)
            prefix, shown = commit_header(wt, sha)
        ahead, behind = ahead_behind(wt, base)
        t_git = time.perf_counter()
        diff_files = parse_diff(text)
        doc = Doc(diff_files, reindent, prefix)
        doc.head = head
        doc.base = base
        if commit:
            doc.commit = shown['hash']
            # comments go on the newer end's files: a commit's, or the worktree's
            end = newer or shown['hash']
            if end not in (UNCOMMITTED, UNTRACKED):
                doc.read_file = blob_reader(wt, end)
        # What a reload would bring is measured against the whole review.
        doc.changed_lines = None if commit else changed_lines(diff_files)
        news = None
        if since and not commit:
            with self._lock:
                prev = self._baselines.get(f'{wt}\0{since}')
            if prev is None:
                # the server restarted, or the diff is too many loads back
                news = {'missing': True}
            elif prev[0] == base:
                news = new_since(prev[1], doc)
        t_doc = time.perf_counter()
        doc_id = hashlib.sha1(f'{wt}\0{base}\0{time.time()}'.encode()).hexdigest()[:16]
        with self._lock:
            self._docs[f'{wt}\0{doc_id}'] = doc
            self._evict_docs()
            if not commit:
                self._baselines[f'{wt}\0{doc_id}'] = (base, baseline(doc))
                # a kept document's stays: reload compares with it
                old = [k for k in self._baselines if k not in self._docs]
                for k in old[:max(0, len(self._baselines) - MAX_BASELINES)]:
                    del self._baselines[k]
        return {
            'id': doc_id, 'worktree': wt, 'base': base, 'mergeBase': mb[:10],
            'mergeBaseCommit': commit_summary(wt, mb),
            'ahead': ahead, 'behind': behind, 'commit': shown,
            'fingerprint': fps[with_untracked], 'fingerprintAll': fps[1],
            'fingerprintKind': 'all' if with_untracked else 'tracked',
            'text': '\n'.join(doc.lines), 'kinds': ''.join(doc.kinds),
            'oldNo': doc.old_no, 'newNo': doc.new_no, 'files': doc.files,
            'hunks': doc.hunks, **self.viewed(wt, doc),
            'news': news,
            'timing': {'git_ms': round((t_git - t0) * 1000),
                       'doc_ms': round((t_doc - t_git) * 1000)},
        }

    def changes(self, wt: str, doc_id: str, base: str) -> dict:
        """What a reload would bring, measured against the loaded diff: the
        commits HEAD moved by, and the changed lines (the diff's + and -
        lines) that would appear or go, per file."""
        wt = self.check_worktree(wt)
        doc = self._doc(wt, doc_id)
        head = git(wt, 'rev-parse', 'HEAD').strip()
        commits, rewritten = 0, False
        if head != doc.head:
            try:
                git(wt, 'merge-base', '--is-ancestor', doc.head, head)
                commits = int(git(wt, 'rev-list', '--count', f'{doc.head}..{head}'))
            except GitError:
                # A rebase or reset: no count of new commits means anything.
                rewritten = True
        if doc.changed_lines is None:
            return {'commits': commits, 'rewritten': rewritten}
        now_files = parse_diff(diff_text(wt, merge_base(wt, base), False))
        now = changed_lines(now_files)
        sigs = {df.path: file_sig(df) for df in now_files}
        files = add = rem = 0
        for path in doc.changed_lines.keys() | now.keys():
            a, b = doc.changed_lines.get(path, []), now.get(path, [])
            if a == b:
                # a file without + and - lines that came, went or changed
                if not a and doc.file_sigs.get(path) != sigs.get(path):
                    files += 1
                continue
            files += 1
            sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
            for op, i1, i2, j1, j2 in sm.get_opcodes():
                if op != 'equal':
                    rem += i2 - i1
                    add += j2 - j1
        return {'commits': commits, 'rewritten': rewritten,
                'files': files, 'add': add, 'del': rem}

    def commits(self, wt: str, base: str, older: int = 0, on_base: bool = False) -> dict:
        wt = self.check_worktree(wt)
        changed, untracked = worktree_status(wt)
        return {'commits': list_commits(wt, base, older, on_base), 'uncommitted': changed, 'untracked': untracked,
                'head': git(wt, 'rev-parse', 'HEAD').strip(), 'baseTip': base_tip(wt, base)}

    def commit_marks(self, wt: str, base: str) -> dict:
        wt = self.check_worktree(wt)
        with self._review_lock:
            store = ReviewStore(Path(wt) / '.gitr')
        return {'marks': comment_commits(wt, base, store),
                'stamp': gitr_stamp(wt, 'review.json')}

    # --viewed marks: .gitr/viewed.json in each worktree

    def viewed(self, wt: str, doc: 'Doc') -> dict:
        stamp = gitr_stamp(wt, 'viewed.json')
        stored = ViewedStore(Path(wt) / '.gitr').load()
        return {'viewed': [h[2] for h in doc.hunks if h[2] in stored], 'viewedStamp': stamp}

    def all_viewed(self, wt: str) -> dict:
        """Every stored key: the page picks its own hunks', so this needs no
        document the server may have dropped from its cache since."""
        wt = self.check_worktree(wt)
        stamp = gitr_stamp(wt, 'viewed.json')
        return {'viewed': list(ViewedStore(Path(wt) / '.gitr').load()), 'viewedStamp': stamp}

    def mark_viewed(self, req: dict) -> dict:
        wt = self.check_worktree(req['wt'])
        keys = req['keys']
        if not (isinstance(keys, list) and all(isinstance(k, str) for k in keys)):
            raise ValueError('keys must be a list of strings')
        if not ViewedStore(Path(wt) / '.gitr').mark(keys, bool(req['viewed'])):
            raise GitError(f'cannot write {wt}/.gitr/viewed.json')
        return {'viewedStamp': gitr_stamp(wt, 'viewed.json')}

    def _export_doc(self, wt: str, doc: 'Doc') -> 'Doc':
        """The agent export (copy for agent) always describes the
        whole review and the untracked files, also while a single commit is
        on screen: its line numbers are the working tree's, and a comment on
        a file the agent created but did not add keeps its place."""
        untracked = untracked_diff(wt)
        if doc.commit is None and not untracked:
            return doc
        text = diff_text(wt, merge_base(wt, doc.base), False) + untracked
        return Doc(parse_diff(text), doc.reindent)

    def _evict_docs(self) -> None:
        """Each worktree's most recently used document stays, as the page
        shows it again when switched back to; of the others (another tab's,
        another base's) the MAX_DOCS last used. Then, over MAX_DOC_LINES, the
        least recently used go whatever they are, all but the newest."""
        latest = {key.split('\0')[0]: key for key in self._docs}
        others = [key for key in self._docs if latest[key.split('\0')[0]] != key]
        for key in others[:max(0, len(others) - MAX_DOCS)]:
            del self._docs[key]
        total = sum(len(d.lines) for d in self._docs.values())
        while total > MAX_DOC_LINES and len(self._docs) > 1:
            total -= len(self._docs.popitem(last=False)[1].lines)

    def has_doc(self, wt: str, doc_id: str) -> bool:
        wt = self.check_worktree(wt)
        key = f'{wt}\0{doc_id}'
        with self._lock:
            if key not in self._docs:
                return False
            # the page shows it again now and asks for its comments next
            self._docs.move_to_end(key)
            return True

    def _doc(self, wt: str, doc_id: str) -> 'Doc':
        with self._lock:
            key = f'{os.path.realpath(wt)}\0{doc_id}'
            doc = self._docs.get(key)
            if doc is not None:
                self._docs.move_to_end(key)
        if doc is None:
            raise GitError('stale document, reload the diff')
        return doc

    # --comments: .gitr/review.json in each worktree, shared with `gitr
    # comments` and `gitr export`, which an agent reads them with.

    def comments(self, wt: str, doc_id: str) -> dict:
        wt = self.check_worktree(wt)
        doc = self._doc(wt, doc_id)
        with self._review_lock:
            store = ReviewStore(Path(wt) / '.gitr')
            out, _ = resolve_comments(wt, doc, store)
        if wt not in self._md_dropped:
            self._md_dropped.add(wt)
            drop_review_md(wt)
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
                entry = store.find(file, snap, line_no, side)
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
                current = file_reader(wt, doc)(file)
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
            out, _ = resolve_comments(wt, doc, store)
        return out

    def clear_comments(self, wt: str, doc_id: str) -> dict:
        wt = self.check_worktree(wt)
        doc = self._doc(wt, doc_id)
        with self._review_lock:
            store = ReviewStore(Path(wt) / '.gitr')
            if not store.clear(store.backup_path()):
                raise GitError('could not back up review.json, nothing cleared')
            out, _ = resolve_comments(wt, doc, store)
        return out

    def export(self, wt: str, doc_id: str) -> dict:
        wt = self.check_worktree(wt)
        doc = self._doc(wt, doc_id)
        with self._review_lock:
            _, items = resolve_comments(wt, self._export_doc(wt, doc), ReviewStore(Path(wt) / '.gitr'))
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


def gitr_stamp(wt: str, name: str) -> int:
    """When .gitr/<name> last changed, 0 if it does not exist: the page
    polls this to see a change from outside."""
    try:
        return os.stat(os.path.join(wt, '.gitr', name)).st_mtime_ns
    except OSError:
        return 0


def resolve_comments(wt: str, doc: 'Doc', store: ReviewStore) -> 'tuple[dict, list[_ExportItem]]':
    """Stored comments placed on the document's lines: a comment on a line no longer in the diff goes to the end
    of its file, marked orphaned. Also returns the agent export items in the
    same order (files not in this diff last)."""
    anchors = _resolve_anchors(store, file_reader(wt, doc))
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
    return {'comments': shown, 'elsewhere': elsewhere, 'stamp': gitr_stamp(wt, 'review.json')}, items


def drop_review_md(wt: str) -> None:
    """Earlier versions kept the agent export in .gitr/review.md; `gitr
    export` and `gitr comments list` read review.json instead, always
    current, so a copy left behind would only mislead an agent."""
    try:
        (Path(wt) / '.gitr' / 'review.md').unlink(missing_ok=True)
    except OSError:
        pass


def merge_base(wt: str, base: str) -> str:
    # The base comes from the browser; a leading dash would be read as an option.
    if not base or base.startswith('-'):
        raise GitError(f'invalid base: {base!r}')
    return git(wt, 'merge-base', base, 'HEAD').strip()


RECENT_COMMITS = 15
REFS_PER_KIND = 200  # most recent first; repos with thousands of tags exist


def list_refs(wt: str) -> dict:
    """Branches, remote branches and tags to offer as the base, most recent
    first, each with how far HEAD is ahead of and behind it."""
    fields = '%(refname)%00%(refname:short)'
    roots = ('refs/heads', 'refs/remotes', 'refs/tags')
    try:
        # ahead-behind counts every ref in one walk; git 2.41 and newer.
        out = git(wt, 'for-each-ref', '--sort=-committerdate',
                  f'--format={fields}%00%(ahead-behind:HEAD)', *roots)
    except GitError:
        out = git(wt, 'for-each-ref', '--sort=-committerdate', f'--format={fields}', *roots)
    kinds = {'refs/heads/': 'branch', 'refs/remotes/': 'remote', 'refs/tags/': 'tag'}
    refs, per_kind = [], {}
    for line in out.splitlines():
        full, short, *ab = line.split('\0')
        kind = next((k for prefix, k in kinds.items() if full.startswith(prefix)), None)
        if kind is None or full.endswith('/HEAD') or per_kind.get(kind, 0) >= REFS_PER_KIND:
            continue
        per_kind[kind] = per_kind.get(kind, 0) + 1
        ref = {'name': short, 'kind': kind}
        if ab and ab[0]:
            # "<only in ref> <only in HEAD>"
            behind, ahead = ab[0].split()
            ref.update(ahead=int(ahead), behind=int(behind))
        refs.append(ref)
    try:
        upstream = git(wt, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}').strip()
    except GitError:
        upstream = None
    # A base a few commits back: the branch's own line (first parents), so a
    # merge's side commits do not crowd it out.
    commits = []
    try:
        log = git(wt, 'log', '--first-parent', f'--max-count={RECENT_COMMITS}',
                  '--format=%h%x00%s%x00%at', 'HEAD')
        for line in log.splitlines():
            h, subject, at = line.split('\0')
            commits.append({'name': h, 'subject': subject, 'time': int(at)})
    except GitError:
        pass  # no commits yet
    return {'refs': refs, 'upstream': upstream, 'commits': commits}


def diff_text(wt: str, merge_base: str, untracked: bool) -> str:
    # parse_diff finds paths by the a/ b/ prefixes, which diff.noprefix
    # or diff.mnemonicPrefix in the user's config would change.
    text = git(wt, 'diff', '--no-color', '--no-ext-diff',
               '--src-prefix=a/', '--dst-prefix=b/', merge_base)
    if untracked:
        text += untracked_diff(wt)
    return text


def file_sig(df) -> str:
    """A file's header lines (index, mode, rename): what tells two versions
    of a file without + and - lines (binary, empty) apart."""
    return '\n'.join(dl.text for dl in df.lines if dl.kind == 'fileheader')


def hunk_key(path: str, lines: 'list[str]', seen: dict[str, int]) -> str:
    """What a hunk is viewed under: its file and its + and - lines, without
    line numbers or context, so a hunk that only moved keeps it. The same
    lines twice in a file (`seen` counts them) are two hunks to view."""
    text = '\n'.join(lines)
    n = seen[text] = seen.get(text, 0) + 1
    return hashlib.sha1(f'{path}\0{n}\0{text}'.encode('utf-8', 'surrogateescape')).hexdigest()[:16]


def changed_lines(diff_files) -> dict[str, list[str]]:
    """The + and - lines of each file, without context or line numbers, so
    an edit elsewhere in the file that only shifts a hunk is no change."""
    return {f.path: [l.text for l in f.lines if l.kind in ('added', 'removed')]
            for f in diff_files}


def baseline(doc: 'Doc') -> dict[str, tuple[str, list[int]]]:
    """Per file, its header lines and a hash of each + and - line: all
    new_since compares, at a fraction of the text's size (a diff that adds
    a big generated file holds most of it in its + lines). The hashes live
    only in this process, so Python's per-process hash seed does no harm."""
    return {path: (doc.file_sigs[path], [hash(t) for t in lines])
            for path, lines in doc.changed_lines.items()}


def new_since(prev: dict[str, tuple[str, list[int]]], doc: 'Doc') -> dict:
    """Where the changed lines of `doc` differ from `prev`, the baseline()
    of the diff it replaces, per file: its lines that are new (as runs of
    document lines), and the lines after which (or, at a file's first
    change, before which) changed lines of the old diff are gone. A hunk
    that only moved, or changed context around it, is no difference. A file
    without + and - lines that is new in the diff or changed (a binary or
    empty file, a pure rename) has its header line marked."""
    lines: set[int] = set()
    headers: set[int] = set()  # marked, but no changed lines to count
    gone_after: set[int] = set()
    gone_before: set[int] = set()
    paths: list[str] = []
    gone = 0
    for f in doc.files:
        path = f['path']
        at = doc.change_lines[path]
        sig, a = prev.get(path, (None, []))
        b = [hash(t) for t in doc.changed_lines[path]]
        if a == b:
            if a or sig == doc.file_sigs[path]:
                continue
            paths.append(path)
            headers.add(f['start'])
            continue
        paths.append(path)
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == 'equal':
                continue
            gone += i2 - i1
            if op != 'delete':
                lines.update(at[j1:j2])
            elif j1:
                gone_after.add(at[j1 - 1])
            else:
                gone_before.add(at[0] if at else f['start'])
    shown = {f['path'] for f in doc.files}
    files_gone = sorted(p for p in prev if p not in shown)
    runs: list[list[int]] = []
    for ln in sorted(lines | headers):
        if runs and runs[-1][1] == ln - 1:
            runs[-1][1] = ln
        else:
            runs.append([ln, ln])
    gone += sum(len(prev[p][1]) for p in files_gone)
    return {'runs': runs, 'lines': len(lines), 'gone': gone, 'files': len(paths), 'paths': paths,
            'goneAfter': sorted(gone_after), 'goneBefore': sorted(gone_before),
            'filesGone': files_gone}


def file_reader(wt: str, doc: 'Doc'):
    return doc.read_file or (lambda f: _read_text_safe(Path(wt) / f))


def blob_reader(wt: str, sha: str):
    """A file as it is in a commit, for placing comments on that commit's
    diff; read once per file."""
    cache: dict[str, 'str | None'] = {}

    def read(path: str) -> 'str | None':
        if path not in cache:
            try:
                cache[path] = git(wt, 'cat-file', 'blob', f'{sha}:{path}')
            except GitError:
                cache[path] = None
        return cache[path]
    return read


def verify_commit(wt: str, rev: str) -> str:
    if not rev or rev.startswith('-'):
        raise GitError(f'invalid commit: {rev!r}')
    try:
        return git(wt, 'rev-parse', '--verify', '--quiet', rev + '^{commit}').strip()
    except GitError:
        raise GitError(f'no commit {rev} in this repository')


RANGE_LIST_MAX = 50


def commit_range(wt: str, spec: str, mb: str) -> 'tuple[str, list[str], dict, str]':
    """older..newer, both included: the diff from the older commit's first
    parent to the newer commit, so it holds the changes of every selected
    commit, and a header listing those commits. The older end can also be
    'all' (from the merge base `mb`) or 'uncommitted' (from HEAD, up to the
    untracked files); the newer end 'uncommitted' (the working tree) or
    'untracked' (the working tree and the untracked files). Returns the
    diff text, the header lines, the summary for the page and the newer end."""
    older, newer = spec.split('..', 1)
    if older == UNCOMMITTED and newer != UNTRACKED or older == UNTRACKED:
        raise GitError(f'not a range: {spec}')
    if older == ALL:
        start, old_sha, has_start = mb, ALL, True
    elif older == UNCOMMITTED:
        start, old_sha, has_start = 'HEAD', UNCOMMITTED, True
    else:
        old_sha = verify_commit(wt, older)
        parents = git(wt, 'rev-list', '--parents', '-n', '1', old_sha).split()[1:]
        start, has_start = (parents[0], True) if parents else (EMPTY_TREE, False)
    if newer in (UNCOMMITTED, UNTRACKED):
        text = diff_text(wt, start, newer == UNTRACKED)
        tip = 'HEAD'
    else:
        newer = verify_commit(wt, newer)
        text = git(wt, 'diff', '--no-color', '--no-ext-diff', '--src-prefix=a/',
                   '--dst-prefix=b/', start, newer)
        tip = newer
    commits = [] if older == UNCOMMITTED else git(
        wt, 'log', '--topo-order', '--format=%h %s', tip, *(['--not', start] if has_start else [])).splitlines()
    n = len(commits)
    if older == UNCOMMITTED:
        lines = ['the uncommitted changes and untracked files', '']
    else:
        names = {ALL: 'the merge base', UNCOMMITTED: 'the working tree', UNTRACKED: 'the working tree'}
        lines = [f'{names.get(old_sha) or old_sha[:10]} to {names.get(newer) or newer[:10]}: '
                 f'{n} commit{"" if n == 1 else "s"}'
                 + {UNCOMMITTED: ' and the uncommitted changes',
                    UNTRACKED: ' and the uncommitted changes and untracked files'}.get(newer, ''), '']
    lines += ['    ' + c for c in commits[:RANGE_LIST_MAX]]
    if n > RANGE_LIST_MAX:
        lines.append(f'    ... and {n - RANGE_LIST_MAX} more')
    lines.append('')
    return text, lines, {'hash': f'{old_sha}..{newer}', 'range': True, 'count': n,
                         'from': old_sha, 'to': newer}, newer


def commit_header(wt: str, sha: str) -> 'tuple[list[str], dict]':
    """git show's header (commit, Merge, Author, Date, the message indented)
    as document lines, and the commit's summary for the page."""
    out = git(wt, 'show', '-s', '--date=format-local:%Y-%m-%d %H:%M',
              '--format=%H%x00%P%x00%an <%ae>%x00%ad%x00%cn <%ce>%x00%s%x00%B', sha)
    h, parents, author, date, committer, subject, body = out.split('\0', 6)
    lines = [f'commit {h}']
    if len(parents.split()) > 1:
        lines.append('Merge: ' + ' '.join(p[:10] for p in parents.split()))
    lines += [f'Author: {author}', f'Date:   {date}']
    if committer != author:
        lines.append(f'Commit: {committer}')
    lines.append('')
    lines += ['    ' + l if l else '' for l in body.rstrip('\n').split('\n')]
    lines.append('')
    return lines, {'hash': h, 'subject': subject}


def commit_summary(wt: str, sha: str) -> 'dict | None':
    """A commit's subject, author and time, for the merge base's tooltip."""
    try:
        subject, author, at = git(wt, 'log', '-1', '--format=%s%x00%an%x00%at', sha, '--').rstrip('\n').split('\0')
    except (GitError, ValueError):
        return None
    return {'subject': subject, 'author': author, 'time': int(at)}


NO_COMMIT = '0' * 40  # what git blame gives a line not committed yet


def comment_commits(wt: str, base: str, store: ReviewStore) -> 'dict[str, dict[str, int]]':
    """Per commit, how many comments of each kind sit on lines it changed:
    the commit that last changed the line (git blame of the working tree),
    or for a removed line the one that removed it (the child, on the way to
    HEAD, of the last commit that still had it). Lines not committed yet
    count for the UNCOMMITTED row, untracked files for the UNTRACKED one."""
    anchors = _resolve_anchors(store, lambda f: _read_text_safe(Path(wt) / f))
    if not anchors:
        return {}
    untracked = {rel for code, rel in status_entries(wt) if code == '??'}
    marks: dict[str, dict[str, int]] = {}

    def count(where: str, kind: str) -> None:
        marks.setdefault(where, {}).setdefault(kind, 0)
        marks[where][kind] += 1

    mb = None
    for file, alist in anchors.items():
        live = [a for a in alist if a.side != '-']
        if file in untracked:
            for a in live:
                count(UNTRACKED, a.kind)
        elif live:
            text = _read_text_safe(Path(wt) / file)
            n = len(text.splitlines()) if text is not None else 0
            live = [a for a in live if 1 <= a.target_line_no <= n]
            for a, sha in zip(live, blame_lines(wt, file, [a.target_line_no for a in live])):
                if sha:
                    count(UNCOMMITTED if sha == NO_COMMIT else sha, a.kind)
        removed = [a for a in alist if a.side == '-']
        if removed:
            mb = mb or merge_base(wt, base)
            for a, sha in zip(removed, removed_by(wt, mb, file, removed)):
                if sha:
                    count(sha, a.kind)
    return marks


def blame_lines(wt: str, file: str, lines: list[int]) -> 'list[str | None]':
    """The commit that last changed each of these lines of the working
    tree's file, in one git blame."""
    if not lines:
        return []
    args = [x for ln in sorted(set(lines)) for x in ('-L', f'{ln},{ln}')]
    try:
        out = git(wt, 'blame', '--porcelain', *args, '--', file)
    except GitError:
        # git blame refuses a file added to the index but not in HEAD yet
        try:
            new = not git(wt, 'ls-tree', '--name-only', 'HEAD', '--', file).strip()
        except GitError:
            new = False
        return [NO_COMMIT if new else None] * len(lines)
    found = {}
    for line in out.splitlines():
        parts = line.split(' ')
        # a line's header: <sha> <line in that commit> <line now> [<group size>]
        if len(parts) in (3, 4) and len(parts[0]) == 40 and parts[2].isdigit():
            found[int(parts[2])] = parts[0]
    return [found.get(ln) for ln in lines]


def removed_by(wt: str, mb: str, file: str, anchors: list) -> 'list[str | None]':
    """The commit that removed each of these removed lines since the merge
    base, UNCOMMITTED if it still is in HEAD. A line is found among the
    diff's removed lines by its text, the nearest to where the comment sits;
    one removed and added again within the branch is not in that diff, and
    gets None."""
    try:
        diff = git(wt, 'diff', '--no-color', '--no-ext-diff', '-U0', mb, '--', file)
    except GitError:
        return [None] * len(anchors)
    gone: list[tuple[int, int, str]] = []  # old line, where it sits now, text
    old = new = 0
    for line in diff.splitlines():
        m = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)', line)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
        elif line.startswith('-') and not line.startswith('---'):
            gone.append((old, new, line[1:]))
            old += 1
        elif line.startswith('+') and not line.startswith('+++'):
            new += 1
    olds = []
    for a in anchors:
        hits = [g for g in gone if g[2] == a.line_text[1:]]
        olds.append(min(hits, key=lambda g: abs(g[1] - a.target_line_no))[0] if hits else None)
    want = sorted({o for o in olds if o})
    if not want:
        return [None] * len(anchors)
    args = [x for ln in want for x in ('-L', f'{ln},{ln}')]
    try:
        out = git(wt, 'blame', '--porcelain', '--reverse', f'{mb}..HEAD', *args, '--', file)
        head = git(wt, 'rev-parse', 'HEAD').strip()
    except GitError:
        return [None] * len(anchors)
    last = {}  # old line -> the last commit that still had it
    for line in out.splitlines():
        parts = line.split(' ')
        if len(parts) in (3, 4) and len(parts[0]) == 40 and parts[2].isdigit():
            last[int(parts[2])] = parts[0]
    children: dict[str, 'str | None'] = {}

    def child(sha: str) -> 'str | None':
        if sha not in children:
            out = git(wt, 'rev-list', '--ancestry-path', '--parents', f'{sha}..HEAD')
            # oldest last; the first commit after it on the way to HEAD
            kids = [ln.split()[0] for ln in out.splitlines() if sha in ln.split()[1:]]
            children[sha] = kids[-1] if kids else None
        return children[sha]

    res = []
    for o in olds:
        sha = last.get(o) if o else None
        res.append(None if sha is None else UNCOMMITTED if sha == head else child(sha))
    return res


def list_commits(wt: str, base: str, older: int = 0, on_base: bool = False) -> list[dict]:
    """The commits in base..HEAD in graph order, newest first, then the
    boundary (the merge base, and anything merged in from base), as gitk
    lists them. Long ranges are cut at MAX_COMMITS. With `older`, the
    boundary comes in graph order with that many of its ancestors, marked
    old: history for context, outside the review. With `on_base`, the
    commits of HEAD..base too (what the base got since the branch left it,
    as `gitk HEAD base` shows them), marked onBase and all in graph order."""
    if not base or base.startswith('-'):
        raise GitError(f'invalid base: {base!r}')
    commits = _log_commits(wt, '--boundary', f'--max-count={MAX_COMMITS + 1}', f'{base}..HEAD')
    if not commits:
        # Nothing since the base: git lists no boundary then, but the base
        # (its merge base with HEAD) is still what the review starts from.
        commits = _log_commits(wt, '-n', '1', merge_base(wt, base), topo=False)
        commits[0]['boundary'] = True
    inside = [c for c in commits if not c['boundary']]
    if len(inside) > MAX_COMMITS:
        # cut: the boundary was not reached, and one commit too many came
        commits = inside[:MAX_COMMITS]
        commits[-1]['cut'] = True
    elif older:
        commits = inside + older_commits(wt, [c for c in commits if c['boundary']], older)
    if on_base:
        theirs = _log_commits(wt, f'--max-count={MAX_COMMITS + 1}', f'HEAD..{base}', topo=False)
        if len(theirs) > MAX_COMMITS:
            theirs = theirs[:MAX_COMMITS]
            theirs[-1]['onBaseCut'] = True
        for c in theirs:
            c['onBase'] = True
        if theirs:
            commits = graph_order(commits + theirs)
    return commits


def older_commits(wt: str, boundary: list[dict], n: int) -> list[dict]:
    """The boundary commits and n of their ancestors, newest first in graph
    order, so lines run from the boundary on into the history."""
    hashes = {c['hash'] for c in boundary}
    # Newest first by date, not --topo-order: without a commit-graph file git
    # would walk the whole history for that before printing the first line.
    out = _log_commits(wt, f'--max-count={n + len(hashes)}', *hashes, topo=False)
    older = [c for c in out if c['hash'] not in hashes][:n]
    for c in older:
        c['old'] = True
    return graph_order(boundary + older)


def graph_order(commits: list[dict]) -> list[dict]:
    """No commit before any of its children (dates can be wrong), else
    newest first."""
    at = {c['hash']: c for c in commits}
    children = {h: 0 for h in at}
    for c in commits:
        for p in set(c['parents']):
            if p in children:
                children[p] += 1
    ready = [(-c['time'], i, c['hash']) for i, c in enumerate(commits) if not children[c['hash']]]
    heapq.heapify(ready)
    out = []
    while ready:
        c = at[heapq.heappop(ready)[2]]
        out.append(c)
        for p in set(c['parents']):
            if p in children:
                children[p] -= 1
                if not children[p]:
                    heapq.heappush(ready, (-at[p]['time'], len(out), p))
    return out


def _log_commits(wt: str, *args: str, topo: bool = True) -> list[dict]:
    out = git(wt, 'log', *(['--topo-order'] if topo else []), '--decorate=full',
              '--format=%m%x00%H%x00%P%x00%an%x00%ae%x00%at%x00%D%x00%s%x00%b%x1e', *args, '--')
    commits = []
    for rec in out.split('\x1e'):
        rec = rec.lstrip('\n')
        if not rec:
            continue
        mark, h, parents, an, ae, at, refs, subject, body = rec.split('\0', 8)
        commits.append({'hash': h, 'parents': parents.split(), 'author': an, 'email': ae,
                        'time': int(at), 'refs': parse_refs(refs), 'subject': subject,
                        'body': body.strip()[:4000], 'boundary': mark == '-'})
    return commits


def parse_refs(decoration: str) -> list[dict]:
    refs = []
    for d in filter(None, decoration.split(', ')):
        head = d.startswith('HEAD -> ')
        d = d.removeprefix('HEAD -> ')
        # origin/HEAD only repeats which branch the remote checks out
        if d.startswith('refs/remotes/') and d.endswith('/HEAD'):
            continue
        for prefix, kind in (('tag: refs/tags/', 'tag'), ('refs/heads/', 'branch'),
                             ('refs/remotes/', 'remote')):
            if d.startswith(prefix):
                refs.append({'name': d[len(prefix):], 'kind': kind, 'head': head})
                break
    return refs


def shows_untracked(commit: 'str | None') -> bool:
    """Whether a view (the diff's `commit` choice) holds the untracked files."""
    return bool(commit) and (commit == UNTRACKED or commit.endswith('..' + UNTRACKED))


def status_entries(wt: str) -> 'list[tuple[str, str]]':
    """(status code, path) per entry of git status, the untracked files
    each listed, gitr's own .gitr/ left out."""
    fields = git(wt, 'status', '--porcelain=v1', '-z', '--untracked-files=all').split('\0')
    out = []
    it = iter(fields)
    for entry in it:
        if len(entry) < 4:
            continue
        code, rel = entry[:2], entry[3:]
        if 'R' in code or 'C' in code:
            next(it, None)  # the old path, a field of its own
        if not (rel == '.gitr' or rel.startswith('.gitr/')):
            out.append((code, rel))
    return out


def worktree_status(wt: str) -> 'tuple[bool, int]':
    """Whether tracked files have uncommitted changes, and how many
    untracked files the untracked view would show (regular files: it
    skips symlinks, as untracked_diff does)."""
    changed, untracked = False, 0
    for code, rel in status_entries(wt):
        if code != '??':
            changed = True
            continue
        try:
            if stat.S_ISREG(os.lstat(os.path.join(wt, rel)).st_mode):
                untracked += 1
        except OSError:
            pass
    return changed, untracked


def ahead_behind(wt: str, base: str) -> tuple[int, int]:
    behind, ahead = git(wt, 'rev-list', '--left-right', '--count', f'{base}...HEAD').split()
    return int(ahead), int(behind)


def base_tip(wt: str, base: str) -> str:
    """The commit the base names: when it moves on and HEAD does not, the
    diff stays the same, but the commits pane's lane of the base's own
    commits changes."""
    return git(wt, 'rev-parse', '--verify', '--quiet', '--end-of-options', base + '^{commit}').strip()


def fingerprint(wt: str, merge_base: str, stat_untracked: bool) -> 'tuple[str, str]':
    """Cheap change detector: HEAD, the status listing and the size/mtime of
    every listed path. Agents editing a file change its mtime even when the
    status line stays the same. Two of them: without the untracked files
    (the review leaves them out, so a scratch file an agent drops must not
    raise the "changed" note) and with them, for the commits pane's
    untracked row and the views that show them. Only those views need an
    edit to an untracked file noticed: the others take just the list, and
    stat no untracked file (an unignored venv has thousands)."""
    head = git(wt, 'rev-parse', 'HEAD').strip()
    tracked = hashlib.sha1(f'{head}\0{merge_base}\0'.encode())
    every = tracked.copy()
    for code, rel in status_entries(wt):
        part = f'{code} {rel}\0'
        if code != '??' or stat_untracked:
            try:
                st = os.stat(os.path.join(wt, rel))
                part += f'{st.st_size}\0{st.st_mtime_ns}\0'
            except OSError:
                pass
        every.update(part.encode())
        if code != '??':
            tracked.update(part.encode())
    return tracked.hexdigest()[:16], every.hexdigest()[:16]


# --pushed changes ----------------------------------------------------------------
#
# Each browser holds one /api/events stream for all its pages of a server
# (static/events.js, a shared worker: over HTTP/1.1 a browser keeps about 6
# connections per host, which a stream per tab would use up), and posts to
# /api/watch what its pages show. A thread checks those worktrees and sends
# what changed: once per view however many tabs show it, and nothing for
# hidden tabs, which post nothing. git has no change notification, and the
# standard library no file watching, so it is a check on a timer still.

WATCH_S = 1.0   # how often the worktrees pages show are checked
LIST_S = 10.0   # ... the worktree list and the page version


class Stream:
    """One /api/events connection and what its pages show: key -> (wt,
    base, commit), a key per page and load, so that its first event comes
    after the page's diff, never from before it."""

    def __init__(self) -> None:
        self.subs: dict[str, tuple[str, str, 'str | None']] = {}
        self.sent: dict[str, dict] = {}
        self.out: list[bytes] = []
        self.lock = threading.Lock()
        # wakes the connection's thread, which waits for the socket too
        self.wake_r, self.wake_w = socket.socketpair()

    def push(self, event: str, data) -> None:
        with self.lock:
            self.out.append(f'event: {event}\ndata: {json.dumps(data, separators=(",", ":"))}\n\n'.encode())
        try:
            self.wake_w.send(b'.')
        except OSError:
            pass

    def take(self) -> bytes:
        with self.lock:
            out, self.out = b''.join(self.out), []
        return out

    def close(self) -> None:
        self.wake_r.close()
        self.wake_w.close()


class Events:
    def __init__(self) -> None:
        self.streams: dict[str, Stream] = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.projects_sig: 'str | None' = None
        self.version: 'str | None' = None
        self.due: dict[tuple, float] = {}  # view -> when to check it next

    def watch(self, sid: str, subs: dict) -> None:
        if not isinstance(subs, dict):
            raise ValueError('subs')
        parsed = {}
        for key, sub in subs.items():
            wt, base, commit = sub['wt'], sub['base'], sub.get('commit')
            if not all(isinstance(v, str) for v in (key, wt, base)) or not isinstance(commit, (str, type(None))):
                raise ValueError('sub')
            parsed[key] = (wt, base, commit)
        with self.lock:
            st = self.streams.get(sid)
            if st is None:
                raise KeyError('no such stream')
            st.subs = parsed
            st.sent = {k: v for k, v in st.sent.items() if k in parsed}
        self.wake.set()

    def run(self, state: 'State') -> None:
        list_at = 0.0
        while True:
            self.wake.wait(WATCH_S)
            self.wake.clear()
            with self.lock:
                streams = list(self.streams.values())
            if not streams:
                continue
            # Whatever goes wrong, the thread goes on: the pages, which poll
            # no more while their stream is open, would hear of nothing again.
            try:
                if time.monotonic() - list_at >= LIST_S:
                    list_at = time.monotonic()
                    self._lists(state, streams)
                self._views(state, streams)
            except Exception:
                traceback.print_exc()

    def _views(self, state: 'State', streams: list[Stream]) -> None:
        seen: dict[tuple, 'dict | None'] = {}
        shown = set()
        for st in streams:
            with self.lock:
                subs = list(st.subs.items())
            for key, sub in subs:
                wt, base, commit = sub
                view = (wt, base, shows_untracked(commit))
                shown.add(view)
                if view not in seen:
                    # A view slow to check (git status in a big repo) is
                    # checked less often, so that it neither keeps a core busy
                    # nor holds up the others; a new page's view at once.
                    if key in st.sent and time.monotonic() < self.due.get(view, 0):
                        continue
                    t = time.monotonic()
                    seen[view] = view_stamps(state, *view)
                    self.due[view] = t + max(WATCH_S, 5 * (time.monotonic() - t))
                data = seen[view]
                with self.lock:
                    # unless the page posted another view meanwhile
                    if data is not None and st.subs.get(key) == sub and st.sent.get(key) != data:
                        st.sent[key] = data
                        st.push('fp', {'key': key, **data})
        self.due = {v: t for v, t in self.due.items() if v in shown}

    def _lists(self, state: 'State', streams: list[Stream]) -> None:
        version = page_version()
        if version != self.version:
            self.version = version
            for st in streams:
                st.push('version', version)
        projects = state.projects()
        # what the page's list shows (app.js listSignature); positions and
        # settings change with every scroll and are no news
        sig = json.dumps([[p.get('group'), p['name'], p['path'], p.get('error'),
                           [[w['path'], w.get('branch'), w.get('head'), w.get('detached')]
                            for w in p['worktrees']]] for p in projects])
        if sig != self.projects_sig:
            self.projects_sig = sig
            for st in streams:
                st.push('projects', projects)


    def bye(self) -> None:
        """Tell every page the server is stopping (`gitr`'s windows close),
        and give the streams a moment to send it."""
        with self.lock:
            streams = list(self.streams.values())
        for st in streams:
            st.push('bye', None)
        end = time.monotonic() + 1
        while time.monotonic() < end and any(st.out for st in streams):
            time.sleep(0.02)
        time.sleep(0.05)  # taken is not yet written


EVENTS = Events()


def serve(srv: ThreadingHTTPServer) -> None:
    """Until Ctrl+C, a kill, or shutdown(); then the pages are told."""
    def stop(*_) -> None:
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=EVENTS.run, args=(Handler.state,), daemon=True).start()
    try:
        srv.serve_forever(poll_interval=0.1)  # shutdown() waits for a poll
    except KeyboardInterrupt:
        pass
    EVENTS.bye()


def view_stamps(state: 'State', wt: str, base: str, untracked: bool) -> 'dict | None':
    """What /api/fingerprint answers, for the events stream; None for a view
    that cannot be checked now (a worktree gone, a base deleted): the page
    learns about those from the worktree list or its next load."""
    try:
        wt = state.check_worktree(wt)
        tracked, every = fingerprint(wt, merge_base(wt, base), untracked)
        return {'fingerprint': tracked, 'fingerprintAll': every, 'baseTip': base_tip(wt, base),
                'review': gitr_stamp(wt, 'review.json'), 'viewed': gitr_stamp(wt, 'viewed.json')}
    except (GitError, OSError, ValueError):
        return None


# --http --------------------------------------------------------------------------

APP_NAME = 'gitr - ' + socket.gethostname().split('.')[0]

ALLOWED_HOSTS = {'localhost', '127.0.0.1', '::1'}

CONTENT_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                 '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.ttf': 'font/ttf',
                 '.webmanifest': 'application/manifest+json'}


class Handler(BaseHTTPRequestHandler):
    state: State  # set on the class by main()
    last_request = 0.0  # time.monotonic(), for standalone()
    standalone = False  # serving `gitr`, whose page closes with it
    # pages holding /api/events open, and when the last one went
    pages = 0
    pages_seen = False
    last_page_left = 0.0
    expect_until = 0.0  # see EXPECT_S
    busy = 0  # requests being answered, the events streams aside
    last_page_request = 0.0  # the page itself asked for, see GRACE_S
    last_stream_open = 0.0
    pages_lock = threading.Lock()

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

    def _events(self, sid: str) -> None:
        """The events stream of a browser's pages (see Events), which also
        tells `gitr` that a page is open: the browser closes the socket at
        once when the last page goes, also when it crashes."""
        self.close_connection = True
        if not sid or len(sid) > 64:
            return self._json({'error': 'bad stream id'}, 400)
        st = Stream()
        # registered before the answer: the page posts /api/watch once it is open
        with EVENTS.lock:
            EVENTS.streams[sid] = st
        with Handler.pages_lock:
            Handler.pages += 1
            Handler.pages_seen = True
            Handler.last_stream_open = time.monotonic()
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            # a page that waited through a restart learns the version at once
            self.wfile.write(f'event: version\ndata: {json.dumps(page_version())}\n\n'.encode())
            self.wfile.flush()
            while True:
                ready, _, _ = select.select([self.connection, st.wake_r], [], [], 15)
                # The page sends nothing, so readable means closed.
                if self.connection in ready and not self.connection.recv(1):
                    break
                if st.wake_r in ready:
                    st.wake_r.recv(4096)
                self.wfile.write(st.take() or b': ping\n\n')
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with EVENTS.lock:
                # a reconnect with the same id may have replaced it already
                if EVENTS.streams.get(sid) is st:
                    del EVENTS.streams[sid]
            st.close()
            with Handler.pages_lock:
                Handler.pages -= 1
                Handler.last_page_left = time.monotonic()

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
        if urlparse(self.path).path == '/api/events':
            return self._get()  # held open: it counts as a page, not as a request
        self._counted(self._get)

    def do_POST(self) -> None:
        self._counted(self._post)

    def _counted(self, handle) -> None:
        # A request still being answered (a big diff after a reload) keeps
        # `gitr` up, as an open page does.
        with Handler.pages_lock:
            Handler.busy += 1
        try:
            handle()
        finally:
            with Handler.pages_lock:
                Handler.busy -= 1
                Handler.last_request = time.monotonic()

    def _get(self) -> None:
        if not self._host_ok():
            return self._send(HTTPStatus.FORBIDDEN, b'forbidden host', 'text/plain')
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == '/api/events':
                return self._events(q.get('id', ''))
            if url.path == '/api/alive':
                return self._json({'standalone': Handler.standalone})
            if url.path == '/api/version':
                return self._json({'version': page_version(), 'gitr': GITR_VERSION})
            if url.path == '/api/projects':
                if q.get('expect'):
                    Handler.expect_until = time.monotonic() + EXPECT_S
                return self._json(self.state.projects())
            if url.path == '/api/diff':
                return self._json(self.state.diff(q['wt'], q['base'],
                                                  q.get('reindent') == '1', q.get('commit') or None,
                                                  q.get('since') or None))
            if url.path == '/api/commits':
                return self._json(self.state.commits(q['wt'], q['base'],
                                                     max(0, min(int(q.get('older') or 0), MAX_OLDER)),
                                                     q.get('onBase') == '1'))
            if url.path == '/api/commit-marks':
                return self._json(self.state.commit_marks(q['wt'], q['base']))
            if url.path == '/api/refs':
                return self._json(list_refs(self.state.check_worktree(q['wt'])))
            if url.path == '/api/words':
                return self._json(self.state.word_ranges(q['wt'], q['id'],
                                                         int(q['from']), int(q['to'])))
            if url.path == '/api/fingerprint':
                wt = self.state.check_worktree(q['wt'])
                mb = merge_base(wt, q['base'])
                tracked, every = fingerprint(wt, mb, shows_untracked(q.get('commit')))
                return self._json({'fingerprint': tracked, 'fingerprintAll': every,
                                   'baseTip': base_tip(wt, q['base']),
                                   'review': gitr_stamp(wt, 'review.json'),
                                   'viewed': gitr_stamp(wt, 'viewed.json'),
                                   'version': page_version()})
            if url.path == '/api/changes':
                return self._json(self.state.changes(q['wt'], q['id'], q['base']))
            if url.path == '/api/comments':
                return self._json(self.state.comments(q['wt'], q['id']))
            if url.path == '/api/export':
                return self._json(self.state.export(q['wt'], q['id']))
            if url.path == '/api/viewed':
                return self._json(self.state.all_viewed(q['wt']))
            if url.path == '/api/kept':
                return self._json({'kept': self.state.has_doc(q['wt'], q['id'])})
        except KeyError as e:
            return self._json({'error': f'missing parameter {e}'}, 400)
        except ValueError as e:
            return self._json({'error': f'bad parameter: {e}'}, 400)
        except GitError as e:
            return self._json({'error': str(e)}, 400)
        self._static(url.path)

    def _post(self) -> None:
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
            if path == '/api/viewed':
                return self._json(self.state.mark_viewed(req))
            if path == '/api/watch':
                EVENTS.watch(req['id'], req['subs'])
                return self._json({})
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
        if rel == 'index.html':
            Handler.last_page_request = time.monotonic()
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
    try:
        return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    except ValueError as e:
        # --reload restarts on every save of it, a half-written one included
        sys.exit(f'gitr-serve: {tilde(str(CONFIG_PATH))}: {e}')


def load_repos(args_repos: list[str]) -> list[dict]:
    items: list = list(args_repos) or read_config().get('projects', [])
    repos = []
    for it in items:
        path = it if isinstance(it, str) else it['path']
        path = os.path.realpath(os.path.expanduser(path))
        repo = {'path': path, 'name': os.path.basename(path)}
        if isinstance(it, dict):
            repo['name'] = it.get('name') or repo['name']
            if it.get('multi'):
                depth = it.get('depth', 1)
                if type(depth) is not int or depth < 1:
                    sys.exit(f'gitr-serve: {tilde(str(CONFIG_PATH))}: "depth" of {it["path"]} '
                             f'is to be a whole number from 1, not {json.dumps(depth)}')
                repo['multi'] = True
                repo['depth'] = depth
        repos.append(repo)
    return repos


REPOS_S = 3  # how long a multi-repo directory's scan is used


def git_dir_of(d: str) -> 'tuple[str, bool] | None':
    """The repository a directory is a checkout of (its common git dir) and
    whether it is a linked worktree; None if it is not a checkout or a bare
    repo."""
    dotgit = os.path.join(d, '.git')
    if os.path.isdir(dotgit):
        return os.path.realpath(dotgit), False
    if os.path.isfile(dotgit):
        try:
            gitdir = Path(dotgit).read_text().strip().removeprefix('gitdir:').strip()
        except OSError:
            return None
        gitdir = os.path.join(d, gitdir)
        try:
            # only a linked worktree's git dir has one
            common = Path(gitdir, 'commondir').read_text().strip()
        except OSError:
            return os.path.realpath(gitdir), False  # a submodule, or --separate-git-dir
        return os.path.realpath(os.path.join(gitdir, common)), True
    if all(os.path.exists(os.path.join(d, n)) for n in ('HEAD', 'objects', 'refs')):
        return os.path.realpath(d), False  # bare: its worktrees are listed under it
    return None


def sub_repos(path: str, depth: int = 1) -> list[str]:
    """The checkouts and bare repos in a directory up to `depth` levels down
    (a workspace with one checkout per service), not looking inside them
    nor through symlinks. A linked worktree stands for its repo only when
    the repo itself is not found: either lists all its worktrees."""
    found = []  # (path, common git dir, linked)

    def scan(d: str, depth: int) -> None:
        try:
            children = sorted((e for e in os.scandir(d) if e.is_dir() and not e.name.startswith('.')),
                              key=lambda e: e.name)
        except OSError:
            return
        for e in children:
            if (g := git_dir_of(e.path)):
                found.append((e.path, *g))
            elif depth > 1 and not e.is_symlink():
                scan(e.path, depth - 1)

    scan(path, depth)
    mains = {g for _, g, linked in found if not linked}
    out, seen = [], set()
    for p, g, linked in found:
        if (linked and g in mains) or g in seen:
            continue
        seen.add(g)
        out.append(p)
    return out


def expand_repos(entries: list[dict]) -> list[dict]:
    """Configured entries with each multi-repo directory replaced by its
    checkouts, named by their folder, with the directories leading to them
    (from the configured one's name) as their group."""
    out = []
    for e in entries:
        if not e.get('multi'):
            out.append(e)
            continue
        if not os.path.isdir(e['path']):
            out.append({**e, 'error': 'no such directory'})
            continue
        subs = sub_repos(e['path'], e['depth'])
        for sub in subs:
            rel = os.path.relpath(sub, e['path']).split(os.sep)
            out.append({'path': os.path.realpath(sub), 'name': rel[-1],
                        'group': '/'.join([e['name'], *rel[:-1]])})
        if not subs:
            out.append({**e, 'error': f"no git checkouts in it (depth {e['depth']})"})
    return out


RELOAD_CHILD_ENV = 'GITR_SERVE_CHILD'


def supervise(argv: list[str]) -> None:
    """--reload: run the server in a child process and start a new one when
    the Python source or the config changes. A separate process rather
    than re-exec'ing itself, so a save with a syntax error leaves the supervisor waiting for
    the next save instead of ending it. The page files are read from disk on
    every request and need no restart, only a browser reload, which is
    printed."""
    here = Path(__file__).resolve()
    # the config is stamped 0 while it is missing, so creating it counts
    watched = [here, Path(gitr.__file__).resolve(), CONFIG_PATH]

    def stamp() -> tuple:
        out = []
        for p in watched:
            try:
                out.append(p.stat().st_mtime_ns)
            except OSError:
                out.append(0)
        return tuple(out)

    cmd = [sys.executable, str(here), *argv]
    env = dict(os.environ, **{RELOAD_CHILD_ENV: '1'})
    # A plain `kill` of the supervisor must not leave the server running.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # Shows when an installed copy, not the checkout being edited, is watched.
    print(f"gitr-serve: --reload watching {', '.join(tilde(str(p)) for p in watched)} "
          f"and the page in {tilde(str(STATIC_DIR))}", file=sys.stderr)
    child = None
    try:
        while True:
            seen = stamp()
            child = subprocess.Popen(cmd, env=env)
            pages = page_stamp()
            while stamp() == seen:
                time.sleep(0.5)
                now = page_stamp()
                if now != pages:
                    changed = sorted(str(p.relative_to(STATIC_DIR)) for p in now.keys() | pages.keys()
                                     if now.get(p) != pages.get(p))
                    print(f"gitr-serve: page changed ({', '.join(changed)}), reload the browser",
                          file=sys.stderr)
                    pages = now
            changed = [tilde(str(p)) for p, a, b in zip(watched, seen, stamp()) if a != b]
            print(f"gitr-serve: {', '.join(changed)} changed, restarting", file=sys.stderr)
            child.terminate()
            child.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if child is not None:
            if child.poll() is None:
                child.terminate()
            child.wait()


def page_stamp() -> dict:
    """Modification time of every page file (the Monaco bundle included)."""
    out = {}
    for p in STATIC_DIR.rglob('*'):
        try:
            if p.is_file():
                out[p] = p.stat().st_mtime_ns
        except OSError:
            pass
    return out


def gitr_version() -> str:
    """gitr's own version, for the window title: a checkout's
    pyproject.toml, else the installed package's."""
    pyproject = Path(__file__).resolve().parent.parent / 'pyproject.toml'
    try:
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    try:
        from importlib.metadata import version
        return version('gitr')
    except Exception:
        return ''


GITR_VERSION = gitr_version()


def page_version() -> str:
    """Changes when the server restarts or a page file changes, so that an
    open page can tell its code is older than the server's."""
    return f'{STARTED}-{max(page_stamp().values(), default=0)}'


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
            if sub_repos(os.getcwd()):
                paths = [{'path': tilde(os.getcwd()), 'multi': True}]
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


def main(argv: 'list[str] | None' = None, prog: str = 'gitr-serve') -> None:
    """`gitr-serve`, or `gitr serve` (gitr.main passes its arguments)."""
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog=prog, description='gitr diff view and review in the browser')
    ap.add_argument('repos', nargs='*', help=f'repositories (default: {CONFIG_PATH})')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=4487)  # "gitr" on a phone keypad
    ap.add_argument('--allow-host', action='append', default=[], metavar='NAME',
                    help='also answer to this host name, e.g. the machine\'s name under '
                         '`tailscale serve` (repeatable; added to "allow_hosts" in '
                         f'{CONFIG_PATH.name}); the server still listens on --host')
    ap.add_argument('--reload', action='store_true',
                    help=f'restart when server.py, gitr.py or {CONFIG_PATH.name} changes (for development)')
    ap.add_argument('--print-config', action='store_true',
                    help=f'print a {CONFIG_PATH.name} for the given repos (or the current one) '
                         f'to stdout, to save as {CONFIG_PATH}')
    a = ap.parse_args(argv)
    if a.print_config:
        print(config_template(a.repos))
        # stderr, so a redirect to the config file gets the JSON alone
        print(f'gitr-serve: save as {CONFIG_PATH}, e.g. with: '
              f'gitr-serve --print-config ... > {CONFIG_PATH}', file=sys.stderr)
        return
    if a.reload and not os.environ.get(RELOAD_CHILD_ENV):
        return supervise(argv)
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
    found = '' if CONFIG_PATH.exists() else ' (not found)'
    print(f'  config {tilde(str(CONFIG_PATH))}{found}', file=sys.stderr)
    for h in allow:
        print(f'  also answering as {h}', file=sys.stderr)
    width = max(len(r['name']) for r in repos)
    for r in repos:
        multi = f" (multi-repo, depth {r['depth']})" if r.get('multi') else ''
        print(f"  {r['name']:<{width}}  {tilde(r['path'])}{multi}", file=sys.stderr)
    serve(srv)


# --gitr: one repo, in a window of its own -----------------------------------
#
# `gitr` in a repository serves it alone, as the Tk window it replaced did:
# started from the terminal, ended with Ctrl+C or a while after its window
# closed. The page's .gitr files are shared with gitr-serve, so the two can
# show the same worktree.

# The last page's stream closing is the page going: the server stops then,
# once no request is being answered. Unless it is a reload, which asks for
# the new page before the old one goes, so after the old one's stream
# opened (a window just opened asked for it before): then the new page gets
# GRACE_S without a request to connect. When no page ever connected (the
# address printed, a browser that never opened it), the server stops IDLE_S
# after the last request.
GRACE_S = 2
IDLE_S = 180
# a window another `gitr` opened on this server (serving()) gets this long to
# connect: a browser starting up may take longer than GRACE_S
EXPECT_S = 60
# Each repo gets a port of its own, the same every time: the page keeps its
# settings (font sizes, wrapping) in the browser per address. Below the ports the system
# hands out for outgoing connections (from 32768 on Linux, 49152 on macOS),
# which could otherwise hold it.
PORT_FIRST, PORT_COUNT = 20000, 12000


def repo_port(path: str) -> int:
    return PORT_FIRST + zlib.crc32(path.encode()) % PORT_COUNT


def serving(port: int, repo: str) -> bool:
    """Whether a gitr for this repo already answers on the port."""
    # http.client rather than urllib, which would ask an http_proxy from the
    # environment
    conn = HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        conn.request('GET', '/api/projects?expect=1')
        return [p['path'] for p in json.loads(conn.getresponse().read())] == [repo]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    finally:
        conn.close()


# The window's address names the server as localhost, not 127.0.0.1: Chrome
# keeps an app window's size and place under a key made of the host name,
# and with the dots of 127.0.0.1 in it the place is saved but never found
# again. The key leaves out the port, so all gitr windows share one place.
WINDOW_HOST = 'localhost'

# Chromium-family browsers, the ones with an app mode, in order of preference.
CHROMES = ('google-chrome', 'google-chrome-stable', 'google-chrome-beta', 'google-chrome-unstable',
           'chromium', 'chromium-browser', 'microsoft-edge', 'microsoft-edge-stable',
           'brave-browser', 'vivaldi', 'vivaldi-stable', 'opera')
CHROME_FLATPAKS = ('com.google.Chrome', 'org.chromium.Chromium', 'com.microsoft.Edge',
                   'com.brave.Browser', 'com.vivaldi.Vivaldi', 'com.opera.Opera')
CHROME_APPS = ('Google Chrome', 'Chromium', 'Microsoft Edge', 'Brave Browser', 'Vivaldi', 'Opera')


def chrome_args(profile: Path, url: str) -> list[str]:
    """App mode (a window with no tabs or address bar) in a profile of
    gitr's own, one per browser: its windows are not the user's browsing
    session, so quitting the browser leaves them open, and a browser
    restarted later does not bring back a window of a gitr long gone. The
    flags keep a new profile from opening with its welcome pages."""
    return [f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check',
            f'--app={url}']


def own_profile(name: str) -> Path:
    return SETTINGS_PATH.parent / 'browsers' / name.lower().replace(' ', '-')


def find_browsers() -> 'Iterator[tuple[str, list[str], Path]]':
    """The Chromium-family browsers installed, in order of preference: the
    name `--browser` takes, the command that runs it, and gitr's profile
    for it (chrome_args). Lazily, as the Flatpak checks take a while and
    are mostly not needed."""
    if sys.platform == 'darwin':
        for app in CHROME_APPS:
            if any(Path(d, f'{app}.app').exists() for d in ('/Applications', Path.home() / 'Applications')):
                yield app, ['open', '-na', app, '--args'], own_profile(app)
        return
    seen = set()
    for name in CHROMES:
        exe = shutil.which(name)
        if not exe:
            continue
        # Ubuntu's chromium-browser is a script running the snap
        snap = Path('/snap/bin/chromium') if name == 'chromium-browser' else Path(exe)
        if snap.parent == Path('/snap/bin') and snap.exists():
            # Every snap command links to /usr/bin/snap: the snap's name
            # tells them apart. A snap may not open hidden folders in the
            # home directory (~/.local/state): its profile goes in the
            # snap's own folder.
            key, found = f'snap:{snap.name}', (name, [str(snap)],
                                               Path.home() / 'snap' / snap.name / 'common' / 'gitr')
        else:
            key, found = os.path.realpath(exe), (name, [exe], own_profile(name))
        # google-chrome and google-chrome-stable are one browser
        if key not in seen:
            seen.add(key)
            yield found
    if shutil.which('flatpak'):
        for app in CHROME_FLATPAKS:
            if subprocess.call(['flatpak', 'info', app], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0:
                # in the app's own data folder, which its sandbox can write
                yield app, ['flatpak', 'run', app], Path.home() / '.var' / 'app' / app / 'data' / 'gitr'


BROWSER_HELP = ('a browser `--browser list` shows (or the path of a Chromium-family one), '
                '"default" for a tab in the default browser, "none" to only print the address')


def pick_browser(choice: 'str | None') -> 'tuple[str, list[str], Path] | str':
    """What `--browser` (or GITR_BROWSER) names: a browser of
    find_browsers(), or "default" or "none". Unset, the first browser found,
    else the default one."""
    b = find_browser(choice)
    if b is None:
        names = ', '.join(b[0] for b in find_browsers()) or 'none found'
        sys.exit(f'gitr: no browser {choice!r} (Chromium-family ones here: {names}; '
                 'or "default", "none")')
    return b


def find_browser(choice: 'str | None') -> 'tuple[str, list[str], Path] | str | None':
    """pick_browser's answer, None for a choice that names nothing."""
    if choice in ('default', 'none'):
        return choice
    if not choice:
        return next(find_browsers(), 'default')
    for b in find_browsers():
        if b[0].lower() == choice.lower():
            return b
    path = Path(choice).expanduser()
    if os.sep in choice and path.is_file() and os.access(path, os.X_OK):
        return (path.name, [str(path)], own_profile(path.name))
    return None


def list_browsers() -> None:
    env = os.environ.get('GITR_BROWSER') or None
    found = list(find_browsers())
    used = find_browser(env) if env else (found[0] if found else 'default')
    used = used if isinstance(used, str) or used is None else used[0]
    rows = [(name, f'{" ".join(cmd)}  (profile {tilde(str(profile))})') for name, cmd, profile in found]
    rows += [('default', 'a tab in the default browser'), ('none', 'only print the address')]
    for name, what in rows:
        print(f'{"*" if name == used else " "} {name:<24} {what}')
    if env and used is None:
        print(f'GITR_BROWSER={env} names none of these: gitr will not start until it does')
    else:
        print('(* is used unless --browser names another' + (f'; GITR_BROWSER={env})' if env else ')'))


def open_window(url: str, browser: 'tuple[str, list[str], Path] | str') -> bool:
    """Opens the page in the browser pick_browser() gave: a Chromium-family
    one's app window, or the default browser. False when it is not opened:
    "none", or no screen to open it on (e.g. over ssh)."""
    screen = os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')
    ssh = os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_TTY')
    if browser == 'none' or (not screen and (ssh or sys.platform.startswith('linux'))):
        return False
    if browser == 'default':
        return webbrowser.open(url)
    _, cmd, profile = browser
    subprocess.Popen([*cmd, *chrome_args(profile, url)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def standalone(argv: list[str], prog: str = 'gitr') -> None:
    """`gitr [base]` (gitr.main passes its arguments)."""
    ap = argparse.ArgumentParser(prog=prog, description='The review of this repository in a window '
                                 '(`gitr help` lists the other commands).')
    ap.add_argument('base', nargs='?',
                    help='what to diff against (default: the last one used, else main or master)')
    ap.add_argument('--browser', metavar='NAME',
                    help=f'where to open the page: {BROWSER_HELP}; '
                         '"list" lists them (default: $GITR_BROWSER, else the first listed)')
    a = ap.parse_args(argv)
    if a.browser == 'list':
        return list_browsers()
    browser = pick_browser(a.browser or os.environ.get('GITR_BROWSER') or None)
    try:
        top = os.path.realpath(git(os.getcwd(), 'rev-parse', '--show-toplevel').strip())
        wts = list_worktrees(top)
        # the main worktree names the repo; the switcher lists the others under it
        repo = os.path.realpath(wts[0]['path'])
    except (GitError, IndexError):
        sys.exit('gitr: not in a git repository')
    # the page knows a worktree by the path git lists it under, which may
    # run through a symlink
    wt = next((w['path'] for w in wts if os.path.realpath(w['path']) == top), top)
    url = f'/#{urlencode({"wt": wt, **({"base": a.base} if a.base else {})})}'
    first = repo_port(repo)
    srv = None
    for port in range(first, first + 10):
        if serving(port, repo):
            # another gitr has it: one more window on that one
            if not open_window(f'http://{WINDOW_HOST}:{port}{url}', browser):
                print(f'gitr: already served at http://{WINDOW_HOST}:{port}{url}', file=sys.stderr)
            return
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        sys.exit(f'gitr: ports {first} to {first + 9} are all taken')
    Handler.state = State([{'path': repo, 'name': os.path.basename(repo)}])
    Handler.standalone = True
    Handler.last_request = time.monotonic()  # the window's start counts as one
    full = f'http://{WINDOW_HOST}:{srv.server_port}{url}'
    shown = open_window(full, browser)
    print(f'gitr: {full}' + ('' if shown else ' (open it in a browser)'), file=sys.stderr)
    print('  Ctrl+C stops it, or closing its windows', file=sys.stderr)

    def watch() -> None:
        while True:
            time.sleep(0.1)
            with Handler.pages_lock:
                if Handler.pages or Handler.busy or time.monotonic() < Handler.expect_until:
                    continue
                quiet = time.monotonic() - max(Handler.last_page_left, Handler.last_request)
                if not Handler.pages_seen:
                    wait = IDLE_S
                elif Handler.last_page_request > Handler.last_stream_open:
                    wait = GRACE_S
                else:
                    wait = 0
                if quiet > wait:
                    break
        srv.shutdown()

    threading.Thread(target=watch, daemon=True).start()
    serve(srv)


if __name__ == '__main__':
    main()
