"""Turn the audit log into plain, screen-friendly summaries (used by the dashboard and its tests)."""
from __future__ import annotations

from typing import Any

from .audit import AuditEvent
from .evidence import summarise_event
from .util import norm_id

REASON_LABELS = {
    "ok": "Inside all of your rules",
    "no_mandate": "No rules have been set yet",
    "kill_switch_on": "The kill switch is on",
    "mandate_not_started": "Your rules have not started yet",
    "mandate_expired": "Your rules have expired",
    "invalid_request": "The request was malformed",
    "unverifiable_order": "Could not check this order at PayPal",
    "unsupported_order": "This kind of order is not supported",
    "order_not_payable": "This order cannot be paid any more",
    "duplicate_order": "This order is already being paid",
    "order_changed_after_approval": "The order changed after you saw it, so nothing was charged",
    "claim_does_not_match_paypal": "The agent's story does not match PayPal's record",
    "currency_mismatch": "Wrong currency",
    "merchant_not_allowed": "This merchant is not on your list",
    "lookalike_merchant": "Looks like a fake of an allowed merchant",
    "over_per_purchase_cap": "Over your per-purchase limit",
    "over_period_budget": "It would break your budget",
    "too_many_purchases_today": "Too many purchases today",
    "needs_owner_approval": "A big purchase: it needs your OK",
    "split_purchase_suspected": "Looks like one big purchase split into pieces",
    "possible_duplicate": "Possible duplicate payment",
}


def reason_label(code: str) -> str:
    return REASON_LABELS.get(code, code.replace("_", " "))


def request_cards(events: list[AuditEvent], limit: int = 100) -> list[dict[str, Any]]:
    """One card per purchase request, newest first. Text from the agent is passed through untouched:
    the page must (and does) show it as plain text, never as HTML."""
    cards: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for ev in events:
        data = ev.data
        rid = data.get("request_id")
        if rid is None:
            continue
        card = cards.get(rid)
        if card is None:
            card = cards[rid] = {
                "request_id": rid, "ts": ev.ts, "status": "in_progress", "agent_id": "", "order_id": "",
                "agent_said": {}, "paypal_said": None, "verdict": None, "reasons": [], "messages": [],
                "approved_flags": [], "capture_id": None, "approval_url": None, "description": "", "detail": "",
            }
            order.append(rid)
        if ev.type == "purchase_requested":
            r = data["request"]
            card.update(agent_id=r.get("agent_id", ""), order_id=r.get("order_id", ""), description=r.get("description", ""),
                        agent_said={"merchant": r.get("claimed_merchant"), "amount": r.get("claimed_amount"),
                                    "currency": r.get("claimed_currency")})
        elif ev.type == "order_verified":
            o = data["order"]
            card["paypal_said"] = {"amount": o.get("amount"), "currency": o.get("currency"),
                                   "payees": list(o.get("payee_ids", [])), "status": o.get("status")}
        elif ev.type == "policy_decision":
            d = data["decision"]
            card.update(verdict=d["verdict"], reasons=d["reasons"], messages=d["messages"],
                        approved_flags=d.get("approved_flags", []))
        elif ev.type == "order_captured":
            card["capture_id"] = data.get("capture_id")
        elif ev.type == "purchase_result":
            res = data["result"]
            card.update(status=res["status"], detail=res.get("detail", ""), approval_url=res.get("approval_url"))
        elif ev.type == "request_invalid":
            card.update(status="blocked", reasons=["invalid_request"], messages=[data.get("problems", "")], agent_id="unknown")
    out = []
    for rid in order[-limit:][::-1]:
        card = cards[rid]
        card["reason_labels"] = [reason_label(r) for r in card["reasons"]]
        card["mismatch"] = _mismatch(card)
        out.append(card)
    return out


def _mismatch(card: dict[str, Any]) -> bool:
    said, real = card.get("agent_said") or {}, card.get("paypal_said")
    if not real:
        return False
    if said.get("merchant") and norm_id(said["merchant"]) not in {norm_id(p) for p in real["payees"]}:
        return True
    return bool(said.get("amount") and real.get("amount") and said["amount"] != real["amount"])


def event_rows(events: list[AuditEvent], limit: int = 500) -> list[dict[str, Any]]:
    return [{"seq": e.seq, "ts": e.ts, "type": e.type, "summary": summarise_event(e), "hash": e.hash[:12]}
            for e in events[-limit:]]
