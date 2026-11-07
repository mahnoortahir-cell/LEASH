"""Settings come from environment variables (and an optional .env file in the folder you run from)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def audit_key() -> Optional[str]:
    return os.environ.get("LEASH_AUDIT_KEY") or None


def paypal_client():
    from .paypal_api import PayPalClient

    client_id = os.environ.get("PAYPAL_CLIENT_ID")
    secret = os.environ.get("PAYPAL_CLIENT_SECRET")
    if not client_id or not secret:
        raise SystemExit("Missing PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET.\n"
                         "Copy .env.example to .env and fill in the keys from your PayPal sandbox app.")
    return PayPalClient(
        client_id, secret, base_url=os.environ.get("PAYPAL_BASE_URL") or PayPalClient.SANDBOX_URL,
        return_url=os.environ.get("PAYPAL_RETURN_URL") or "https://example.com/return",
        cancel_url=os.environ.get("PAYPAL_CANCEL_URL") or "https://example.com/cancel")


def load_merchants(path: str = "merchants.json") -> dict[str, list[str]]:
    """A simple file: {"Pizza Palace": ["pizza@business.example.com"], ...}"""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"I could not find {path}. Copy merchants.example.json to {path} and edit it.")
    data = json.loads(p.read_text(encoding="utf-8"))
    return {name: ([ids] if isinstance(ids, str) else list(ids)) for name, ids in data.items()}
