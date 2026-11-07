"""A tamper-evident diary of everything that happens.

Every entry carries the fingerprint of the entry before it (a "hash chain"). Change, delete or
re-order any past entry and the chain breaks, and verify() tells you where.

Be honest about what this is: it is tamper-EVIDENT, not tamper-PROOF.
* Without a key, anyone who can edit the file could rebuild the whole chain. Set LEASH_AUDIT_KEY
  and entries are signed with HMAC, so only someone with the key can rebuild it.
* Cutting entries off the END of the file is not visible from inside the file. Write down (or
  publish) head() now and then; it proves how long the log was at that moment.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union

from pydantic import BaseModel

from .util import canonical_json, to_jsonable, utcnow

GENESIS = "0" * 64


class AuditError(Exception):
    pass


class AuditEvent(BaseModel):
    seq: int
    ts: str
    type: str
    data: dict[str, Any]
    prev_hash: str
    hash: str


@dataclass
class VerifyReport:
    ok: bool
    count: int
    head: str
    broken_at: Optional[int] = None
    reason: str = ""
    signed: bool = False


def _as_key(key: Union[str, bytes, None]) -> Optional[bytes]:
    if key is None or key == "" or key == b"":
        return None
    return key.encode() if isinstance(key, str) else key


class AuditLog:
    def __init__(self, path: Union[str, Path, None] = None, *, key: Union[str, bytes, None] = None,
                 clock: Callable = utcnow, fsync: bool = True):
        self.path = Path(path) if path else None
        self._fsync = fsync  # True = force every entry onto the disk (safest); tests turn it off for speed
        self._key = _as_key(key)
        self._clock = clock
        self._lock = threading.RLock()
        self._events: list[AuditEvent] = []
        if self.path and self.path.exists():
            self._load()

    @property
    def signed(self) -> bool:
        return self._key is not None

    def _load(self) -> None:
        assert self.path is not None
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                self._events.append(AuditEvent.model_validate(json.loads(line)))
            except (ValueError, TypeError) as exc:
                raise AuditError(f"{self.path}: line {number} is not a valid audit entry ({exc})") from exc

    @staticmethod
    def _body(seq: int, ts: str, type_: str, data: dict, prev_hash: str) -> str:
        return canonical_json({"seq": seq, "ts": ts, "type": type_, "data": data, "prev_hash": prev_hash})

    @staticmethod
    def _digest(body: str, key: Optional[bytes]) -> str:
        if key:
            return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def head(self) -> str:
        with self._lock:
            return self._events[-1].hash if self._events else GENESIS

    def events(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)

    def append(self, type_: str, data: dict[str, Any]) -> AuditEvent:
        with self._lock:
            clean = to_jsonable(data)  # exactly what will be stored, so hashes always match later
            seq = len(self._events)
            ts = self._clock().isoformat()
            prev = self.head()
            event = AuditEvent(seq=seq, ts=ts, type=type_, data=clean, prev_hash=prev,
                               hash=self._digest(self._body(seq, ts, type_, clean, prev), self._key))
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.model_dump(), ensure_ascii=False, separators=(",", ":")) + "\n")
                    fh.flush()
                    if self._fsync:
                        os.fsync(fh.fileno())
            self._events.append(event)
            return event

    def verify(self) -> VerifyReport:
        with self._lock:
            return self._verify(self._events, self._key)

    @staticmethod
    def _verify(events: list[AuditEvent], key: Optional[bytes]) -> VerifyReport:
        prev = GENESIS
        for index, ev in enumerate(events):
            if ev.seq != index:
                return VerifyReport(False, len(events), prev, index, "an entry is missing or out of order", key is not None)
            if ev.prev_hash != prev:
                return VerifyReport(False, len(events), prev, index, "the link to the previous entry is broken", key is not None)
            expected = AuditLog._digest(AuditLog._body(ev.seq, ev.ts, ev.type, ev.data, ev.prev_hash), key)
            if not hmac.compare_digest(expected, ev.hash):
                return VerifyReport(False, len(events), prev, index,
                                    "this entry was edited (or the signing key is wrong)", key is not None)
            prev = ev.hash
        return VerifyReport(True, len(events), prev, None, "", key is not None)

    @classmethod
    def verify_file(cls, path: Union[str, Path], *, key: Union[str, bytes, None] = None) -> VerifyReport:
        log = cls(path, key=key)
        return log.verify()
