"""herdr's startup hook: `gitr serve --herdr` in the background, its output
in the plugin's state directory. herdr waits for its startup commands to end
and does not supervise what they leave running, so the server runs in a
session of its own and ends itself with herdr's server."""

# so that an older python3 gets as far as the version check in main()
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# herdr installs a plugin as a clone of its whole repository, so the gitr
# next to the plugin is the one it was released with, and needs no install
CHECKOUT_GITR = HERE.parent / 'gitr.py'
PLUGIN_ID = 'gitr'  # as in herdr-plugin.toml


def notify(body: str) -> None:
    herdr = os.environ.get('HERDR_BIN_PATH') or 'herdr'
    try:
        subprocess.run([herdr, 'notification', 'show', 'gitr', '--body', body], timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def fail(message: str) -> None:
    notify(message)
    sys.exit(f'gitr: {message}')


def write_launcher() -> None:
    """A `gitr` command for the shell (and the agents' `gitr export`) when
    gitr is not installed: in the plugin's config directory, which, unlike
    the plugin's own, stays put across updates, and pointed at this copy on
    every start. The user puts its directory on PATH."""
    config = os.environ.get('HERDR_PLUGIN_CONFIG_DIR')
    if not config:
        return
    launcher = Path(config) / 'bin' / 'gitr'
    try:
        launcher.parent.mkdir(parents=True, exist_ok=True)
        tmp = launcher.with_name('.gitr.tmp')
        tmp.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(CHECKOUT_GITR))} "$@"\n')
        tmp.chmod(0o755)
        tmp.replace(launcher)
    except OSError as e:
        print(f'gitr: cannot write {launcher}: {e}', file=sys.stderr)


def command() -> list[str]:
    if CHECKOUT_GITR.exists():
        write_launcher()
        return [sys.executable, str(CHECKOUT_GITR)]
    # herdr's server may not have the login shell's PATH, where uv tool and
    # pipx put their commands
    gitr = shutil.which('gitr') or str(Path.home() / '.local' / 'bin' / 'gitr')
    if not os.access(gitr, os.X_OK):
        fail('gitr not found; install it with: uv tool install gitr-review')
    return [gitr]


def main() -> None:
    if sys.version_info < (3, 10):
        fail(f'gitr needs Python 3.10 or newer, python3 here is {sys.version.split()[0]}')
    state = Path(os.environ.get('HERDR_PLUGIN_STATE_DIR') or HERE)
    state.mkdir(parents=True, exist_ok=True)
    log = state / 'gitr-serve.log'
    with open(log, 'a') as out:
        try:
            # --reload: a linked plugin (a checkout being worked on) restarts
            # on changes to gitr; an installed one never changes in place
            proc = subprocess.Popen([*command(), 'serve', '--herdr', '--reload'], stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                                    env=dict(os.environ, GITR_HERDR_PLUGIN=PLUGIN_ID))
        except OSError as e:
            fail(f'cannot start gitr: {e}')
    # what goes wrong goes wrong at once (the port taken by something else,
    # herdr's socket not found); later it would only be in the log
    try:
        code = proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        print(f'gitr serve started, its output in {log}')
        return
    if code:
        lines = log.read_text(errors='replace').strip().splitlines()
        fail(f"gitr serve did not start: {lines[-1] if lines else f'exit {code}'} (log: {log})")
    print(f'gitr serve: one runs already, see {log}')


main()
