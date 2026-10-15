"""Book lock: one writing process per book. DEC-049.

The night cycle, the day watch and a manual `trade-resume` write to one
and the same book — accounting in the database and the paper exchange file. Without a lock a
manual run during the night one would overwrite one with the other (bot platform
design 2026-09-29: state is saved as a whole row,
the exchange file as a whole file).

`fcntl.flock` on a local file, not a lock in the database: what is protected is
a local file, and the lock must live exactly as long as the process.
A process frozen in sleep holds it; a crashed one releases it by itself (the kernel
drops the flock when the descriptor is closed). A database lock over TCP in Docker
cannot be trusted after sleep.

Acquisition is non-blocking: busy — immediate refusal, the schedule retries in
an hour. Waiting is not an option: at night the Mac has seconds for this.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_DIR = Path("var/live/locks")


class BookBusy(RuntimeError):
    """The book is held by another process."""


@contextmanager
def book_lock(book: str) -> Iterator[None]:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCK_DIR / f"{book.replace(':', '_')}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BookBusy(
                f"book {book} is busy with another process (cycle, watch or trade-resume) — "
                "try again later"
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(fd)  # closing releases the flock


__all__ = ["LOCK_DIR", "BookBusy", "book_lock"]
