"""The Guard: the ONLY thing that may move money. The AI agent never holds PayPal credentials.

Flow for one purchase:
  agent asks  ->  Leash asks PayPal "what is this order really?"  ->  rules check  ->
  (owner says yes, if needed)  ->  budget reserved  ->  buyer approves at PayPal  ->
  Leash checks the order did NOT change  ->  payment captured  ->  everything written to the log.

All state is rebuilt from the audit log when a Guard starts, so a restart cannot reset the budget.
"""
from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Optional, Union

from pydantic import ValidationError

from . import policy
from .audit import AuditEvent, AuditLog
from .models import (Decision, Mandate, OrderFacts, PurchaseRequest, PurchaseResult, Reason, Status, Verdict)
from .paypal_api import PayPalError, PayPalLike
from .policy import SpendRecord
from .util import canonical_json, norm_id, parse_ts, utcnow


class GuardError(Exception):
    pass


class Guard:
    ORDER_TTL = timedelta(hours=6)  # PayPal gives the buyer about 6 hours to approve an order

    def __init__(self, paypal: PayPalLike, audit: AuditLog, *, mandate: Optional[Mandate] = None,
                 clock: Callable[[], datetime] = utcnow,
                 buyer_approver: Optional[Callable[[str], None]] = None,
                 lockout_after: int = 5, lockout_window: timedelta = timedelta(minutes=10)):
        self.paypal = paypal
        self.audit = audit
        self.clock = clock
        # buyer_approver exists for the demo/mock only: it plays the human clicking "Pay" at PayPal.
        self.buyer_approver = buyer_approver
        self.lockout_after = lockout_after
        self.lockout_window = lockout_window
        self._lock = threading.RLock()
        self.mandate: Optional[Mandate] = None
        self.killed = False
        self._requests: dict[str, PurchaseRequest] = {}
        self._results: dict[str, PurchaseResult] = {}
        self._verified: dict[str, dict[str, Any]] = {}   # the last order facts Leash saw for each request
        self._pending: dict[str, Optional[dict[str, Any]]] = {}  # waiting for the owner -> the order they were SHOWN
        self._spend: dict[str, SpendRecord] = {}
        self._violations: list[datetime] = []
        for event in self.audit.events():  # rebuild memory from the diary
            self._apply(event)
        if mandate is not None and (self.mandate is None or self.mandate.fingerprint() != mandate.fingerprint()):
            self.set_mandate(mandate)

    # ------------------------------------------------------------------ state from events
    def _emit(self, type_: str, data: dict[str, Any]) -> AuditEvent:
        event = self.audit.append(type_, data)
        self._apply(event)
        return event

    def _apply(self, ev: AuditEvent) -> None:
        kind, d = ev.type, ev.data
        if kind == "mandate_set":
            self.mandate = Mandate.model_validate(d["mandate"])
        elif kind == "kill_switch_engaged":
            self.killed = True
        elif kind == "kill_switch_released":
            self.killed = False
            self._violations.clear()
        elif kind == "purchase_requested":
            self._requests[d["request_id"]] = PurchaseRequest.model_validate(d["request"])
        elif kind == "request_invalid":
            self._violations.append(parse_ts(ev.ts))
        elif kind == "policy_decision":
            if d["decision"]["verdict"] == Verdict.BLOCK.value and not d.get("human_approved"):
                self._violations.append(parse_ts(ev.ts))
        elif kind == "order_verified":
            self._verified[d["request_id"]] = d["order"]
        elif kind == "approval_requested":
            self._pending[d["request_id"]] = self._verified.get(d["request_id"])
        elif kind in ("human_approved", "human_rejected"):
            self._pending.pop(d["request_id"], None)
        elif kind == "payment_authorized":
            order = d["order"]
            self._spend[d["request_id"]] = SpendRecord(
                request_id=d["request_id"], order_id=order["order_id"],
                payee_ids=frozenset(norm_id(p) for p in order["payee_ids"]),
                amount=Decimal(order["amount"]), currency=order["currency"], at=parse_ts(ev.ts), state="pending")
        elif kind == "order_captured":
            rec = self._spend.get(d["request_id"])
            if rec is not None:
                self._spend[d["request_id"]] = replace(rec, state="committed")
        elif kind == "order_released":
            self._spend.pop(d["request_id"], None)
        elif kind == "purchase_result":
            self._results[d["request_id"]] = PurchaseResult.model_validate(d["result"])

    def _ledger(self, now: datetime, exclude: Optional[str] = None) -> list[SpendRecord]:
        out = []
        for rid, rec in self._spend.items():
            if rid == exclude:
                continue
            if rec.state == "pending" and now - rec.at > self.ORDER_TTL:
                continue  # never approved at PayPal, so it has expired and holds no money
            out.append(rec)
        return out

    # ------------------------------------------------------------------ owner controls
    def set_mandate(self, mandate: Mandate, by: str = "owner") -> None:
        with self._lock:
            self._emit("mandate_set", {"mandate": mandate, "fingerprint": mandate.fingerprint(), "set_by": by})

    def engage_kill_switch(self, reason: str = "owner pressed the kill switch", by: str = "owner") -> None:
        with self._lock:
            if not self.killed:
                self._emit("kill_switch_engaged", {"reason": reason, "by": by})

    def release_kill_switch(self, by: str = "owner") -> None:
        with self._lock:
            if self.killed:
                self._emit("kill_switch_released", {"by": by})

    # ------------------------------------------------------------------ the agent's door
    def pay(self, request: Union[PurchaseRequest, dict[str, Any]]) -> PurchaseResult:
        with self._lock:
            req = self._coerce(request)
            if isinstance(req, PurchaseResult):
                return req
            if self.mandate is None:
                return self._blocked_without_log(req, Reason.NO_MANDATE, "No mandate has been set.")
            if req.request_id in self._requests:  # same request twice -> same answer, no second charge
                self._emit("duplicate_request_ignored", {"request_id": req.request_id})
                return self._results.get(req.request_id) or self._interrupted(req)
            self._emit("purchase_requested", {"request_id": req.request_id, "request": req})
            facts = self._lookup(req)
            decision = self._decide(req, facts, human_approved=False)
            self._emit("policy_decision", {"request_id": req.request_id, "decision": decision, "human_approved": False})
            if decision.verdict == Verdict.BLOCK:
                self._maybe_lockout()
                return self._finish(req, Status.BLOCKED, decision, detail=" ".join(decision.messages))
            if decision.verdict == Verdict.ESCALATE:
                self._emit("approval_requested", {"request_id": req.request_id, "reasons": decision.reasons})
                return self._finish(req, Status.NEEDS_OWNER_APPROVAL, decision, order_id=req.order_id,
                                    detail="Waiting for the owner to say yes: " + " ".join(decision.messages))
            return self._authorize(req, decision, facts)

    # ------------------------------------------------------------------ owner answers
    def approve(self, request_id: str, approver: str = "owner") -> PurchaseResult:
        """Owner says yes to a 'soft' flag. This can never override a hard limit."""
        with self._lock:
            req = self._need_pending(request_id)
            shown = self._pending.get(request_id)  # exactly what the owner was looking at when they said yes
            self._emit("human_approved", {"request_id": request_id, "approver": approver})
            facts = self._lookup(req)
            change = self._changed_since(shown, facts) if (shown is not None and facts is not None) else None
            if change:
                blocked = Decision(
                    verdict=Verdict.BLOCK, reasons=[Reason.ORDER_CHANGED], checked_at=self.clock(),
                    messages=[f"The order changed while it waited for your OK ({change}). Your yes was for the "
                              f"order you saw, so nothing was authorised. Ask the agent to try again."],
                    mandate_fingerprint=self.mandate.fingerprint() if self.mandate else "",
                    verified_amount=facts.amount, verified_currency=facts.currency,
                    verified_payee_ids=list(facts.payee_ids), order_status=facts.status)
                self._emit("policy_decision", {"request_id": request_id, "decision": blocked, "human_approved": True})
                return self._finish(req, Status.BLOCKED, blocked, order_id=req.order_id, detail=blocked.messages[0])
            decision = self._decide(req, facts, human_approved=True)
            self._emit("policy_decision", {"request_id": request_id, "decision": decision, "human_approved": True})
            if decision.verdict == Verdict.BLOCK:
                return self._finish(req, Status.BLOCKED, decision,
                                    detail="Blocked when re-checked after approval: " + " ".join(decision.messages))
            return self._authorize(req, decision, facts)

    def reject(self, request_id: str, approver: str = "owner", reason: str = "") -> PurchaseResult:
        with self._lock:
            req = self._need_pending(request_id)
            decision = self._results[request_id].decision
            self._emit("human_rejected", {"request_id": request_id, "approver": approver, "reason": reason})
            return self._finish(req, Status.REJECTED, decision, detail=reason or "The owner said no.")

    def finalize(self, request_id: str) -> PurchaseResult:
        """Call after the buyer approved the payment at PayPal. Re-checks, then captures."""
        with self._lock:
            req = self._requests.get(request_id)
            result = self._results.get(request_id)
            if req is None or result is None or request_id not in self._spend:
                raise GuardError("There is no authorised payment waiting for that request id.")
            return self._settle(req, result.decision)

    # ------------------------------------------------------------------ internals
    def _need_pending(self, request_id: str) -> PurchaseRequest:
        if request_id not in self._pending or request_id not in self._requests:
            raise GuardError("Nothing is waiting for the owner's approval under that request id.")
        return self._requests[request_id]

    def _coerce(self, request: Union[PurchaseRequest, dict[str, Any]]) -> Union[PurchaseRequest, PurchaseResult]:
        if isinstance(request, PurchaseRequest):
            return request
        try:
            return PurchaseRequest.model_validate(request)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
            raw = canonical_json(request)[:500] if isinstance(request, dict) else repr(request)[:500]
            rid = request.get("request_id", "invalid") if isinstance(request, dict) else "invalid"
            self._emit("request_invalid", {"request_id": str(rid)[:64], "problems": problems, "raw": raw})
            self._maybe_lockout()
            decision = Decision(verdict=Verdict.BLOCK, reasons=[Reason.INVALID_REQUEST],
                                messages=[f"The request was malformed: {problems}"], checked_at=self.clock())
            return PurchaseResult(request_id=str(rid)[:64], status=Status.BLOCKED, decision=decision,
                                  detail=decision.messages[0])

    @staticmethod
    def _changed_since(shown: dict[str, Any], now: OrderFacts) -> Optional[str]:
        """Describe how the order differs from what the owner was shown (None if it is the same)."""
        problems = []
        if Decimal(str(shown.get("amount"))) != now.amount or shown.get("currency") != now.currency:
            problems.append(f"it was {shown.get('amount')} {shown.get('currency')} and is now {now.amount} {now.currency}")
        if {norm_id(p) for p in shown.get("payee_ids", [])} != {norm_id(p) for p in now.payee_ids}:
            problems.append("the person being paid is different")
        return "; ".join(problems) or None

    def _blocked_without_log(self, req: PurchaseRequest, reason: Reason, message: str) -> PurchaseResult:
        decision = Decision(verdict=Verdict.BLOCK, reasons=[reason], messages=[message], checked_at=self.clock())
        return PurchaseResult(request_id=req.request_id, status=Status.BLOCKED, decision=decision, detail=message)

    def _interrupted(self, req: PurchaseRequest) -> PurchaseResult:
        decision = Decision(verdict=Verdict.BLOCK, reasons=[Reason.UNVERIFIABLE_ORDER],
                            messages=["This request was interrupted before it finished."], checked_at=self.clock())
        return PurchaseResult(request_id=req.request_id, status=Status.FAILED, decision=decision,
                              detail=decision.messages[0])

    def _lookup(self, req: PurchaseRequest) -> Optional[OrderFacts]:
        try:
            facts = self.paypal.get_order(req.order_id)
        except PayPalError as exc:
            self._emit("order_lookup_failed", {"request_id": req.request_id, "order_id": req.order_id,
                                               "error": exc.to_dict()})
            return None
        self._emit("order_verified", {"request_id": req.request_id, "order": facts})
        return facts

    def _decide(self, req: PurchaseRequest, facts: Optional[OrderFacts], *, human_approved: bool,
                exclude: Optional[str] = None) -> Decision:
        assert self.mandate is not None
        now = self.clock()
        return policy.evaluate(self.mandate, req, facts, self._ledger(now, exclude), now,
                               killed=self.killed, human_approved=human_approved)

    def _maybe_lockout(self) -> None:
        now = self.clock()
        recent = [t for t in self._violations if now - t <= self.lockout_window]
        if len(recent) >= self.lockout_after and not self.killed:
            self._emit("kill_switch_engaged", {
                "reason": f"{len(recent)} blocked attempts within {int(self.lockout_window.total_seconds() // 60)} minutes",
                "by": "auto_lockout"})

    def _finish(self, req: PurchaseRequest, status: Status, decision: Decision, *, order_id: Optional[str] = None,
                capture_id: Optional[str] = None, approval_url: Optional[str] = None, detail: str = "") -> PurchaseResult:
        result = PurchaseResult(request_id=req.request_id, status=status, decision=decision,
                                order_id=order_id or req.order_id, capture_id=capture_id,
                                approval_url=approval_url, detail=detail)
        self._emit("purchase_result", {"request_id": req.request_id, "result": result})
        return result

    def _authorize(self, req: PurchaseRequest, decision: Decision, facts: Optional[OrderFacts]) -> PurchaseResult:
        assert facts is not None
        self._emit("payment_authorized", {"request_id": req.request_id, "order": facts})  # budget is reserved now
        if self.buyer_approver is not None:
            self.buyer_approver(facts.order_id)
        return self._settle(req, decision)

    def _release(self, req: PurchaseRequest, reason: str, **extra: Any) -> None:
        self._emit("order_released", {"request_id": req.request_id, "reason": reason, **extra})

    def _settle(self, req: PurchaseRequest, decision: Decision) -> PurchaseResult:
        rec = self._spend.get(req.request_id)
        if rec is None:
            return self._finish(req, Status.FAILED, decision, detail="No authorised payment found for this request.")
        try:
            facts = self.paypal.get_order(rec.order_id)
        except PayPalError as exc:
            self._emit("order_lookup_failed", {"request_id": req.request_id, "order_id": rec.order_id,
                                               "error": exc.to_dict()})
            return self._finish(req, Status.FAILED, decision, order_id=rec.order_id,
                                detail=f"Could not re-check the order at PayPal ({exc}). Try finalize again.")

        # 1. Did the order change after we said yes? (the "bait and switch" check)
        payee_now = frozenset(norm_id(p) for p in facts.payee_ids)
        if facts.amount != rec.amount or facts.currency != rec.currency or not (payee_now & rec.payee_ids):
            self._release(req, "the order changed after it was authorised",
                          before={"amount": rec.amount, "currency": rec.currency, "payee_ids": sorted(rec.payee_ids)},
                          after={"amount": facts.amount, "currency": facts.currency, "payee_ids": list(facts.payee_ids)})
            changed = Decision(
                verdict=Verdict.BLOCK, reasons=[Reason.ORDER_CHANGED],
                messages=[f"The order was authorised at {rec.amount} {rec.currency} but PayPal now says "
                          f"{facts.amount} {facts.currency}. Nothing was charged."],
                mandate_fingerprint=decision.mandate_fingerprint, checked_at=self.clock(),
                verified_amount=facts.amount, verified_currency=facts.currency,
                verified_payee_ids=list(facts.payee_ids), order_status=facts.status)
            self._emit("policy_decision", {"request_id": req.request_id, "decision": changed, "human_approved": True})
            return self._finish(req, Status.BLOCKED, changed, order_id=rec.order_id, detail=changed.messages[0])

        # 2. If the money already moved (e.g. the merchant captured it), record the truth, whatever
        #    the rules say now. Leash cannot undo a payment, and the books must match reality.
        if facts.status == "COMPLETED":
            cap_id = facts.capture_ids[0] if facts.capture_ids else None
            self._emit("order_captured", {"request_id": req.request_id, "order_id": rec.order_id, "capture_id": cap_id,
                                          "status": "COMPLETED", "amount": facts.amount, "currency": facts.currency,
                                          "note": "captured by the merchant"})
            return self._finish(req, Status.COMPLETED, decision, order_id=rec.order_id, capture_id=cap_id)

        # 3. Re-run the rules (the kill switch or the mandate may have changed meanwhile)
        recheck = self._decide(req, facts, human_approved=True, exclude=req.request_id)
        if recheck.verdict == Verdict.BLOCK:
            self._emit("policy_decision", {"request_id": req.request_id, "decision": recheck, "human_approved": True})
            self._release(req, "blocked on re-check just before capture")
            return self._finish(req, Status.BLOCKED, recheck, order_id=rec.order_id,
                                detail="Blocked just before capture: " + " ".join(recheck.messages))

        # 4. Where is the order at PayPal?
        if facts.status in ("CREATED", "PAYER_ACTION_REQUIRED"):
            return self._finish(req, Status.AWAITING_CHECKOUT, decision, order_id=rec.order_id,
                                approval_url=facts.approval_url,
                                detail="Waiting for the buyer to approve the payment at PayPal.")
        if facts.status != "APPROVED":
            self._release(req, f"order status is {facts.status}")
            return self._finish(req, Status.FAILED, decision, order_id=rec.order_id,
                                detail=f"The order cannot be paid (status {facts.status}).")

        # 5. Capture
        try:
            cap = self.paypal.capture_order(rec.order_id, request_id=f"{req.request_id}-capture")
        except PayPalError as exc:
            if exc.checkout_not_approved:
                return self._finish(req, Status.AWAITING_CHECKOUT, decision, order_id=rec.order_id,
                                    approval_url=facts.approval_url,
                                    detail="PayPal says the buyer has not approved yet.")
            self._emit("order_capture_failed", {"request_id": req.request_id, "order_id": rec.order_id,
                                                "error": exc.to_dict()})
            self._release(req, "capture failed")
            return self._finish(req, Status.FAILED, decision, order_id=rec.order_id,
                                detail=f"PayPal could not take the payment: {exc}")
        self._emit("order_captured", {"request_id": req.request_id, "order_id": rec.order_id,
                                      "capture_id": cap.capture_id, "status": cap.status,
                                      "amount": cap.amount, "currency": cap.currency})
        return self._finish(req, Status.COMPLETED, decision, order_id=rec.order_id, capture_id=cap.capture_id)

    # ------------------------------------------------------------------ read-only views
    def result_for(self, request_id: str) -> Optional[PurchaseResult]:
        return self._results.get(request_id)

    def status(self) -> dict[str, Any]:
        now = self.clock()
        ledger = self._ledger(now)
        window = timedelta(hours=self.mandate.budget_window_hours) if self.mandate else timedelta(0)
        spent = sum((r.amount for r in ledger if r.at > now - window), Decimal("0"))
        budget = self.mandate.budget_amount if self.mandate else Decimal("0")
        today = sum(1 for r in ledger if r.at > now - timedelta(hours=24))
        return {
            "purchases_today": today,
            "max_per_day": self.mandate.max_purchases_per_day if self.mandate else 0,
            "kill_switch": self.killed,
            "mandate": self.mandate.model_dump(mode="json") if self.mandate else None,
            "spent_in_window": spent,
            "remaining_budget": budget - spent,
            "waiting_for_owner": sorted(self._pending),
            "waiting_for_checkout": sorted(rid for rid, r in self._results.items()
                                           if r.status == Status.AWAITING_CHECKOUT and rid in self._spend),
            "audit_events": len(self.audit.events()),
            "audit_head": self.audit.head(),
        }
