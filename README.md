# gitrevue

Lightweight terminal-launched Git diff viewer. Pipe any `git diff` into it and
get a two-panel GUI: coloured diff on the left, clickable file list on the right.

## Install

```bash
uv tool install .
```

## Usage

```bash
git diff main...HEAD | gitrevue          # branch diff vs main
git diff HEAD | gitrevue                 # staged + unstaged vs last commit
git diff --cached | gitrevue             # staged only
git diff HEAD~5 | gitrevue              # last 5 commits
git show HEAD | gitrevue                 # single commit
git diff --first-parent main...HEAD | gitrevue
```

Running `gitrevue` without a pipe prints the above as a reminder.

## Requirements

Python 3.10+, Tkinter (included in most Python distributions). No other dependencies.
