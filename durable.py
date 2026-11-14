"""Durable files for paymesh: append-only logs and atomic replaces, with an fsync policy.

    PAYMESH_FSYNC=safe     (default) chain blocks, checkpoints, wallet seeds: fsync before
                           anyone is answered; the seller's channel log: fsync every 50 ms
    PAYMESH_FSYNC=strict   every write, channel ticks too, is fsynced before it is used
    PAYMESH_FSYNC=off      no fsync (data still survives a killed process, not a power cut)

A killed process loses nothing it wrote: every record is flushed to the OS
before the call that depends on it returns. fsync is about the OS losing its
cache (power cut, OS crash). A record cut off by a crash mid-write is found
and cut away on the next open (`repair_tail`), so the next append starts on a
clean line. Stdlib only.
"""
import os
import threading

POLICY = os.environ.get("PAYMESH_FSYNC", "safe")
if POLICY not in ("safe", "strict", "off"):
    raise ValueError("PAYMESH_FSYNC must be safe, strict or off")
LOG_INTERVAL = 0.05  # seconds between fsyncs of a log under the "safe" policy


def must_sync(critical: bool) -> bool:
    """fsync now? critical: data that money or keys depend on (blocks, seeds, checkpoints)."""
    return POLICY == "strict" or (POLICY == "safe" and critical)


def fsync_file(f):
    f.flush()
    os.fsync(f.fileno())


def atomic_write(path: str, data: bytes, sync: bool = True):
    """Replace path with data: readers see the old file or the new one, never a mix.
    With sync the bytes are on disk before the rename, so a power cut can't leave an empty file."""
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        if sync and POLICY != "off":
            fsync_file(f)
    os.replace(tmp, path)


def repair_tail(path: str, line_ok) -> int:
    """Cut a last line that a crash left unfinished. A complete record whose newline was
    lost gets the newline back (line_ok(bytes) says if a line is a complete record).
    Returns the number of bytes cut (0 when the file was clean)."""
    if not os.path.exists(path):
        return 0
    with open(path, "r+b") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0:
            return 0
        f.seek(size - 1)
        if f.read(1) == b"\n":
            return 0
        back = min(size, 1 << 20)
        while True:  # find the start of the last line (blocks can be big)
            f.seek(size - back)
            chunk = f.read(back)
            nl = chunk.rfind(b"\n")
            if nl >= 0 or back == size:
                break
            back = min(size, back * 4)
        start = size - back + nl + 1 if nl >= 0 else 0
        f.seek(start)
        last = f.read()
        if line_ok(last):
            f.seek(size)
            f.write(b"\n")
            return 0
        f.truncate(start)
        return size - start


class AppendLog:
    """A line log. append() returns once the line is with the OS (it survives a killed
    process). Critical logs fsync each line; others are fsynced by a background thread
    every LOG_INTERVAL s (policy "safe") or never ("off"). No handle stays open between
    appends: on Windows that would lock the file against rename and delete."""

    def __init__(self, path: str, critical: bool = False):
        self.path = path
        self.sync_each = must_sync(critical)
        self.lock = threading.Lock()
        self.dirty = False
        self.closed = threading.Event()
        if not self.sync_each and POLICY == "safe":
            threading.Thread(target=self._flusher, daemon=True).start()

    def append(self, line: str):
        with self.lock:
            with open(self.path, "ab") as f:
                f.write(line.encode("utf-8") + b"\n")
                if self.sync_each:
                    fsync_file(f)
                else:
                    self.dirty = True

    def sync(self):
        with self.lock:
            if self.dirty and os.path.exists(self.path):
                with open(self.path, "ab") as f:  # fsync through any write handle flushes the file
                    fsync_file(f)
            self.dirty = False

    def _flusher(self):
        while not self.closed.wait(LOG_INTERVAL):
            try:
                self.sync()
            except OSError:
                pass

    def reopen(self, rewrite):
        """Let rewrite() replace the file (compaction) with no append in between."""
        with self.lock:
            rewrite()
            self.dirty = False

    def close(self):
        """Stop the flusher after a last fsync."""
        self.closed.set()
        if POLICY != "off":
            self.sync()
