"""Per-reader storage: append-only decisions.jsonl (the record) + state.json (latest per card,
cursor, versions), rebuilt from the JSONL when missing or corrupt."""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:                                   # Windows: one server per study, no import-reader while it runs
    fcntl = None


def write_json_atomic(path: Path, obj) -> None:
    """Write to a unique temp file, then os.replace, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class VersionMismatch(RuntimeError):
    pass


def log_stamp(log: Path) -> list:
    """Size and file identity of the log: a log replaced by import-reader is noticed even when the
    new file happens to have the old size."""
    if not log.exists():
        return [0, 0]
    s = log.stat()
    return [s.st_size, s.st_ino]


@contextmanager
def reader_lock(reader_dir: Path):
    """Exclusive lock on one reader's files across processes (server, import-reader)."""
    reader_dir.mkdir(parents=True, exist_ok=True)
    with open(reader_dir / ".lock", "a") as f:
        locked = False
        if fcntl:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
                locked = True
            except OSError:                           # e.g. a network or sync mount without flock
                _warn_unlocked(reader_dir)
        try:
            yield
        finally:
            if locked:
                fcntl.flock(f, fcntl.LOCK_UN)


_warned: set = set()


def _warn_unlocked(reader_dir: Path) -> None:
    if reader_dir not in _warned:
        _warned.add(reader_dir)
        print(f"warning: no file lock on {reader_dir}; do not run import-reader while the server runs",
              file=sys.stderr)


def rebuild_state(log: Path, rid: str) -> dict:
    """State from the JSONL alone (last record per card wins; unreadable lines, e.g. a line torn by
    a crash, are skipped)."""
    st = {"rid": rid, "cursor": 0, "latest": {}}
    if log.exists():
        for line in open(log):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            st["latest"][rec["card_id"]] = rec
            st["cursor"] = max(st["cursor"], rec.get("pos", -1) + 1)
            st["cards_sha"], st["prefill_version"] = rec.get("cards_sha"), rec.get("prefill_version")
    st["log_stamp"] = log_stamp(log)
    return st


def load_state(reader_dir: Path) -> dict:
    """The reader's current state: state.json when it matches the log, else rebuilt from the log
    (state.json missing, corrupt, or older than the log, e.g. after import-reader)."""
    log, path = reader_dir / "decisions.jsonl", reader_dir / "state.json"
    try:
        st = json.load(open(path))
        if isinstance(st.get("latest"), dict) and st.get("log_stamp") == log_stamp(log):
            return st
    except (OSError, ValueError):
        pass
    return rebuild_state(log, reader_dir.name)


class ReaderStore:
    def __init__(self, root: Path, rid: str, cards_sha: str, prefill_version: str,
                 allow_version_change: bool = False) -> None:
        self.dir = root / rid
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = self.dir / "decisions.jsonl"
        self.state_path = self.dir / "state.json"
        self.rid = rid
        self.lock = threading.Lock()
        self.state = load_state(self.dir)
        self.versions = {"cards_sha": cards_sha, "prefill_version": prefill_version}
        old = {k: self.state.get(k) for k in self.versions}
        if self.state["latest"] and old != self.versions and not allow_version_change:
            raise VersionMismatch(f"reader {rid}: stored {old} != current {self.versions}")
        self.state.update(self.versions)
        if self.log.exists():                               # a rebuilt state is on disk for export at once
            write_json_atomic(self.state_path, self.state)

    def rebuild(self) -> dict:
        return rebuild_state(self.log, self.rid)

    def sync(self) -> None:
        """Reload when the log changed behind this process (import-reader while the server runs), so a
        later submit never writes a stale state over the imported one."""
        with self.lock, reader_lock(self.dir):
            self._sync()

    def _sync(self) -> None:
        if self.state.get("log_stamp") != log_stamp(self.log):
            self.state = {**self.rebuild(), **self.versions}
            write_json_atomic(self.state_path, self.state)

    def append(self, rec: dict) -> None:
        with self.lock, reader_lock(self.dir):
            self._sync()
            with open(self.log, "ab") as f:
                # a line torn by a crash has no newline; end it so the new record stays a line of its own
                if f.tell() > 0:
                    with open(self.log, "rb") as r:
                        r.seek(-1, os.SEEK_END)
                        if r.read(1) != b"\n":
                            f.write(b"\n")
                f.write((json.dumps(rec) + "\n").encode())
                f.flush()
                os.fsync(f.fileno())
            self.state["latest"][rec["card_id"]] = rec
            self.state["cursor"] = max(self.state["cursor"], rec["pos"] + 1)
            self.state["log_stamp"] = log_stamp(self.log)
            write_json_atomic(self.state_path, self.state)

    def latest(self, card_id: str) -> dict | None:
        return self.state["latest"].get(card_id)

    @property
    def done(self) -> int:
        return len(self.state["latest"])
