"""herdr's startup hook: `gitr serve --herdr` in the background, its output
in the plugin's state directory. herdr waits for its startup commands to end
and does not supervise what they leave running, so the server runs in a
session of its own and ends itself with herdr's server."""

# so that an older python3 gets as far as the version check in main()
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from http.client import HTTPConnection
from pathlib import Path

HERE = Path(__file__).resolve().parent
# herdr installs a plugin as a clone of its whole repository, so the gitr
# next to the plugin is the one it was released with, and needs no install
CHECKOUT_GITR = HERE.parent / 'gitr.py'
PLUGIN_ID = 'gitr'  # as in herdr-plugin.toml
PORT = 4487  # gitr serve's


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


def running() -> 'dict | None':
    """What the gitr serving the port says of itself, if one does."""
    conn = HTTPConnection('127.0.0.1', PORT, timeout=2)
    try:
        conn.request('GET', '/api/version?probe=1')
        info = json.loads(conn.getresponse().read())
        return info if isinstance(info, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        conn.close()


def root_id(path: Path) -> str:
    # as server.py's root_id: which copy of gitr a server runs
    return hashlib.sha256(str(path).encode()).hexdigest()[:16]


def ours(pid: int) -> bool:
    """Whether the pid is a gitr serve --herdr of this user: what answers
    on the port names it, and is not to be trusted with any other."""
    try:
        if os.stat(f'/proc/{pid}').st_uid != os.getuid():
            return False
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    except OSError:
        return False
    return b'--herdr' in argv and any(b'gitr' in a for a in argv)


def stop_older() -> None:
    """A gitr serve this plugin started from another copy of it (before
    `herdr plugin update`, which installs the new one beside the old) is
    stopped, so that this one starts in its place. One started otherwise is
    left alone."""
    info = running()
    if (not info or info.get('plugin') != PLUGIN_ID or not CHECKOUT_GITR.exists()
            or info.get('root') == root_id(CHECKOUT_GITR.parent)):
        return
    try:
        pid = int(info['pid'])
    except (KeyError, ValueError, TypeError):
        return
    if not ours(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)  # its --reload supervisor ends with it
    except OSError:
        return
    # until the process is gone: it holds the port till it exits, also when
    # too busy to answer
    end = time.monotonic() + 10
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    print(f"gitr: stopped the gitr serve of another copy, gitr {info.get('gitr')}")


def main() -> None:
    if sys.version_info < (3, 10):
        fail(f'gitr needs Python 3.10 or newer, python3 here is {sys.version.split()[0]}')
    state = Path(os.environ.get('HERDR_PLUGIN_STATE_DIR') or HERE)
    state.mkdir(parents=True, exist_ok=True)
    # One start at a time (herdr's hook, the start action and a handover
    # after an update may meet): the next one finds this one's server.
    lock = open(state / 'start.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX)
    stop_older()
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
