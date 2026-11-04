#!/usr/bin/env python3
"""Prototype of `gitr serve`: a web diff viewer for many repos and worktrees.

Run:  python3 web/server.py [--port 8765] [repo ...]
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
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# gitr imports tkinter at module level; the server never opens a window but
# still needs Tk installed. Fine for the prototype, split out later.
from gitr import (_pair_lines_for_word_diff, _word_matcher,  # noqa: E402
                  parse_diff)

STATIC_DIR = Path(__file__).resolve().parent / 'static'
CONFIG_PATH = Path.home() / '.config' / 'gitr' / 'projects.json'
MAX_DOCS = 4  # a huge diff's document is tens of MB
UNTRACKED_MAX_BYTES = 2 * 1024 * 1024
TOKEN_RE = re.compile(r'\w+|[^\w\s]|\s+')

# Line kinds in the combined document, one char per line so the browser gets
# a compact string instead of a list.
K_FILE, K_HUNK, K_ADD, K_REM, K_CTX, K_META = 'f', 'h', 'a', 'r', 'c', 'm'


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

    def __init__(self, diff_files) -> None:
        self.lines: list[str] = []
        self.kinds: list[str] = []
        self.old_no: list[int] = []
        self.new_no: list[int] = []
        self.files: list[dict] = []
        # (removed line numbers, added line numbers) per run of removed lines
        # followed by added lines; the word-diff pass works on these.
        self.blocks: list[tuple[list[int], list[int]]] = []
        for df in diff_files:
            self._add_file(df)

    def _push(self, text: str, kind: str, old: 'int | None' = None, new: 'int | None' = None) -> int:
        self.lines.append(text)
        self.kinds.append(kind)
        self.old_no.append(old or 0)
        self.new_no.append(new or 0)
        return len(self.lines)

    def _add_file(self, df) -> None:
        title = f'{df.old_path} -> {df.path}' if df.status == 'R' and df.old_path else df.path
        # The status letter leads the header so the browser's tokenizer can
        # tell a header from a +/-/space/@ line by its first character.
        start = self._push(f'{df.status} {title}', K_FILE)
        if not any(dl.kind == 'hunk' for dl in df.lines):
            # Every header is two lines, like file + hunk: the minimap's file
            # label needs the room, and headers look alike throughout.
            self._push('', K_HUNK)
        body_start = len(self.lines)
        adds = dels = 0
        rem: list[int] = []
        add: list[int] = []

        def flush() -> None:
            nonlocal rem, add
            if rem and add:
                self.blocks.append((rem, add))
            rem, add = [], []

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
                if add:
                    flush()
                rem.append(self._push(dl.text, K_REM, old=dl.old_line_no))
                dels += 1
            elif k == 'added':
                ln = self._push(dl.text, K_ADD, new=dl.new_line_no)
                if rem:
                    add.append(ln)
                adds += 1
            elif dl.text.startswith('\\') and has_hunk:
                # "\ No newline at end of file" sits between the removed and
                # added versions of a file's last line; the pair stays a block.
                self._push(dl.text, K_META)
            elif not has_hunk:
                # The body of a hunkless file ("Binary files ... differ").
                flush()
                self._push(dl.text, K_META)
            else:
                flush()
                self._push(dl.text, K_CTX, old=dl.old_line_no, new=dl.new_line_no)
        flush()
        # git prints only header lines for an empty new or deleted file;
        # without a note the bare header looks like something is missing.
        if len(self.lines) == body_start and df.status in ('A', 'D'):
            self._push('(empty file)', K_META)
        self.files.append({'path': df.path, 'oldPath': df.old_path, 'status': df.status,
                           'add': adds, 'del': dels, 'start': start,
                           'end': len(self.lines)})

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


def _u16len(s: str) -> int:
    # Monaco columns count UTF-16 code units: characters outside the BMP
    # (most emoji) take two.
    return len(s) if s.isascii() else len(s.encode('utf-16-le')) // 2


# --state -----------------------------------------------------------------------

class State:
    def __init__(self, repos: list[dict]) -> None:
        self.repos = repos
        self._lock = threading.Lock()
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
            except GitError as e:
                entry['error'] = str(e)
                entry['worktrees'] = []
            out.append(entry)
        return out

    def check_worktree(self, wt: str) -> str:
        """Only worktrees of configured projects may be read, so the server
        cannot be pointed at an arbitrary directory."""
        wt = os.path.realpath(wt)
        for r in self.repos:
            try:
                if any(os.path.realpath(w['path']) == wt for w in list_worktrees(r['path'])):
                    return wt
            except GitError:
                continue
        raise GitError(f'not a configured worktree: {wt}')

    def diff(self, wt: str, base: str, untracked: bool) -> dict:
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
        doc = Doc(parse_diff(text))
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

    def word_ranges(self, wt: str, doc_id: str, first: int, last: int) -> list[int]:
        with self._lock:
            key = f'{os.path.realpath(wt)}\0{doc_id}'
            doc = self._docs.get(key)
            if doc is not None:
                self._docs.move_to_end(key)
        if doc is None:
            raise GitError('stale document')
        return doc.word_ranges(first, last)


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

ALLOWED_HOSTS = {'localhost', '127.0.0.1', '::1'}

CONTENT_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                 '.css': 'text/css; charset=utf-8'}


class Handler(BaseHTTPRequestHandler):
    state: State  # set on the class by main()

    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get('GITR_SERVE_LOG'):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, separators=(',', ':')).encode(), 'application/json')

    def _host_ok(self) -> bool:
        # Binding to localhost does not stop DNS rebinding: a page from another
        # site can resolve its own name to 127.0.0.1 and read the API. The Host
        # header still carries that name, so only loopback names are served.
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
                return self._json(self.state.diff(q['wt'], q['base'], q.get('untracked') == '1'))
            if url.path == '/api/words':
                return self._json(self.state.word_ranges(q['wt'], q['id'],
                                                         int(q['from']), int(q['to'])))
            if url.path == '/api/fingerprint':
                wt = self.state.check_worktree(q['wt'])
                mb = merge_base(wt, q['base'])
                return self._json({'fingerprint': fingerprint(wt, mb, q.get('untracked') == '1')})
        except KeyError as e:
            return self._json({'error': f'missing parameter {e}'}, 400)
        except ValueError as e:
            return self._json({'error': f'bad parameter: {e}'}, 400)
        except GitError as e:
            return self._json({'error': str(e)}, 400)
        self._static(url.path)

    def _static(self, path: str) -> None:
        rel = 'index.html' if path in ('', '/') else path.lstrip('/')
        p = (STATIC_DIR / rel).resolve()
        if STATIC_DIR not in p.parents or not p.is_file():
            return self._send(HTTPStatus.NOT_FOUND, b'not found', 'text/plain')
        self._send(200, p.read_bytes(), CONTENT_TYPES.get(p.suffix, 'application/octet-stream'))


def load_repos(args_repos: list[str]) -> list[dict]:
    items: list = list(args_repos)
    if not items and CONFIG_PATH.exists():
        items = json.loads(CONFIG_PATH.read_text()).get('projects', [])
    repos = []
    for it in items:
        path = it if isinstance(it, str) else it['path']
        path = os.path.realpath(os.path.expanduser(path))
        name = it.get('name') if isinstance(it, dict) else None
        repos.append({'path': path, 'name': name or os.path.basename(path)})
    return repos


def main() -> None:
    ap = argparse.ArgumentParser(description='gitr web prototype')
    ap.add_argument('repos', nargs='*', help=f'repositories (default: {CONFIG_PATH})')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8765)
    a = ap.parse_args()
    repos = load_repos(a.repos)
    if not repos:
        sys.exit(f'gitr serve: no repositories given and none in {CONFIG_PATH}')
    Handler.state = State(repos)
    ALLOWED_HOSTS.add(a.host.lower())
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f'gitr serve: http://{a.host}:{a.port}/  ({len(repos)} projects)', file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
