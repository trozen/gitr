# gitr

Lightweight terminal-launched Git diff viewer. Two-panel GUI: coloured diff on the left, clickable file list on the right.

## Install

```bash
uv tool install .
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

## Web version (prototype)

`web/server.py` is a proof of concept of `gitr serve`: the same diff view in
a browser, for several repositories and all their worktrees. It runs where the
repositories are (e.g. a remote machine with agents working in worktrees).

```bash
python3 web/server.py ~/dev/repo-a ~/dev/repo-b    # http://127.0.0.1:8765/
python3 web/server.py --port 9000 ~/dev/repo-a
```

With no repository arguments the list is read from
`~/.config/gitr/projects.json`:

```json
{"projects": ["~/dev/repo-a", {"path": "~/dev/repo-b", "name": "b"}]}
```

The server listens on localhost only and has no authentication, so reach it
from another machine through an SSH tunnel rather than exposing the port:

```bash
ssh -L 8765:127.0.0.1:8765 remote-host    # then open http://127.0.0.1:8765/
```

Pick a worktree on the left; the base defaults to `main` / `master` and can be
any ref. The page loads Monaco from a CDN, so the browser needs internet
access. Comments are not supported in the web version yet.

## Requirements

Python 3.10+, Tkinter (included in most Python distributions). No other dependencies.
