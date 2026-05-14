#!/usr/bin/env python3
"""gitr - lightweight Git diff viewer"""

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_CONFIG_PATH = Path.home() / '.config' / 'gitr' / 'config.json'


USAGE = """\
usage:
  gitr                         # git diff (unstaged changes)
  gitr master                  # git diff master (to working tree)
  gitr --merge-base master     # diff from common ancestor to working tree
  gitr master HEAD             # git diff master HEAD (committed only)
  git diff | gitr              # pipe a patch
  gitr -                       # read stdin explicitly
  gitr -p patch.diff           # read from a patch file

  GITR_SCALE=2 gitr master   # scale UI up (HiDPI)
"""


# --data structures ----------------------------------------------------------

@dataclass
class FileEntry:
    path: str
    status: str              # A M D R
    additions: int = 0
    deletions: int = 0


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
class _CommentEditTarget:
    """In-flight state for the inline comment editor. Set when the editor
    opens, consumed (or discarded) when the user confirms or cancels."""
    file: str
    new_line_no: int
    side: str
    line_text: str
    # Non-None when editing an existing comment; None for a fresh comment
    # whose snapshot will be taken at confirm time.
    existing_snapshot: Optional[str] = None
    existing_snap_line_no: Optional[int] = None


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
    moved: bool = False
    matched: bool = False
    src_line: Optional[int] = None  # diff Text line number once rendered


# --git helpers ------------------------------------------------------------

def try_current_branch() -> str:
    r = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ''


# --diff sources ------------------------------------------------------------

class PatchSource:
    """Diff text from stdin or a patch file. Cannot fetch full file contents."""
    def __init__(self, text: str, label: str = '') -> None:
        self._text = text
        self._label = label

    def diff_text(self) -> str:
        return self._text

    def label(self) -> str:
        return self._label

    def commits(self) -> list[tuple[str, str]]:
        return []

    def has_staged(self) -> bool:
        return False

    def has_unstaged(self) -> bool:
        return False

class GitSource:
    """Diff text from a live git invocation. Can also fetch full file contents."""
    def __init__(self, refs: list[str], merge_base: bool = False) -> None:
        self._refs = refs
        self._merge_base = merge_base

    def diff_text(self) -> str:
        try:
            if self._merge_base:
                sha = subprocess.check_output(
                    ['git', 'merge-base', self._refs[0], 'HEAD'],
                    text=True, stderr=subprocess.PIPE).strip()
                return subprocess.check_output(
                    ['git', 'diff', '--no-color', sha], text=True, stderr=subprocess.PIPE)
            return subprocess.check_output(
                ['git', 'diff', '--no-color'] + self._refs, text=True, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            sys.exit(f'gitr: git command failed: {e.stderr.strip()}')
        except FileNotFoundError:
            sys.exit('gitr: git not found in PATH')

    def label(self) -> str:
        if self._merge_base:
            return f'--merge-base {self._refs[0]}'
        return ' '.join(self._refs)

    @staticmethod
    def _has_changes(cmd: list[str]) -> bool:
        try:
            return subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0
        except FileNotFoundError:
            return False

    def has_staged(self) -> bool:
        return self._has_changes(['git', 'diff', '--cached', '--quiet'])

    def has_unstaged(self) -> bool:
        return self._has_changes(['git', 'diff', '--quiet'])

    def commits(self) -> list[tuple[str, str]]:
        try:
            if self._merge_base:
                sha = subprocess.check_output(
                    ['git', 'merge-base', self._refs[0], 'HEAD'],
                    text=True, stderr=subprocess.PIPE).strip()
                range_arg = f'{sha}..HEAD'
            elif len(self._refs) == 0:
                return []
            elif len(self._refs) == 1:
                r = self._refs[0]
                range_arg = r.replace('...', '..') if '..' in r else f'{r}..HEAD'
            elif len(self._refs) == 2:
                range_arg = f'{self._refs[0]}..{self._refs[1]}'
            else:
                return []
            out = subprocess.check_output(
                ['git', 'log', '--pretty=format:%h%x09%s', range_arg],
                text=True, stderr=subprocess.PIPE)
            return [tuple(line.split('\t', 1)) for line in out.splitlines() if '\t' in line]
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []



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


def _load_window_state() -> 'dict | None':
    gitr_dir = _find_gitr_dir()
    if not gitr_dir:
        return None
    p = gitr_dir / 'window.json'
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _save_window_state(geometry: str, sash_ratio: float,
                       scroll_frac: float) -> None:
    gitr_dir = _find_gitr_dir()
    if not gitr_dir:
        return
    try:
        gitr_dir.mkdir(parents=True, exist_ok=True)
        (gitr_dir / 'window.json').write_text(json.dumps({
            'geometry':    geometry,
            'sash_ratio':  sash_ratio,
            'scroll_frac': scroll_frac,
        }, indent=2))
    except OSError:
        pass


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
            # there's no surviving line above, so leave it unmapped — the
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
           "line_text": "<diff line text>", "comment": "<text>"}
      ]}}

    Snapshot blobs live in .gitr/snapshots/<sha1> as plain text. Multiple
    comments on the same file at the same time share one snapshot.

    TODO: GC unreferenced snapshot files periodically (e.g. on store load
    when the count exceeds a threshold).
    """

    def __init__(self) -> None:
        gitr_dir = _find_gitr_dir()
        self._gitr_dir = gitr_dir
        self._path = (gitr_dir / 'review.json') if gitr_dir else None
        self._snap_dir = (gitr_dir / 'snapshots') if gitr_dir else None
        self._data: dict[str, list[dict]] = {}
        # Cache of snapshot content keyed by sha; populated on demand.
        self._snap_cache: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
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
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({'files': self._data}, indent=2))
        except OSError:
            pass

    def write_snapshot(self, content: str) -> 'str | None':
        """Persist ``content`` under .gitr/snapshots/<sha1> and return the sha,
        or ``None`` if the snapshot can't be persisted. Returning ``None``
        prevents callers from storing an entry whose snapshot won't survive
        this process — without persistence the in-memory cache is the only
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

    def add(self, file: str, snapshot: str, line_no: int, side: str,
            line_text: str, comment: str) -> None:
        entries = self._data.setdefault(file, [])
        for e in entries:
            if (e.get('snapshot') == snapshot and e.get('line_no') == line_no
                    and e.get('side') == side):
                e['line_text'] = line_text
                e['comment'] = comment
                self._save()
                return
        entries.append({'snapshot': snapshot, 'line_no': line_no, 'side': side,
                        'line_text': line_text, 'comment': comment})
        self._save()

    def delete(self, file: str, snapshot: str, line_no: int, side: str) -> None:
        entries = self._data.get(file)
        if not entries:
            return
        for i, e in enumerate(entries):
            if (e.get('snapshot') == snapshot and e.get('line_no') == line_no
                    and e.get('side') == side):
                del entries[i]
                if not entries:
                    del self._data[file]
                self._save()
                return

    def all_entries(self) -> list[tuple[str, dict]]:
        return [(f, e) for f in sorted(self._data) for e in self._data[f]]

    def is_empty(self) -> bool:
        return not any(self._data.values())

    def clear(self) -> None:
        if not self._data:
            return
        self._data.clear()
        self._save()


# --diff parsing ------------------------------------------------------------

_FILEHEADER_PREFIXES = (
    'diff ', 'index ', '--- ', '+++ ',
    'new file', 'deleted file', 'old mode', 'new mode', 'rename ',
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


_HUNK_RE = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@')


def parse_diff(text: str) -> list[DiffFile]:
    files: list[DiffFile] = []
    current: Optional[DiffFile] = None
    cur_old = cur_new = 0  # next line numbers within the active hunk

    for raw in text.splitlines():
        if raw.startswith('diff --git '):
            if current is not None:
                files.append(current)
            b_idx = raw.rfind(' b/')
            path = raw[b_idx + 3:] if b_idx != -1 else 'unknown'
            current = DiffFile(path)
            cur_old = cur_new = 0
        if current is not None:
            kind = _classify(raw)
            new_no: Optional[int] = None
            old_no: Optional[int] = None
            if kind == 'hunk':
                m = _HUNK_RE.match(raw)
                if m:
                    cur_old = int(m.group(1))
                    cur_new = int(m.group(2))
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
                elif raw.startswith('index '):
                    current.index = raw

    if current is not None:
        files.append(current)
    return files


def entries_from_diff(diff_files: list[DiffFile]) -> list[FileEntry]:
    return [
        FileEntry(df.path, df.status,
                  sum(1 for l in df.lines if l.kind == 'added'),
                  sum(1 for l in df.lines if l.kind == 'removed'))
        for df in diff_files
    ]


def _build_tree_rows(
    entries: list[FileEntry],
) -> list[tuple[str, int, 'FileEntry | None']]:
    """Return flat render list for tree view: (label, depth, entry_or_None).

    Directories with a single subdirectory and no files are folded into their
    child, so e.g. src/ -> main/ -> foo.py becomes ('src/main/', 0, None).
    Children at each level are ordered by their earliest position in the
    original entry list so tree order matches the diff panel order.
    """
    trie: dict = {}
    for i, e in enumerate(entries):
        parts = e.path.split('/')
        node = trie
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (i, e)  # leaf stores original index for ordering

    rows: list[tuple[str, int, 'FileEntry | None']] = []
    _walk_trie(trie, rows, 0, '')
    return rows


def _trie_min_idx(node: dict) -> int:
    best = 10 ** 9
    for v in node.values():
        if isinstance(v, tuple):
            best = min(best, v[0])
        elif isinstance(v, dict):
            best = min(best, _trie_min_idx(v))
    return best


def _walk_trie(node: dict, rows: list, depth: int, dir_label: str) -> None:
    leaves = [(k, v) for k, v in node.items() if isinstance(v, tuple)]
    dirs   = [(k, v) for k, v in node.items() if isinstance(v, dict)]

    # Fold single-child dir chains that contain no files.
    if not leaves and len(dirs) == 1:
        name, child = dirs[0]
        _walk_trie(child, rows, depth, dir_label + name + '/')
        return

    if dir_label:
        rows.append((dir_label, depth, None))
        depth += 1

    # Interleave files and subdirs in original diff order.
    children: list[tuple[int, str, 'FileEntry | None', 'dict | None']] = []
    for name, (idx, entry) in leaves:
        children.append((idx, name, entry, None))
    for name, child in dirs:
        children.append((_trie_min_idx(child), name, None, child))
    children.sort(key=lambda x: x[0])

    for _, name, entry, child in children:
        if entry is not None:
            rows.append((name, depth, entry))
        else:
            _walk_trie(child, rows, depth, name + '/')


def _common_dir_prefix(prev: str, curr: str) -> str:
    """Return the directory prefix shared between prev and curr, at path boundaries."""
    prev_dirs = prev.split('/')[:-1]
    curr_dirs = curr.split('/')[:-1]
    common = []
    for a, b in zip(prev_dirs, curr_dirs):
        if a == b:
            common.append(a)
        else:
            break
    return '/'.join(common) + '/' if common else ''


# --config -------------------------------------------------------------------

class CFG:
    font_family        = 'monospace'
    font_size          = 12
    menu_font_size     = 8
    window_scale       = 0.75    # fraction of screen size on startup
    sash_ratio         = 0.70
    scrollbar_w        = 16
    minimap_w          = 160
    scroll_speed       = 8   # lines per mouse-wheel tick
    diff_hi_blend      = 0.12   # bg intensity for changed lines / word-diff changed words
    diff_dim_blend     = 0.06   # bg intensity for word-diff unchanged words
    diff_dim_fg        = 0.50   # fg intensity for word-diff unchanged words
    word_diff_min_ratio = 0.35  # below this similarity, fall back to plain line diff
    hover_hide_delay_ms     = 150
    hover_btn_leave_delay_ms = 80
    edit_focus_out_delay_ms = 50
    list_pane_max_lines      = 10
    menu_label_max_len       = 80
    cmt_panel_label_max_len  = 120
    section_collapsed_arrow  = '▶'
    section_expanded_arrow   = '▼'


# --colour scheme (dracula) --------------------------------------------------

C = {
    'bg':            '#282a36',
    'fg':            '#f8f8f2',
    'added_fg':      '#50fa7b',
    'added_bg':      '#283636',
    'removed_fg':    '#ff5555',
    'removed_bg':    '#342a36',
    'hunk_fg':       '#ffb86c',
    'fileheader_fg': '#bd93f9',
    'subdued':       '#6272a4',
    'topbar_bg':     '#44475a',
    'selected_bg':   '#44475a',
    'status_A':      '#50fa7b',
    'status_M':      '#bd93f9',
    'status_D':      '#ff5555',
    'status_R':      '#ff79c6',
    'comment_fg':    '#f1fa8c',
}



def _blend(color: str, factor: float = 0.5) -> str:
    """Blend color toward the canvas background by factor (0=bg, 1=color)."""
    bg = C['bg']
    def _p(h: str) -> tuple[int, int, int]:
        h = h.lstrip('#')
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r0, g0, b0 = _p(bg)
    r1, g1, b1 = _p(color)
    r = int(r0 + (r1 - r0) * factor)
    g = int(g0 + (g1 - g0) * factor)
    b = int(b0 + (b1 - b0) * factor)
    return f'#{r:02x}{g:02x}{b:02x}'


def _mix(c1: str, c2: str, t: float) -> str:
    """Linear interpolation between two hex colors (t=0 → c1, t=1 → c2)."""
    def _p(h: str) -> tuple[int, int, int]:
        h = h.lstrip('#')
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r1, g1, b1 = _p(c1)
    r2, g2, b2 = _p(c2)
    return f'#{int(r1+(r2-r1)*t):02x}{int(g1+(g2-g1)*t):02x}{int(b1+(b2-b1)*t):02x}'


# non-whitespace pixel colours in the minimap; None = leave as canvas bg
_MINIMAP_COLORS: dict[str, str | None] = {
    'added':    _blend(C['added_fg'],      0.45),
    'removed':  _blend(C['removed_fg'],    0.45),
    'hunk':     _blend(C['hunk_fg'],       0.35),
    'filehdr':  _blend(C['fileheader_fg'], 0.35),
    'fileidx':  _blend(C['fileheader_fg'], 0.35),
    'context':  _blend(C['fg'], 0.18),
    'reindent': _blend(C['fg'], 0.18),
    'comment':  _blend(C['comment_fg'], 0.80),
    'orphan':   _blend(C['subdued'], 0.40),
}


# --application ------------------------------------------------------------

def _detect_scale(root: tk.Tk) -> float:
    """Return UI scale factor: 1.0 = 96 DPI (standard), 2.0 = HiDPI, etc.

    GITR_SCALE env var overrides auto-detection.
    """
    env = os.environ.get('GITR_SCALE')
    if env:
        try:
            return max(0.25, float(env))
        except ValueError:
            pass
    # winfo_fpixels('1i') = pixels per inch; 96 is the baseline for scale=1.
    dpi = root.winfo_fpixels('1i')
    return dpi / 96.0


def _primary_monitor_size() -> tuple[int, int]:
    try:
        out = subprocess.run(['xrandr', '--query'], capture_output=True, text=True,
                             timeout=1).stdout
        for line in out.splitlines():
            if 'primary' in line:
                m = re.search(r'(\d+)x(\d+)', line)
                if m:
                    return int(m.group(1)), int(m.group(2))
        # no primary keyword: use the first connected monitor
        for line in out.splitlines():
            if ' connected' in line:
                m = re.search(r'(\d+)x(\d+)', line)
                if m:
                    return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 1920, 1080


_MM_LINE_H = 2  # natural minimap pixels per source line (matches VS Code behaviour)


def _pair_lines_for_word_diff(
    rem_lines: list[str], add_lines: list[str]
) -> list[tuple]:
    """Order-preserving optimal matching between removed and added lines.

    Returns a list of ('pair', old, new), ('rem', old), or ('add', new).
    Uses DP to maximise total similarity, so a re-indented block mixed with
    inserted/deleted lines gets correctly paired rather than sequentially
    mis-matched.  Lines with similarity below CFG.word_diff_min_ratio are
    left unpaired and rendered as plain removed/added.
    """
    m, n = len(rem_lines), len(add_lines)

    def nws_tokens(text: str) -> list[str]:
        return [t for t in re.findall(r'\w+|[^\w\s]|\s+', text) if not t.isspace()]

    tok_rem = [nws_tokens(line) for line in rem_lines]
    tok_add = [nws_tokens(line) for line in add_lines]

    def sim(i: int, j: int) -> float:
        return difflib.SequenceMatcher(None, tok_rem[i], tok_add[j], autojunk=False).ratio()

    sims = [[sim(i, j) for j in range(n)] for i in range(m)]

    # dp[i][j] = best total similarity pairing rem[0..i-1] with add[0..j-1]
    dp     = [[0.0] * (n + 1) for _ in range(m + 1)]
    choice = [['']  * (n + 1) for _ in range(m + 1)]

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            best, ch = dp[i - 1][j], 'rem'
            if dp[i][j - 1] >= best:          # prefer 'add' on tie → removes-before-adds when unpaired
                best, ch = dp[i][j - 1], 'add'
            if sims[i - 1][j - 1] >= CFG.word_diff_min_ratio:
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
            result.append(('pair', rem_lines[i - 1], add_lines[j - 1]))
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


class App:
    def __init__(self, root: tk.Tk, diff_text: str,
                 commits: 'list[tuple[str, str]] | None' = None,
                 has_staged: bool = False, has_unstaged: bool = False) -> None:
        self.root = root
        # Override Text/Entry class bindings so Ctrl+W/Q always close the window
        # (default Text binding for Ctrl+W is "delete previous word", which would
        # otherwise consume the event before our bind_all reaches it).
        for cls in ('Text', 'Entry'):
            root.bind_class(cls, '<Control-w>', lambda e: self._close_app())
            root.bind_class(cls, '<Control-q>', lambda e: self._close_app())
        root.bind_all('<Control-w>', lambda e: self._close_app())
        root.bind_all('<Control-q>', lambda e: self._close_app())
        root.protocol('WM_DELETE_WINDOW', self._close_app)
        self.diff_text = diff_text
        self._entries: list[FileEntry] = []
        self._diff_files: list[DiffFile] = []
        self._positions: dict[str, str] = {}
        self._pos_order: list[tuple[int, str]] = []
        self._minimap_lines: list[tuple[str, str]] = []  # (kind, text)
        self._scroll_pos: tuple[float, float] = (0.0, 1.0)
        self._minimap_content_h: int = 0
        self._hunk_seps: list[tk.Canvas] = []
        self._comment_frames: list[tk.Frame] = []
        # Rebuilt every render from the review store + working-tree files.
        self._pending_anchors: dict[str, list['_ResolvedAnchor']] = {}
        self._line_to_anchor: dict[int, '_ResolvedAnchor'] = {}
        # (file, new_line_no, side, line_text) for every rendered diff line —
        # used when opening the editor on a not-yet-commented line.
        self._line_post_image: dict[int, tuple[str, int, str, str]] = {}
        # Avoid re-reading and re-hashing a file for each new comment in a burst.
        self._session_snapshots: dict[str, str] = {}
        self._repo_root = _find_repo_root()
        self._scroll_target: float = 0.0
        self._scroll_animating: bool = False
        self._flist_selected_row: int = -1
        self._flist_row_to_entry: list[FileEntry | None] = []
        self._flist_path_to_row: dict[str, int] = {}
        self._manual_scroll: bool = False
        self._review = ReviewStore()
        self._commits = commits or []
        self._has_staged = has_staged
        self._has_unstaged = has_unstaged
        self._active_comment_frame: tk.Frame | None = None
        self._active_comment_entry: tk.Text | None = None
        self._comment_target: '_CommentEditTarget | None' = None
        self._hover_line: int = -1
        self._hover_range: tuple[str, str] | None = None
        self._hover_btn_line: int = -1
        self._hide_after_id: str | None = None
        self._over_hover_btn: bool = False
        self._btn_leave_after_id: str | None = None
        self._has_focus: bool = True
        cfg = self._load_config()
        self._wrap_var = tk.BooleanVar(value=cfg.get('wrap_lines', True))
        self._tree_var = tk.BooleanVar(value=cfg.get('tree_view', False))
        _wd_default = 2 if cfg.get('word_diff', True) else 0  # migrate old bool config
        self._word_diff_var = tk.IntVar(value=cfg.get('word_diff_mode', _wd_default))
        self._scale = _detect_scale(root)

        self._build_ui()
        self._load()

    # --UI ------------------------------------------------------------

    def _make_read_only(self, widget: tk.Text) -> None:
        # Ctrl+W conflicts: Text class binds it to "delete previous word".
        # Overriding it here is unavoidable; extract to one place so each
        # read-only widget needs only a single call.
        widget.bind('<Key>', lambda e: 'break')
        widget.bind('<Control-c>', lambda e: None)
        widget.bind('<Control-w>', lambda e: self._close_app())
        widget.bind('<Control-q>', lambda e: self._close_app())

    def _make_scrollbar(self, parent: tk.Widget, **kw) -> tk.Scrollbar:
        return tk.Scrollbar(parent,
                            bg=C['selected_bg'],
                            troughcolor=C['bg'],
                            activebackground=C['subdued'],
                            relief='flat', bd=0,
                            width=int(CFG.scrollbar_w * self._scale),
                            **kw)

    def _build_ui(self) -> None:
        menu_font = (CFG.font_family, int(CFG.menu_font_size * self._scale))
        menu_kw = dict(bg=C['topbar_bg'], fg=C['fg'],
                       activebackground=C['selected_bg'], activeforeground=C['fg'],
                       relief='flat', bd=0, font=menu_font)
        menubar = tk.Menu(self.root, **menu_kw)
        file_menu = tk.Menu(menubar, tearoff=0, **menu_kw)
        file_menu.add_command(label='Quit', accelerator='Ctrl+Q',
                              command=self._close_app)
        menubar.add_cascade(label='File', menu=file_menu)
        view_menu = tk.Menu(menubar, tearoff=0, **menu_kw)
        view_menu.add_checkbutton(label='Wrap long lines', variable=self._wrap_var,
                                  command=self._on_wrap_toggle)
        view_menu.add_checkbutton(label='Tree view', variable=self._tree_var,
                                  command=self._on_tree_toggle)
        word_diff_menu = tk.Menu(view_menu, tearoff=0, **menu_kw)
        word_diff_menu.add_radiobutton(label='Off',                      value=0,
                                       variable=self._word_diff_var,
                                       command=self._on_word_diff_toggle)
        word_diff_menu.add_radiobutton(label='On',                       value=1,
                                       variable=self._word_diff_var,
                                       command=self._on_word_diff_toggle)
        word_diff_menu.add_radiobutton(label='On + collapse re-indented', value=2,
                                       variable=self._word_diff_var,
                                       command=self._on_word_diff_toggle)
        view_menu.add_cascade(label='Word diff', menu=word_diff_menu, accelerator='d')
        menubar.add_cascade(label='View', menu=view_menu)
        go_menu = tk.Menu(menubar, tearoff=0, **menu_kw)
        go_menu.add_command(label='Next file',     accelerator='n / Tab',
                            command=lambda: self._jump_to_adjacent_file(1))
        go_menu.add_command(label='Previous file', accelerator='p / Shift+Tab',
                            command=lambda: self._jump_to_adjacent_file(-1))
        menubar.add_cascade(label='Go', menu=go_menu)
        self._review_menu = tk.Menu(menubar, tearoff=0,
                                    postcommand=self._rebuild_review_menu, **menu_kw)
        menubar.add_cascade(label='Review', menu=self._review_menu)
        self.root.configure(bg=C['bg'], menu=menubar)
        self._sash_ratio = CFG.sash_ratio
        self._pending_scroll_frac: 'float | None' = None
        saved_state = _load_window_state()
        if saved_state and isinstance(saved_state.get('geometry'), str):
            self.root.geometry(saved_state['geometry'])
        else:
            sw, sh = _primary_monitor_size()
            w, h = int(sw * CFG.window_scale), int(sh * CFG.window_scale)
            self.root.geometry(f'{w}x{h}')
        if saved_state and isinstance(saved_state.get('sash_ratio'), (int, float)):
            r = float(saved_state['sash_ratio'])
            if 0.05 < r < 0.95:
                self._sash_ratio = r
        if saved_state and isinstance(saved_state.get('scroll_frac'), (int, float)):
            f = float(saved_state['scroll_frac'])
            if 0.0 <= f <= 1.0:
                self._pending_scroll_frac = f
        font = (CFG.font_family, CFG.font_size)

        # top bar
        bar = tk.Frame(self.root, bg=C['topbar_bg'], pady=5)
        bar.pack(fill='x')

        self._lbl_branch = tk.Label(bar, bg=C['topbar_bg'], fg=C['fg'], font=font)
        self._lbl_branch.pack(side='left', padx=10)

        self._lbl_stat = tk.Label(bar, bg=C['topbar_bg'], fg=C['subdued'], font=font)
        self._lbl_stat.pack(side='left')


        # two-panel split
        self._sash = tk.PanedWindow(self.root, orient='horizontal',
                                     bg=C['subdued'], sashwidth=3, sashrelief='flat')
        self._sash.pack(fill='both', expand=True)

        # left: diff (grid so the scrollbar corner square fits neatly)
        lf = tk.Frame(self._sash, bg=C['bg'])
        lf.grid_rowconfigure(2, weight=1)
        lf.grid_columnconfigure(0, weight=1)

        bar_font = (CFG.font_family, int(CFG.menu_font_size * self._scale))
        diff_bar = tk.Frame(lf, bg=C['topbar_bg'])
        diff_bar.grid(row=0, column=0, columnspan=3, sticky='ew')
        menu_kw_bar = dict(bg=C['topbar_bg'], fg=C['fg'],
                           activebackground=C['selected_bg'], activeforeground=C['fg'],
                           relief='flat', bd=0, font=bar_font, tearoff=0)
        self._wd_btn = tk.Menubutton(diff_bar, bg=C['topbar_bg'], fg=C['fg'],
                                      activebackground=C['selected_bg'], activeforeground=C['fg'],
                                      relief='groove', bd=1, highlightthickness=0,
                                      font=bar_font, padx=8, pady=2)
        wd_menu = tk.Menu(self._wd_btn, **menu_kw_bar)
        self._wd_btn['menu'] = wd_menu
        for i, name in enumerate(('plain', 'word', 'word+~')):
            wd_menu.add_command(label=name, command=lambda v=i: self._set_word_diff_mode(v))
        self._wd_btn.pack(side='left')
        self._update_wd_bar()

        self._wrap_btn = tk.Menubutton(diff_bar, bg=C['topbar_bg'], fg=C['fg'],
                                        activebackground=C['selected_bg'], activeforeground=C['fg'],
                                        relief='groove', bd=1, highlightthickness=0,
                                        font=bar_font, padx=8, pady=2)
        wrap_menu = tk.Menu(self._wrap_btn, **menu_kw_bar)
        self._wrap_btn['menu'] = wrap_menu
        for name in ('wrap', 'no wrap'):
            wrap_menu.add_command(label=name,
                                  command=lambda v=(name == 'wrap'): self._set_wrap_mode(v))
        self._wrap_btn.pack(side='left', padx=(4, 0))
        self._update_wrap_bar()

        self._sticky = tk.Label(lf, bg=C['topbar_bg'], fg=C['fg'],
                                 font=font, anchor='w', padx=10, pady=3, text='')
        self._sticky.grid(row=1, column=0, columnspan=3, sticky='ew')

        self._diff = tk.Text(lf, bg=C['bg'], fg=C['fg'],
                              font=font, wrap='char',
                              relief='flat', bd=0, cursor='arrow',
                              selectbackground=C['selected_bg'],
                              selectforeground=C['fg'],
                              inactiveselectbackground=C['selected_bg'],
                              insertwidth=0)
        self._make_read_only(self._diff)
        self._diff.bind('<Configure>', self._on_diff_configure)
        self._diff.bind('<Button-4>',   lambda e: self._on_wheel(-1) or 'break')
        self._diff.bind('<Button-5>',   lambda e: self._on_wheel( 1) or 'break')
        self._diff.bind('<MouseWheel>', lambda e: self._on_wheel(-e.delta // 120) or 'break')
        self._diff.bind('<Up>',    lambda e: self._on_wheel(-1) or 'break')
        self._diff.bind('<Down>',  lambda e: self._on_wheel( 1) or 'break')
        self._diff.bind('<Prior>', lambda e: self._on_page_scroll(-1) or 'break')
        self._diff.bind('<Next>',  lambda e: self._on_page_scroll( 1) or 'break')
        self._diff.bind('<Home>',  lambda e: self._scroll_to(0.0) or 'break')
        self._diff.bind('<End>',   lambda e: self._scroll_to(1.0) or 'break')
        self._diff.bind('n',              lambda e: self._jump_to_adjacent_file( 1) or 'break')
        self._diff.bind('p',              lambda e: self._jump_to_adjacent_file(-1) or 'break')
        self._diff.bind('d',              lambda e: self._toggle_word_diff() or 'break')
        self._diff.bind('t',              lambda e: self._toggle_tree() or 'break')
        self._diff.bind('w',              lambda e: self._toggle_wrap() or 'break')
        self._diff.bind('c',              lambda e: self._copy_loc_and_lines() or 'break')
        self._diff.bind('a',              lambda e: self._add_comment_at_cursor() or 'break')
        self._diff.bind('<Tab>',          lambda e: self._jump_to_adjacent_file( 1) or 'break')
        self._diff.bind('<Shift-Tab>',      lambda e: self._jump_to_adjacent_file(-1) or 'break')
        self._diff.bind('<ISO_Left_Tab>',   lambda e: self._jump_to_adjacent_file(-1) or 'break')
        self._diff.bind('<ButtonRelease-3>', self._show_diff_context_menu)
        self._diff.bind('<Motion>', self._on_diff_hover)
        self._diff.bind('<Leave>',  lambda e: self._schedule_hide())
        self.root.bind('<FocusOut>', self._on_root_focus_out, add='+')
        self.root.bind('<FocusIn>',  self._on_root_focus_in,  add='+')
        self._comment_hover_btn = self._make_hover_button('+comment(a)', C['comment_fg'], self._on_comment_btn_click)
        self._copy_hover_btn    = self._make_hover_button('copy(c)',      C['fg'],          self._on_copy_btn_click)
        self._diff_vs = self._make_scrollbar(lf, orient='vertical', command=self._diff.yview)
        self._diff_vs.bind('<ButtonPress-1>', lambda e: setattr(self, '_manual_scroll', True))
        hs = self._make_scrollbar(lf, orient='horizontal', command=self._diff.xview)
        self._diff.configure(yscrollcommand=self._on_diff_yscroll, xscrollcommand=hs.set)
        self._diff.grid(row=2, column=0, sticky='nsew')

        self._minimap = tk.Canvas(lf, width=int(CFG.minimap_w * self._scale),
                                  bg=C['bg'], highlightthickness=0)
        self._minimap.grid(row=2, column=1, rowspan=2, sticky='ns')
        self._minimap.bind('<Configure>',  lambda e: self._render_minimap())
        self._minimap.bind('<Button-1>',   self._on_minimap_click)
        self._minimap.bind('<B1-Motion>',  self._on_minimap_click)

        self._diff_vs.grid(row=2, column=2, sticky='ns')
        hs.grid(row=3, column=0, sticky='ew')
        _sw = int(CFG.scrollbar_w * self._scale)
        corner = tk.Frame(lf, bg=C['topbar_bg'], width=_sw, height=_sw)
        corner.grid(row=3, column=2)
        self._diff_hs = hs
        self._diff_hs_corner = corner
        # wrap on by default — horizontal scrollbar not needed
        hs.grid_remove()
        corner.grid_remove()

        # right: optional collapsible Comments + Commits panels above the file list
        rf = tk.Frame(self._sash, bg=C['bg'])

        def _make_section_toggle(parent: tk.Frame, command) -> tk.Button:
            return tk.Button(
                parent, text='',
                bg=C['topbar_bg'], fg=C['fg'],
                activebackground=C['topbar_bg'], activeforeground=C['fg'],
                relief='raised', bd=2, highlightthickness=0, cursor='hand2',
                font=bar_font, padx=8, pady=4, anchor='w',
                command=command)

        def _make_list_text(parent: tk.Frame) -> tuple[tk.Frame, tk.Text]:
            pane = tk.Frame(parent, bg=C['bg'])
            txt = tk.Text(pane, bg=C['bg'], fg=C['fg'],
                          font=font, wrap='none', height=1,
                          relief='flat', bd=0, state='disabled', cursor='arrow',
                          selectbackground=C['bg'], selectforeground=C['fg'],
                          inactiveselectbackground=C['bg'])
            sb = self._make_scrollbar(pane, orient='vertical', command=txt.yview)
            txt.configure(yscrollcommand=sb.set)
            sb.pack(side='right', fill='y')
            txt.pack(fill='both', expand=True)
            return pane, txt

        # Comments section — created always; visibility/content updated per render.
        self._comments_expanded = False
        self._comments_header = tk.Frame(rf, bg=C['topbar_bg'])
        self._comments_toggle = _make_section_toggle(self._comments_header, self._toggle_comments_pane)
        self._comments_toggle.pack(fill='x')
        self._comments_pane, self._cmt_list = _make_list_text(rf)

        # Commits section — only when there are commits or staged/unstaged changes
        self._commits_expanded = False
        self._has_commits_section = bool(self._commits or self._has_staged or self._has_unstaged)
        if self._has_commits_section:
            self._commits_header = tk.Frame(rf, bg=C['topbar_bg'])
            n = len(self._commits) + (1 if self._has_staged else 0) + (1 if self._has_unstaged else 0)
            self._commits_toggle = _make_section_toggle(self._commits_header, self._toggle_commits_pane)
            self._commits_toggle.configure(text=f'{CFG.section_collapsed_arrow} Commits ({n})')
            self._commits_toggle.pack(fill='x')
            self._commits_pane, self._clist = _make_list_text(rf)
            self._clist.configure(height=min(n + 1, CFG.list_pane_max_lines))
            self._render_clist()

        flist_bar = tk.Frame(rf, bg=C['topbar_bg'])
        self._flist_btn = tk.Menubutton(flist_bar, bg=C['topbar_bg'], fg=C['fg'],
                                         activebackground=C['selected_bg'], activeforeground=C['fg'],
                                         relief='groove', bd=1, highlightthickness=0,
                                         font=bar_font, padx=8, pady=2)
        flist_menu = tk.Menu(self._flist_btn, **menu_kw_bar)
        self._flist_btn['menu'] = flist_menu
        for name in ('list', 'tree'):
            flist_menu.add_command(label=name,
                                   command=lambda v=(name == 'tree'): self._set_tree_mode(v))
        self._flist_btn.pack(side='left')
        self._update_flist_bar()

        self._files_pane = tk.Frame(rf, bg=C['bg'])
        self._flist = tk.Text(self._files_pane, bg=C['bg'], fg=C['fg'],
                               font=font, wrap='none',
                               relief='flat', bd=0, state='disabled', cursor='arrow',
                               selectbackground=C['bg'], selectforeground=C['fg'],
                               inactiveselectbackground=C['bg'])
        fvs = self._make_scrollbar(self._files_pane, orient='vertical', command=self._flist.yview)
        self._flist.configure(yscrollcommand=fvs.set)
        fvs.pack(side='right', fill='y')
        self._flist.pack(fill='both', expand=True)
        self._flist_bar = flist_bar
        # Pack the persistent rows in final top-to-bottom order. The
        # comments/commits headers and panes get pack()ed in via _update_*
        # / toggle methods using before=self._flist_bar (or _commits_header).
        if self._has_commits_section:
            self._commits_header.pack(fill='x')
        flist_bar.pack(fill='x')
        self._files_pane.pack(fill='both', expand=True)

        self._sash.add(lf, stretch='always')
        self._sash.add(rf, stretch='never')
        self.root.after(50, self._init_sash)

        # diff tags — line highlight is a little more visible than the raw added_bg/removed_bg
        _rem_hi = _blend(C['removed_fg'], CFG.diff_hi_blend)
        _add_hi = _blend(C['added_fg'],   CFG.diff_hi_blend)
        self._diff.tag_configure('added',      foreground=C['added_fg'],   background=_add_hi)
        self._diff.tag_configure('removed',    foreground=C['removed_fg'], background=_rem_hi)
        self._diff.tag_configure('hunk',        foreground=C['hunk_fg'])
        self._diff.tag_configure('fileheader',  foreground=C['fileheader_fg'])
        self._diff.tag_configure('context',     foreground=C['fg'])
        self._diff.tag_configure('subdued',     foreground=C['subdued'])
        self._diff.tag_configure('filehdr',     foreground=C['fileheader_fg'], background=C['topbar_bg'])
        self._diff.tag_configure('fileidx',     foreground=C['fileheader_fg'], background=C['topbar_bg'])
        self._diff.tag_configure('status_A',    foreground=C['status_A'])
        self._diff.tag_configure('status_M',    foreground=C['status_M'])
        self._diff.tag_configure('status_D',    foreground=C['status_D'])
        self._diff.tag_configure('status_R',    foreground=C['status_R'])
        self._diff.tag_configure('hover',       background=C['topbar_bg'])

        # file list tags
        self._flist.tag_configure('status_A',  foreground=C['status_A'])
        self._flist.tag_configure('status_M',  foreground=C['status_M'])
        self._flist.tag_configure('status_D',  foreground=C['status_D'])
        self._flist.tag_configure('status_R',  foreground=C['status_R'])
        self._flist.tag_configure('stats',     foreground=C['subdued'])
        self._flist.tag_configure('dir',       foreground=C['subdued'])
        self._flist.tag_configure('selected',  background=C['selected_bg'])

        # Word diff: unchanged words — colored text, barely-there bg so they recede
        self._diff.tag_configure('removed_word', foreground=_blend(C['removed_fg'], CFG.diff_dim_fg), background=_blend(C['removed_fg'], CFG.diff_dim_blend))
        self._diff.tag_configure('added_word',   foreground=_blend(C['added_fg'],   CFG.diff_dim_fg), background=_blend(C['added_fg'],   CFG.diff_dim_blend))
        self._diff.tag_configure('reindent',     foreground=C['subdued'])
        self._diff.tag_configure('orphan_src',   foreground=C['subdued'], background=C['topbar_bg'])
        _comment_bg = _blend(C['comment_fg'], 0.55)
        self._comment_bg = _comment_bg
        self._diff.tag_configure('comment', foreground=C['bg'], background=_comment_bg,
                                 spacing1=6, spacing3=6)
        self._diff.tag_bind('comment', '<Button-1>', self._on_comment_click)
        self._diff.tag_bind('comment', '<Enter>', lambda e: self._diff.config(cursor='hand2'))
        self._diff.tag_bind('comment', '<Leave>', lambda e: self._diff.config(cursor=''))
        # Word diff: changed words — same "change highlight" bg as the full-line removed/added tags
        self._diff.tag_configure('removed_hi',   foreground=C['removed_fg'], background=_rem_hi)
        self._diff.tag_configure('added_hi',     foreground=C['added_fg'],   background=_add_hi)
        self._diff.tag_raise('hover')
        self._diff.tag_raise('sel')

        self._flist.bind('<Button-1>', self._on_file_click)
        self._flist.bind('<B1-Motion>', lambda e: 'break')
        self._flist.bind('<Double-Button-1>', lambda e: 'break')
        self._flist.bind('<Triple-Button-1>', lambda e: 'break')
        self._flist.bind('<Button-4>',   lambda e: self._flist.yview_scroll(-4, 'units') or 'break')
        self._flist.bind('<Button-5>',   lambda e: self._flist.yview_scroll( 4, 'units') or 'break')
        self._flist.bind('<MouseWheel>', lambda e: self._flist.yview_scroll(-e.delta // 30, 'units') or 'break')
        self._flist.bind('<Up>',         lambda e: self._flist_nav(-1) or 'break')
        self._flist.bind('<Down>',       lambda e: self._flist_nav( 1) or 'break')
        self._flist.bind('<Return>',     lambda e: self._flist_activate() or 'break')
        self._flist.bind('d',            lambda e: self._toggle_word_diff() or 'break')
        self._flist.bind('t',            lambda e: self._toggle_tree() or 'break')
        self._flist.bind('w',            lambda e: self._toggle_wrap() or 'break')
        self._flist.bind('c',            lambda e: self._copy_loc_and_lines() or 'break')
        self._on_wrap_toggle()

    def _update_wrap_bar(self) -> None:
        name = 'wrap' if self._wrap_var.get() else 'no wrap'
        self._wrap_btn.configure(text=f'Wrap (w): {name}')

    def _set_wrap_mode(self, wrap: bool) -> None:
        self._wrap_var.set(wrap)
        self._on_wrap_toggle()

    def _toggle_wrap(self) -> None:
        self._wrap_var.set(not self._wrap_var.get())
        self._on_wrap_toggle()

    def _on_wrap_toggle(self) -> None:
        wrap = self._wrap_var.get()
        if wrap:
            self._diff.configure(wrap='char')
            self._diff_hs.grid_remove()
            self._diff_hs_corner.grid_remove()
        else:
            self._diff.configure(wrap='none')
            self._diff_hs.grid()
            self._diff_hs_corner.grid()
        self._update_wrap_bar()
        self._save_config({'wrap_lines': wrap})

    def _update_flist_bar(self) -> None:
        name = 'tree' if self._tree_var.get() else 'list'
        self._flist_btn.configure(text=f'Files (t): {name}')

    def _set_tree_mode(self, tree: bool) -> None:
        self._tree_var.set(tree)
        self._on_tree_toggle()

    def _toggle_tree(self) -> None:
        self._tree_var.set(not self._tree_var.get())
        self._on_tree_toggle()

    def _on_tree_toggle(self) -> None:
        self._update_flist_bar()
        self._save_config({'tree_view': self._tree_var.get()})
        self._render_flist(self._entries)

    def _update_wd_bar(self) -> None:
        name = ('plain', 'word', 'word+~')[self._word_diff_var.get()]
        self._wd_btn.configure(text=f'Diff (d): {name}')

    def _set_word_diff_mode(self, mode: int) -> None:
        self._word_diff_var.set(mode)
        self._on_word_diff_toggle()

    def _toggle_word_diff(self) -> None:
        self._word_diff_var.set((self._word_diff_var.get() + 1) % 3)
        self._on_word_diff_toggle()

    def _on_word_diff_toggle(self) -> None:
        self._update_wd_bar()
        self._save_config({'word_diff_mode': self._word_diff_var.get()})
        self._rerender_preserving_scroll()

    @staticmethod
    def _load_config() -> dict:
        try:
            return json.loads(_CONFIG_PATH.read_text())
        except Exception:
            return {}

    @staticmethod
    def _save_config(data: dict) -> None:
        try:
            _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            existing = App._load_config()
            existing.update(data)
            _CONFIG_PATH.write_text(json.dumps(existing))
        except Exception:
            pass

    def _init_sash(self) -> None:
        w = self._sash.winfo_width()
        if w > 1:
            self._sash.sash_place(0, int(w * self._sash_ratio), 0)
            self.root.bind('<Configure>', self._on_window_configure)
            self._sash.bind('<ButtonRelease-1>', self._on_sash_release, add='+')
        else:
            self.root.after(50, self._init_sash)

    def _on_window_configure(self, event: tk.Event) -> None:
        if event.widget is self.root:
            self.root.after_idle(self._place_sash)

    def _place_sash(self) -> None:
        w = self._sash.winfo_width()
        if w > 1:
            self._sash.sash_place(0, int(w * self._sash_ratio), 0)

    def _on_sash_release(self, _event: tk.Event) -> None:
        w = self._sash.winfo_width()
        if w <= 1:
            return
        try:
            x = self._sash.sash_coord(0)[0]
        except (tk.TclError, IndexError):
            return
        self._sash_ratio = max(0.05, min(0.95, x / w))

    # --smooth scroll ---------------------------------------------------------

    def _scroll_by(self, frac: float) -> None:
        first, last = self._diff.yview()
        max_pos = 1.0 - (last - first)
        self._manual_scroll = True
        self._scroll_target = max(0.0, min(max_pos, self._scroll_target + frac))
        if not self._scroll_animating:
            self._scroll_animating = True
            self._animate_scroll()

    def _on_wheel(self, ticks: int) -> None:
        total = int(self._diff.index('end').split('.')[0])
        if total < 2:
            return
        self._scroll_by((CFG.scroll_speed * ticks) / total)

    def _scroll_to(self, pos: float) -> None:
        first, last = self._diff.yview()
        max_pos = 1.0 - (last - first)
        self._manual_scroll = True
        self._scroll_target = max_pos if pos >= 1.0 else 0.0
        if not self._scroll_animating:
            self._scroll_animating = True
            self._animate_scroll()

    def _on_page_scroll(self, direction: int) -> None:
        first, last = self._diff.yview()
        self._scroll_by(direction * (last - first) * 2 / 3)

    def _animate_scroll(self) -> None:
        current = self._diff.yview()[0]
        remaining = self._scroll_target - current
        if abs(remaining) < 0.0003:
            self._diff.yview_moveto(self._scroll_target)
            self._scroll_animating = False
            return
        self._diff.yview_moveto(current + remaining * 0.35)
        self.root.after(16, self._animate_scroll)

    # --hunk separators -------------------------------------------------------

    def _make_hover_button(self, text: str, fg: str, command) -> tk.Label:
        # Match the diff text's font so the label's natural height equals the
        # diff line height. Multiplying menu_font_size by self._scale (as menus
        # do) double-applies DPI scaling and overflows the ruler row.
        # tk.Label (rather than tk.Button) avoids the platform theme shadow.
        btn = tk.Label(
            self._diff, text=text,
            bg=C['topbar_bg'], fg=fg,
            bd=0, highlightthickness=0, cursor='hand2',
            padx=4, pady=0,
            font=(CFG.font_family, CFG.font_size),
        )
        btn.bind('<Button-1>', lambda e: command())
        btn.bind('<Enter>', lambda e: self._on_btn_enter())
        btn.bind('<Leave>', lambda e: self._on_btn_leave())
        return btn

    def _diff_row_pad(self) -> int:
        cached = getattr(self, '_diff_row_pad_cached', None)
        if cached is not None:
            return cached
        try:
            padx = int(str(self._diff.cget('padx')))
            hl   = int(str(self._diff.cget('highlightthickness')))
        except (tk.TclError, ValueError):
            padx, hl = 1, 1
        self._diff_row_pad_cached = 2 * (padx + hl)
        return self._diff_row_pad_cached

    def _on_diff_configure(self, event: tk.Event) -> None:
        if event.width > 1:
            for sep in self._hunk_seps:
                sep.configure(width=event.width)
            row_w = max(event.width - self._diff_row_pad(), 1)
            for f in self._comment_frames:
                if f.winfo_exists():
                    f.configure(width=row_w)
            if self._active_comment_frame and self._active_comment_frame.winfo_exists():
                self._active_comment_frame.configure(width=row_w)

    def _update_hunk_sep_widths(self) -> None:
        w = self._diff.winfo_width()
        if w > 1:
            for sep in self._hunk_seps:
                sep.configure(width=w)
            row_w = max(w - self._diff_row_pad(), 1)
            for f in self._comment_frames:
                if f.winfo_exists():
                    f.configure(width=row_w)
            if self._active_comment_frame and self._active_comment_frame.winfo_exists():
                self._active_comment_frame.configure(width=row_w)
        else:
            self.root.after(50, self._update_hunk_sep_widths)

    # --minimap ---------------------------------------------------------------

    def _render_minimap(self) -> None:
        c = self._minimap
        cw, ch = c.winfo_width(), c.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        n = len(self._minimap_lines)
        if n == 0:
            c.delete('all')
            self._minimap_content_h = 0
            return

        bg = C['bg']
        blank = '{' + ' '.join([bg] * cw) + '}'

        # Only stretch when the diff is too tall to fit at natural scale.
        natural_h = n * _MM_LINE_H
        img_h = min(natural_h, ch)
        self._minimap_content_h = img_h

        # Cache rendered rows per source-line index to avoid redundant work
        # when multiple canvas rows map to the same source line.
        line_cache: dict[int, str] = {}

        def _row(i: int) -> str:
            if i in line_cache:
                return line_cache[i]
            kind, text = self._minimap_lines[i]
            color = _MINIMAP_COLORS.get(kind)
            if color is None:
                line_cache[i] = blank
                return blank
            if kind == 'comment':
                result = '{' + ' '.join([color] * cw) + '}'
            else:
                pixels = [color if x < len(text) and text[x] not in (' ', '\t') else bg
                          for x in range(cw)]
                result = '{' + ' '.join(pixels) + '}'
            line_cache[i] = result
            return result

        rows = [_row(min(int(y * n / img_h), n - 1)) for y in range(img_h)]
        img = tk.PhotoImage(width=cw, height=img_h)
        img.put(' '.join(rows))

        c.delete('all')
        c.create_image(0, 0, anchor='nw', image=img)
        c._mm_img = img  # keep reference; PhotoImage is GC'd without it
        self._update_minimap_viewport()

    def _update_minimap_viewport(self) -> None:
        c = self._minimap
        if c.winfo_height() <= 1 or self._minimap_content_h <= 0:
            return
        c.delete('viewport')
        first, last = self._scroll_pos
        h = self._minimap_content_h
        y0, y1 = int(first * h), int(last * h)
        c.create_rectangle(0, y0, c.winfo_width(), y1,
                           fill=C['fg'], stipple='gray12',
                           outline=C['fg'], width=1, tags='viewport')

    def _on_minimap_click(self, event: tk.Event) -> None:
        h = self._minimap_content_h
        if h <= 0:
            return
        self._manual_scroll = True
        first, last = self._scroll_pos
        span = last - first
        frac = max(0.0, min(1.0 - span, event.y / h - span / 2))
        self._diff.yview_moveto(frac)

    @staticmethod
    def _file_label(df: DiffFile) -> tuple[str, str]:
        """Return (name_line, index_line) for both the sticky label and the diff header."""
        name = f'{df.old_path} -> {df.path}' if (df.status == 'R' and df.old_path) else df.path
        return name, df.index

    def _on_diff_yscroll(self, first: str, last: str) -> None:
        self._diff_vs.set(first, last)
        self._scroll_pos = (float(first), float(last))
        if not self._scroll_animating:
            self._scroll_target = float(first)
        self._update_sticky_header()
        self._update_minimap_viewport()
        if self._hover_line >= 0 or self._hover_btn_line >= 0:
            self._do_hide_hover(force=True)

    def _update_sticky_header(self) -> None:
        if not self._pos_order:
            self._sticky.configure(text='')
            return
        top = int(self._diff.index('@0,0').split('.')[0])
        path = self._pos_order[0][1]
        for line_no, p in self._pos_order:
            if line_no <= top:
                path = p
            else:
                break
        df = next((d for d in self._diff_files if d.path == path), None)
        if df is None:
            return
        name, idx = self._file_label(df)
        self._sticky.configure(text=f' {name}\n {idx}' if idx else f' {name}\n',
                               fg=C['fileheader_fg'], justify='left')
        if not self._manual_scroll:
            return
        row = self._flist_path_to_row.get(path, -1)
        if row > 0 and row != self._flist_selected_row:
            self._highlight_row(row)

    # --data ------------------------------------------------------------

    def _load(self) -> None:
        diff_files = parse_diff(self.diff_text)
        entries = entries_from_diff(diff_files)
        branch = try_current_branch()

        n = len(diff_files)
        add = sum(e.additions for e in entries)
        rem = sum(e.deletions for e in entries)
        stat = f'{n} file{"s" if n != 1 else ""} changed, +{add} -{rem}' if n else ''

        self._entries = entries
        self._render(branch, stat, diff_files, entries)
        self._diff.focus_set()

    def _render(self, branch: str, stat: str,
                diff_files: list[DiffFile], entries: list[FileEntry]) -> None:
        self._lbl_branch.configure(text=f'branch:  {branch}' if branch else '')
        self._lbl_stat.configure(text=f'  {stat}' if stat else '')
        self._diff_files = diff_files
        self._render_diff_panel()
        self._render_flist(entries)

    def _render_diff_panel(self) -> None:
        self._cancel_hide_schedule()
        self._comment_hover_btn.place_forget()
        self._copy_hover_btn.place_forget()
        self._hover_line = -1
        self._hover_btn_line = -1
        if self._active_comment_frame:
            self._active_comment_frame.destroy()
            self._active_comment_frame = None
            self._active_comment_entry = None
            self._comment_target = None
        for sep in self._hunk_seps:
            sep.destroy()
        self._hunk_seps.clear()
        self._comment_frames.clear()
        self._line_to_anchor = {}
        self._line_post_image = {}
        self._pending_anchors = self._resolve_review_anchors()
        self._diff.delete('1.0', 'end')
        self._positions.clear()
        self._minimap_lines = []

        if self._diff_files:
            for i, df in enumerate(self._diff_files):
                if i > 0:
                    self._diff.insert('end', '\n', 'context')
                    self._minimap_lines.append(('context', ''))
                name, idx = self._file_label(df)
                self._diff.insert('end', f' {name}\n', 'filehdr')
                self._positions[df.path] = self._diff.index('end-2c linestart')
                self._minimap_lines.append(('filehdr', f' {name}'))
                if idx:
                    self._diff.insert('end', f' {idx}\n', 'fileidx')
                    self._minimap_lines.append(('fileidx', f' {idx}'))
                self._render_file_diff(df)
        else:
            self._diff.insert('end', 'Empty diff.\n', 'subdued')

        self._pos_order = sorted(
            (int(pos.split('.')[0]), path)
            for path, pos in self._positions.items()
        )
        self.root.after_idle(self._update_sticky_header)
        self.root.after_idle(self._render_minimap)
        self.root.after_idle(self._update_hunk_sep_widths)
        self.root.after_idle(self._update_comments_section)
        if self._pending_scroll_frac is not None:
            frac = self._pending_scroll_frac
            self._pending_scroll_frac = None
            self.root.after_idle(lambda f=frac: self._diff.yview_moveto(f))

    def _insert_word_diff(self, old_dl: DiffLine, new_dl: DiffLine, file_path: str) -> None:
        old_text = old_dl.text[1:]
        new_text = new_dl.text[1:]
        tok_old = re.findall(r'\w+|[^\w\s]|\s+', old_text) or ['']
        tok_new = re.findall(r'\w+|[^\w\s]|\s+', new_text) or ['']
        nws_old = [t for t in tok_old if not t.isspace()]
        nws_new = [t for t in tok_new if not t.isspace()]
        if difflib.SequenceMatcher(None, nws_old, nws_new, autojunk=False).ratio() < CFG.word_diff_min_ratio:
            self._diff.insert('end', f'-{old_text}\n', 'removed')
            self._minimap_lines.append(('removed', '-' + old_text))
            self._insert_comment_annotation(file_path, old_dl, old_dl.text)
            self._diff.insert('end', f'+{new_text}\n', 'added')
            self._minimap_lines.append(('added', '+' + new_text))
            self._insert_comment_annotation(file_path, new_dl, new_dl.text)
            return
        if nws_old == nws_new and self._word_diff_var.get() == 2:
            self._diff.insert('end', f'~{new_text}\n', 'reindent')
            self._minimap_lines.append(('reindent', new_text))
            # The single rendered line stands in for both the - and + sides;
            # offer both as anchor candidates.
            self._insert_comment_annotation(file_path, old_dl, old_dl.text)
            self._insert_comment_annotation(file_path, new_dl, new_dl.text)
            return
        opcodes = difflib.SequenceMatcher(None, tok_old, tok_new, autojunk=False).get_opcodes()

        self._diff.insert('end', '-', 'removed')
        for op, i1, i2, j1, j2 in opcodes:
            text = ''.join(tok_old[i1:i2])
            tag = 'removed_word' if (op == 'equal' or text.isspace()) else 'removed_hi'
            self._diff.insert('end', text, tag)
        self._diff.insert('end', '\n')
        self._minimap_lines.append(('removed', '-' + old_text))
        self._insert_comment_annotation(file_path, old_dl, old_dl.text)

        self._diff.insert('end', '+', 'added')
        for op, i1, i2, j1, j2 in opcodes:
            text = ''.join(tok_new[j1:j2])
            tag = 'added_word' if (op == 'equal' or text.isspace()) else 'added_hi'
            self._diff.insert('end', text, tag)
        self._diff.insert('end', '\n')
        self._minimap_lines.append(('added', '+' + new_text))
        self._insert_comment_annotation(file_path, new_dl, new_dl.text)

    def _render_file_diff(self, df: DiffFile) -> None:
        word_diff = self._word_diff_var.get()
        pending_rem: list[DiffLine] = []
        pending_add: list[DiffLine] = []

        def flush():
            if word_diff and pending_rem and pending_add:
                actions = _pair_lines_for_word_diff(
                    [d.text[1:] for d in pending_rem],
                    [d.text[1:] for d in pending_add])
                rem_iter = iter(pending_rem)
                add_iter = iter(pending_add)
                for action in actions:
                    if action[0] == 'pair':
                        old_dl = next(rem_iter)
                        new_dl = next(add_iter)
                        self._insert_word_diff(old_dl, new_dl, df.path)
                    elif action[0] == 'rem':
                        dl = next(rem_iter)
                        self._diff.insert('end', dl.text + '\n', 'removed')
                        self._minimap_lines.append(('removed', dl.text))
                        self._insert_comment_annotation(df.path, dl, dl.text)
                    else:
                        dl = next(add_iter)
                        self._diff.insert('end', dl.text + '\n', 'added')
                        self._minimap_lines.append(('added', dl.text))
                        self._insert_comment_annotation(df.path, dl, dl.text)
            else:
                for dl in pending_rem:
                    self._diff.insert('end', dl.text + '\n', 'removed')
                    self._minimap_lines.append(('removed', dl.text))
                    self._insert_comment_annotation(df.path, dl, dl.text)
                for dl in pending_add:
                    self._diff.insert('end', dl.text + '\n', 'added')
                    self._minimap_lines.append(('added', dl.text))
                    self._insert_comment_annotation(df.path, dl, dl.text)
            pending_rem.clear()
            pending_add.clear()

        for dl in df.lines:
            if dl.kind == 'fileheader':
                continue
            if dl.kind == 'hunk':
                flush()
                sep = tk.Canvas(self._diff, height=1, bg=C['subdued'],
                                highlightthickness=0, bd=0, width=1)
                self._diff.window_create('end', window=sep)
                self._diff.insert('end', '\n')
                self._hunk_seps.append(sep)
                self._diff.insert('end', dl.text + '\n', dl.kind)
                self._minimap_lines.append((dl.kind, dl.text))
            elif dl.kind == 'removed':
                if pending_add:
                    flush()
                pending_rem.append(dl)
            elif dl.kind == 'added':
                pending_add.append(dl)
            else:
                flush()
                self._diff.insert('end', dl.text + '\n', dl.kind)
                self._minimap_lines.append((dl.kind, dl.text))
                self._insert_comment_annotation(df.path, dl, dl.text)
        flush()
        self._insert_orphan_comments_for_file(df.path)

    def _render_flist(self, entries: list[FileEntry]) -> None:
        self._flist_selected_row = -1
        self._flist_row_to_entry = []
        self._flist_path_to_row = {}
        self._flist.configure(state='normal')
        self._flist.delete('1.0', 'end')

        if self._tree_var.get():
            rows = _build_tree_rows(entries)
            for label, depth, entry in rows:
                indent = '  ' * depth
                if entry is None:
                    self._flist.insert('end', f'{indent}{label}\n', 'dir')
                    self._flist_row_to_entry.append(None)
                else:
                    stats: list[str] = []
                    if entry.additions:
                        stats.append(f'+{entry.additions}')
                    if entry.deletions:
                        stats.append(f'-{entry.deletions}')
                    self._flist.insert('end', f'{indent}', 'dir')
                    self._flist.insert('end', f'{entry.status} ', f'status_{entry.status}')
                    self._flist.insert('end', label)
                    if stats:
                        self._flist.insert('end', f'  {" ".join(stats)}', 'stats')
                    self._flist.insert('end', '\n')
                    display_row = len(self._flist_row_to_entry) + 1
                    self._flist_path_to_row[entry.path] = display_row
                    self._flist_row_to_entry.append(entry)
        else:
            prev_path = ''
            for e in entries:
                parts: list[str] = []
                if e.additions:
                    parts.append(f'+{e.additions}')
                if e.deletions:
                    parts.append(f'-{e.deletions}')
                self._flist.insert('end', f' {e.status} ', f'status_{e.status}')
                prefix = _common_dir_prefix(prev_path, e.path)
                self._flist.insert('end', ' ')
                if prefix:
                    self._flist.insert('end', prefix, 'dir')
                    self._flist.insert('end', e.path[len(prefix):])
                else:
                    self._flist.insert('end', e.path)
                if parts:
                    self._flist.insert('end', f'  {" ".join(parts)}', 'stats')
                self._flist.insert('end', '\n')
                display_row = len(self._flist_row_to_entry) + 1
                self._flist_path_to_row[e.path] = display_row
                self._flist_row_to_entry.append(e)
                prev_path = e.path

        self._flist.configure(state='disabled')
        if entries:
            first_file_row = next(
                (i + 1 for i, e in enumerate(self._flist_row_to_entry) if e is not None), -1
            )
            if first_file_row > 0:
                self._highlight_row(first_file_row)

    # --interaction ----------------------------------------------------------

    def _flist_nav(self, offset: int) -> None:
        self._jump_to_adjacent_file(offset)
        self._flist.focus_set()

    def _flist_activate(self) -> None:
        if self._flist_selected_row > 0:
            entry = self._flist_row_to_entry[self._flist_selected_row - 1]
            if entry is not None:
                self._jump_to(entry.path)
                self._diff.focus_set()

    def _on_file_click(self, event: tk.Event) -> None:
        idx = self._flist.index(f'@{event.x},{event.y}')
        row_0 = int(idx.split('.')[0]) - 1
        if 0 <= row_0 < len(self._flist_row_to_entry):
            entry = self._flist_row_to_entry[row_0]
            if entry is not None:
                self._highlight_row(row_0 + 1)
                self._jump_to(entry.path)

    def _highlight_row(self, row: int) -> None:
        self._flist_selected_row = row
        self._flist.tag_remove('selected', '1.0', 'end')
        self._flist.tag_add('selected', f'{row}.0', f'{row}.end+1c')
        self._flist.see(f'{row}.0')

    def _jump_to_adjacent_file(self, offset: int) -> None:
        if not self._entries:
            return
        cur_entry: FileEntry | None = None
        if self._flist_selected_row > 0:
            cur_entry = self._flist_row_to_entry[self._flist_selected_row - 1]
        elif self._pos_order:
            top = int(self._diff.index('@0,0').split('.')[0])
            path = self._pos_order[0][1]
            for line_no, p in self._pos_order:
                if line_no <= top:
                    path = p
                else:
                    break
            cur_entry = next((e for e in self._entries if e.path == path), None)
        if cur_entry is None:
            return
        paths = [e.path for e in self._entries]
        try:
            idx = paths.index(cur_entry.path)
        except ValueError:
            return
        target = (idx + offset) % len(self._entries)
        target_entry = self._entries[target]
        display_row = self._flist_path_to_row.get(target_entry.path, -1)
        if display_row > 0:
            self._highlight_row(display_row)
        self._jump_to(target_entry.path)
        self._diff.focus_set()

    def _source_location(self, text_line: int) -> tuple[str, int | None]:
        """Return (file_path, new-file line number) for a diff text widget line."""
        if not self._pos_order:
            return '', None

        path = self._pos_order[0][1]
        for ln, p in self._pos_order:
            if ln <= text_line:
                path = p
            else:
                break

        # Scan backwards for the nearest @@ hunk header.
        hunk_line = None
        new_start = None
        for ln in range(text_line, 0, -1):
            content = self._diff.get(f'{ln}.0', f'{ln}.end')
            if content.startswith('@@ '):
                m = re.search(r'\+(\d+)', content)
                if m:
                    new_start = int(m.group(1))
                    hunk_line = ln
                break

        if hunk_line is None or new_start is None:
            return path, None

        # Walk from the hunk header to the clicked line tracking new-file line number.
        # Removed lines (-) don't exist in the new file, so only context and added lines advance.
        new_line = new_start - 1
        for ln in range(hunk_line + 1, text_line + 1):
            if not self._diff.get(f'{ln}.0', f'{ln}.1').startswith('-'):
                new_line += 1

        return path, new_line

    def _widget_under_pointer(self) -> tk.Widget | None:
        try:
            return self.root.winfo_containing(*self.root.winfo_pointerxy())
        except (tk.TclError, KeyError):
            return None

    def _line_under_pointer(self) -> int | None:
        try:
            x_root, y_root = self.root.winfo_pointerxy()
        except tk.TclError:
            return None
        x = x_root - self._diff.winfo_rootx()
        y = y_root - self._diff.winfo_rooty()
        if x < 0 or y < 0 or x >= self._diff.winfo_width() or y >= self._diff.winfo_height():
            return None
        return int(self._diff.index(f'@{x},{y}').split('.')[0])

    @staticmethod
    def _format_comment_block(comment: str, moved: bool = False) -> str:
        prefix       = '~ >> ' if moved else '  >> '
        continuation = '~    ' if moved else '     '
        cmt_lines = comment.splitlines() or ['']
        return '\n'.join([prefix + cmt_lines[0]]
                         + [continuation + l for l in cmt_lines[1:]])

    def _loc_for_line(self, line_no: int) -> str | None:
        path, ln = self._source_location(line_no)
        if not path:
            return None
        return f'{path}:{ln}' if ln is not None else path

    def _comment_for_line(self, line_no: int) -> '_ResolvedAnchor | None':
        if 'comment' not in self._diff.tag_names(f'{line_no}.0'):
            return None
        src_line = line_no - 1
        if src_line < 1:
            return None
        return self._line_to_anchor.get(src_line)

    def _copy_loc_and_lines(self, anchor_line: int | None = None) -> None:
        try:
            sel_first = self._diff.index('sel.first')
            sel_last  = self._diff.index('sel.last')
        except tk.TclError:
            sel_first = sel_last = None

        if sel_first is not None:
            lines_start = int(sel_first.split('.')[0])
            lines_end   = int(sel_last.split('.')[0])
            if sel_last.split('.')[1] == '0' and lines_end > lines_start:
                lines_end -= 1
        else:
            line = (anchor_line
                    or self._line_under_pointer()
                    or int(self._diff.index('insert').split('.')[0]))
            # If cursor is on a comment annotation, anchor to source line above
            # and include the annotation in the copy.
            if 'comment' in self._diff.tag_names(f'{line}.0') and line > 1:
                lines_start = line - 1
                lines_end   = line
            else:
                lines_start = lines_end = line

        loc = self._loc_for_line(lines_start)
        if loc is None:
            return
        parts = []
        for ln in range(lines_start, lines_end + 1):
            content = self._diff.get(f'{ln}.0', f'{ln}.end')
            if content:
                parts.append(content)
                continue
            cmt = self._comment_for_line(ln)
            if cmt is not None:
                parts.append(self._format_comment_block(cmt.comment, cmt.moved))
        text = '\n'.join(parts)
        self.root.clipboard_clear()
        self.root.clipboard_append(f'{loc}\n{text}\n')

    def _add_comment_at_cursor(self) -> None:
        line_no = (self._line_under_pointer()
                   or (self._hover_line if self._hover_line >= 0 else None)
                   or int(self._diff.index('insert').split('.')[0]))
        self._open_comment_editor(line_no)

    def _show_diff_context_menu(self, event: tk.Event) -> None:
        text_line = int(self._diff.index(f'@{event.x},{event.y}').split('.')[0])
        path, line_no = self._source_location(text_line)
        if not path:
            return

        loc = f'{path}:{line_no}' if line_no is not None else path
        menu_kw = dict(bg=C['topbar_bg'], fg=C['fg'],
                       activebackground=C['selected_bg'], activeforeground=C['fg'],
                       relief='flat', bd=0, tearoff=0,
                       font=(CFG.font_family, int(CFG.menu_font_size * self._scale)))
        menu = tk.Menu(self.root, **menu_kw)
        menu.add_command(label=f'Copy "{loc}"',
                         command=lambda: (self.root.clipboard_clear(),
                                          self.root.clipboard_append(loc)))

        try:
            sel_first = self._diff.index('sel.first')
            sel_last  = self._diff.index('sel.last')
        except tk.TclError:
            sel_first = sel_last = None

        if sel_first is not None:
            lines_start = int(sel_first.split('.')[0])
            lines_end   = int(sel_last.split('.')[0])
            if sel_last.split('.')[1] == '0' and lines_end > lines_start:
                lines_end -= 1
            lines_path, lines_line_no = self._source_location(lines_start)
            lines_loc = f'{lines_path}:{lines_line_no}' if lines_line_no is not None else lines_path
        else:
            lines_start = lines_end = text_line
            lines_loc = loc

        n_lines = lines_end - lines_start + 1
        lines_text = self._diff.get(f'{lines_start}.0', f'{lines_end}.end')
        menu.add_command(
            label=f'Copy "{lines_loc}" + {n_lines} {"line" if n_lines == 1 else "lines"}',
            accelerator='c',
            command=lambda: self._copy_loc_and_lines(text_line))

        menu.tk_popup(event.x_root, event.y_root)

    @staticmethod
    def _side_for_kind(kind: str) -> str:
        if kind == 'added':   return '+'
        if kind == 'removed': return '-'
        return ' '

    def _read_current_file(self, file_path: str) -> 'str | None':
        if not self._repo_root:
            return None
        return _read_text_safe(self._repo_root / file_path)

    def _resolve_review_anchors(self) -> dict[str, list['_ResolvedAnchor']]:
        """Map every stored comment through its snapshot to a target line in
        the current working tree (via difflib). Unmatched anchors fall
        through to orphan rendering at the end of each file's hunks."""
        result: dict[str, list[_ResolvedAnchor]] = {}
        current_cache: dict[str, str | None] = {}
        map_cache: dict[tuple[str, str], dict[int, int]] = {}
        for file, entry in self._review.all_entries():
            snap_sha  = str(entry.get('snapshot') or '')
            line_no   = int(entry.get('line_no') or 0)
            side      = str(entry.get('side') or ' ')
            line_text = str(entry.get('line_text') or '')
            comment   = str(entry.get('comment') or '')
            if not (snap_sha and line_no and comment):
                continue
            if file not in current_cache:
                current_cache[file] = self._read_current_file(file)
            current = current_cache[file]
            snap    = self._review.read_snapshot(snap_sha)
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
                comment=comment,
            ))
        return result

    def _consume_anchor(self, file_path: str, new_line_no: int, side: str,
                        rendered_text: str) -> '_ResolvedAnchor | None':
        anchors = self._pending_anchors.get(file_path)
        if not anchors:
            return None
        exact: '_ResolvedAnchor | None' = None
        loose: '_ResolvedAnchor | None' = None
        for a in anchors:
            if a.matched or a.target_line_no != new_line_no or a.side != side:
                continue
            if a.line_text == rendered_text:
                exact = a
                break
            loose = loose or a
        a = exact or loose
        if a is None:
            return None
        a.matched = True
        a.moved   = (exact is None) or (a.snap_line_no != a.target_line_no)
        return a

    def _insert_comment_annotation(self, file_path: str, dl: DiffLine,
                                   line_text: str) -> None:
        # end-2c steps past Tk's implicit trailing \n and the \n we just
        # inserted with the source line, landing on the source line itself.
        src_line_no = int(self._diff.index('end-2c').split('.')[0])
        if dl.new_line_no is not None:
            self._line_post_image[src_line_no] = (
                file_path, dl.new_line_no, self._side_for_kind(dl.kind), line_text,
            )
        if dl.new_line_no is None:
            return
        anchor = self._consume_anchor(
            file_path, dl.new_line_no, self._side_for_kind(dl.kind), line_text,
        )
        if anchor is None:
            return
        anchor.src_line = src_line_no
        self._line_to_anchor[src_line_no] = anchor
        self._render_comment_frame(src_line_no, anchor)

    def _render_comment_frame(self, src_line_no: int, anchor: '_ResolvedAnchor') -> None:
        cmt_display = self._format_comment_block(anchor.comment, anchor.moved)
        frame = tk.Frame(self._diff, bg=self._comment_bg)
        label = tk.Label(
            frame, text=cmt_display,
            bg=self._comment_bg, fg=C['bg'],
            anchor='w', justify='left', cursor='hand2',
            font=(CFG.font_family, CFG.font_size),
        )
        btn = tk.Button(
            frame, text='remove',
            bg=self._comment_bg, fg=C['removed_fg'],
            activebackground=self._comment_bg, activeforeground=C['removed_fg'],
            relief='flat', bd=0, highlightthickness=0, cursor='hand2',
            font=(CFG.font_family, int(CFG.menu_font_size * self._scale)),
            command=lambda a=anchor: self._delete_comment(a),
        )
        copy_btn = tk.Button(
            frame, text='copy(c)',
            bg=self._comment_bg, fg=C['fg'],
            activebackground=self._comment_bg, activeforeground=C['fg'],
            relief='flat', bd=0, highlightthickness=0, cursor='hand2',
            font=(CFG.font_family, int(CFG.menu_font_size * self._scale)),
            command=lambda sl=src_line_no: self._copy_loc_and_lines(sl + 1),
        )
        btn.pack(side='right', padx=4)
        copy_btn.pack(side='right', padx=4)
        label.pack(side='left', fill='x', expand=True)
        frame.update_idletasks()
        h = max(label.winfo_reqheight(), btn.winfo_reqheight(), copy_btn.winfo_reqheight())
        w = max(self._diff.winfo_width() - self._diff_row_pad(), 1)
        frame.configure(width=w, height=h)
        frame.pack_propagate(False)
        def _on_click(e: tk.Event) -> str:
            self._open_comment_editor(src_line_no)
            return 'break'
        label.bind('<Button-1>', _on_click)
        frame.bind('<Button-1>', _on_click)
        frame.bind('<Enter>', lambda e: self._do_hide_hover())
        label.bind('<Enter>', lambda e: self._do_hide_hover())
        btn.bind('<Enter>',   lambda e: self._do_hide_hover())
        copy_btn.bind('<Enter>', lambda e: self._do_hide_hover())
        self._diff.window_create('end', window=frame)
        self._diff.insert('end', '\n')
        cmt_line_no = src_line_no + 1
        self._diff.tag_add('comment', f'{cmt_line_no}.0', f'{cmt_line_no}.end')
        self._comment_frames.append(frame)
        for line in cmt_display.splitlines() or ['']:
            self._minimap_lines.append(('comment', line))

    def _insert_orphan_comments_for_file(self, file_path: str) -> None:
        anchors = self._pending_anchors.get(file_path) or []
        for a in anchors:
            if a.matched:
                continue
            self._diff.insert('end', a.line_text + '\n', 'orphan_src')
            self._minimap_lines.append(('orphan', a.line_text))
            src_line_no = int(self._diff.index('end-2c').split('.')[0])
            a.matched = True
            a.moved = True
            a.src_line = src_line_no
            self._line_to_anchor[src_line_no] = a
            self._line_post_image[src_line_no] = (
                file_path, a.snap_line_no, a.side, a.line_text,
            )
            self._render_comment_frame(src_line_no, a)

    def _delete_comment(self, anchor: '_ResolvedAnchor') -> None:
        self._review.delete(anchor.file, anchor.snapshot,
                            anchor.snap_line_no, anchor.side)
        self._rerender_preserving_scroll()

    def _scroll_diff_to_line(self, line_no: int) -> None:
        self._diff.yview(f'{line_no}.0')
        self._scroll_target = self._diff.yview()[0]

    def _rerender_preserving_scroll(self) -> None:
        top_line = int(self._diff.index('@0,0').split('.')[0])
        self._render_diff_panel()
        self._scroll_diff_to_line(top_line)

    def _cancel_hide_schedule(self) -> None:
        if self._hide_after_id:
            self.root.after_cancel(self._hide_after_id)
            self._hide_after_id = None

    def _schedule_hide(self) -> None:
        self._cancel_hide_schedule()
        self._hide_after_id = self.root.after(CFG.hover_hide_delay_ms, self._do_hide_hover)

    def _on_btn_enter(self) -> None:
        if self._btn_leave_after_id:
            self.root.after_cancel(self._btn_leave_after_id)
            self._btn_leave_after_id = None
        self._over_hover_btn = True
        self._cancel_hide_schedule()

    def _on_btn_leave(self) -> None:
        if self._btn_leave_after_id:
            self.root.after_cancel(self._btn_leave_after_id)
        self._btn_leave_after_id = self.root.after(CFG.hover_btn_leave_delay_ms, self._finalize_btn_leave)

    def _finalize_btn_leave(self) -> None:
        self._btn_leave_after_id = None
        if self._widget_under_pointer() in (self._comment_hover_btn, self._copy_hover_btn):
            return
        self._over_hover_btn = False
        self._schedule_hide()

    def _on_root_focus_out(self, event: tk.Event) -> None:
        # FocusOut fires for child widgets too; only act when the toplevel
        # itself loses focus (another app/window taking it).
        if event.widget is self.root:
            self._has_focus = False
            self._do_hide_hover(force=True)

    def _on_root_focus_in(self, event: tk.Event) -> None:
        if event.widget is self.root:
            self._has_focus = True

    def _do_hide_hover(self, force: bool = False) -> None:
        self._hide_after_id = None
        if not force and self._widget_under_pointer() in (self._comment_hover_btn, self._copy_hover_btn):
            return
        if self._hover_range is not None:
            start, end = self._hover_range
            self._diff.tag_remove('hover', start, end)
            self._hover_range = None
        self._hover_line = -1
        self._comment_hover_btn.place_forget()
        self._copy_hover_btn.place_forget()
        self._hover_btn_line = -1
        self._over_hover_btn = False

    def _on_diff_hover(self, event: tk.Event) -> None:
        if not self._has_focus:
            return
        if self._active_comment_frame or self._over_hover_btn:
            return
        self._cancel_hide_schedule()
        idx = self._diff.index(f'@{event.x},{event.y}')
        line_no = int(idx.split('.')[0])
        if line_no == self._hover_line:
            return
        tags = set(self._diff.tag_names(f'{line_no}.0'))
        if not tags & {'added', 'removed', 'context'}:
            self._do_hide_hover()
            return
        if self._hover_range is not None:
            start, end = self._hover_range
            self._diff.tag_remove('hover', start, end)
        # Highlight a single display row under the cursor. For unwrapped lines
        # we extend past the newline so Tk fills the tag bg to the row's right
        # edge; for wrapped lines we stop at display lineend (no newline to
        # latch onto, and we don't want to highlight the other wrap rows).
        disp_start = self._diff.index(f'{idx} display linestart')
        disp_end   = self._diff.index(f'{idx} display lineend')
        line_end   = self._diff.index(f'{disp_start} lineend')
        if self._diff.compare(disp_end, '>=', line_end):
            tag_end = self._diff.index(f'{line_end}+1c')
        else:
            tag_end = disp_end
        self._hover_range = (disp_start, tag_end)
        self._hover_line  = line_no
        self._diff.tag_add('hover', disp_start, tag_end)
        info = self._diff.dlineinfo(disp_start)
        if info:
            _, y, _, h, _ = info
            # Inset the button so it can never exceed the ruler vertically,
            # regardless of font metrics / theme quirks. Centered in the row.
            inset  = 2
            btn_h  = max(1, h - 2 * inset)
            btn_y  = y + inset
            comment_w = self._comment_hover_btn.winfo_reqwidth()
            copy_w    = self._copy_hover_btn.winfo_reqwidth()
            comment_x = self._diff.winfo_width() - comment_w - 4
            copy_x    = comment_x - copy_w - 6
            if copy_x > 0:
                self._comment_hover_btn.place(x=comment_x, y=btn_y, height=btn_h)
                self._copy_hover_btn.place(x=copy_x, y=btn_y, height=btn_h)
                self._hover_btn_line = line_no
            else:
                self._comment_hover_btn.place_forget()
                self._copy_hover_btn.place_forget()
                self._hover_btn_line = -1
        else:
            self._comment_hover_btn.place_forget()
            self._copy_hover_btn.place_forget()
            self._hover_btn_line = -1

    def _on_comment_btn_click(self) -> None:
        line_no = self._hover_btn_line
        self._do_hide_hover()
        if line_no > 0:
            self._open_comment_editor(line_no)

    def _on_copy_btn_click(self) -> None:
        line_no = self._hover_btn_line
        self._do_hide_hover()
        if line_no > 0:
            self._copy_loc_and_lines(line_no)

    def _on_comment_click(self, event: tk.Event) -> str | None:
        if self._active_comment_frame:
            return None
        line_no = int(self._diff.index(f'@{event.x},{event.y}').split('.')[0])
        self._do_hide_hover()
        self._open_comment_editor(line_no)
        return 'break'

    def _open_comment_editor(self, line_no: int) -> None:
        if self._active_comment_frame:
            self._cancel_comment_edit()
            return
        if 'comment' in self._diff.tag_names(f'{line_no}.0'):
            line_no -= 1
            if line_no < 1:
                return
        raw_line = self._diff.get(f'{line_no}.0', f'{line_no}.end')
        if not raw_line:
            return
        post = self._line_post_image.get(line_no)
        if post is None:
            return
        file, new_line_no, side, line_text = post
        anchor = self._line_to_anchor.get(line_no)
        existing = anchor.comment if anchor else ''
        self._comment_target = _CommentEditTarget(
            file=file, new_line_no=new_line_no, side=side, line_text=line_text,
            existing_snapshot     = anchor.snapshot     if anchor else None,
            existing_snap_line_no = anchor.snap_line_no if anchor else None,
        )
        bar_font = (CFG.font_family, int(CFG.menu_font_size * self._scale))
        frame = tk.Frame(self._diff, bg=C['topbar_bg'])
        prefix = tk.Label(frame, text='  >> ', bg=C['topbar_bg'], fg=C['comment_fg'],
                          font=bar_font)
        prefix.pack(side='left', padx=(4, 0), pady=2, anchor='nw')
        line_count = max(1, existing.count('\n') + 1)
        entry = tk.Text(frame, bg=C['bg'], fg=C['comment_fg'],
                        insertbackground=C['comment_fg'],
                        relief='flat', bd=0, height=line_count,
                        wrap='word', undo=True,
                        font=(CFG.font_family, CFG.font_size))
        entry.pack(side='left', fill='both', expand=True, padx=(0, 4), pady=2)
        if existing:
            entry.insert('1.0', existing)
            entry.tag_add('sel', '1.0', 'end-1c')
        def _newline(e: tk.Event) -> str:
            entry.insert('insert', '\n')
            self._resize_editor_frame(frame, prefix, entry)
            return 'break'
        def _confirm(e: tk.Event) -> str:
            self._confirm_comment_edit()
            return 'break'
        entry.bind('<Return>',         _confirm)
        entry.bind('<KP_Enter>',       _confirm)
        entry.bind('<Shift-Return>',   _newline)
        entry.bind('<Shift-KP_Enter>', _newline)
        entry.bind('<Alt-Return>',     _newline)
        entry.bind('<Alt-KP_Enter>',   _newline)
        entry.bind('<Escape>',         lambda e: self._cancel_comment_edit() or 'break')
        entry.bind('<FocusOut>',       lambda e: self.root.after(CFG.edit_focus_out_delay_ms, self._cancel_if_still_active))
        def _on_modified(e: tk.Event) -> None:
            if entry.edit_modified():
                entry.edit_modified(False)
                self._resize_editor_frame(frame, prefix, entry)
        entry.bind('<<Modified>>', _on_modified)
        self._active_comment_frame = frame
        self._active_comment_entry = entry
        if 'comment' in self._diff.tag_names(f'{line_no + 1}.0'):
            self._diff.delete(f'{line_no + 1}.0', f'{line_no + 1}.end')
        else:
            self._diff.insert(f'{line_no}.end', '\n')
        self._diff.window_create(f'{line_no + 1}.0', window=frame)
        self._resize_editor_frame(frame, prefix, entry)
        entry.focus_set()

    def _resize_editor_frame(self, frame: tk.Frame, prefix: tk.Label, entry: tk.Text) -> None:
        line_count = max(1, int(entry.index('end-1c').split('.')[0]))
        entry.configure(height=line_count)
        frame.update_idletasks()
        h = max(prefix.winfo_reqheight(), entry.winfo_reqheight()) + 4
        w = max(self._diff.winfo_width() - self._diff_row_pad(), 1)
        frame.configure(width=w, height=h)
        frame.pack_propagate(False)

    def _cancel_if_still_active(self) -> None:
        if self._active_comment_frame:
            text = self._active_comment_entry.get('1.0', 'end-1c') if self._active_comment_entry else ''
            if text.strip():
                self._confirm_comment_edit()
            else:
                self._cancel_comment_edit()

    def _cancel_comment_edit(self) -> None:
        if self._active_comment_frame:
            self._active_comment_frame.destroy()
            self._active_comment_frame = None
        self._active_comment_entry = None
        self._comment_target = None
        self._rerender_preserving_scroll()
        self._diff.focus_set()

    def _confirm_comment_edit(self) -> None:
        if not self._active_comment_entry or not self._comment_target:
            return
        target = self._comment_target
        comment = self._active_comment_entry.get('1.0', 'end-1c').strip()
        if self._active_comment_frame:
            self._active_comment_frame.destroy()
            self._active_comment_frame = None
        self._active_comment_entry = None
        self._comment_target = None
        if target.existing_snapshot is not None and target.existing_snap_line_no is not None:
            if comment:
                self._review.add(target.file, target.existing_snapshot,
                                 target.existing_snap_line_no,
                                 target.side, target.line_text, comment)
            else:
                self._review.delete(target.file, target.existing_snapshot,
                                    target.existing_snap_line_no, target.side)
        elif comment:
            snap_sha = self._session_snapshots.get(target.file)
            if not snap_sha:
                content = self._read_current_file(target.file)
                if content is not None:
                    snap_sha = self._review.write_snapshot(content)
                    self._session_snapshots[target.file] = snap_sha
            if snap_sha:
                self._review.add(target.file, snap_sha, target.new_line_no,
                                 target.side, target.line_text, comment)
        self._rerender_preserving_scroll()
        self._diff.focus_set()

    @staticmethod
    def _row_from_event(widget: tk.Text, event: tk.Event) -> int:
        return int(widget.index(f'@{event.x},{event.y}').split('.')[0]) - 1

    @staticmethod
    def _bind_list_mouse_events(widget: tk.Text, on_click) -> None:
        widget.bind('<Button-1>',         on_click)
        widget.bind('<Double-Button-1>',  lambda e: 'break')
        widget.bind('<Triple-Button-1>',  lambda e: 'break')
        widget.bind('<B1-Motion>',        lambda e: 'break')

    def _iter_all_comments(self) -> 'Iterator[tuple[int | None, str, str, str, bool, bool]]':
        """Yield (src_line, loc, src_text, comment, is_orphan, moved) for every
        stored comment. ``is_orphan`` covers both files-not-in-diff and lines
        rendered via the orphan placeholder."""
        for file, anchors in self._pending_anchors.items():
            for a in anchors:
                src_line = a.src_line
                is_orphan = (src_line is None
                             or 'orphan_src' in self._diff.tag_names(f'{src_line}.0'))
                if src_line is None:
                    loc = file
                elif is_orphan:
                    loc = f'{file} (orphaned)'
                else:
                    loc = self._loc_for_line(src_line) or file
                yield src_line, loc, a.line_text, a.comment, is_orphan, a.moved

    def _rebuild_review_menu(self) -> None:
        m = self._review_menu
        m.delete(0, 'end')
        m.add_command(label='Dump to terminal', command=self._dump_to_terminal)
        m.add_command(label='Clear all', command=self._clear_all_comments,
                      state='disabled' if self._review.is_empty() else 'normal')
        items = list(self._iter_all_comments())
        if items:
            m.add_separator()
            for src_line, loc, _src_text, cmt, _is_orphan, moved in items:
                first_line = (cmt.splitlines() or [cmt])[0]
                marker = '~ ' if moved else ''
                label = f'{marker}{loc} - {first_line[:CFG.menu_label_max_len]}'
                m.add_command(label=label,
                              state='disabled' if src_line is None else 'normal',
                              command=lambda ln=src_line: self._jump_to_diff_line(ln) if ln else None)

    def _clear_all_comments(self) -> None:
        if self._review.is_empty():
            return
        self._review.clear()
        self._rerender_preserving_scroll()

    def _jump_to_diff_line(self, line_no: int) -> None:
        self._manual_scroll = False
        self._scroll_diff_to_line(line_no)

    def _close_app(self) -> None:
        if not self._review.is_empty():
            self._dump_to_terminal()
        try:
            scroll_frac = self._diff.yview()[0]
            _save_window_state(self.root.winfo_geometry(), self._sash_ratio,
                               scroll_frac)
        except tk.TclError:
            pass
        self.root.destroy()

    def _show_commit(self, sha: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(sha)
        try:
            out = subprocess.check_output(
                ['git', 'show', '--stat', '--no-color', sha],
                text=True, stderr=subprocess.PIPE)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f'gitr: git show {sha} failed: {e}')
            return
        print(out)

    @staticmethod
    def _section_arrow(expanded: bool) -> str:
        return CFG.section_expanded_arrow if expanded else CFG.section_collapsed_arrow

    def _toggle_pane(self, expanded_attr: str, pane: tk.Frame, toggle_btn: tk.Button,
                     title: str, count: int, anchor: tk.Widget) -> None:
        expanded = getattr(self, expanded_attr)
        if expanded:
            pane.pack_forget()
        else:
            pane.pack(fill='x', before=anchor)
        new_state = not expanded
        setattr(self, expanded_attr, new_state)
        toggle_btn.configure(text=f'{self._section_arrow(new_state)} {title} ({count})')

    def _toggle_commits_pane(self) -> None:
        if not self._has_commits_section:
            return
        n = len(self._commits) + (1 if self._has_staged else 0) + (1 if self._has_unstaged else 0)
        self._toggle_pane('_commits_expanded', self._commits_pane, self._commits_toggle,
                          'Commits', n, self._flist_bar)

    def _comments_anchor(self) -> tk.Widget:
        return self._commits_header if self._has_commits_section else self._flist_bar

    def _toggle_comments_pane(self) -> None:
        n = len(list(self._iter_all_comments()))
        self._toggle_pane('_comments_expanded', self._comments_pane, self._comments_toggle,
                          'Comments', n, self._comments_anchor())

    def _update_comments_section(self) -> None:
        items = list(self._iter_all_comments())
        n = len(items)
        anchor = self._comments_anchor()
        if n == 0:
            self._comments_pane.pack_forget()
            self._comments_header.pack_forget()
            self._comments_expanded = False
            return
        self._comments_header.pack_forget()
        self._comments_header.pack(fill='x', before=anchor)
        self._comments_toggle.configure(
            text=f'{self._section_arrow(self._comments_expanded)} Comments ({n})')
        self._render_cmt_list(items)
        self._cmt_list.configure(height=min(n + 1, CFG.list_pane_max_lines))
        if self._comments_expanded:
            self._comments_pane.pack_forget()
            self._comments_pane.pack(fill='x', before=anchor)

    def _render_cmt_list(self, items: list) -> None:
        self._cmt_list.tag_configure('loc',      foreground=C['fileheader_fg'])
        self._cmt_list.tag_configure('cmt',      foreground=C['comment_fg'])
        self._cmt_list.tag_configure('orphan',   foreground=C['subdued'])
        self._cmt_list.tag_configure('moved',    foreground=C['comment_fg'])
        self._cmt_list.configure(state='normal')
        self._cmt_list.delete('1.0', 'end')
        self._cmt_list_actions: list = []
        for src_line, loc, _src_text, cmt, is_orphan, moved in items:
            first = (cmt.splitlines() or [cmt])[0]
            self._cmt_list.insert('end', '~ ' if moved and not is_orphan else '  ', 'moved')
            self._cmt_list.insert('end', loc, 'orphan' if is_orphan else 'loc')
            self._cmt_list.insert('end', '  ' + first[:CFG.cmt_panel_label_max_len] + '\n', 'cmt')
            self._cmt_list_actions.append(src_line)
        self._cmt_list.configure(state='disabled')
        self._bind_list_mouse_events(self._cmt_list, self._on_cmt_list_click)

    def _on_cmt_list_click(self, event: tk.Event) -> str:
        row = self._row_from_event(self._cmt_list, event)
        if 0 <= row < len(self._cmt_list_actions):
            line = self._cmt_list_actions[row]
            if line is not None:
                self._jump_to_diff_line(line)
        return 'break'

    def _render_clist(self) -> None:
        self._clist.tag_configure('sha',      foreground=C['fileheader_fg'])
        self._clist.tag_configure('subject',  foreground=C['fg'])
        self._clist.tag_configure('marker',   foreground=C['comment_fg'])
        self._clist.tag_configure('selected', background=C['selected_bg'])
        self._clist.configure(state='normal')
        self._clist.delete('1.0', 'end')
        self._clist_actions: list = []
        if self._has_unstaged:
            self._clist.insert('end', '* unstaged changes\n', 'marker')
            self._clist_actions.append(('unstaged',))
        if self._has_staged:
            self._clist.insert('end', '* staged changes\n', 'marker')
            self._clist_actions.append(('staged',))
        if (self._has_unstaged or self._has_staged) and self._commits:
            self._clist.insert('end', '\n')
            self._clist_actions.append(None)
        for sha, subject in self._commits:
            self._clist.insert('end', sha, 'sha')
            self._clist.insert('end', '  ' + subject + '\n', 'subject')
            self._clist_actions.append(('commit', sha))
        self._clist.configure(state='disabled')
        self._bind_list_mouse_events(self._clist, self._on_clist_click)

    def _on_clist_click(self, event: tk.Event) -> str:
        row = self._row_from_event(self._clist, event)
        if 0 <= row < len(self._clist_actions):
            action = self._clist_actions[row]
            if action and action[0] == 'commit':
                self._show_commit(action[1])
            elif action and action[0] == 'staged':
                self._show_staged_or_unstaged(['git', 'diff', '--cached', '--no-color'])
            elif action and action[0] == 'unstaged':
                self._show_staged_or_unstaged(['git', 'diff', '--no-color'])
        return 'break'

    def _show_staged_or_unstaged(self, cmd: list[str]) -> None:
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f'gitr: {" ".join(cmd)} failed: {e}')
            return
        print(out)

    def _dump_to_terminal(self) -> None:
        if self._review.is_empty():
            print('gitr: no review comments')
            return
        for _src_line, loc, src_text, cmt, _is_orphan, moved in self._iter_all_comments():
            print(f'{loc}\n{src_text}\n{self._format_comment_block(cmt, moved)}\n')

    def _jump_to(self, path: str) -> None:
        self._manual_scroll = False
        pos = self._positions.get(path)
        if not pos:
            return
        df = next((d for d in self._diff_files if d.path == path), None)
        header_lines = 2 if (df and df.index) else 1
        self._scroll_diff_to_line(int(pos.split('.')[0]) + header_lines)


# --entry point ------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(prog='gitr', description=USAGE,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--merge-base', action='store_true', dest='merge_base')
    parser.add_argument('-p', '--patch', metavar='FILE', default=None)
    parser.add_argument('refs', nargs='*')
    args = parser.parse_args()

    if args.merge_base and not args.refs:
        sys.exit('gitr: --merge-base requires a ref (e.g. gitr --merge-base master)')

    source: PatchSource | GitSource

    if args.patch is not None:
        try:
            text = sys.stdin.read() if args.patch == '-' else Path(args.patch).read_text()
        except OSError as e:
            sys.exit(f'gitr: {e}')
        label = '' if args.patch == '-' else args.patch
        source = PatchSource(text, label=label)
    elif args.refs == ['-']:
        source = PatchSource(sys.stdin.read())
    elif args.refs or args.merge_base:
        source = GitSource(args.refs, merge_base=args.merge_base)
    elif not sys.stdin.isatty():
        source = PatchSource(sys.stdin.read())
    else:
        source = GitSource([])

    diff_text = source.diff_text()
    if not diff_text.strip():
        print('gitr: no changes')
        sys.exit(0)

    root = tk.Tk()
    cwd = Path(os.getcwd())
    try:
        cwd_label = '~/' + cwd.relative_to(Path.home()).as_posix()
    except ValueError:
        cwd_label = cwd.as_posix()
    title_parts = ['gitr', cwd_label]
    src_label = source.label()
    if src_label:
        title_parts.append(src_label)
    root.title(' | '.join(title_parts))
    App(root, diff_text,
        commits=source.commits(),
        has_staged=source.has_staged(),
        has_unstaged=source.has_unstaged())
    root.mainloop()


if __name__ == '__main__':
    main()
