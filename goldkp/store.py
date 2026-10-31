"""Per-reader storage: append-only decisions.jsonl (the record) + state.json (latest per card,
cursor, versions), rebuilt from the JSONL when missing or corrupt."""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path


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


def log_size(log: Path) -> int:
    return log.stat().st_size if log.exists() else 0


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
    st["log_size"] = log_size(log)
    return st


def load_state(reader_dir: Path) -> dict:
    """The reader's current state: state.json when it matches the log, else rebuilt from the log
    (state.json missing, corrupt, or older than the log, e.g. after import-reader)."""
    log, path = reader_dir / "decisions.jsonl", reader_dir / "state.json"
    try:
        st = json.load(open(path))
        if isinstance(st.get("latest"), dict) and st.get("log_size") == log_size(log):
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
        with self.lock:
            self._sync()

    def _sync(self) -> None:
        if self.state.get("log_size") != log_size(self.log):
            self.state = {**self.rebuild(), **self.versions}
            write_json_atomic(self.state_path, self.state)

    def append(self, rec: dict) -> None:
        with self.lock:
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
            self.state["log_size"] = log_size(self.log)
            write_json_atomic(self.state_path, self.state)

    def latest(self, card_id: str) -> dict | None:
        return self.state["latest"].get(card_id)

    @property
    def done(self) -> int:
        return len(self.state["latest"])
