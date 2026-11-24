"""A throwaway git repository for the tests, isolated from the user's git
configuration (signing, hooks, default branch names)."""

import os
import subprocess
import tempfile
from pathlib import Path

# set for the git the tests and the code under test run
GIT_ENV = {
    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_AUTHOR_NAME': 'gitr test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
    'GIT_COMMITTER_NAME': 'gitr test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
}


class GitEnv:
    """os.environ with GIT_ENV for the length of a test class."""

    saved: dict = {}

    @classmethod
    def enter(cls) -> None:
        cls.saved = {k: os.environ.get(k) for k in GIT_ENV}
        os.environ.update(GIT_ENV)

    @classmethod
    def leave(cls) -> None:
        for k, v in cls.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Repo:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.realpath(self._tmp.name)
        self.git('init', '-q', '-b', 'main')

    def close(self) -> None:
        self._tmp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(['git', *args], cwd=self.path, check=True,
                              capture_output=True, text=True).stdout

    def write(self, rel: str, text: str) -> None:
        p = Path(self.path) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def read(self, rel: str) -> str:
        return (Path(self.path) / rel).read_text()

    def commit(self, message: str) -> str:
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD').strip()
