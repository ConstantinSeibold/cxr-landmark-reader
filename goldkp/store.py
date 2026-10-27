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


class ReaderStore:
    def __init__(self, root: Path, rid: str, cards_sha: str, prefill_version: str,
                 allow_version_change: bool = False) -> None:
        self.dir = root / rid
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = self.dir / "decisions.jsonl"
        self.state_path = self.dir / "state.json"
        self.rid = rid
        self.lock = threading.Lock()
        self.state = self._load()
        versions = {"cards_sha": cards_sha, "prefill_version": prefill_version}
        old = {k: self.state.get(k) for k in versions}
        if self.state["latest"] and old != versions and not allow_version_change:
            raise VersionMismatch(f"reader {rid}: stored {old} != current {versions}")
        self.state.update(versions)

    def _load(self) -> dict:
        try:
            st = json.load(open(self.state_path))
            if isinstance(st.get("latest"), dict):
                return st
        except (OSError, ValueError):
            pass
        return self.rebuild()

    def rebuild(self) -> dict:
        """State from the JSONL alone (last record per card wins)."""
        st = {"rid": self.rid, "cursor": 0, "latest": {}}
        if self.log.exists():
            for line in open(self.log):
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
        return st

    def append(self, rec: dict) -> None:
        with self.lock:
            with open(self.log, "a") as f:
                f.write(json.dumps(rec) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.state["latest"][rec["card_id"]] = rec
            self.state["cursor"] = max(self.state["cursor"], rec["pos"] + 1)
            write_json_atomic(self.state_path, self.state)

    def latest(self, card_id: str) -> dict | None:
        return self.state["latest"].get(card_id)

    @property
    def done(self) -> int:
        return len(self.state["latest"])
