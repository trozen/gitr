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
      `path:line` plus the line(s) and their comments on the clipboard, in a
      diff code fence so a markdown renderer keeps the +/- markers (the context
      menu has a raw variant without the fence)
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
- [ ] Search within diff (`Ctrl+F`) in the Tk window (gitr-serve has Monaco's)
- [ ] Hunk navigation (`j` / `k` to jump between `@@` hunks within a file)
- [ ] Jump to `$EDITOR` at the correct line (`o`)
- [ ] Fold / collapse individual file diffs
- [ ] Syntax highlighting per language (no extra deps — use `re`-based tokeniser)
- [ ] Font size adjustment (`Ctrl++` / `Ctrl+-`)
- [ ] Color theme override via `config.json`
- [ ] Keyboard shortcut help (`?`)
- [ ] Export diff as HTML
- [x] Comments from the command line: `gitr comments list|add|rm|accept`
      on the same `.gitr/review.json`, so an agent or a script can review
      too; ids are hashes of the stored key, or path:line. A comment from
      the CLI is on a line of the current file, so the views match an
      added and an unchanged line alike
- [ ] Removed lines from the CLI (`path:-42` against a base) and editing
      a comment's text (`gitr comments edit`)
- [ ] Subcommands: `gitr diff|export|comments|serve` are there (plain
      `gitr` is `gitr diff`); to come: `gitr serve start|stop|status`
      (see gitr-serve's daemon item) and the viewer as the web page

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

## Web version: `gitr-serve`

A browser-based gitr for agent work on a remote machine: several projects,
each with one worktree per agent. A small server runs next to the worktrees
(stdlib Python, reusing gitr's diff parsing, review store and export, so
`.gitr/review.json` and `gitr export` stay the agent contract) and is
reached over an SSH tunnel or Tailscale; the frontend is Monaco, with the
whole diff in one read-only document as in gitr.

### V1 - gitr parity, multi-repo

- [x] Project list from a config file, worktrees per project
      (`git worktree list --porcelain`), switch worktree by clicking
- [x] Diff base per worktree, default `main` / `master`; diff against the
      merge base including uncommitted and untracked files
- [x] Single continuous diff, file list (tree only, `+N -N`, per-kind
      comment counts), minimap with comment marks, word diff, line numbers,
      sticky file header
- [x] File list synced with the diff: scrolling highlights the current file,
      clicking a file scrolls to its start
- [x] Comments with kinds and presets, same storage as gitr; copy for agent
- [x] "Worktree changed" banner with reload (no silent re-render)
- Mouse navigation only; no Commits panel (a commits pane and Ctrl+P came later)

### Later

- [ ] `gitr-serve start|stop|status` as a user daemon: install and drive a
      systemd user unit (`systemctl --user`, with `loginctl enable-linger`
      so it survives logout) rather than self-daemonizing; plain
      `gitr-serve` stays the foreground mode the unit runs
- [x] Font sizes (diff, comments, sidebar, top and status bar) in a
      settings popup, from the gear at the bottom of an activity bar left
      of the sidebar (as in VS Code); kept per browser
- [x] Project switcher: the top bar spans the window, and its left end,
      as wide as the activity bar and sidebar below it, is a
      `project > branch` button opening a popup with the worktrees by
      project and a filter field; the file list keeps the full height
- [x] Base picker: the base field lists the default, the upstream, the
      other worktrees' branches, and all branches and tags with ahead /
      behind counts; a ref that does not exist marks the field
- [x] Wrap and collapse re-indent into the settings popup: they change
      only how the diff is shown, not what is reviewed, so they are one
      choice per browser like the font sizes
- [ ] Keep each project's state across switches: the loaded diff, scroll
      position and open editors stay as they were, so a project that
      needs a reload still shows the pre-reload diff on return (with its
      banner) until reload is clicked
- [x] Changes since the last reload: on reload, mark the lines that differ
      from the previous load (per file, the old diff text against the
      new one), so after an agent's follow-up only the new work stands
      out; the marks clear on the next reload
- [x] Go to file: Ctrl+P opens a VS Code style quick open over the diff,
      the changed files matched loosely by name, Enter jumps to one
- [x] Ctrl+P lists the projects and worktrees too (as a second group
      below the files), so one popup goes anywhere
- [ ] Ctrl+P prefixes, as VS Code's quick open has them: one that lists
      only the worktrees (in a big diff they come after up to 300 files
      when nothing is typed), and maybe `>` for actions (reload, copy for
      agent, switch view) and `@` for the hunks or comments of the
      current file; the prefixes are shown as hints when nothing is typed
- [x] Project switcher: up / down arrows move through the filtered
      worktrees, Enter opens the selected one (now Enter takes the first)
- [x] Base picker: recent commits of the branch as well (short hash,
      subject, age), so a base a few commits back is a click away rather
      than a typed `HEAD~3`
- [x] Notice when `.gitr/review.json` changes from outside (a gitr window,
      another tab, an agent or the CLI): the comments already reload on
      their own, silently; say so, e.g. "comments updated: 2 added, 1
      removed", and mark the new ones for a while
- [ ] Push from the server (Server-Sent Events) instead of polling: the
      page keeps one `/api/events` connection open; the browser reconnects
      by itself after a restart and the first message tells the new
      version, so the "app changed" note comes at once. Worktree and
      review.json changes could come the same way. To check: a held
      thread per open tab, and proxies (`tailscale serve`) buffering the
      stream
- [ ] Keyboard navigation (n / p / Tab), including next / previous comment
      (the top bar arrows do it with the mouse) and `c` to comment on the
      cursor line
- [x] Comments panel in the sidebar, picked with the activity bar (files /
      comments): every comment in diff order under its file, with kind,
      first line and line number; click to jump. Comments on files not in
      the current diff listed at the end
- [ ] Multi-repo projects: a project directory that is not a git repo but
      holds several (e.g. a workspace with one checkout per service). To
      decide: list the sub-repos as separate entries under the project, or
      review them as one combined diff with the repo name prefixed to each
      path (one base per sub-repo, comments still in each repo's `.gitr/`);
      and how agent worktrees map when each sub-repo has its own
- [ ] Find pane in the sidebar, as VS Code's search view: every match
      listed under its file, click to jump; maybe filters (only added /
      removed lines) and the comment texts searched too. For now Monaco's
      own find widget (Ctrl+F in the diff) does the searching
- [ ] Less browser outside the diff, so the page acts like an app: there
      Ctrl+F opens the browser's find (which sees only the diff lines on
      screen, as Monaco draws no others) and a right-click the browser's
      menu. To decide: Ctrl+F opening the diff's find from anywhere, and
      our own menu (or none) in the sidebar and bars, keeping the
      browser's where text can be selected or typed
- [ ] Tabs: open a file or a subdirectory as its own view of the diff
- [ ] Side-by-side and whole-file views (Monaco `DiffEditor` / editor tab)
- [ ] Viewed marks, keyed by path and blob hash so a changed file un-marks
- [ ] Per-file syntax highlighting in the combined view
- [x] Commits pane: base..HEAD as a graph, as gitk draws it (merges, the
      boundary, branch and tag labels, uncommitted changes on top); a
      click shows that commit alone, as git show does (header and message
      before the diff; a merge against its first parent), with comments
      placed on that commit's version of each file; the agent export stays
      the whole review's
- [x] Commits pane ranges: shift-click a second commit to see the commits
      between the two combined (from the older one's first parent to the
      newer one, or to the working tree), listed before the diff
- [x] Commit rows' right-click menu: copy hash or subject, a range to the
      shown commit, and "use as base" (review the changes after a commit)
- [x] Untracked files in the commits pane: an entry of their own below
      "All changes", above the commits (and uncommitted changes), showing
      only the untracked files; "All changes" leaves them out, and a range
      from it to them adds them (the top bar's checkbox is gone)
- [x] Commits pane with no commits since the base: it said only "no
      commits since the base", while with commits the base shows as the
      boundary commit at the bottom; the base commit shows there too
- [ ] Comment marks in the commits pane: per commit, the comments on lines
      it changed (found with blame on the commented lines), so it shows
      which commits have been reviewed and which not yet
- [ ] Phone view (Android, as an installed web app over `tailscale serve`):
      Monaco is not supported on mobile browsers and our comment controls
      need hover and right-click, so a separate light page rather than the
      desktop one squeezed: the diff from the same API drawn as plain HTML,
      one file at a time with a file picker, tap a line to comment, the
      comment boxes with accept / copy / delete as on the desktop. Same
      `review.json`, so a review moves between phone and desktop
- [ ] Replace the Tk window with the web view: `gitr` becomes a launcher
      for a one-diff `gitr-serve`, so there is one UI to maintain. Steps:
      1. A one-diff mode in gitr-serve: refs or a piped patch (also outside
         configured repos), a free local port, a Chrome app window
         (`--app=URL`, else a browser tab via `webbrowser`), exit when the
         window closes (page heartbeat). Stdlib only: no embedded web view.
      2. Parity with the Tk window (keyboard navigation, comments panel,
         word-diff and line-number modes); Monaco is already bundled, so it
         works offline. Check the start-up time
         (a browser window vs Tk's) is acceptable.
      3. `gitr` launches it; the Tk window stays behind `gitr --tk` until
         it is no longer used, then goes.

### Known issues

- [x] Minimap file names were clipped for files shorter than about 7
      lines (empty files, pure renames, one-line changes): Monaco draws
      each label on a box about 7 minimap rows tall. Fixed by padding every
      file to at least 7 lines.
- [x] Sticky headers broke when the top of the view was inside a long
      wrapped line that ends a file or hunk (a Monaco 0.52 bug). Fixed by
      moving to Monaco 0.57, bundled with esbuild (`monaco/`).

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
