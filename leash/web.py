"""A local dashboard for the owner: see what the agent tried, say yes or no, hit the kill switch.

It runs only on YOUR computer (127.0.0.1) and has NO login: anyone who can open the page can approve
payments, so never expose it to a network. A random per-run token, a Host check and a strict
Content-Security-Policy stop other websites (and injected text) from poking it through your browser.
"""
from __future__ import annotations

import hmac
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi import Path as ApiPath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import views
from .audit import AuditLog
from .demo_world import NOODLE, SCENARIOS, DemoWorld
from .evidence import EvidenceError, build_packet, link_dispute
from .guard import Guard, GuardError
from .llm import LLM
from .mandate_parser import MandateError, parse_mandate
from .models import Mandate, check_amount
from .paypal_api import PayPalClient, PayPalError
from .scenarios import PIZZA, SUSHI
from .util import new_id, to_jsonable, utcnow

STATIC = Path(__file__).parent / "static"
STATIC_FILES = {
    "dashboard.js": "application/javascript",
    "dashboard.css": "text/css",
    "vendor/ag-grid-community.min.js": "application/javascript",
}
HOST_OK = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\]|testserver)(:\d{1,5})?$")  # strict: no tricks
MAX_BODY = 64 * 1024
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


@dataclass
class World:
    mode: str  # "demo" or "sandbox"
    guard: Guard
    audit: AuditLog
    demo: Optional[DemoWorld] = None
    client: Optional[PayPalClient] = None
    directory: dict = field(default_factory=dict)
    llm: Optional[LLM] = None
    owner: str = "owner"
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    lock: threading.RLock = field(default_factory=threading.RLock)
    previews: dict = field(default_factory=dict)


def make_demo_world(log_path: Optional[Path] = None, llm: Optional[LLM] = None) -> World:
    demo = DemoWorld.create(log_path)
    return World(mode="demo", guard=demo.guard, audit=demo.audit, demo=demo, llm=llm, owner="Sam",
                 directory={"Pizza Palace": [PIZZA], "Sushi Go": [SUSHI], "Noodle House": [NOODLE]})


def make_sandbox_world(client: PayPalClient, log_path: Path, *, mandate_path: Optional[Path] = None,
                       directory: Optional[dict] = None, llm: Optional[LLM] = None,
                       audit_key: Optional[str] = None, owner: str = "owner") -> World:
    audit = AuditLog(log_path, key=audit_key)
    mandate = None
    if mandate_path and Path(mandate_path).exists():
        mandate = Mandate.model_validate_json(Path(mandate_path).read_text(encoding="utf-8"))
    guard = Guard(client, audit, mandate=mandate)
    return World(mode="sandbox", guard=guard, audit=audit, client=client, directory=directory or {}, llm=llm, owner=owner)


class MandateText(BaseModel):
    text: str = Field(min_length=3, max_length=1000)


class PreviewRef(BaseModel):
    preview_id: str = Field(min_length=8, max_length=64)


class NewOrder(BaseModel):
    payee: Optional[str] = Field(default=None, max_length=200)
    amount: Decimal
    currency: str = Field(default="USD", max_length=3)
    description: str = Field(default="", max_length=127)


class RejectBody(BaseModel):
    reason: str = Field(default="", max_length=200)


def _http(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


@contextmanager
def _translate() -> Iterator[None]:
    """Turn the project's own errors into clear HTTP errors."""
    try:
        yield
    except GuardError as exc:
        raise _http(409, str(exc)) from exc
    except MandateError as exc:
        raise _http(400, str(exc)) from exc
    except EvidenceError as exc:
        raise _http(404, str(exc)) from exc
    except PayPalError as exc:
        raise _http(502, f"PayPal said: {exc}") from exc
    except ValueError as exc:
        raise _http(400, str(exc)) from exc


def create_app(world: World) -> FastAPI:
    app = FastAPI(title="Leash dashboard", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if not HOST_OK.match(request.headers.get("host", "")):
            return JSONResponse({"error": "This dashboard only answers on localhost."}, status_code=400)
        if request.method == "POST":
            given = request.headers.get("x-leash-token", "")
            if not hmac.compare_digest(given.encode(), world.token.encode()):
                return JSONResponse({"error": "Missing or wrong dashboard token. Reload the page."}, status_code=403)
            if int(request.headers.get("content-length") or 0) > MAX_BODY:
                return JSONResponse({"error": "That request is too large."}, status_code=413)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}" for e in exc.errors())
        return JSONResponse({"error": f"That input was not valid ({problems})."}, status_code=422)

    # ------------------------------------------------------------------ the page
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", world.token))

    @app.get("/static/{name:path}")
    def static(name: str) -> FileResponse:
        if name not in STATIC_FILES:
            raise _http(404, "Not found.")
        return FileResponse(STATIC / name, media_type=STATIC_FILES[name])

    # ------------------------------------------------------------------ reading
    @app.get("/api/state")
    def state() -> JSONResponse:
        with world.lock:
            guard = world.guard
            st = guard.status()
            report = world.audit.verify()
            events = world.audit.events()
            cards = views.request_cards(events)
            waiting_owner, waiting_checkout = set(st["waiting_for_owner"]), set(st["waiting_for_checkout"])
            for c in cards:
                c["can_decide"] = c["request_id"] in waiting_owner
                c["can_finalize"] = c["request_id"] in waiting_checkout
            m = guard.mandate
            payload = {
                "mode": world.mode, "ai": world.llm is not None, "kill_switch": guard.killed,
                "mandate": m.model_dump(mode="json") if m else None,
                "mandate_lines": m.describe() if m else [],
                "budget": {"spent": st["spent_in_window"], "total": m.budget_amount if m else Decimal("0"),
                           "remaining": st["remaining_budget"], "window_days": (m.budget_window_hours / 24) if m else 0,
                           "purchases_today": st["purchases_today"], "max_per_day": st["max_per_day"],
                           "currency": m.currency if m else "USD"},
                "chain": {"ok": report.ok, "count": report.count, "head": report.head, "signed": report.signed,
                          "broken_at": report.broken_at, "reason": report.reason},
                "cards": cards, "event_count": len(events),
                "scenarios": SCENARIOS if world.mode == "demo" else [],
            }
            return JSONResponse(to_jsonable(payload))

    @app.get("/api/events")
    def events() -> JSONResponse:
        with world.lock:
            return JSONResponse(to_jsonable(views.event_rows(world.audit.events())))

    @app.get("/api/verify")
    def verify() -> JSONResponse:
        with world.lock:
            r = world.audit.verify()
            return JSONResponse({"ok": r.ok, "count": r.count, "head": r.head, "broken_at": r.broken_at,
                                 "reason": r.reason, "signed": r.signed})

    @app.get("/api/evidence/{rid}")
    def evidence(rid: str = ApiPath(max_length=64), dispute_id: Optional[str] = None, ai: bool = False) -> JSONResponse:
        with world.lock, _translate():
            dispute, link = None, ""
            if dispute_id:
                dispute = world.guard.paypal.get_dispute(dispute_id)
                _, link = link_dispute(world.audit, dispute)
            packet = build_packet(world.audit, rid, dispute=dispute, dispute_link=link, llm=world.llm if ai else None)
            return JSONResponse(to_jsonable({"markdown": packet.to_markdown(), "json": packet.to_dict(),
                                             "filename": f"leash-evidence-{rid[:8]}.md"}))

    # ------------------------------------------------------------------ the agent's door and the merchant's desk
    @app.post("/api/pay")
    def pay(body: dict[str, Any]) -> JSONResponse:
        with world.lock, _translate():
            return JSONResponse(to_jsonable(world.guard.pay(body)))

    @app.post("/api/orders")
    def create_order(body: NewOrder) -> JSONResponse:
        with world.lock, _translate():
            amount = check_amount(body.amount)
            facts = world.guard.paypal.create_order(request_id=new_id(), payee=body.payee or None, amount=amount,
                                                    currency=body.currency.upper(), description=body.description,
                                                    custom_id="leash-dashboard")
            return JSONResponse(to_jsonable(facts))

    # ------------------------------------------------------------------ the owner's controls
    @app.post("/api/approve/{rid}")
    def approve(rid: str = ApiPath(max_length=64)) -> JSONResponse:
        with world.lock, _translate():
            return JSONResponse(to_jsonable(world.guard.approve(rid, approver=f"{world.owner} (dashboard)")))

    @app.post("/api/reject/{rid}")
    def reject(body: RejectBody, rid: str = ApiPath(max_length=64)) -> JSONResponse:
        with world.lock, _translate():
            return JSONResponse(to_jsonable(world.guard.reject(rid, approver=f"{world.owner} (dashboard)", reason=body.reason)))

    @app.post("/api/finalize/{rid}")
    def finalize(rid: str = ApiPath(max_length=64)) -> JSONResponse:
        with world.lock, _translate():
            return JSONResponse(to_jsonable(world.guard.finalize(rid)))

    @app.post("/api/kill")
    def kill() -> JSONResponse:
        with world.lock:
            world.guard.engage_kill_switch("owner pressed the kill switch in the dashboard", by=world.owner)
            return JSONResponse({"kill_switch": world.guard.killed})

    @app.post("/api/release")
    def release() -> JSONResponse:
        with world.lock:
            world.guard.release_kill_switch(by=world.owner)
            return JSONResponse({"kill_switch": world.guard.killed})

    @app.post("/api/mandate/preview")
    def mandate_preview(body: MandateText) -> JSONResponse:
        with world.lock, _translate():
            mandate, notes, how = parse_mandate(body.text, world.directory, owner=world.owner, now=utcnow(), llm=world.llm)
            now = time.time()
            for key in [k for k, (t, *_rest) in world.previews.items() if now - t > 600]:
                del world.previews[key]
            preview_id = secrets.token_urlsafe(12)
            world.previews[preview_id] = (now, mandate, notes, how)  # the owner confirms EXACTLY what was shown
            return JSONResponse({"preview_id": preview_id, "lines": mandate.describe(), "assumptions": notes, "how": how})

    @app.post("/api/mandate/confirm")
    def mandate_confirm(body: PreviewRef) -> JSONResponse:
        with world.lock:
            entry = world.previews.pop(body.preview_id, None)
            if entry is None:
                raise _http(404, "That preview has expired. Preview your rules again.")
            world.guard.set_mandate(entry[1], by=f"{world.owner} (dashboard)")
            return JSONResponse({"ok": True, "lines": entry[1].describe()})

    # ------------------------------------------------------------------ demo mode only
    @app.post("/api/demo/reset")
    def demo_reset() -> JSONResponse:
        if world.mode != "demo":
            raise _http(404, "Demo scenarios only exist in demo mode.")
        with world.lock:
            fresh = DemoWorld.create()
            world.demo, world.guard, world.audit = fresh, fresh.guard, fresh.audit
            world.previews.clear()
            return JSONResponse({"ok": True})

    @app.post("/api/demo/{name}")
    def demo_run(name: str = ApiPath(max_length=32)) -> JSONResponse:
        if world.mode != "demo" or world.demo is None:
            raise _http(404, "Demo scenarios only exist in demo mode.")
        with world.lock, _translate():
            try:
                outcome = world.demo.run(name)
            except KeyError as exc:
                raise _http(404, f"There is no scenario called '{name}'.") from exc
            return JSONResponse(to_jsonable(outcome))

    return app
