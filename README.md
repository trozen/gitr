# gitrevue

Lightweight terminal-launched Git diff viewer. Two-panel GUI: coloured diff on the left, clickable file list on the right.

## Install

```bash
uv tool install .
```

## Usage

```bash
gitrevue                         # git diff (unstaged changes)
gitrevue master                  # git diff master (to working tree)
gitrevue --merge-base master     # diff from common ancestor to working tree
gitrevue master HEAD             # git diff master HEAD (committed only)

git diff | gitrevue              # pipe a patch
gitrevue -                       # read stdin explicitly
gitrevue -p patch.diff           # read from a patch file
```

## Requirements

Python 3.10+, Tkinter (included in most Python distributions). No other dependencies.
