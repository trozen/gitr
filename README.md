# gitr

A Git diff viewer for reviewing what coding agents changed, in a browser
window: the whole review against a base as one diff, with a file list,
comments an agent can read back, viewed marks, and the branch's commits.

## Install

```bash
uv tool install .            # installs gitr and gitr-serve
```

Or just point an alias at the script directly (no install step needed):

```bash
alias gitr=~/path/to/gitr/gitr.py
```

## Usage

```bash
gitr                         # this repository's review in a window
gitr master                  # ... against master
gitr -- comments             # a base named like one of gitr's commands
gitr --browser list          # the browsers gitr found, the one it uses marked
gitr --browser vivaldi       # that one (a listed name, or a path)
gitr --browser default       # a tab in the default browser
gitr --browser none          # only print the address
```

`GITR_BROWSER` takes the same values, for a choice that sticks; `--browser`
wins over it.

`gitr` serves the repository it is run in, alone, and opens the page on the
current worktree: in a Chromium-family browser's app mode (a window with no
tabs or address bar) when one is installed (Chrome, Chromium, Edge, Brave,
Vivaldi, Opera; also as a snap or a Flatpak), else in a tab of the default
browser (Firefox, Safari), where everything works the same but the tab
cannot close itself. The app window runs in a browser profile of
gitr's own (under `~/.local/state/gitr/browsers/`; a snap's or a Flatpak's
in its own folder, which its sandbox can write), apart from your
browsing: quitting the browser leaves it open, and the browser's session
restore never brings back a window of a gitr that has stopped. The base is the one last used for the
worktree, else `main` or `master`; the switcher lists the repository's
other worktrees. It runs until Ctrl+C, which closes its windows too, or
until its last window closes (a page reload does not count); either end
follows within a fraction of a second. A browser
that puts a minimized window to sleep to save memory closes its
connection, which counts as closing it: the window then closes when it
wakes. A page that cannot close itself (a tab in a browser without app
mode) says "server stopped" instead, and reconnects when `gitr` runs
again. The window opens where the last one was closed. Each repository
gets a port of its own, the same every time, so the page's settings (font
sizes, wrapping) stay with it; running `gitr` again
while one serves the repository just opens another window on it. Over ssh,
with no screen to open the window on, it prints the address instead. The
page is the same as gitr-serve's below, which describes it.

## Comments from the command line

The review comments (in `.gitr/review.json` of the repository) can be read
and written from the terminal, so an agent or a script can take part in a
review:

```bash
gitr export master                       # all comments as markdown for an agent
gitr export --kinds bad,note master      # only these kinds
gitr export -p change.diff               # placed on a patch (- for stdin)
gitr comments list [--kinds bad] [src/]  # one line each: id, path:line, kind, text
gitr comments list --full                # with the whole texts (--json for scripts)
gitr comments add src/foo.py:42 "this leaks the handle" --kind bad
gitr comments add src/foo.py:42 - < note.txt   # the text from stdin
gitr comments accept 3f9a2c1             # resolved: a good comment ending in "(accepted)"
gitr comments rm src/foo.py:42           # by id (any unique prefix) or path:line
```

A comment from `add` goes on that line of the file as it is in the working
tree, with no diff involved: the page shows it on that
line whether their diff has it added or unchanged (a line outside every
hunk is listed after the diff). `list` gives the lines as they are now,
flagged when a line's text changed, its file is gone, or it is a removed
line (a comment from the page). A line takes one
comment from `add`; remove the old one first, or edit it in the page.

## gitr-serve

`gitr-serve` is the same page for several repositories and all their
worktrees. It runs where the repositories are (e.g. a remote machine with
agents working in worktrees), in the foreground.

```bash
gitr-serve ~/dev/repo-a ~/dev/repo-b         # http://127.0.0.1:4487/
gitr-serve --port 9000 ~/dev/repo-a
gitr serve ~/dev/repo-a                      # the same
```

From a checkout, without installing: `python3 gitr_web/server.py`, with
`--reload` to restart on changes to `gitr_web/server.py`, `gitr.py` or
`~/.config/gitr/projects.json`. The page files under `gitr_web/static/` are
read from disk on every request, so changes to them only need a browser
refresh. The Monaco bundle under
`gitr_web/static/monaco/` is built and committed; to move to another Monaco
version, change it in `monaco/package.json` and run
`cd monaco && npm install && npm run build` (Node is needed for that only).

With no repository arguments the list is read from
`~/.config/gitr/projects.json`:

```json
{"projects": ["~/dev/repo-a", {"path": "~/dev/repo-b", "name": "b"}]}
```

A directory that is not a git repo but holds several (a workspace with one
checkout per service, say) is listed as a heading with each checkout under
it, picked up afresh with every listing, when its entry says so:

```json
{"projects": [{"path": "~/dev/workspace", "multi": true}]}
```

Only the checkouts directly in it are found; `"depth": 2` looks one level
further down (never inside a checkout).

`gitr-serve --print-config [repo ...]` prints one for the repos given (or the
one you are in), ready to redirect into that file.

The server listens on localhost only and has no authentication, so reach it
from another machine through SSH or Tailscale rather than exposing the port.
Plain http to a remote address is not enough either way: browsers allow the
clipboard (the copy buttons) and app install only over https or on localhost.

Through an SSH tunnel the page is on localhost:

```bash
ssh -N -L 4487:127.0.0.1:4487 remote-host    # then open http://127.0.0.1:4487/
```

(or `LocalForward 4487 127.0.0.1:4487` for that host in `~/.ssh/config`).

With Tailscale, `tailscale serve` puts it on https with a real certificate,
for your tailnet only. On the remote machine:

```bash
tailscale serve --bg --https 4487 4487
gitr-serve --allow-host remote-host.your-tailnet.ts.net
# then open https://remote-host.your-tailnet.ts.net:4487/
```

The same port number as the server's own does not clash with it: Tailscale
listens on the tailnet address, gitr-serve on 127.0.0.1. A port of its own
also leaves the default one (443) to whatever else the machine serves;
`tailscale serve --bg 4487` would take it over. `tailscale serve
status` shows what is served where. The allowed name does not depend on the
port.

`--allow-host` names the address the browser uses: the server refuses other
names, so that a web page cannot reach it by pointing its own name at
127.0.0.1. It can be given several times, and the names can be kept in
`projects.json` instead (they apply also when the repos are given on the
command line):

```json
{"projects": ["~/dev/repo-a"], "allow_hosts": ["remote-host.your-tailnet.ts.net"]}
```

`--print-config` fills in the machine's tailnet name when Tailscale runs
there (`tailscale status --json` shows it as `Self.DNSName`).

Chrome can install the page as an app with its own window (the install icon
in the address bar, or menu > Cast, save and share > Install page). The app
belongs to the address it was installed from, port included, so keep the
tunnel on the same local port. To install apps from two machines, reach them
on different local ports (e.g. `ssh -L 4488:127.0.0.1:4487 remote-host` for
the remote one) or one of them through Tailscale; each app is named after its
server's hostname.

Pick a worktree with the `project > branch` button at the top left; the
base defaults to `main` / `master` and can be any ref. Clicking the base
field lists the default, the branch's upstream, the branches of the
project's other worktrees, the branch's recent commits (to review only
what came after one), and all branches and tags, each with how many
commits HEAD is ahead of (and behind) it; typing filters the list, and
Enter takes the text as typed (a commit hash, `HEAD~3`). The counts for the
current base are also shown next to the merge-base. The base is
remembered per worktree on the server (`~/.local/state/gitr/web.json`); a
worktree opened for the first time starts from the one last used in its
project (a base that is a commit rather than a branch or tag stays with
its worktree). The review leaves untracked files out; the commits pane
shows them (below). Wrapping long lines and collapsing re-indented ones
are set in the settings popup (the gear), per browser. Monaco is
bundled in the package, so the page needs no internet access.

When the worktree changes, a "worktree changed" note with a reload button
appears in the top bar within about a second (the view never re-renders by
itself). The server checks the worktrees open pages show and sends what
changed over one stream per browser, shared by its tabs; hidden tabs are
not checked for. It says what
a reload would bring, compared with the diff on screen: the new commits,
and how many changed lines would appear and go, in how many files. After
the reload those changes stay marked, so an agent's follow-up stands out
from what was already reviewed: an orange bar at the left of each new
changed line, an orange line where changed lines went, marks in the
scrollbar and the minimap, and a bar on those files (and their folders) in
the file list. A note in the top bar sums them up; its x clears the
marks, which otherwise stay until the next reload (or another base or
commit). The server keeps what the
marks are measured against in memory, so a reload just after it restarted
has none (the note says so). When gitr-serve restarts or its page
files change, an "app changed" note in the top bar offers to reload the
page, whose code may no longer fit the server's.

Switching to another worktree keeps the one left as it was: switching
back shows the same diff again, at the same place, with an unsaved
comment still open and the marks of its last reload, and the "worktree
changed" note if an agent changed it meanwhile; it changes only when
reload is clicked. The page keeps up to a million lines of such diffs
(the least recently left go first); when the server no longer has one (it
restarted, or many big diffs since), switching back loads the diff anew,
with what changed marked as after a reload.

As in gitr, collapsing re-indented lines shows a removed and added line that differ
only in whitespace as one dimmed `~` line, and the right-click menu copies
the file path, `path:line`, or `path:line` with the line (or the selection
holding it) in a `diff` code fence for pasting into an agent's chat.

Comment on a line with the `+` that appears in the gutter on hover, or
right-click a line for gitr's one-click presets; click a comment to edit it.
Each comment has "accept" (it becomes a good comment ending in "(accepted)",
e.g. once the agent has dealt with it), "copy" (the comment with its location
and line, as "copy for agent" gives them) and "delete". The arrows next to
the comment count jump to the next and previous comment. Comments on files
not in the current diff are listed in a section after its last line. The
icons at the top of the left bar switch the sidebar between the changed
files and a list of all comments by file; clicking a comment scrolls to it,
and its x (on hover) deletes it. The file list's header has collapse all and
expand all, and "Reveal in file list" in the right-click menu shows the
file of that line in the list, opening its folders. "Find..." in the same
menu (or Ctrl+F in the diff) opens Monaco's find, starting from the
selected text. Ctrl+P (or the magnifier in the file list's header, or "Go
to file..." in the right-click menu) opens a list of the changed files to
jump to, as in VS Code, followed by the worktrees of all projects to
switch to: type parts of the name (`srvpy` finds `server.py`, `wtag` the
`agent-1` worktree of `wt`), pick with the arrows and Enter or a click.
The project switcher takes the arrows and Enter too.
The box at the end of each row in the file list marks a file (or all the
files of a folder, or in the list's header all files) as viewed; a folder
folds from its chevron. The same box is in the diff's gutter at each file
header and (in a file of several hunks) each hunk header, where other
lines show the + that adds a comment, and right-clicking a line marks its file or
hunk. Viewed hunks are dimmed, marked grey in the scrollbar and shaded in
the minimap; a half-filled box is a file with some hunks viewed, and the
arrows in the list's header go to the next and previous hunk not viewed
yet. The marks are
kept per hunk in the worktree's `.gitr/viewed.json`, under the hunk's
changed lines rather than its place, so when an agent changes a hunk only
that one comes back unviewed (the reload note counts the viewed files a
reload changed), a hunk that only moved stays viewed, and a hunk viewed in
a single commit is viewed in the whole review too, while it is the same.
The third icon of the left bar shows the commits between the base and
HEAD as a graph, as gitk draws them, with the uncommitted changes and the
untracked files on top, and the base at the bottom, followed by some
older commits, faded, for context (how many is set in the settings popup,
per browser; 0 hides them). They are not part of "All changes", but can be
picked as any other commit.
Clicking one shows only that commit, as `git show` does (a merge against
its first parent), or only the uncommitted changes or untracked files.
Shift-clicking a second row (or the compare icon in the pane's header,
then two clicks) shows everything from one to the other combined, both
included (from the older commit's parent). "Uncommitted changes" and
"Untracked files" can be the newer end (the working tree, without and with
the untracked files), and "All changes" the older one (from the merge
base): shift-clicking from "All changes" to "Untracked files" shows the
whole review with the untracked files. The "All changes" row at the
top of the list, or the x on the note in the top bar, goes back to the
whole review. Right-clicking a commit copies its hash or subject, or makes
it the base, to review only what came after it. Comments can be read and written there too; they are
placed on that commit's version of the file, and the agent export always
describes the whole review and the untracked files (so a comment on a file
an agent created but did not add keeps its line).
Comments are stored in the worktree's `.gitr/review.json`, which `gitr`
and gitr-serve share. "copy for agent"
puts the agent export on the clipboard; an agent in the worktree can read
the same with `gitr export` (or `gitr comments list`, see above). When `review.json` changes from outside the page (another
tab, `gitr comments`, an agent), the comments reload by themselves, a note
in the top bar says how many were added, edited and removed, and the added
and edited ones are flagged; the note's x clears the flags, which otherwise
stay until the next such change or another diff. Keep `.gitr/` out of commits (e.g. in a global gitignore), or
agents may commit it.

## Requirements

Python 3.10+, no other dependencies (the page's Monaco editor is bundled), and a browser.

## Credits

gitr-serve's icons are from [Codicons](https://github.com/microsoft/vscode-codicons)
by Microsoft, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
