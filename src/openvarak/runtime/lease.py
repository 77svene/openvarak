"""OS-owned lease: process death releases ownership without unsafe TTL guesses."""

from pathlib import Path


class RuntimeLease:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        handle = self.path.open("a+b")
        try:
            if __import__("os").name == "nt":
                import msvcrt

                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError(
                "Another desktop runtime owns this state directory"
            ) from None
        self.handle = handle

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None
