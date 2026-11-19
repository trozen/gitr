# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`gitr` is a Git diff viewer for reviewing coding agents' changes, in the browser: a stdlib-only Python 3.10+ server and a Monaco-based page. It is installed as a CLI tool via `uv`. (It began as a Tkinter window, since removed.)

## Commands

```bash
# Install / reinstall after changes
uv tool install --reinstall .

# Use it
gitr                        # this repo's review in a window (standalone server, Chrome app mode)
gitr master                 # ... against master
gitr --browser list         # which browser opens it (--browser NAME|default|none, $GITR_BROWSER)
gitr export master          # review comments as text for a coding agent
gitr export --kinds bad,note   # only these kinds
gitr comments list|add|rm|accept ...   # read and write comments
gitr serve [repos]          # gitr-serve: many repos and worktrees
```

No test suite yet. Syntax check:
```bash
python3 -c "import ast; ast.parse(open('gitr.py').read())"
```

## Architecture

- `gitr.py`: the command line (`main()` dispatches to the window, `export`, `comments`, `serve`) and what the server shares with it: `parse_diff()` (raw diff to `DiffFile`s of `DiffLine(text, kind)`, `kind` one of `added | removed | context | hunk | fileheader`), `ReviewStore` (comments in `.gitr/review.json`, anchored to snapshots of the file in `.gitr/snapshots/`, remapped by difflib on read), `ViewedStore` (`.gitr/viewed.json`), the anchor matching and the agent export (`_format_export`), and the word-diff line pairing.
- `gitr_web/server.py`: a stdlib HTTP server serving the page (`static/`). `main()` is gitr-serve, for many repos and their worktrees; `standalone()` is `gitr`, one repo on a port of its own, opened in a Chromium-family browser's app mode in a profile of gitr's own, ending on Ctrl+C (which tells the pages, so `gitr`'s windows close) or when the last page dropped its `/api/events` stream (after a short wait if the page was just asked for again: a reload). That stream (one per browser, held by the shared worker `static/events.js` for all its tabs) carries the changes: the pages post what they show to `/api/watch`, and `Events.run` checks those views and pushes what changed; the page's polls only stand in while it is down. The whole diff is one Monaco document plus per-line metadata (`Doc`); word-diff ranges are fetched in windows. Run from a checkout with `python3 gitr_web/server.py --reload`.

## Constraints

- **No non-ASCII characters** anywhere in the source (no Unicode arrows, box-drawing chars, etc.).
- No dependencies beyond stdlib. Do not add packages. The page uses Monaco, bundled by `monaco/build.sh` into `gitr_web/static/monaco/`, which is generated and committed: do not edit it by hand.
- Colour scheme is Dracula (the page's CSS variables in `app.css`).
