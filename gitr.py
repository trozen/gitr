#!/usr/bin/env python3
"""gitr - review a coding agent's changes in a browser window"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

try:
    import fcntl  # not on Windows: there the store's writes go unlocked
except ImportError:
    fcntl = None


USAGE = """\
usage: gitr [--browser NAME] [base]   this repository's review in a window
       gitr <command> [args]

commands:
  export      print the review comments as text for a coding agent
  comments    read and write comments: list, add, rm, accept
  serve       one page for many repositories and their worktrees
  status      what of gitr runs: gitr serve, this repository's, herdr's plugin
  help        this text

`gitr <command> --help` shows a command's options.

examples:
  gitr                         # against the base last used, else main or master
  gitr master                  # against master
  gitr -- comments             # a base named like a command
  gitr --browser list          # the browsers found; --browser NAME|default|none
                               # picks one (also $GITR_BROWSER)
  gitr export --kinds bad,note # only these kinds (default: all)
  gitr comments list --full
  gitr serve ~/dev/repo-a ~/dev/repo-b
"""


# --data structures ----------------------------------------------------------

@dataclass
class DiffLine:
    text: str
    kind: str                # added | removed | context | hunk | fileheader
    # Post-image line number (None for hunk/fileheader; for removed lines this
    # is the new-side position the line would occupy if restored, i.e. the
    # post-image line number of the next + or context line in the hunk).
    new_line_no: Optional[int] = None
    old_line_no: Optional[int] = None  # pre-image line number, similarly defined


@dataclass
class DiffFile:
    path: str
    lines: list[DiffLine] = field(default_factory=list)
    status: str = 'M'
    old_path: str = ''
    index: str = ''



@dataclass
class _ResolvedAnchor:
    """A comment entry mapped through its snapshot to a position in the
    current diff. ``target_line_no`` is the post-image line number we expect
    to find the line at (after diffing the snapshot vs. the current file);
    ``moved`` is set when the line shifted or its text changed."""
    file: str
    snapshot: str
    snap_line_no: int       # line_no stored with the entry
    target_line_no: int     # snap_line_no remapped to the current file
    side: str               # '+', '-', or ' '
    line_text: str          # diff line text including +/-/ prefix
    comment: str
    kind: str = 'note'
    moved: bool = False
    matched: bool = False


# --repository ---------------------------------------------------------------

def _find_repo_root() -> 'Path | None':
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / '.git').is_dir() or (d / '.git').is_file():
            return d
    return None


def _find_gitr_dir() -> 'Path | None':
    root = _find_repo_root()
    return (root / '.gitr') if root else None


def _read_text_safe(path: Path) -> 'str | None':
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _git_diff(refs: list[str], merge_base: bool) -> str:
    """git diff with these refs, or from the merge base of the first one."""
    try:
        if merge_base:
            refs = [subprocess.check_output(['git', 'merge-base', refs[0], 'HEAD'],
                                            text=True, stderr=subprocess.PIPE).strip()]
        return subprocess.check_output(['git', 'diff', '--no-color', *refs],
                                       text=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        sys.exit(f'gitr: git command failed: {e.stderr.strip()}')
    except FileNotFoundError:
        sys.exit('gitr: git not found in PATH')


# --review store ---------------------------------------------------------------

@contextmanager
def _locked(gitr_dir: 'Path | None', name: str):
    """An exclusive lock on .gitr/<name>, for a read-change-write of a file
    that the page's server (gitr or gitr serve) and the CLI may all write."""
    lock = None
    if fcntl is not None and gitr_dir is not None:
        try:
            gitr_dir.mkdir(parents=True, exist_ok=True)
            lock = open(gitr_dir / name, 'a')
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError:
            lock = None
    try:
        yield
    finally:
        if lock is not None:
            lock.close()  # releases the lock


def _write_json(path: Path, obj) -> bool:
    """Through a temporary file: a reader in another process must never
    see the file half written. Returns whether it was written."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(obj, indent=2))
        tmp.replace(path)
        return True
    except OSError:
        return False


def _compute_line_map(snapshot: str, current: str) -> dict[int, int]:
    """Return a mapping {snapshot_line_no -> current_line_no} (1-based).

    For 'equal' opcode blocks the mapping is exact. For 'replace' / 'delete'
    blocks each snapshot line maps to the closest surviving current line
    (clamped to the new block's range, or to the line just before for pure
    deletes). The caller decides what to do with such mappings (typically
    flag the comment as 'moved').
    """
    a = snapshot.splitlines()
    b = current.splitlines()
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                out[i1 + k + 1] = j1 + k + 1
        elif tag == 'replace':
            span_b = max(j2 - j1, 1)
            for k in range(i2 - i1):
                out[i1 + k + 1] = min(j2, j1 + k * span_b // max(i2 - i1, 1)) + 1
        elif tag == 'delete':
            # Anchor each deleted snapshot line to the surviving current line
            # right before the delete. For a pure prefix delete (j1 == 0)
            # there's no surviving line above, so leave it unmapped: the
            # caller falls back to the original line_no, which won't match
            # any rendered diff line and routes the comment to the orphan
            # section instead of attaching it to unrelated code at line 1.
            if j1 == 0:
                continue
            for k in range(i2 - i1):
                out[i1 + k + 1] = j1
    return out


class ReviewStore:
    """Comments anchored to a snapshot of the file at creation time. On
    lookup the snapshot is diffed against the current working-tree file to
    remap line numbers, so a comment stays near the right line even after
    the file is edited.

    JSON layout (.gitr/review.json):
      {"files": {"<path>": [
          {"snapshot": "<sha1>", "line_no": <int>, "side": "+|-| ",
           "line_text": "<diff line text>", "comment": "<text>",
           "kind": "note|good|bad"}      (absent = note)
      ]}}

    Snapshot blobs live in .gitr/snapshots/<sha1> as plain text. Multiple
    comments on the same file at the same time share one snapshot.

    TODO: GC unreferenced snapshot files periodically (e.g. on store load
    when the count exceeds a threshold).
    """

    def __init__(self, gitr_dir: 'Path | None' = None) -> None:
        # the server passes the worktree's .gitr; the CLI uses the cwd's repo
        gitr_dir = gitr_dir or _find_gitr_dir()
        self._gitr_dir = gitr_dir
        self._path = (gitr_dir / 'review.json') if gitr_dir else None
        self._snap_dir = (gitr_dir / 'snapshots') if gitr_dir else None
        self._data: dict[str, list[dict]] = {}
        # Cache of snapshot content keyed by sha; populated on demand.
        self._snap_cache: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        self._data = {}
        if not (self._path and self._path.exists()):
            return
        try:
            obj = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(obj, dict):
            # Old list-shaped review.json from before the snapshot anchor
            # rewrite. The comments there don't have snapshots and can't be
            # remapped, so they're ignored. Tell the user once so the data
            # vanishing isn't a surprise.
            print(f'gitr: ignoring legacy {self._path} '
                  '(snapshot-based anchors required)', file=sys.stderr)
            return
        files = obj.get('files')
        if isinstance(files, dict):
            self._data = {f: list(entries) for f, entries in files.items()
                          if isinstance(entries, list)}

    def _save(self) -> None:
        if self._path:
            _write_json(self._path, {'files': self._data})

    def write_snapshot(self, content: str) -> 'str | None':
        """Persist ``content`` under .gitr/snapshots/<sha1> and return the sha,
        or ``None`` if the snapshot can't be persisted. Returning ``None``
        prevents callers from storing an entry whose snapshot won't survive
        this process: without persistence the in-memory cache is the only
        copy and remap on the next run would silently degrade."""
        sha = hashlib.sha1(content.encode('utf-8', errors='replace')).hexdigest()
        if not self._snap_dir:
            return None
        try:
            self._snap_dir.mkdir(parents=True, exist_ok=True)
            p = self._snap_dir / sha
            if not p.exists():
                p.write_text(content)
        except OSError as e:
            print(f'gitr: cannot write snapshot {sha}: {e}', file=sys.stderr)
            return None
        self._snap_cache[sha] = content
        return sha

    def read_snapshot(self, sha: str) -> 'str | None':
        if sha in self._snap_cache:
            return self._snap_cache[sha]
        if not self._snap_dir:
            return None
        text = _read_text_safe(self._snap_dir / sha)
        if text is not None:
            self._snap_cache[sha] = text
        return text

    @staticmethod
    def is_entry(e: dict, snapshot: str, line_no: int, side: str) -> bool:
        """Whether `e` is the comment with this key; an entry without a side
        is read as ' ' everywhere (_resolve_anchors), so it is matched so."""
        return (e.get('snapshot') == snapshot and e.get('line_no') == line_no
                and (e.get('side') or ' ') == (side or ' '))

    def find(self, file: str, snapshot: str, line_no: int, side: str) -> 'dict | None':
        return next((e for e in self._data.get(file, [])
                     if self.is_entry(e, snapshot, line_no, side)), None)

    @contextmanager
    def _writing(self):
        """Each change goes into the file as it is now, under a lock: gitr,
        gitr serve and `gitr comments` may all write it, and one
        that kept its own copy would otherwise undo the others' changes."""
        with _locked(self._gitr_dir, 'review.lock'):
            self._load()
            yield

    def add(self, file: str, snapshot: str, line_no: int, side: str,
            line_text: str, comment: str, kind: str = 'note') -> None:
        with self._writing():
            e = self.find(file, snapshot, line_no, side)
            if e is not None:
                e.update(line_text=line_text, comment=comment, kind=kind)
            else:
                self._data.setdefault(file, []).append(
                    {'snapshot': snapshot, 'line_no': line_no, 'side': side,
                     'line_text': line_text, 'comment': comment, 'kind': kind})
            self._save()

    def delete(self, file: str, snapshot: str, line_no: int, side: str) -> None:
        with self._writing():
            e = self.find(file, snapshot, line_no, side)
            if e is None:
                return
            self._data[file].remove(e)
            if not self._data[file]:
                del self._data[file]
            self._save()

    def all_entries(self) -> list[tuple[str, dict]]:
        return [(f, e) for f in sorted(self._data) for e in self._data[f]]

    def is_empty(self) -> bool:
        return not any(self._data.values())

    def backup_path(self) -> 'Path | None':
        """Where clear() keeps the previous file: review-<timestamp>.json.bak
        beside it, so repeated clears never overwrite an earlier backup."""
        if not self._path:
            return None
        stamp = time.strftime('%Y%m%d-%H%M%S')
        return self._path.with_name(f'review-{stamp}.json.bak')

    def clear(self, backup: 'Path | None') -> bool:
        """Drop every comment, first moving the current file to `backup` so
        a mis-click can be undone by renaming it back. Nothing is dropped
        when that move fails; returns whether the store was cleared."""
        with self._writing():
            if not self._data:
                return True
            if backup is not None and self._path and self._path.exists():
                try:
                    self._path.replace(backup)  # one atomic rename, no partial copy
                except OSError:
                    return False
            self._data.clear()
            self._save()
            return True


class ViewedStore:
    """The hunks marked as viewed, in .gitr/viewed.json: {"viewed": {key:
    unix time marked}}. A key is a hunk's content (hunk_key in gitr serve),
    so a hunk an agent changes is no longer viewed, and one that only moved
    still is. Keys of hunks no longer in the diff stay, as they may be in
    another view of it (another base, a single commit); the oldest go once
    there are more than MAX."""

    MAX = 5000

    def __init__(self, gitr_dir: 'Path | None') -> None:
        self._gitr_dir = gitr_dir
        self._path = (gitr_dir / 'viewed.json') if gitr_dir else None

    def load(self) -> dict[str, float]:
        if not (self._path and self._path.exists()):
            return {}
        try:
            obj = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        viewed = obj.get('viewed') if isinstance(obj, dict) else None
        return viewed if isinstance(viewed, dict) else {}

    def mark(self, keys: 'list[str]', viewed: bool) -> bool:
        """Returns whether the file was written."""
        if not self._path:
            return False
        with _locked(self._gitr_dir, 'viewed.lock'):
            data = self.load()
            now = round(time.time())
            for k in keys:
                if viewed:
                    data[k] = now
                else:
                    data.pop(k, None)
            if len(data) > self.MAX:
                # The keys just marked all have the same time; none of them
                # goes, only older ones.
                batch = set(keys) if viewed else set()
                older = sorted((kv for kv in data.items() if kv[0] not in batch), key=lambda kv: kv[1])
                keep = older[len(data) - self.MAX:] if len(data) - self.MAX < len(older) else []
                data = {**dict(keep), **{k: data[k] for k in batch}}
            return _write_json(self._path, {'viewed': data})


# --diff parsing ------------------------------------------------------------

_FILEHEADER_PREFIXES = (
    'diff ', 'index ', '--- ', '+++ ',
    'new file', 'deleted file', 'old mode', 'new mode', 'rename ',
    # a renamed or copied file with changes (copies only with git diff -C)
    'similarity index ', 'dissimilarity index ', 'copy from ', 'copy to ',
)


def _classify(line: str) -> str:
    if line.startswith(_FILEHEADER_PREFIXES):
        return 'fileheader'
    if line.startswith('@@ '):
        return 'hunk'
    if line.startswith('+'):
        return 'added'
    if line.startswith('-'):
        return 'removed'
    return 'context'


_HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def parse_diff(text: str) -> list[DiffFile]:
    files: list[DiffFile] = []
    current: Optional[DiffFile] = None
    cur_old = cur_new = 0  # next line numbers within the active hunk
    # Lines the active hunk still holds, from its header. Inside a hunk only
    # the first character decides the kind: a removed "-- x" reads "--- x"
    # and must not be taken for a file header.
    left_old = left_new = 0

    for raw in text.splitlines():
        if raw.startswith('diff --git '):
            if current is not None:
                files.append(current)
            b_idx = raw.rfind(' b/')
            path = raw[b_idx + 3:] if b_idx != -1 else 'unknown'
            current = DiffFile(path)
            cur_old = cur_new = 0
            left_old = left_new = 0
        if current is not None:
            if (left_old > 0 or left_new > 0) and raw[:1] in ('+', '-', ' ', ''):
                kind = {'+': 'added', '-': 'removed'}.get(raw[:1], 'context')
                if kind != 'added':
                    left_old -= 1
                if kind != 'removed':
                    left_new -= 1
            else:
                kind = _classify(raw)
            new_no: Optional[int] = None
            old_no: Optional[int] = None
            if kind == 'hunk':
                m = _HUNK_RE.match(raw)
                if m:
                    cur_old = int(m.group(1))
                    cur_new = int(m.group(3))
                    left_old = int(m.group(2) or 1)
                    left_new = int(m.group(4) or 1)
            elif kind == 'context':
                old_no, new_no = cur_old, cur_new
                cur_old += 1
                cur_new += 1
            elif kind == 'added':
                new_no = cur_new
                cur_new += 1
            elif kind == 'removed':
                old_no = cur_old
                # Anchor a removed line to the post-image position it sits
                # between (same as the next + / context line that follows it).
                new_no = cur_new
                cur_old += 1
            dl = DiffLine(raw, kind, new_line_no=new_no, old_line_no=old_no)
            current.lines.append(dl)
            if kind == 'fileheader':
                if raw.startswith('new file'):
                    current.status = 'A'
                elif raw.startswith('deleted file'):
                    current.status = 'D'
                elif raw.startswith('rename from '):
                    current.status = 'R'
                    current.old_path = raw[len('rename from '):]
                elif raw.startswith('copy from '):
                    current.status = 'C'
                    current.old_path = raw[len('copy from '):]
                elif raw.startswith('index '):
                    current.index = raw

    if current is not None:
        files.append(current)
    return files


# Review comment kinds, the order they are listed and exported in.
COMMENT_KINDS = ('note', 'good', 'bad')


def _comment_kind(name: object) -> str:
    return name if isinstance(name, str) and name in COMMENT_KINDS else 'note'


# --review export -------------------------------------------------------------
#
# One text shape for handing comments to a coding agent, shared by the
# page's "copy for agent" and `gitr export`: grouped by file,
# each item a path:line reference the agent can open, the quoted diff line
# and the comment, with the kind spelled out.

@dataclass
class _ExportItem:
    path: str
    line: Optional[int]  # post-image line number; None when unknown
    side: str
    kind: str
    text: str            # diff line text including its +/-/space prefix
    comment: str
    moved: bool = False
    orphaned: bool = False


def _export_kinds(spec: str) -> list[str]:
    """Kinds named by --kinds: 'all' or a comma-separated list."""
    if spec == 'all':
        return list(COMMENT_KINDS)
    kinds = [k.strip() for k in spec.split(',') if k.strip()]
    bad = [k for k in kinds if k not in COMMENT_KINDS]
    if bad or not kinds:
        sys.exit(f'gitr: --kinds takes "all" or a list of {", ".join(COMMENT_KINDS)}')
    return kinds


def _diff_side(kind: str) -> str:
    """The side letter a comment is stored with for a diff line kind."""
    if kind == 'added':   return '+'
    if kind == 'removed': return '-'
    return ' '


def _entry_is_valid(entry: dict) -> bool:
    """A stored comment that can be anchored; the others are ignored
    everywhere (rendering, export, counts) so no view shows a comment the
    others do not."""
    return bool(entry.get('snapshot') and entry.get('line_no') and entry.get('comment'))


def _same_line(side_a: str, side_b: str) -> bool:
    """Whether two comment sides can name the same diff line. An added and
    an unchanged line are both a line of the current file, and which one it
    is depends only on the diff it is seen in: a comment written on a +
    line (or from `gitr comments add`, which knows no diff) belongs on that
    line in any diff. A removed line exists only in a diff."""
    return side_a == side_b or (side_a in '+ ' and side_b in '+ ')


def _match_anchor(anchors: list['_ResolvedAnchor'], new_line_no: int, side: str,
                  text: str) -> '_ResolvedAnchor | None':
    """Claim the stored comment for a diff line: the one whose text still
    matches wins over one at the same line whose text changed."""
    exact: '_ResolvedAnchor | None' = None
    loose: '_ResolvedAnchor | None' = None
    for a in anchors:
        if a.matched or a.target_line_no != new_line_no or not _same_line(a.side, side):
            continue
        # the text without its +/-/space prefix, which is the side again
        if a.line_text[1:] == text[1:]:
            exact = a
            break
        loose = loose or a
    a = exact or loose
    if a is None:
        return None
    a.matched = True
    a.moved = (exact is None) or (a.snap_line_no != a.target_line_no)
    return a


def _code_fence(text: str) -> str:
    """A backtick fence longer than any backtick run in `text`, so the
    fence cannot be closed early by the content."""
    return '`' * max(3, max((len(m) for m in re.findall('`+', text)), default=0) + 1)


def _format_export(items: list[_ExportItem], kinds: list[str]) -> str:
    """Markdown: a heading per file, a list item per comment. The diff line
    goes in a fenced code block, since quoted or bare it would be parsed
    (a leading + or - is a list bullet, # a heading, indentation a code
    block); the fence is longer than any backtick run in the line."""
    items = [i for i in items if i.kind in kinds]  # in diff order, files contiguous
    label = ', '.join(k for k in COMMENT_KINDS if k in kinds)
    n = len(items)
    out = [f'Review comments: {n} item{"s" if n != 1 else ""} ({label})', '']
    path = None
    for it in items:
        if it.path != path:
            path = it.path
            out += [f'## {path}', '']
        ref = f'{it.path}:{it.line}' if it.line else it.path
        side = f' ({it.side})' if it.side in '+-' else ''
        flags = [it.kind, 'orphaned'] if it.orphaned else [it.kind, 'moved'] if it.moved else [it.kind]
        out.append(f'- {ref}{side} [{", ".join(flags)}]')
        fence = _code_fence(it.text)
        out += [f'  {fence}diff', f'  {it.text}', f'  {fence}']
        out += [f'  {l}' for l in (it.comment.splitlines() or [''])]
        out.append('')
    out.append('---')  # marks the end, e.g. among other terminal output
    return '\n'.join(out)


def _resolve_anchors(review: 'ReviewStore',
                     read_current: 'Callable[[str], str | None]') -> dict[str, list['_ResolvedAnchor']]:
    """Map every stored comment through its snapshot to a target line in
    the current working tree (via difflib)."""
    result: dict[str, list[_ResolvedAnchor]] = {}
    current_cache: dict[str, str | None] = {}
    map_cache: dict[tuple[str, str], dict[int, int]] = {}
    for file, entry in review.all_entries():
        snap_sha  = str(entry.get('snapshot') or '')
        line_no   = int(entry.get('line_no') or 0)
        side      = str(entry.get('side') or ' ')
        line_text = str(entry.get('line_text') or '')
        comment   = str(entry.get('comment') or '')
        kind      = _comment_kind(entry.get('kind'))
        if not _entry_is_valid(entry):
            continue
        if file not in current_cache:
            current_cache[file] = read_current(file)
        current = current_cache[file]
        snap    = review.read_snapshot(snap_sha)
        if current is not None and snap is not None:
            key = (snap_sha, file)
            line_map = map_cache.get(key)
            if line_map is None:
                line_map = _compute_line_map(snap, current)
                map_cache[key] = line_map
            target = line_map.get(line_no, line_no)
        else:
            target = line_no
        result.setdefault(file, []).append(_ResolvedAnchor(
            file=file, snapshot=snap_sha, snap_line_no=line_no,
            target_line_no=target, side=side, line_text=line_text,
            comment=comment, kind=kind,
        ))
    return result


def _export_items_from_store(review: 'ReviewStore', repo_root: Path,
                             diff_files: list[DiffFile]) -> list[_ExportItem]:
    """The export for a diff: anchors are matched against the parsed diff
    the way gitr serve does (same target
    line and side; the text decides between exact and moved)."""
    anchors = _resolve_anchors(review, lambda f: _read_text_safe(repo_root / f))

    def item(a: '_ResolvedAnchor') -> _ExportItem:
        return _ExportItem(a.file, a.target_line_no, a.side, a.kind, a.line_text,
                           a.comment, a.moved, orphaned=not a.matched)

    items: list[_ExportItem] = []
    for df in diff_files:  # diff order, each file's orphans after its hunks, as rendered
        for dl in df.lines:
            if dl.new_line_no is None:
                continue
            a = _match_anchor(anchors.get(df.path, []), dl.new_line_no, _diff_side(dl.kind), dl.text)
            if a is not None:
                items.append(item(a))
        items += [item(a) for a in anchors.pop(df.path, []) if not a.matched]
    for alist in anchors.values():  # files not in this diff
        items += [item(a) for a in alist]
    return items


# --word diff (gitr serve's word ranges) ---------------------------------------

WORD_DIFF_MIN_RATIO = 0.35       # below this similarity, a plain line diff
WORD_DIFF_AUTOJUNK_TOKENS = 300  # longer lines use difflib's junk heuristic (much faster)
WORD_DIFF_MAX_BLOCK = 40000      # removed x added lines above which pairing is sequential


def _use_autojunk(a: list[str], b: list[str]) -> bool:
    """Exact matching is quadratic in line length; on very long lines (prose,
    minified data) difflib's autojunk heuristic is ~25x faster at the cost of
    slightly fewer matched tokens, so it is switched on only for those."""
    return max(len(a), len(b)) > WORD_DIFF_AUTOJUNK_TOKENS


def _word_matcher(a: list[str], b: list[str]) -> difflib.SequenceMatcher:
    return difflib.SequenceMatcher(None, a, b, autojunk=_use_autojunk(a, b))


def _pair_lines_for_word_diff(
    rem_lines: list[str], add_lines: list[str]
) -> list[tuple]:
    """Order-preserving optimal matching between removed and added lines.

    Returns a list of ('pair', old, new, same_tokens), ('rem', old), or
    ('add', new); same_tokens is True when the lines differ only in whitespace.
    Uses DP to maximise total similarity, so a re-indented block mixed with
    inserted/deleted lines gets correctly paired rather than sequentially
    mis-matched.  Lines with similarity below WORD_DIFF_MIN_RATIO are
    left unpaired and rendered as plain removed/added.
    """
    m, n = len(rem_lines), len(add_lines)

    def nws_tokens(text: str) -> list[str]:
        return [t for t in re.findall(r'\w+|[^\w\s]|\s+', text) if not t.isspace()]

    # Similarity counts words only, each by its length: punctuation is
    # shared by unrelated lines of code (`print(f(x))` and `p = g(h(3))`
    # match on half their tokens), and counted per token, a version bump
    # (`websockets==16.0`, `websockets==17.4`) matched on one word in three.
    def sim_tokens(toks: list[str]) -> list[str]:
        return [t for t in toks if t[0].isalnum() or t[0] == '_'] or toks

    tok_rem = [nws_tokens(line) for line in rem_lines]
    tok_add = [nws_tokens(line) for line in add_lines]
    sim_rem = [sim_tokens(t) for t in tok_rem]
    sim_add = [sim_tokens(t) for t in tok_add]
    len_rem = [sum(map(len, t)) for t in sim_rem]
    len_add = [sum(map(len, t)) for t in sim_add]
    bag_rem: list['Counter[str] | None'] = [None] * m
    bag_add: list['Counter[str] | None'] = [None] * n

    thr = WORD_DIFF_MIN_RATIO

    def similarity(i: int, j: int, sm: difflib.SequenceMatcher) -> float:
        """The share of the two lines' word characters in matching words,
        0 when that is below the threshold; `sm` has the added line as seq2."""
        if tok_rem[i] == tok_add[j]:  # whitespace apart, blank lines too
            return 1.0
        total = len_rem[i] + len_add[j]
        if not total or 2 * min(len_rem[i], len_add[j]) < thr * total:
            return 0.0
        # Most cross pairs in a big block are far below the threshold: the
        # words both have, ignoring order, bound the matching from above.
        if bag_rem[i] is None:
            bag_rem[i] = Counter(sim_rem[i])
        if bag_add[j] is None:
            bag_add[j] = Counter(sim_add[j])
        if 2 * sum(len(t) * k for t, k in (bag_rem[i] & bag_add[j]).items()) < thr * total:
            return 0.0
        sm.set_seq1(sim_rem[i])
        a = sim_rem[i]
        same = sum(len(t) for blk in sm.get_matching_blocks() for t in a[blk.a:blk.a + blk.size])
        r = 2 * same / total
        return r if r >= thr else 0.0
    if m * n > WORD_DIFF_MAX_BLOCK:
        # The optimal matching is quadratic (a 500x500 block takes seconds);
        # pair lines in order instead, as git's word diff does, and only
        # check each pair's similarity.
        result: list[tuple] = []
        k = min(m, n)
        for i in range(k):
            if similarity(i, i, _word_matcher(sim_rem[i], sim_add[i])):
                result.append(('pair', rem_lines[i], add_lines[i], tok_rem[i] == tok_add[i]))
            else:
                result.append(('rem', rem_lines[i]))
                result.append(('add', add_lines[i]))
        result.extend(('rem', line) for line in rem_lines[k:])
        result.extend(('add', line) for line in add_lines[k:])
        return result
    sims = [[0.0] * n for _ in range(m)]
    for j, b in enumerate(sim_add):
        # One matcher per added line: set_seq2 builds the b-side index that
        # every removed line is compared against, so reuse it instead of
        # rebuilding it m times.  The autojunk choice is per pair.
        exact = difflib.SequenceMatcher(None, autojunk=False)
        exact.set_seq2(b)
        junk: 'difflib.SequenceMatcher | None' = None
        for i, a in enumerate(sim_rem):
            if _use_autojunk(a, b):
                if junk is None:
                    junk = difflib.SequenceMatcher(None, autojunk=True)
                    junk.set_seq2(b)
                sm = junk
            else:
                sm = exact
            # Only pairs at or above the threshold feed the DP.
            sims[i][j] = similarity(i, j, sm)

    # dp[i][j] = best total similarity pairing rem[0..i-1] with add[0..j-1]
    dp     = [[0.0] * (n + 1) for _ in range(m + 1)]
    choice = [['']  * (n + 1) for _ in range(m + 1)]

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            best, ch = dp[i - 1][j], 'rem'
            if dp[i][j - 1] >= best:          # prefer 'add' on tie: removes-before-adds when unpaired
                best, ch = dp[i][j - 1], 'add'
            if sims[i - 1][j - 1] >= WORD_DIFF_MIN_RATIO:
                v = dp[i - 1][j - 1] + sims[i - 1][j - 1]
                if v > best:
                    best, ch = v, 'pair'
            dp[i][j] = best
            choice[i][j] = ch

    result: list[tuple] = []
    i, j = m, n
    while i > 0 or j > 0:
        if i == 0:
            result.append(('add', add_lines[j - 1]))
            j -= 1
        elif j == 0:
            result.append(('rem', rem_lines[i - 1]))
            i -= 1
        elif choice[i][j] == 'pair':
            result.append(('pair', rem_lines[i - 1], add_lines[j - 1],
                           tok_rem[i - 1] == tok_add[j - 1]))
            i -= 1
            j -= 1
        elif choice[i][j] == 'rem':
            result.append(('rem', rem_lines[i - 1]))
            i -= 1
        else:
            result.append(('add', add_lines[j - 1]))
            j -= 1
    result.reverse()
    return result


# --command line: comments ------------------------------------------------------
#
# `gitr comments ...` reads and writes .gitr/review.json from the terminal,
# so an agent or a script can take part in a review. A comment is put on a
# line of the file as it is in the working tree (a snapshot of it, as the
# page takes), with no diff involved; the views place it on
# that line whether their diff shows it added or unchanged (_same_line).

_ACCEPTED = '(accepted)'  # gitr serve's "accept" marks the text the same way


def _comment_id(file: str, entry: dict) -> str:
    """Stable while the comment exists: the key the store finds it by (an
    edit keeps it), hashed. review.json has no ids of its own."""
    side = entry.get('side') or ' '  # as ReviewStore.is_entry reads it
    key = f"{file}\0{entry.get('snapshot')}\0{entry.get('line_no')}\0{side}"
    return hashlib.sha1(key.encode()).hexdigest()


def _cli_repo() -> Path:
    root = _find_repo_root()
    if root is None:
        sys.exit('gitr: not in a git repository (comments live in <repo>/.gitr)')
    return root


def _repo_path(root: Path, arg: str) -> str:
    """A path as git takes it (relative to the current directory, or
    absolute) as the repo-relative path review.json keys files by."""
    # Not resolved: a symlink in the repo is a file of its own to git and the
    # views, so a comment on it is keyed by its own path.
    full = os.path.normpath(os.path.join(os.getcwd(), arg))
    rel = os.path.relpath(full, os.path.normpath(str(root)))
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        sys.exit(f'gitr: {arg} is outside the repository {root}')
    return '' if rel == '.' else Path(rel).as_posix()


@dataclass
class _CliComment:
    id: str
    file: str
    anchor: '_ResolvedAnchor'
    changed: bool  # the line's text is no longer the one commented on
    gone: bool     # the file is gone
    removed: bool  # on a removed line, which is in no current file


def _cli_comments(root: Path, review: 'ReviewStore') -> list[_CliComment]:
    """Every comment, its line remapped to the current file, in file and
    line order."""
    current: dict[str, 'str | None'] = {}

    def read(f: str) -> 'str | None':
        if f not in current:
            current[f] = _read_text_safe(root / f)
        return current[f]

    out = []
    for file, anchors in _resolve_anchors(review, read).items():
        text = read(file)
        lines = text.splitlines() if text is not None else []
        for a in anchors:
            entry = {'snapshot': a.snapshot, 'line_no': a.snap_line_no, 'side': a.side}
            gone, removed = text is None, a.side == '-'
            changed = not gone and not removed and (a.target_line_no > len(lines)
                                                    or lines[a.target_line_no - 1] != a.line_text[1:])
            out.append(_CliComment(_comment_id(file, entry), file, a, changed, gone, removed))
    out.sort(key=lambda c: (c.file, c.anchor.target_line_no))
    return out


def _cli_line(c: _CliComment, width: int = 0) -> str:
    loc = f'{c.file}:{c.anchor.target_line_no}'
    first, *more = c.anchor.comment.splitlines() or ['']
    flags = [f'+{len(more)} line{"s" if len(more) != 1 else ""}'] if more else []
    flags += ['file gone'] if c.gone else ['removed line'] if c.removed else ['line changed'] if c.changed else []
    tail = f'  ({", ".join(flags)})' if flags else ''
    return f'{c.id[:7]}  {loc:<{width}}  {"[" + c.anchor.kind + "]":<6}  {first}{tail}'


def _cli_select(root: Path, review: 'ReviewStore', sel: str) -> _CliComment:
    """A comment by id (any unique prefix of 4 or more) or by path:line."""
    found = _cli_comments(root, review)
    path, sep, line = sel.rpartition(':')
    if sep and line.isdigit():
        rel = _repo_path(root, path)
        hits = [c for c in found if c.file == rel and c.anchor.target_line_no == int(line)]
        what = f'on {rel}:{line}'
    else:
        if len(sel) < 4 or not re.fullmatch(r'[0-9a-f]+', sel):
            sys.exit(f'gitr: {sel!r} is neither a comment id (4 or more hex digits) nor path:line')
        hits = [c for c in found if c.id.startswith(sel)]
        what = f'with id {sel}'
    if not hits:
        sys.exit(f'gitr: no comment {what} (gitr comments list shows them)')
    if len(hits) > 1:
        sys.exit(f'gitr: {len(hits)} comments {what}, name one by its id:\n'
                 + '\n'.join('  ' + _cli_line(c) for c in hits))
    return hits[0]


def _comments_list(args: argparse.Namespace) -> None:
    root = _cli_repo()
    kinds = _export_kinds(args.kinds)
    found = [c for c in _cli_comments(root, ReviewStore()) if c.anchor.kind in kinds]
    if args.paths:
        prefixes = [_repo_path(root, p) for p in args.paths]
        found = [c for c in found
                 if any(not p or c.file == p or c.file.startswith(p.rstrip('/') + '/') for p in prefixes)]
    if args.json:
        print(json.dumps([{'id': c.id[:7], 'path': c.file, 'line': c.anchor.target_line_no,
                           'side': c.anchor.side, 'kind': c.anchor.kind, 'text': c.anchor.comment,
                           'lineText': c.anchor.line_text, 'changed': c.changed, 'gone': c.gone,
                           'removed': c.removed}
                          for c in found], indent=2))
        return
    width = max((len(f'{c.file}:{c.anchor.target_line_no}') for c in found), default=0)
    for c in found:
        print(_cli_line(c, width))
        if args.full:
            for l in c.anchor.comment.splitlines():
                print('    ' + l)


def _comments_add(args: argparse.Namespace) -> None:
    root = _cli_repo()
    path, sep, line = args.location.rpartition(':')
    if not sep or not line.isdigit() or not int(line):
        sys.exit(f'gitr: {args.location!r} is not path:line (a line of the file as it is now)')
    rel, n = _repo_path(root, path), int(line)
    text = sys.stdin.read() if args.text == '-' else args.text
    text = text.strip('\n')
    if not text.strip():
        sys.exit('gitr: the comment is empty')
    content = _read_text_safe(root / rel)
    if content is None:
        sys.exit(f'gitr: cannot read {rel} (comments go on lines of an existing text file)')
    lines = content.splitlines()
    if n > len(lines):
        sys.exit(f'gitr: {rel} has {len(lines)} lines, no line {n}')
    review = ReviewStore()
    for c in _cli_comments(root, review):
        if c.file == rel and c.anchor.target_line_no == n and c.anchor.side != '-':
            sys.exit(f'gitr: {rel}:{n} already has a comment: {_cli_line(c)}\n'
                     '  remove it first (gitr comments rm), or edit it in gitr or gitr serve')
    snap = review.write_snapshot(content)
    if snap is None:
        sys.exit(f'gitr: cannot write a snapshot to {root / ".gitr"}')
    review.add(rel, snap, n, ' ', ' ' + lines[n - 1], text, args.kind)
    entry = {'snapshot': snap, 'line_no': n, 'side': ' '}
    print(f'{_comment_id(rel, entry)[:7]}  {rel}:{n}  [{args.kind}]')


def _comments_rm(args: argparse.Namespace) -> None:
    root = _cli_repo()
    review = ReviewStore()
    c = _cli_select(root, review, args.comment)
    review.delete(c.file, c.anchor.snapshot, c.anchor.snap_line_no, c.anchor.side)
    print('removed ' + _cli_line(c))


def _comments_accept(args: argparse.Namespace) -> None:
    root = _cli_repo()
    review = ReviewStore()
    c = _cli_select(root, review, args.comment)
    a = c.anchor
    if a.kind == 'good' and a.comment.rstrip().endswith(_ACCEPTED):
        print('already accepted: ' + _cli_line(c))
        return
    # as gitr serve's accept: the marker once, whatever the kind was
    text = a.comment if a.comment.rstrip().endswith(_ACCEPTED) else a.comment.rstrip() + ' ' + _ACCEPTED
    review.add(c.file, a.snapshot, a.snap_line_no, a.side, a.line_text, text, 'good')
    a.comment, a.kind = text, 'good'
    print('accepted ' + _cli_line(c))


def _main_comments(argv: list[str]) -> None:
    kinds = ', '.join(COMMENT_KINDS)
    p = argparse.ArgumentParser(
        prog='gitr comments', description='Read and write the review comments of the '
        'repository you are in (.gitr/review.json, shared with the page). '
        'A comment is named by its id (as listed, or any unique prefix of 4 or more '
        'characters) or by path:line.')
    sub = p.add_subparsers(dest='action', required=True, metavar='list|add|rm|accept')
    ls = sub.add_parser('list', help='list the comments, lines as in the current files')
    ls.add_argument('paths', nargs='*', help='only comments on these files or under these folders')
    ls.add_argument('--kinds', default='all', help=f'"all" or some of {kinds} (default: all)')
    ls.add_argument('--full', action='store_true', help='each comment\'s whole text')
    ls.add_argument('--json', action='store_true', help='as JSON')
    ls.set_defaults(fn=_comments_list)
    add = sub.add_parser('add', help='comment on a line of a file as it is now')
    add.add_argument('location', metavar='path:line')
    add.add_argument('text', help='the comment; - reads it from stdin')
    add.add_argument('--kind', choices=list(COMMENT_KINDS), default='note')
    add.set_defaults(fn=_comments_add)
    rm = sub.add_parser('rm', help='delete a comment')
    rm.add_argument('comment', metavar='id|path:line')
    rm.set_defaults(fn=_comments_rm)
    acc = sub.add_parser('accept', help=f'resolved: make it a good comment ending in "{_ACCEPTED}"')
    acc.add_argument('comment', metavar='id|path:line')
    acc.set_defaults(fn=_comments_accept)
    args = p.parse_args(argv)
    args.fn(args)


# --command line ------------------------------------------------------------------

# The first argument names one of these, or it is the base of the default
# command, the window; after `--` it is the base.
COMMANDS = ('export', 'comments', 'serve', 'status', 'help')


def main() -> None:
    argv = sys.argv[1:]
    if argv[:1] == ['--export']:  # the old spelling
        argv = ['export', *argv[1:]]
    cmd = argv[0] if argv and argv[0] in COMMANDS else None
    rest = argv[1:] if cmd else argv
    if not cmd and rest[:1] in (['-h'], ['--help']):
        cmd = 'help'
    if rest[:1] == ['--'] and not cmd:
        rest = rest[1:]
    if cmd == 'help':
        print(USAGE, end='')
        return
    if cmd == 'comments':
        return _main_comments(rest)
    if cmd == 'export':
        return _main_export(rest)
    if not cmd and _piped():
        print('gitr: a piped diff is not read any more; showing the worktree against its base',
              file=sys.stderr)
    # gitr_web is the package beside this module (installed, or in the checkout)
    from gitr_web import server
    if cmd == 'serve':
        server.main(rest, prog='gitr serve')
    elif cmd == 'status':
        server.status(rest)
    else:
        server.standalone(rest)


def _piped() -> bool:
    """stdin is a pipe or a file, not a terminal or nothing (an agent's
    shell often gives an empty one, which says nothing)."""
    try:
        mode = os.fstat(sys.stdin.fileno()).st_mode
    except (OSError, ValueError, AttributeError):
        return False
    return stat.S_ISFIFO(mode) or (stat.S_ISREG(mode) and os.fstat(sys.stdin.fileno()).st_size > 0)


def _main_export(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='gitr export', description='Print the review comments as text for a coding agent, '
        'placed on the diff git diff gives for the refs (default: the unstaged changes), or on '
        'a patch with -p FILE (- for stdin).')
    parser.add_argument('--merge-base', action='store_true', dest='merge_base',
                        help='diff from the merge base of the first ref')
    parser.add_argument('--kinds', default='all', metavar='KINDS',
                        help=f'"all" or some of {",".join(COMMENT_KINDS)}')
    parser.add_argument('-p', '--patch', metavar='FILE')
    parser.add_argument('refs', nargs='*')
    args = parser.parse_args(argv)
    if args.merge_base and not args.refs:
        sys.exit('gitr: --merge-base requires a ref (e.g. gitr export --merge-base master)')
    kinds = _export_kinds(args.kinds)
    root = _find_repo_root()
    if root is None:
        sys.exit('gitr: export needs a git repository (comments live in <repo>/.gitr)')
    if args.patch is not None or args.refs == ['-']:
        try:
            text = sys.stdin.read() if args.patch in (None, '-') else Path(args.patch).read_text()
        except OSError as e:
            sys.exit(f'gitr: {e}')
    else:
        text = _git_diff(args.refs, args.merge_base)
    diff_files = parse_diff(text)
    items = _export_items_from_store(ReviewStore(), root, diff_files)
    sys.stdout.write(_format_export(items, kinds) + '\n')


if __name__ == '__main__':
    main()
