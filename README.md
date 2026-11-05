# gitr

Lightweight terminal-launched Git diff viewer. Two-panel GUI: coloured diff on the left, clickable file list on the right.

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
gitr                         # git diff (unstaged changes)
gitr master                  # git diff master (to working tree)
gitr --merge-base master     # diff from common ancestor to working tree
gitr master HEAD             # git diff master HEAD (committed only)

git diff | gitr              # pipe a patch
gitr -                       # read stdin explicitly
gitr -p patch.diff           # read from a patch file
```

## gitr-serve

`gitr-serve` is the same diff view and review in a browser, for several
repositories and all their worktrees. It runs where the repositories are
(e.g. a remote machine with agents working in worktrees), in the foreground,
and needs no Tk.

```bash
gitr-serve ~/dev/repo-a ~/dev/repo-b         # http://127.0.0.1:4487/
gitr-serve --port 9000 ~/dev/repo-a
```

From a checkout, without installing: `python3 gitr_web/server.py`, with
`--reload` to restart on changes to `gitr_web/server.py` or `gitr.py`. The
page files under `gitr_web/static/` are read from disk on every request, so
changes to them only need a browser refresh. The Monaco bundle under
`gitr_web/static/monaco/` is built and committed; to move to another Monaco
version, change it in `monaco/package.json` and run
`cd monaco && npm install && npm run build` (Node is needed for that only).

With no repository arguments the list is read from
`~/.config/gitr/projects.json`:

```json
{"projects": ["~/dev/repo-a", {"path": "~/dev/repo-b", "name": "b"}]}
```

`gitr-serve --print-config [repo ...]` prints one for the repos given (or the
one you are in), ready to redirect into that file.

The server listens on localhost only and has no authentication, so reach it
from another machine through an SSH tunnel rather than exposing the port:

```bash
ssh -L 4487:127.0.0.1:4487 remote-host    # then open http://127.0.0.1:4487/
```

Chrome can install the page as an app with its own window (the install icon
in the address bar, or menu > Cast, save and share > Install page). The app
belongs to the address it was installed from, port included, so keep the
tunnel on the same local port. To install apps from two machines, reach them
on different local ports (e.g. `ssh -L 4488:127.0.0.1:4487 remote-host` for
the remote one); each app is named after its server's hostname.

Pick a worktree on the left; the base defaults to `main` / `master` and can be
any ref. The base and the untracked / wrap options are remembered per
worktree on the server (`~/.local/state/gitr/web.json`); a worktree opened
for the first time starts from the ones last used in its project. Monaco is
bundled in the package, so the page needs no internet access.

As in gitr, "collapse re-indent" shows a removed and added line that differ
only in whitespace as one dimmed `~` line, and the right-click menu copies
the file path, `path:line`, or `path:line` with the line (or the selection
holding it) in a `diff` code fence for pasting into an agent's chat.

Comment on a line with the `+` that appears in the gutter on hover, or
right-click a line for gitr's one-click presets; click a comment to edit it.
Comments are stored in the worktree's `.gitr/review.json`, the same file a
gitr window uses, so a review can move between the two. On every change the
server also writes the agent export to `.gitr/review.md`, so an agent in that
worktree can be told to read it; "copy for agent" puts the same text on the
clipboard. Keep `.gitr/` out of commits (e.g. in a global gitignore), or
agents may commit it.

## Requirements

Python 3.10+, Tkinter (included in most Python distributions). No other dependencies.
