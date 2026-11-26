# gitr - Roadmap

## Overview

`gitr` is a Git diff viewer and code review annotation tool, in the browser.
It fills the gap between `gitk` (great for history browsing, no branch diff) and
full Git GUIs (too heavy) or hosted review tools (need a forge).

Primary use case: reviewing LLM-agent-generated code across one or more branches,
with the ability to attach persistent local comments to specific diff lines.

gitr began as a Tk window (`git diff | gitr`, `gitr master`): a diff with a
file list, a minimap, word diff, line comments in `.gitr/review.json` and
the agent export. gitr serve below grew from it and replaced it: `gitr`
now opens gitr serve's page for the repository it is run in.

---

## Future

- [ ] Side-by-side diff view (toggle between unified and split; Monaco's
      `DiffEditor`)
- [ ] Whole-file view: besides the continuous all-files diff, open one file
      in its own view (an editor tab, or a window) showing the entire file,
      with the diff overlaid and switchable: off (plain file), inline
      unified, or two files side by side. The point is to see the changed
      lines in the context of the whole file, not just the hunks.
- [ ] Fog of war (experiment, branch `fog-of-war`): per-line exposure while
      on screen, minimap rows wiping clear after a dwell, seen state per hunk
      in `.gitr/seen.json`. Tried in use: the time-based per-line signal is
      too noisy to act on (a hunk on screen while reading another counts as
      seen, a skimmed one does not), and the shading is hard to read on the
      narrow minimap. Parked; the per-hunk content keys on that branch are
      the basis for the viewed marks. Marks set automatically once the view
      scrolled past a file were dropped for the same reason: scrolling is
      not reading, and wrong marks cost more than setting them by hand.
- [ ] Hunk navigation (`j` / `k` to jump between `@@` hunks within a file)
- [ ] Jump to `$EDITOR` at the correct line (`o`)
- [ ] Fold / collapse individual file diffs
- [ ] Syntax highlighting per language, per file in the combined view
      (Monaco's own tokenizers)
- [ ] Colour themes, picked in the settings popup (Dracula stays the
      default)
- [ ] Keyboard shortcut help (`?`)
- [ ] Export diff as HTML
- [x] Comments from the command line: `gitr comments list|add|rm|accept`
      on the same `.gitr/review.json`, so an agent or a script can review
      too; ids are hashes of the stored key, or path:line. A comment from
      the CLI is on a line of the current file, so the views match an
      added and an unchanged line alike
- [ ] Removed lines from the CLI (`path:-42` against a base) and editing
      a comment's text (`gitr comments edit`)
- [x] Subcommands: `gitr export|comments|serve`; plain `gitr [base]` is
      the review of the repository it is run in
- [x] The Tk window replaced by the page: `gitr` serves the repository
      alone on a port of its own (the same every time, so the page's
      settings stored in the browser stay), opens it in a Chromium-family browser's app
      mode, in a profile of gitr's own so that quitting the browser or its
      session restore leaves gitr's windows alone (else the default browser), and ends on Ctrl+C (closing its
      windows) or when its last window closed: each page holds a
      server-sent events stream open, and the server stops when the last
      one dropped (waiting a moment only when the page was just asked for
      again: a reload); on Ctrl+C it tells the pages, which close; pipe mode and `-p` went with the window
- [ ] `gitr`'s window with the installed app's title bar (its colour, icon
      and name): only an installed web app gets it, an install needs a
      click in the page, and it belongs to one address, port included; so
      one shared port for every repo's `gitr` (one server for all of them),
      installed once and opened with `--app-id`. Dropped for now
- [x] Changes pushed on the events stream instead of the page polling
      for them every few seconds, in gitr serve too: one stream per
      browser, held by a SharedWorker for all its tabs (HTTP/1.1 allows
      six connections per host), which posts what each visible tab shows
      to `/api/watch`. A server thread checks those views every second
      (once per view, however many tabs) and the worktree list and page
      version every 10, and sends what changed. The polls stay for when
      the stream is down (or a browser without SharedWorker). To check:
      `tailscale serve` passing the stream on unbuffered

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

## Web version: `gitr serve`

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

- [ ] `gitr serve start|stop|status` as a user daemon: install and drive a
      systemd user unit (`systemctl --user`, with `loginctl enable-linger`
      so it survives logout) rather than self-daemonizing; plain
      `gitr serve` stays the foreground mode the unit runs
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
- [x] Keep each project's state across switches: the loaded diff, scroll
      position and open editors stay as they were, so a project that
      needs a reload still shows the pre-reload diff on return (with its
      banner) until reload is clicked; the server keeps each worktree's
      last document for it
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
- [x] Push from the server (Server-Sent Events) instead of polling: see
      "Changes pushed on the events stream" above; the stream's first
      message is the server's version, so the "app changed" note comes as
      soon as it reconnects after a restart
- [ ] Keyboard navigation (n / p / Tab), including next / previous comment
      (the top bar arrows do it with the mouse) and `c` to comment on the
      cursor line
- [x] Comments panel in the sidebar, picked with the activity bar (files /
      comments): every comment in diff order under its file, with kind,
      first line and line number; click to jump. Comments on files not in
      the current diff listed at the end
- [x] Multi-repo projects: a project directory that is not a git repo but
      holds several (e.g. a workspace with one checkout per service) lists
      its checkouts under it, each an ordinary repo with its worktrees
- [ ] Maybe: one combined diff over a multi-repo directory's checkouts, the
      repo name prefixed to each path (one base per checkout, comments still
      in each repo's `.gitr/`), agent worktrees matched by branch name
- [ ] Find pane in the sidebar, as VS Code's search view: every match
      listed under its file, click to jump; maybe filters (only added /
      removed lines) and the comment texts searched too. For now Monaco's
      own find widget (Ctrl+F in the diff) does the searching
- [x] Right-click outside the diff: the page's own menu on every list row
      (files and folders, the comments panel, the comment boxes, commits),
      none elsewhere, the browser's only in text fields and on selected
      text
- [ ] Less browser outside the diff, so the page acts like an app: there
      Ctrl+F opens the browser's find (which sees only the diff lines on
      screen, as Monaco draws no others). To decide: Ctrl+F opening the
      diff's find from anywhere
- [ ] Tabs: open a file or a subdirectory as its own view of the diff
- [x] Viewed marks: a box per file and folder in the file list (and one
      for all files), in the gutter of each file and hunk header in the
      diff, and per file or hunk in the right-click menu; kept per hunk in
      `.gitr/viewed.json` under a key of its + and - lines, so a hunk an
      agent changed comes back unviewed and one that only moved does not;
      viewed hunks dimmed, marked in the scrollbar and shaded in the
      minimap, next / previous hunk not viewed in the list's header, and
      the reload note counts the viewed files a reload changed. The fold
      chevrons gave their gutter to the boxes (the folding regions stay,
      for the sticky headers), and folders in the list fold from their
      chevron only
- [ ] Viewed marks: to try in use, collapsing viewed files (Monaco hides
      lines only through internal API), a key to mark and go to the next
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
- [x] Comment marks in the commits pane: per commit, the comments on lines
      it changed (found with blame on the commented lines, reverse blame
      for removed ones), marked as in the file list, so it
      shows which commits have been reviewed and which not yet
- [ ] Git blame in the page: which commit last changed a line (author,
      date, subject), e.g. in the gutter or on hover, with a click to show
      that commit; details to decide
- [x] Older commits in the commits pane: a number of commits before the
      base (30 by default, set in the settings popup), faded like the base
      and not part of "All changes", to look at or pick as a single commit
- [x] The base's own commits in the commits pane: HEAD..base, as
      `gitk HEAD base` draws them, faded, toggled by an icon in the pane's
      header (on by default), so a branch behind its base shows it
- [ ] Phone view (Android, as an installed web app over `tailscale serve`):
      Monaco is not supported on mobile browsers and our comment controls
      need hover and right-click, so a separate light page rather than the
      desktop one squeezed: the diff from the same API drawn as plain HTML,
      one file at a time with a file picker, tap a line to comment, the
      comment boxes with accept / copy / delete as on the desktop. Same
      `review.json`, so a review moves between phone and desktop
- [x] UI polish, before the herdr integration
- [x] A count on the comments button in the left pane: how many comments
      the review holds, coloured as the worst of them (bad over note over
      good)
- [x] Tests: a small suite for the parts most changed of late (diff
      parsing, comment anchoring, the word diff pairing, the commit marks),
      `python3 -m unittest discover -s tests -t .`; the page has none
- [x] Release on PyPI: 0.1.0 as `gitr-review` (`uv tool install
      gitr-review`), MIT, the README for the package rather than a checkout
- [ ] The name `gitr` on PyPI: refused as too close to the abandoned
      `git-r`; asked for under PEP 541 in
      https://github.com/pypi/support/issues/13278 (a long queue). If
      granted, publish as `gitr` and make `gitr-review` a release that
      depends on it
- [x] Publish to PyPI from GitHub Actions on a `v*` tag, with Trusted
      Publishing (no token): check the tag against the version in
      `pyproject.toml`, run the tests, `uv build`, `uv publish`; a `pypi`
      environment that asks for approval. Before the next release
- [ ] Branches `main` (the default, moved only by releases) and `devel`
      (daily work) instead of `master`: herdr installs and updates a
      plugin from the default branch, and the plugin runs the gitr of its
      checkout, so plain installs would get releases only, no `--ref`
- [x] Integration with herdr, or gitr as a herdr plugin
- [x] herdr plugin (`herdr-plugin/`): its `[[startup]]` hook starts
      `gitr serve --herdr`, which serves the repos open in herdr's panes
      besides the configured ones and stops with herdr's server (after a
      wait for a live handoff) or when the plugin is disabled; it runs the
      gitr of the plugin's own checkout (herdr clones the whole repo), else
      `gitr` from PATH, and writes a `gitr` launcher for the shell
- [x] `gitr status`: gitr serve, the running `gitr` windows, herdr's plugin
- [x] herdr plugin from GitHub: an install keeps the whole repo, so the
      plugin runs the gitr of its own checkout (tried on a second machine)
- [ ] The `herdr-plugin` topic on the GitHub repo, for herdr's marketplace
- [ ] herdr: the agents' state (working, waiting, done) in the worktree list
- [ ] herdr: comments sent to the agent's pane (`herdr agent prompt`), next
      to "copy for agent"
- [ ] herdr: gitr shown in herdr's UI, e.g. "gitr is running" in the tab
      bar's status area, or the review state per workspace in the sidebar
- [ ] A newer gitr announced, as an opt-in setting (the README promises no
      internet needed): a check of GitHub's master (the plugin) or PyPI
      (gitr-review) now and then, shown in the page or as a herdr
      notification with the command that updates
- [x] Replace the Tk window with the web view: `gitr` launches a
      one-repo server in a Chrome app window (see "The Tk window replaced"
      above). Not carried over: pipe mode, the window's keys (postponed
      for the page), word diff off, the flat file list, line-number modes,
      "copy for agent: notes + bad" (`gitr export --kinds bad,note` has
      it), copying lines without the code fence, copying one comment for
      an agent, and the staged-only diff (`--cached`)

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
gitr                         # this repository's review in a window
gitr master                  # ... against master
gitr export master           # the comments as text for an agent
gitr comments list           # read and write comments from the terminal
gitr serve [repos]           # many repositories and worktrees
```

---

## Technology

- **Language:** Python 3.10+, stdlib only
- **UI:** a page in the browser, Monaco bundled (`monaco/`)
- **Install:** `uv tool install .`
- **Platform:** Linux-first, should work on macOS
