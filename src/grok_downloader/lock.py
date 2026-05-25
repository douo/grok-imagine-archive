from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass
from pathlib import Path


class ArchiveLockError(RuntimeError):
    pass


@dataclass
class ArchiveLock:
    path: Path
    fd: int | None = None

    def __enter__(self) -> "ArchiveLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise ArchiveLockError(f"archive is already being written: {self.path}") from exc
        os.ftruncate(self.fd, 0)
        os.write(self.fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None
