# gitr - Roadmap

## Overview

`gitr` is a lightweight Git diff viewer and code review annotation tool.
It fills the gap between `gitk` (great for history browsing, no branch diff) and
full Git GUIs (too heavy) or web-based review tools (require a server).

Primary use case: reviewing LLM-agent-generated code across one or more branches,
with the ability to attach persistent local comments to specific diff lines.

---

## V1 - Diff Viewer + Annotations

- [x] Pipe-only: `git diff ... | gitr`
- [x] Direct invocation: `gitr master`, `gitr master HEAD`, `gitr --merge-base master`
- [x] Read from patch file: `gitr -p patch.diff`
- [x] Two-panel layout: diff on left, file list on right
- [x] File list: flat and tree view, status badge (A/M/D/R), per-file `+N -N` counts
- [x] Diff view: unified diff with coloured +/- lines, hunk separators
- [x] Sticky file header while scrolling
- [x] Minimap, VS Code style (1 px per character), with viewport indicator;
      comments shown as coloured bands
- [x] Smooth scrolling, keyboard navigation (n/p/Tab for next/prev file)
- [x] Config persistence: wrap, tree view, word diff, line numbers
- [x] Word-level diff: highlight changed words, dim unchanged words within changed
      lines; optional collapsing of re-indented lines
- [x] Line numbers in a gutter: off / new / old+new (`l`)
- [x] Reload the git source without restarting (`r`, F5)
- [x] Large diffs: skeleton-first render behind a progress label, word diff
      applied afterwards from the visible region outward
- [x] Hover ruler on diff lines with `+comment (a)` and `copy (c)`; copy puts
      `path:line` plus the line(s) and their comments on the clipboard
- [x] Comments and Commits panels above the file list; comments dumped to the
      terminal on exit (and on demand), Clear all with confirmation and a
      timestamped backup
- [x] Ctrl+C in the terminal closes the app like Ctrl+W
- [x] Inline annotations: hover button, `a` or the context menu opens an inline
      editor; the saved comment renders as a bar below the line
- [x] Comments stored in `.gitr/review.json` (repo-local) with file snapshots in
      `.gitr/snapshots/`
- [x] Comment kinds: note / good / bad, each with a colour and a text marker
      (`>>`, `++`, `!!`) used on screen and in the panels;
      one-click presets and a kind submenu in the context menu, a kind button on
      the comment and in the editor (Ctrl+1/2/3)
- [x] Comment markers on an overview strip beside the scrollbar
- [x] Commented files marked in file list: per-kind comment counts (`>>2 ++1 !!3`) in the kind colours after the `+N -N` stats
- [x] Export for coding agents: Review > "Copy for agent" (all, or notes + bad,
      `Ctrl+Shift+C` / `Ctrl+Shift+X`), per comment from its context menu, and
      `gitr --export [--kinds bad,note] [refs]` printing the same text without a window
- [x] Robust comment anchoring: each comment is anchored to a snapshot of the
      file taken when it was written; on load the snapshot is diffed against the
      current file to remap the line, so comments survive edits, rebases and
      merges (moved ones are flagged with `~`)

## Future

- [ ] Side-by-side diff view (toggle between unified and split)
- [ ] Whole-file view: besides the continuous all-files diff, open one file
      in its own view (or window) showing the entire file, with the diff
      overlaid and switchable: off (plain file), inline unified, or two
      files side by side. The point is to see the changed lines in the
      context of the whole file, not just the hunks.
- [ ] Viewed marks: an explicit per-file (maybe per-hunk) "viewed" state set
      by the user (a key, or a checkbox in the file list, as GitHub and Gerrit
      do), shown in the file list, the overview strip and the minimap; kept
      per hunk under a content key so a reload resets only files whose diff
      changed. A variant worth trying: a file counts as viewed automatically
      once the view scrolled past its end.
- [ ] Fog of war (experiment, branch `fog-of-war`): per-line exposure while
      on screen, minimap rows wiping clear after a dwell, seen state per hunk
      in `.gitr/seen.json`. Tried in use: the time-based per-line signal is
      too noisy to act on (a hunk on screen while reading another counts as
      seen, a skimmed one does not), and the shading is hard to read on the
      narrow minimap. Parked; the per-hunk content keys on that branch are
      the basis for the viewed marks above.
- [ ] Search within diff (`Ctrl+F`)
- [ ] Hunk navigation (`j` / `k` to jump between `@@` hunks within a file)
- [ ] Jump to `$EDITOR` at the correct line (`o`)
- [ ] Fold / collapse individual file diffs
- [ ] Syntax highlighting per language (no extra deps — use `re`-based tokeniser)
- [ ] Font size adjustment (`Ctrl++` / `Ctrl+-`)
- [ ] Color theme override via `config.json`
- [ ] Keyboard shortcut help (`?`)
- [ ] Export diff as HTML

### Annotation storage format

`<repo-root>/.gitr/review.json`, snapshots as plain text in `.gitr/snapshots/<sha1>`:

```json
{
  "files": {
    "src/foo.py": [
      {
        "snapshot": "<sha1 of the file when the comment was written>",
        "line_no": 42,
        "side": "+",
        "line_text": "+    return x",
        "comment": "this logic seems wrong",
        "kind": "bad"
      }
    ]
  }
}
```

`side` is `+`, `-` or a space (the diff line kind); `kind` is `note`, `good`
or `bad` and defaults to `note` when absent.

---

## Usage

```bash
gitr                         # git diff (unstaged changes)
gitr master                  # git diff master
gitr --merge-base master     # diff from common ancestor
gitr master HEAD             # committed changes only
git diff | gitr              # pipe a patch
gitr -                       # read stdin explicitly
gitr -p patch.diff           # read from a patch file
GITR_SCALE=2 gitr master     # scale the UI up (HiDPI)
```

Comments need a git repository (they live in `<repo>/.gitr`), so run gitr from
the repository the diff belongs to; outside one they are not offered.

---

## Technology

- **Language:** Python 3.10+
- **GUI:** Tkinter (stdlib only, zero extra deps)
- **Install:** `uv tool install .`
- **Platform:** Linux-first (X11), should work on macOS/Windows
- **Distribution:** single file (`gitr.py`)
