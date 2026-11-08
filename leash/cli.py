"""Command line: python -m leash <command>"""
from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

from . import __version__, config
from .audit import AuditError, AuditLog
from .evidence import EvidenceError, build_packet, find_request_by_capture, link_dispute
from .guard import Guard, GuardError
from .llm import get_llm
from .mandate_parser import MandateError, parse_mandate
from .models import Mandate, PurchaseRequest, Status
from .paypal_api import PayPalError
from .util import new_id

console = Console()
DEFAULT_LOG = "leash_audit.jsonl"


def _print_result(result) -> None:
    colour = {"completed": "green", "blocked": "red", "rejected_by_owner": "red", "failed": "red"}.get(result.status.value, "yellow")
    console.print(f"Status: [{colour}]{result.status.value}[/{colour}]   request id: [bold]{result.request_id}[/bold]")
    if result.order_id:
        console.print(f"PayPal order: {result.order_id}")
    if result.capture_id:
        console.print(f"Capture id:   {result.capture_id}")
    for message in result.decision.messages:
        console.print(f"  - {message}")
    if result.detail and result.detail not in result.decision.messages:
        console.print(f"  {result.detail}")
    if result.status == Status.AWAITING_CHECKOUT and result.approval_url:
        console.print(f"\nNext: open this link, log in as your SANDBOX BUYER and approve the payment:\n  {result.approval_url}")
        console.print(f"Then run: python -m leash sandbox finalize --request {result.request_id}")
    if result.status == Status.NEEDS_OWNER_APPROVAL:
        console.print(f"\nNext: python -m leash sandbox approve --request {result.request_id}   (or: reject)")


# ----------------------------------------------------------------------------- commands
def cmd_demo(args) -> int:
    from .demo import run_demo
    run_demo(console, args.log)
    return 0


def cmd_attack(args) -> int:
    from .scenarios import run_all
    outcomes = run_all()
    table = Table(title="Leash attack suite", show_lines=False)
    for col in ("#", "What was tried", "Leash did", "Money out", "Result"):
        table.add_column(col, overflow="fold")
    for i, o in enumerate(outcomes, start=1):
        table.add_row(str(i), o.title, o.got, str(o.money_moved), "[green]PASS[/green]" if o.passed else "[red]FAIL[/red]")
    console.print(table)
    failed = [o for o in outcomes if not o.passed]
    if failed:
        console.print(f"[red bold]{len(failed)} of {len(outcomes)} scenarios FAILED.[/red bold]")
        return 1
    console.print(f"[green bold]All {len(outcomes)} scenarios passed.[/green bold] No attack got money out.")
    return 0


def cmd_verify(args) -> int:
    report = AuditLog.verify_file(args.log, key=config.audit_key())
    if report.ok:
        console.print(f"[green]Log is intact[/green]: {report.count} entries, signed with key: {report.signed}")
        console.print(f"Head fingerprint (write this down to prove the log's length later):\n  {report.head}")
        return 0
    console.print(f"[red bold]TAMPERING FOUND[/red bold] at entry {report.broken_at}: {report.reason}")
    return 1


def cmd_list(args) -> int:
    log = AuditLog(args.log, key=config.audit_key())
    table = Table(title=f"Purchases in {args.log}")
    for col in ("request id", "status", "order", "agent says"):
        table.add_column(col)
    last: dict[str, tuple[str, str]] = {}
    said: dict[str, str] = {}
    for ev in log.events():
        if ev.type == "purchase_requested":
            r = ev.data["request"]
            said[ev.data["request_id"]] = f"{r.get('claimed_amount') or '-'} to {r.get('claimed_merchant') or '-'}"
        if ev.type == "purchase_result":
            last[ev.data["request_id"]] = (ev.data["result"]["status"], ev.data["result"].get("order_id") or "")
    for rid, (status, order) in last.items():
        table.add_row(rid, status, order, said.get(rid, ""))
    console.print(table)
    return 0


def cmd_mandate(args) -> int:
    directory = config.load_merchants(args.merchants)
    llm = get_llm()
    mandate, notes, how = parse_mandate(args.text, directory, owner=args.owner, now=datetime.now(timezone.utc), llm=llm)
    console.print(f"Understood using: {'AI' if how == 'ai' else 'simple rule reader (set ANTHROPIC_API_KEY to use AI)'}")
    for line in mandate.describe():
        console.print(f"  - {line}")
    for note in notes:
        console.print(f"  [dim](assumed) {note}[/dim]")
    if not args.yes and input("Save these rules? [y/N] ").strip().lower() not in ("y", "yes"):
        console.print("Not saved.")
        return 1
    Path(args.out).write_text(mandate.model_dump_json(indent=2), encoding="utf-8")
    console.print(f"Saved to [bold]{args.out}[/bold]")
    return 0


def cmd_evidence(args) -> int:
    log = AuditLog(args.log, key=config.audit_key())
    llm = get_llm() if args.ai else None
    dispute = None
    link = ""
    rid = args.request
    if args.dispute:
        dispute = config.paypal_client().get_dispute(args.dispute)
        found, link = link_dispute(log, dispute)
        rid = rid or found
    if args.capture and not rid:
        rid = find_request_by_capture(log, args.capture)
    if not rid:
        raise EvidenceError("I could not tell which purchase you mean. Use --request <id> (see `python -m leash list`).")
    packet = build_packet(log, rid, dispute=dispute, dispute_link=link, llm=llm)
    text = packet.to_json() if args.json else packet.to_markdown()
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        console.print(f"Saved to [bold]{args.out}[/bold]")
    else:
        print(text)
    return 0


def cmd_dashboard(args) -> int:
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("The dashboard needs two extra packages. Run: pip install -r requirements.txt")
    from .web import create_app, make_demo_world, make_sandbox_world
    llm = get_llm()
    if args.sandbox:
        try:
            directory = config.load_merchants(args.merchants)
        except SystemExit:
            directory = {}  # fine: you can still set rules later, once merchants.json exists
        world = make_sandbox_world(config.paypal_client(), Path(args.log),
                                   mandate_path=Path(args.mandate) if args.mandate else None,
                                   directory=directory, llm=llm, audit_key=config.audit_key(), owner=args.owner)
    else:
        world = make_demo_world(llm=llm)
    url = f"http://127.0.0.1:{args.port}/"
    console.print(f"Leash dashboard ({world.mode} mode): [bold]{url}[/bold]   (Ctrl+C to stop)")
    console.print("[dim]It only answers on your own computer and has no login. Do not expose it to a network.[/dim]")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(world), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def _sandbox_guard(args, *, need_mandate: bool = False):
    client = config.paypal_client()
    audit = AuditLog(args.log, key=config.audit_key())
    mandate = Mandate.model_validate_json(Path(args.mandate).read_text()) if getattr(args, "mandate", None) else None
    guard = Guard(client, audit, mandate=mandate)
    if need_mandate and guard.mandate is None:
        raise GuardError("No rules yet. Create them with `python -m leash mandate ...` and pass --mandate mandate.json")
    return client, guard


def cmd_sandbox(args) -> int:
    action = args.action
    if action == "check":
        client = config.paypal_client()
        client._token()
        console.print("[green]OK[/green]: PayPal accepted your keys and gave Leash an access token.")
        console.print(f"Talking to: {client.base_url}")
        return 0
    if action == "selftest":
        from .selftest import run_selftest
        return run_selftest(config.paypal_client(), console)
    if action == "order":
        client = config.paypal_client()
        order = client.create_order(request_id=new_id(), payee=args.payee, amount=Decimal(args.amount),
                                    currency=args.currency, description=args.description, custom_id="leash-demo")
        console.print(f"Created PayPal order [bold]{order.order_id}[/bold] for {order.amount} {order.currency}, status {order.status}")
        console.print(f"Pays: {', '.join(order.payee_ids) or '(unknown)'}")
        console.print(f"Next: python -m leash sandbox pay --order {order.order_id} --mandate mandate.json")
        return 0
    if action == "show":
        order = config.paypal_client().get_order(args.order)
        console.print(f"Order {order.order_id}: status {order.status}, amount {order.amount} {order.currency}")
        console.print(f"PayPal says the money goes to: {', '.join(order.payee_ids) or '(not stated)'}")
        console.print("Put one of those ids in your merchants.json so Leash will allow this merchant.")
        return 0
    if action == "pay":
        _, guard = _sandbox_guard(args, need_mandate=True)
        req = PurchaseRequest(order_id=args.order, agent_id=args.agent, claimed_merchant=args.claim_merchant,
                              claimed_amount=Decimal(args.claim_amount) if args.claim_amount else None,
                              description=args.description or "")
        _print_result(guard.pay(req))
        return 0
    if action in ("approve", "reject", "finalize"):
        _, guard = _sandbox_guard(args)
        if guard.mandate is None:
            raise GuardError("This log has no rules in it yet, so there is nothing to continue.")
        if action == "approve":
            _print_result(guard.approve(args.request, approver=args.by))
        elif action == "reject":
            _print_result(guard.reject(args.request, approver=args.by, reason=args.reason or ""))
        else:
            _print_result(guard.finalize(args.request))
        return 0
    if action == "dispute":
        client = config.paypal_client()
        try:
            did = client.create_sandbox_dispute(buyer_email=args.buyer_email, buyer_transaction_id=args.txn,
                                                amount=Decimal(args.amount), currency=args.currency, reason=args.reason)
        except PayPalError as exc:
            if exc.status in (400, 401, 403) or "permission" in str(exc).lower():
                console.print(f"[yellow]PayPal did not let this test app open a dispute for the buyer:[/yellow] {exc}")
                console.print("This is a PayPal sandbox permission limit. It is not a problem with your payment or with Leash.")
                console.print("Create the dispute by hand instead:")
                console.print("  1. Log in at sandbox.paypal.com as your sandbox BUYER, open Activity, open the payment.")
                console.print("  2. Choose 'Report a problem' and say the item did not arrive.")
                console.print("  3. Then run: python -m leash sandbox disputes   (to see its PP-D- number)")
                console.print(f"  4. And:      python -m leash evidence --log {args.log} --dispute PP-D-XXXX --request REQUEST_ID --out evidence.md")
                console.print("     (python -m leash list shows your REQUEST_ID)")
                return 2
            raise
        console.print(f"Created sandbox dispute [bold]{did}[/bold]")
        console.print(f"Next: python -m leash evidence --log {args.log} --dispute {did} --out evidence.md")
        return 0
    if action == "disputes":
        client = config.paypal_client()
        for d in client.list_disputes():
            console.print(f"{d.dispute_id}  {d.status}  {d.reason}  {d.amount} {d.currency}")
        return 0
    return 2


# ----------------------------------------------------------------------------- wiring
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="leash", description="A leash for AI shopping agents: spend limits before the money moves, proof after.")
    p.add_argument("--version", action="version", version=f"leash {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("demo", help="a guided story, no PayPal account needed")
    s.add_argument("--log", help="write the audit log to this file (default: a temp folder)")

    sub.add_parser("attack", help="run the attack suite; exits 1 if any attack gets money out")

    s = sub.add_parser("dashboard", help="open the visual dashboard in your browser")
    s.add_argument("--sandbox", action="store_true", help="use the real PayPal sandbox instead of the pretend PayPal")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--log", default=DEFAULT_LOG)
    s.add_argument("--mandate", help="path to mandate.json (sandbox mode)")
    s.add_argument("--merchants", default="merchants.json")
    s.add_argument("--owner", default="owner")
    s.add_argument("--no-browser", action="store_true")

    s = sub.add_parser("verify", help="check an audit log has not been changed")
    s.add_argument("--log", default=DEFAULT_LOG)

    s = sub.add_parser("list", help="list the purchases in an audit log")
    s.add_argument("--log", default=DEFAULT_LOG)

    s = sub.add_parser("mandate", help="turn plain English into rules and save them")
    s.add_argument("text")
    s.add_argument("--merchants", default="merchants.json")
    s.add_argument("--owner", default="owner")
    s.add_argument("--out", default="mandate.json")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    s = sub.add_parser("evidence", help="build a proof packet for one purchase or dispute")
    s.add_argument("--log", default=DEFAULT_LOG)
    s.add_argument("--request")
    s.add_argument("--capture")
    s.add_argument("--dispute")
    s.add_argument("--ai", action="store_true", help="also draft a statement with AI (needs ANTHROPIC_API_KEY)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--out")

    s = sub.add_parser("sandbox", help="use the REAL PayPal sandbox (needs keys in .env)")
    s.add_argument("action", choices=["selftest", "check", "order", "show", "pay", "approve", "reject", "finalize", "dispute", "disputes"])
    s.add_argument("--log", default=DEFAULT_LOG)
    s.add_argument("--mandate", help="path to mandate.json")
    s.add_argument("--amount", default="24.50")
    s.add_argument("--currency", default="USD")
    s.add_argument("--payee", help="merchant e-mail or id for the order (default: your app's sandbox business account)")
    s.add_argument("--description", default="Leash test order")
    s.add_argument("--order")
    s.add_argument("--agent", default="sandbox-agent")
    s.add_argument("--claim-merchant")
    s.add_argument("--claim-amount")
    s.add_argument("--request")
    s.add_argument("--by", default="owner")
    s.add_argument("--reason", default="MERCHANDISE_OR_SERVICE_NOT_RECEIVED")
    s.add_argument("--buyer-email")
    s.add_argument("--txn", help="the BUYER's transaction id (from the sandbox buyer's activity page)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    config.load_env()
    args = build_parser().parse_args(argv)
    handlers = {"demo": cmd_demo, "attack": cmd_attack, "dashboard": cmd_dashboard, "verify": cmd_verify, "list": cmd_list,
                "mandate": cmd_mandate, "evidence": cmd_evidence, "sandbox": cmd_sandbox}
    try:
        return handlers[args.cmd](args)
    except (PayPalError, GuardError, MandateError, EvidenceError, AuditError) as exc:
        console.print(f"[red]Problem:[/red] {exc}")
        if isinstance(exc, PayPalError) and exc.debug_id:
            console.print(f"[dim]PayPal debug id: {exc.debug_id}[/dim]")
        return 2
    except FileNotFoundError as exc:
        console.print(f"[red]File not found:[/red] {exc.filename}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
