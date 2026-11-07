"""Small helpers shared by every module."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


def norm_id(value: str) -> str:
    """Normalise a merchant id (PayPal merchant id or e-mail) so ids can be compared exactly."""
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def _default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, datetime):
        return obj.astimezone(timezone.utc).isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    raise TypeError(f"cannot serialise {type(obj).__name__}")


def to_jsonable(obj: Any) -> Any:
    """Turn Decimals, datetimes, enums and models into plain JSON types."""
    return json.loads(json.dumps(obj, default=_default, ensure_ascii=False))


def canonical_json(obj: Any) -> str:
    """Stable JSON text (sorted keys, no spaces) so hashes never change by accident."""
    return json.dumps(to_jsonable(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_ts(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
