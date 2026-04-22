# gitrevue - Design Document

## Overview

`gitrevue` is a lightweight Git diff viewer and code review annotation tool.
It fills the gap between `gitk` (great for history browsing, no branch diff) and
full Git GUIs (too heavy) or web-based review tools (require a server).

Primary use case: reviewing LLM-agent-generated code across one or more branches,
with the ability to attach persistent local comments to specific diff lines.

---

## Versioning / Roadmap

### V1 - Diff Viewer (done)
- Pipe-only: `git diff ... | gitrevue`
- Two-panel layout: diff on left, file list on right (gitk-style)
- File list with status badge (A/M/D/R) and per-file `+N -N` counts, derived from diff
- Diff view: standard unified diff with coloured +/- lines (hunks only)
- Clicking a file in the list scrolls the diff to that file
- Running with no pipe prints usage with example commands

### V2 - Annotations + Word-level Diff
- Word-level diff within changed lines (highlight changed words, not just whole lines)
- Inline annotations: double-click a line -> input box -> saved comment rendered inline
- Comments stored in `.gitrevue/comments.json` (repo-local, not committed)
- Commented files marked in file list

### V3 - Side-by-side + Robust Line Tracking
- Side-by-side diff view (toggle between unified and split)
- Robust comment line anchoring: survive rebases/merges by anchoring to source
  file line number + surrounding context, not diff line number

### V4+ - Polish & Integration
- Syntax highlighting per language
- Jump to `$EDITOR` at specific line
- Search within diff
- Export diff as HTML
- Keyboard navigation (next/prev hunk, next/prev file)

---

## Usage

```bash
git diff main...HEAD | gitrevue          # branch diff vs main
git diff HEAD | gitrevue                 # staged + unstaged vs last commit
git diff --cached | gitrevue             # staged only
git diff HEAD~5 | gitrevue              # last 5 commits
git show HEAD | gitrevue                 # single commit
git diff --first-parent main...HEAD | gitrevue
```

The user controls the git command; gitrevue is purely a renderer.

---

## Technology

- **Language:** Python 3.10+
- **GUI:** Tkinter (stdlib only, zero extra deps)
- **Install:** `uv tool install .`
- **Platform:** Linux-first (GNOME/X11), should work on macOS/Windows as-is
- **Distribution:** single file (`gitrevue.py`)

---

## Layout

Two-panel horizontal split (resizable via sash), **diff on left, file list on right**:

```
+---------------------------------------------------------------------+
|  branch: feature-branch    3 files, +42 -7                         |  <- top bar
+---------------------------------------+-----------------------------+
|  --- a/src/foo.py                     |  M  src/foo.py    +8  -3   |
|  +++ b/src/foo.py                     |  A  src/bar.py    +21      |
|  @@ -10,6 +10,8 @@                   |  D  tests/old.py  -18      |
|   def hello():                        |                             |
| -     return "hi"                     |                             |
| +     return "hello world"            |                             |
|                                       |                             |
+---------------------------------------+-----------------------------+
```

### Top bar

- Current branch name (best-effort, silent if not in a git repo)
- Diffstat computed from parsed diff (`N files changed, +X -Y`)

### Left panel - diff view

- Standard unified diff rendering
- Colour scheme (Catppuccin Mocha):
  - Added lines: green fg, dark green bg
  - Removed lines: red fg, dark red bg
  - Hunk headers (`@@`): yellow/amber
  - File headers (`---`/`+++`, `diff`, `index`): blue/muted
  - Context lines: default text colour
- Scrollable both axes

### Right panel - file list

- One file per row
- Status badge: `M` (modified), `A` (added), `D` (deleted), `R` (renamed)
- Badge coloured: green=A, blue=M, red=D, purple=R
- Per-file `+N -N` line count
- Clicking a file jumps to that file's diff in the left panel
- Selected file highlighted

---

## Annotations - V2

### Interaction

- **Double-click** any line in the diff -> inline input box appears below that line
- **Enter** to save, **Escape** to cancel
- **Single-click** an existing comment -> opens for editing
- Delete all text + Enter -> removes the comment
- Commented files marked with `*` in the file list

### Storage

`<repo-root>/.gitrevue/comments.json`

```json
{
  "version": 1,
  "comments": [
    {
      "file": "src/foo.py",
      "line": 42,
      "text": "this logic seems wrong",
      "ref": "abc1234",
      "created_at": "2025-04-22T10:00:00"
    }
  ]
}
```

> V2 anchors comments to diff line numbers (simple, may shift after rebase).
> Robust anchoring deferred to V3.

---

## File structure

```
gitrevue.py    # single-file implementation
pyproject.toml
```
