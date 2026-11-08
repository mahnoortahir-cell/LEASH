# Leash

**A leash for AI shopping agents.** Spend limits *before* the money moves. Proof *after*.

An AI agent that can pay is useful, and risky. A web page can trick it, a merchant can change the
price, a bug can make it buy in a loop. Leash sits between the agent and PayPal. The agent never
holds PayPal keys. It can only ask Leash to pay, and Leash pays only inside rules **you** set.

## Why this exists

- PayPal's own March 2026 merchant survey found merchants split on who is responsible when an AI agent
  buys something, and almost two-thirds of merchants already testing agentic tools wanted a standard
  liability framework urgently ([report](https://www.paypalobjects.com/marketing/GCI_Q1_26_PayPal_Agentic_Pulse_Final_Report.pdf)).
- The IMF (Note 2026/004) says agents are "probabilistic" but payments must be "deterministic", and
  recommends mandate-based authorisation, human approval thresholds, kill switches and dispute
  frameworks built for AI ([note](https://www.imf.org/-/media/files/publications/imf-notes/2026/english/insea2026004.pdf)).
- PayPal's FY2025 10-K says its buyer and seller protection programs cause substantial losses from
  disputes ([10-K](https://www.sec.gov/Archives/edgar/data/1633917/000163391726000024/pypl-20251231.htm)).
- A 2026 security paper (arXiv:2609.00060) found agent payment protocols can approve one thing and pay
  for another ("pay A, get B"). Leash checks the *real* order at PayPal, not the agent's story.

## How it works

```
 AI agent ──"pay order X"──▶  LEASH  ──"what is order X really?"──▶  PayPal
                                │
        1. reads the REAL amount and payee from PayPal (the agent's claims are only claims)
        2. checks YOUR rules: allowed merchants, caps, budget, daily count, expiry, kill switch
        3. asks YOU first if the purchase is big (you can never unlock a hard limit by saying yes)
        4. reserves the budget, the buyer approves at PayPal
        5. checks the order did NOT change, then captures the payment
                                │
                                ▼
              hash-chained audit log ──▶ evidence packet when a dispute arrives
```

**The decision to move money never involves AI.** The AI only helps at the edges (see below).

## Try it in 2 minutes (no PayPal account needed)

Needs Python 3.10 or newer (built and tested on 3.12).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m leash demo               # a short guided story with a pretend PayPal
python -m leash attack             # 22 attack scenarios; exits with an error if any gets money out
python -m pytest                   # 260+ automated tests
python -m leash dashboard          # the visual dashboard (opens in your browser)
```

## The dashboard

```bash
python -m leash dashboard             # demo mode: pretend PayPal, ten one-click scenarios
python -m leash dashboard --sandbox   # real PayPal sandbox (needs your keys, see below)
```

One screen shows: your rules in plain English (and a box to change them), a budget meter, every purchase
the agent attempted as a card (green paid, red blocked, amber waiting for you), **what the agent said versus
what PayPal says**, Approve / Reject buttons, a kill switch, the audit log in a data grid
([AG Grid Community](https://www.ag-grid.com/), bundled so it works offline), and an **Evidence** button that
builds the dispute packet. In demo mode, click a scenario on the left (prompt injection, price change, rogue
agent, dispute, log tampering...) and watch it happen.

Safety of the dashboard itself: it answers only on `127.0.0.1`, has **no login** (never expose it to a network),
needs a random per-run token for every action (so other websites cannot poke it through your browser), refuses
unknown `Host` headers, loads no code from the internet, and uses a strict Content-Security-Policy. Text written
by agents is shown as plain text, never as HTML.

## What Leash stops (the attack suite)

| Attack | What Leash does |
|---|---|
| A web page tricks the agent into paying a stranger (prompt injection) | Blocks: PayPal says the money goes elsewhere than the agent claimed |
| Look-alike merchant (`examp1e.com`, Cyrillic letters) | Blocks and says why |
| Agent lies about the amount | Blocks: claim does not match PayPal |
| Merchant raises the price after you said yes | Blocks at capture, nothing charged |
| Over the per-purchase cap / weekly budget / daily count | Blocks |
| Big purchase | Waits for the owner; a yes cannot override a hard limit |
| One big purchase split into small ones | Held for the owner |
| Agent stuck in a loop, same order paid twice | Charged once |
| Five blocked attempts in a row | Automatic lock-out until the owner steps in |
| Kill switch, expired rules, wrong currency, malformed input | Blocks (bad input never even reaches PayPal) |
| Restarting the program to reset the budget | Does not work: state is rebuilt from the log |
| Someone edits or deletes a log entry | Detected, and the entry is pinpointed |

## Use it with the real PayPal sandbox (fake money)

> This part could not be run by the author against PayPal's servers (see "What is tested"). Expect to
> possibly fix a small thing; the place to fix it is `leash/paypal_api.py`.

1. Make a PayPal developer account, open the **Sandbox**, and create an app. Copy the Client ID and Secret.
2. `cp .env.example .env` (Windows: `copy .env.example .env`) and paste the keys in.
3. `python -m leash sandbox selftest` checks every PayPal step Leash needs, one by one, and prints PASS/FAIL with
   PayPal's own error text. If anything fails, copy the whole output and ask for help.
4. `python -m leash sandbox order --amount 24.50` creates an order *as the merchant*. It prints who PayPal
   says gets the money. Copy that id into `merchants.json` (copy `merchants.example.json` first).
5. `python -m leash mandate "Max $50 per order, $150 a week at My Sandbox Shop, ask me above $30" --owner YourName`
   (Windows PowerShell: use single quotes around the text. In Command Prompt double quotes are fine.)
   Or skip steps 5-7 and do it all in `python -m leash dashboard --sandbox`.
6. `python -m leash sandbox pay --order ORDER_ID --mandate mandate.json` and open the link it prints. Log in
   as your sandbox **buyer** and approve.
7. `python -m leash sandbox finalize --request REQUEST_ID` captures the payment.
8. Make a test dispute **by hand**: log in at sandbox.paypal.com as your sandbox buyer, open Activity, open the
   payment, choose "Report a problem" and say the item did not arrive. Then:
   `python -m leash sandbox disputes` (shows the PP-D- number) and
   `python -m leash evidence --dispute PP-D-XXXX --request REQUEST_ID --out evidence.md`
   (`python -m leash list` shows your REQUEST_ID). PayPal's docs also describe a command-line way
   (`python -m leash sandbox dispute ...`), but in our tests PayPal refused it with a permission error
   ("No permissions to set target_client_id"), so Leash prints these hand-made steps instead.

If the dispute does not auto-link to the purchase, use `python -m leash list` and
`python -m leash evidence --request REQUEST_ID`.

## How AI is used, and how it is kept on a leash

- **Plain English to rules.** You write "Keep lunches under $200 a week at Pizza Palace". An AI (or a simple
  built-in reader if you have no key) proposes the rules. Plain code then: takes merchants **only** from your
  own `merchants.json` (the AI cannot invent one), refuses numbers above hard ceilings (the AI cannot hand out
  big limits), and you must read and confirm the result.
- **Dispute statement.** The facts and timeline come only from the tamper-evident log. An optional AI paragraph
  is labelled a draft. Text written by the agent or a web page is cleaned and passed as *data*, never as
  instructions.
- Set `ANTHROPIC_API_KEY` to turn the AI on. Everything works without it.

## What is tested, and what is not

**Tested (automated, offline):** 260+ Python tests (rules engine, Guard, audit log, evidence, parser, CLI, dashboard
server and its security checks); a 22-scenario attack suite; a randomised stress test (40 random behaviours x 80
steps, checking after every step that the books, the money that really moved, and the rules all agree); and 17
browser-simulation tests (optional, needs Node: `cd tests/js && npm install && npm test`) that run the dashboard's
real JavaScript in a simulated browser and feed it hostile text. I also deliberately sabotaged 18 different
protections (22 runs) one at a time to confirm the tests catch each one. That found a real blind spot in my first
stress test, now fixed. A second real bug was found by actually using the
dashboard: if a merchant changed an order's price while it waited for the owner's OK, a "yes" to the old price
paid the new one. It is fixed (approval is now tied to the exact order the owner was shown) and guarded by an
attack scenario, direct tests and the stress test. The author's first run on Windows passed all of it.

**Not tested live:** (1) the real PayPal client was tested against a *fake* PayPal server built from PayPal's
public docs, so it proves we send and read what the docs describe, not that PayPal agrees; (2) the Claude adapter
was tested with a stand-in client, not a live call; (3) the dashboard's looks and layout have not been checked in a
real browser by the author (the logic is tested in a simulated one, and AG Grid mounts there).

## Known limits (honest list)

- In the sandbox one app plays both merchant and agent. In real life the merchant creates the order and Leash
  would watch PayPal webhooks instead of capturing.
- Owner approval is just a name label here. A real product needs an authenticated owner (for example passkeys).
- The audit log is tamper-**evident**, not tamper-proof. Set `LEASH_AUDIT_KEY` so it cannot be rebuilt without a
  secret, and write down `python -m leash verify` output (the "head") now and then: cutting entries off the
  *end* of a log is only visible that way.
- The dashboard has no login. It is for you, on your own computer.
- One process only. Several processes would need a database transaction around the ledger.
- The kill switch stops new payments and refuses to capture pending ones, but it cannot stop a merchant who
  captures an already-approved order. Leash then records the truth. Unapproved PayPal orders expire by themselves.
- Currencies: USD, EUR, GBP, AUD, CAD. Single-item orders only. Free text from agents is stored (cut to 500 and
  2000 characters); buyer names, emails and addresses are never stored.

## Settings

| Variable | Needed for |
|---|---|
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | the real sandbox commands |
| `ANTHROPIC_API_KEY` (and optional `LEASH_MODEL`) | AI help (optional) |
| `LEASH_AUDIT_KEY` | signing the audit log (recommended) |

## Layout

```
leash/policy.py       the rules engine (no AI)            leash/guard.py     the only thing that moves money
leash/audit.py        hash-chained log                    leash/evidence.py  proof packets for disputes
leash/paypal_api.py   real PayPal client (sandbox only)   leash/mock_paypal.py  pretend PayPal for tests/demo
leash/mandate_parser.py  plain English -> rules           leash/scenarios.py the attack suite
```
MIT licensed.