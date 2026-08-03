from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO


class ProcessLock:
    def __init__(self, name: str):
        safe = "".join(character for character in name if character.isalnum() or character in "-_")
        self.path = Path(f".{safe or 'samsara-monitor'}.lock")
        self._file: IO[str] | None = None

    def acquire(self) -> None:
        self._file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._file.close()
            self._file = None
            raise RuntimeError("Another Samsara monitor process already holds the worker lock") from exc

    def release(self) -> None:
        if self._file:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

