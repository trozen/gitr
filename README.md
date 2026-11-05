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
Each comment has "accept" (it becomes a good comment ending in "(accepted)",
e.g. once the agent has dealt with it), "copy" (the comment with its location
and line, as "copy for agent" gives them) and "delete". The arrows next to
the comment count jump to the next and previous comment. Comments on files not in the current diff are
listed in a section after its last line.
Comments are stored in the worktree's `.gitr/review.json`, the same file a
gitr window uses, so a review can move between the two. On every change the
server also writes the agent export to `.gitr/review.md`, so an agent in that
worktree can be told to read it; "copy for agent" puts the same text on the
clipboard. Keep `.gitr/` out of commits (e.g. in a global gitignore), or
agents may commit it.

## Requirements

Python 3.10+, Tkinter (included in most Python distributions). No other dependencies.
