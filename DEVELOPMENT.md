# Developing gitr

## From a checkout

From a checkout, without installing: `python3 gitr_web/server.py`, with
`--reload` to restart on changes to `gitr_web/server.py`, `gitr.py` or
`~/.config/gitr/projects.json`. The page files under `gitr_web/static/` are
read from disk on every request, so changes to them only need a browser
refresh. Tests: `uv run pytest` (or `python3 -m unittest discover -s tests
-t .`). The Monaco bundle under `gitr_web/static/monaco/` is built and
committed; to move to another Monaco version, change it in
`monaco/package.json` and run `cd monaco && npm install && npm run build`
(Node is needed for that only).

## The herdr plugin

Link the plugin from the checkout instead of installing it (from the
repository root):

```bash
herdr plugin link herdr-plugin
```

A linked plugin runs the checkout's gitr, with `--reload`, so the server
restarts on changes to the Python files as above. herdr runs the plugin's
startup hook when its server starts; to start the server without
restarting herdr, run the hook once by hand: `python3 herdr-plugin/start.py`
(its log then goes next to it rather than to herdr's state directory for
the plugin, `~/.local/state/herdr/plugins/gitr/gitr-serve.log`).
`herdr plugin list` shows the plugin, `herdr plugin log list` the hook's
runs.
