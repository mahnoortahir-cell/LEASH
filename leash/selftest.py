"""`python -m leash sandbox selftest`: check every PayPal step Leash relies on, in one go.

It creates ONE throw-away $1.00 order in the sandbox (fake money, it expires by itself) and then
checks that Leash can read it the way the Guard needs to. If something fails, the report says what
and shows PayPal's own error text, so you can paste it to whoever is helping you.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Callable, Optional

from rich.console import Console

from . import policy
from .models import Mandate, OrderFacts, PurchaseRequest
from .paypal_api import PayPalClient, PayPalError
from .policy import PAYABLE_STATUSES
from .util import new_id, utcnow


def _explain(exc: PayPalError) -> str:
    bits = [str(exc)]
    extra = [f"HTTP {exc.status}" if exc.status else "", exc.name, f"issues: {', '.join(exc.issues)}" if exc.issues else "",
             f"PayPal debug id {exc.debug_id}" if exc.debug_id else ""]
    return " | ".join(bits + [e for e in extra if e])


def run_selftest(client: PayPalClient, console: Console, *, clock: Callable = utcnow) -> int:
    results: list[tuple[str, bool]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok))
        tag = "[green]PASS[/green]" if ok else "[red]FAIL[/red]"
        console.print(f"{tag}  {name}")
        if detail:
            console.print(f"        [dim]{detail}[/dim]")

    def finish() -> int:
        bad = [name for name, ok in results if not ok]
        console.print()
        if bad:
            console.print(f"[red bold]{len(bad)} check(s) failed.[/red bold] Copy everything above and send it to whoever is helping you.")
            return 1
        console.print("[green bold]All checks passed.[/green bold] Leash and the PayPal sandbox understand each other.")
        return 0

    console.print(f"[bold]Leash self-test[/bold] against {client.base_url}\n")

    # 1. keys / token
    try:
        client._token()
        record("PayPal accepts your keys", True, "an access token was issued (it is not shown)")
    except PayPalError as exc:
        record("PayPal accepts your keys", False, _explain(exc) + "  -> check PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET "
               "in .env, and that they are the SANDBOX keys of your app")
        return finish()

    # 2. create a throw-away order (plays the merchant)
    try:
        created = client.create_order(request_id=new_id(), payee=None, amount=Decimal("1.00"), currency="USD",
                                      description="Leash selftest", custom_id="leash-selftest")
        record("Create a $1.00 test order", True, f"order {created.order_id}, status {created.status}")
    except PayPalError as exc:
        record("Create a $1.00 test order", False, _explain(exc))
        return finish()

    # 3. read it back the way the Guard does
    try:
        facts: Optional[OrderFacts] = client.get_order(created.order_id)
    except PayPalError as exc:
        record("Read the order back from PayPal", False, _explain(exc))
        return finish()
    ok_amount = facts.amount == Decimal("1.00") and facts.currency == "USD" and facts.unit_count == 1
    record("Read the order back with its real amount", ok_amount,
           f"PayPal says: {facts.amount} {facts.currency}, {facts.unit_count} purchase unit(s), status {facts.status}")
    record("PayPal says who gets paid (Leash needs this)", bool(facts.payee_ids),
           f"payee ids: {', '.join(facts.payee_ids)}" if facts.payee_ids else
           "PayPal's reply had no payee, so Leash would block every order. Please report this.")
    record("Order is payable and has a buyer link", facts.status in PAYABLE_STATUSES and bool(facts.approval_url),
           f"status {facts.status}; buyer link: {facts.approval_url or 'MISSING'}")

    # 4. do Leash's rules understand the real order?
    if facts.payee_ids and facts.amount is not None:
        now = clock()
        mandate = Mandate(owner="selftest", per_purchase_cap=Decimal("50"), budget_amount=Decimal("100"),
                          step_up_threshold=Decimal("25"), allowed_merchants=tuple(facts.payee_ids),
                          valid_from=now - timedelta(minutes=1), valid_until=now + timedelta(days=1))
        decision = policy.evaluate(mandate, PurchaseRequest(order_id=facts.order_id, claimed_amount=Decimal("1.00")),
                                   facts, [], now)
        record("Leash's rules accept this real order (dry run, nothing is paid)", decision.verdict.value == "allow",
               f"verdict {decision.verdict.value}: {' '.join(decision.messages)}")

    # 5. disputes access (needed only for the evidence feature)
    try:
        found = client.list_disputes(page_size=1)
        record("PayPal lets this app read disputes", True, f"{len(found)} dispute(s) listed")
    except PayPalError as exc:
        record("PayPal lets this app read disputes", False, _explain(exc) +
               "  -> if this is a permission error, open your app in the PayPal developer dashboard and look for a "
               "Disputes option under Features (the exact name may differ)")

    if facts.payee_ids:
        console.print(f"\n[bold]For merchants.json[/bold]: use one of these ids for your sandbox shop: {', '.join(facts.payee_ids)}")
    if facts.approval_url:
        console.print("[bold]To try a real payment later[/bold]: this test order's buyer link is above; real ones come from "
                      "`python -m leash sandbox order` and `sandbox pay`.")
    return finish()
