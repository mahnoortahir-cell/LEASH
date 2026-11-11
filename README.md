# Leash

**Spending controls before payment. Evidence afterwards.**

Leash is a local safety layer between AI shopping agents and PayPal. It checks payment requests against owner-approved spending rules, retrieves order details directly from PayPal, and records the results in a tamper-evident audit log.

Built for developers exploring agentic commerce, Leash demonstrates how an agent can request purchases while payment controls remain in deterministic code.

The project uses **PayPal Sandbox only**, with test money.

## The problem

An AI shopping agent can follow a malicious instruction, claim the wrong recipient, repeat a purchase, or accept an order that changed after the owner reviewed it.

Leash treats the agent’s statements as claims to verify. It checks the actual payment details against the owner’s rules before proceeding.

## Features

- Plain-English spending rules with an owner-reviewed preview.
- Per-purchase limits, rolling spending budgets, daily purchase limits, and expiry.
- Allowed merchant checks against PayPal order details.
- Owner approval for purchases at or above a configured threshold.
- Order revalidation before capture.
- Duplicate-payment protection and suspicious-attempt controls.
- A kill switch for new and pending payment requests.
- A hash-chained audit log with optional keyed signing.
- Evidence reports linking payment history and dispute information.
- A local dashboard with payment cards, budget information, and an audit table.
- Offline demonstration scenarios and a 22-scenario attack suite.

## How it works

1. The owner defines and confirms spending rules.
2. An agent submits a payment request.
3. Leash retrieves the order’s amount and recipient from the payment provider.
4. Deterministic code checks the order against the rules.
5. If required, the owner approves the reviewed purchase. Approval cannot override a hard limit.
6. Leash reserves the budget and directs the buyer to PayPal checkout.
7. Before capture, Leash rechecks the order for changes.
8. The result is recorded in the audit log.

**AI proposes rules. The owner confirms them. Deterministic code controls payments.**

## Quick start

Python 3.10 or newer is required. A virtual environment is recommended.

### 1. Clone the repository

```bash
git clone https://github.com/mahnoortahir-cell/LEASH.git
cd LEASH
python -m venv .venv
```

### 2. Activate the virtual environment

Windows Command Prompt:

```bat
.venv\Scripts\activate
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS or Linux:

```bash
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Run the offline demonstration

No PayPal account or AI key is required for these commands:

```bash
python -m leash demo
python -m leash attack
python -m leash dashboard
```

Open the local address printed in the terminal. Stop the dashboard with **Ctrl+C**.

Demo mode uses a simulated payment provider. It does not create real PayPal transactions.

## Dashboard walkthrough

The dashboard shows:

- Your current spending rules.
- A budget meter and purchase count.
- Green completed, red blocked, and amber pending payment cards.
- The agent’s claims compared with provider-reported details.
- Approve and Reject controls.
- A kill switch.
- An audit table using AG Grid Community.
- Evidence reports for recorded purchases.

In demo mode, try these scenarios:

| Scenario | Expected behavior |
|---|---|
| Agent buys team lunch | Completes within the default rules |
| A web page tricks the agent | Blocks the recipient mismatch |
| Look-alike merchant | Blocks the suspicious merchant |
| A big purchase | Requires owner approval |
| Price changes while you decide | Blocks the changed order after approval |
| Merchant raises the price | Blocks the changed order before capture |
| Agent pays the same order twice | Prevents a second payment |
| Agent goes rogue | Demonstrates lock-out and kill-switch behavior |
| A dispute arrives | Links a simulated dispute to a purchase |
| Someone edits the log | Detects an edit in a copy of the log |

Use **Reset the demo** between scenarios when you need the default rules and a clean demonstration state.

For the dispute walkthrough, reset the demo, create a team-lunch payment, click **A dispute arrives**, and open **Evidence** on the paid card.

## AI setup with Groq

Plain-English rule interpretation has been tested using **OpenAI GPT-OSS-120B hosted on Groq**.

### 1. Create a private configuration file

Only if `.env` does not already exist:

Windows Command Prompt:

```bat
copy .env.example .env
```

macOS or Linux:

```bash
cp .env.example .env
```

Do not overwrite an existing working `.env`.

### 2. Configure the provider

Add these values to your private `.env`:

```dotenv
LEASH_AI_API_KEY=your_groq_api_key
LEASH_AI_BASE_URL=https://api.groq.com/openai/v1
LEASH_AI_MODEL=openai/gpt-oss-120b
```

Use your own provider credentials. Account access and usage limits depend on the provider.

### 3. Test rule interpretation

Start the dashboard:

```bash
python -m leash dashboard
```

Open **Change the rules**, enter the following, and click **Preview**:

```text
Max $50 per order, $150 a week at Pizza Palace, ask me above $30
```

A successful AI preview displays:

```text
Understood using: AI
```

Review the proposed rules and assumptions before selecting **Yes, use these rules**. Check the exact approval boundary shown in the preview.

Normal CLI startup loads the optional `.env` file from the working directory.

### AI boundaries

AI helps interpret spending instructions. Proposed merchants are checked against the configured merchant directory, values are validated, and the owner must confirm the result.

Without an AI key, the application provides a built-in rule reader and template-based evidence output.

The project also includes an Anthropic adapter and optional AI-assisted dispute drafting. Live Claude requests and AI-generated evidence drafting have not been confirmed in the demonstrated setup.

## PayPal Sandbox setup

A PayPal developer account, sandbox app, Business merchant account, and separate Personal buyer account are needed.

Judges can run the offline demo without these credentials. To reproduce PayPal transactions, configure your own sandbox accounts.

### 1. Configure the sandbox app

Create or select a sandbox app at:

https://developer.paypal.com/

Add its credentials to your private `.env`:

```dotenv
PAYPAL_CLIENT_ID=your_sandbox_client_id
PAYPAL_CLIENT_SECRET=your_sandbox_client_secret
PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com
```

Never commit `.env` or put credentials in the README.

### 2. Run the connection checks

```bash
python -m leash sandbox selftest
```

Review the individual results. Dispute-related permissions can differ from payment permissions.

### 3. Configure the merchant directory

If `merchants.json` does not exist, copy `merchants.example.json` to it.

Create a test order to inspect the recipient reported by PayPal:

```bash
python -m leash sandbox order --amount 24.50
```

Use the reported merchant identifier in `merchants.json`:

```json
{
  "Pizza Palace": ["pizza-palace@example.com"],
  "Sushi Go": ["sushi-go@example.com"],
  "My Sandbox Shop": ["YOUR_SANDBOX_MERCHANT_ID"]
}
```

Replace the placeholder with your own sandbox merchant identifier.

### 4. Start the sandbox dashboard

Stop any running dashboard, then run:

```bash
python -m leash dashboard --sandbox
```

Open the printed address and check for the **SANDBOX** badge.

### 5. Confirm sandbox spending rules

Enter and preview:

```text
Max $50 per order, $150 a week at My Sandbox Shop, ask me above $30
```

Check that the allowed merchant matches your configured sandbox shop, then confirm.

### 6. Complete a test payment

1. Enter `24.50` in the test-order form.
2. Leave **Pay to** empty to use the default sandbox shop.
3. Create the order and check the returned details.
4. Submit the order using **Ask Leash to pay**, keeping the merchant and amount claims accurate.
5. Open PayPal checkout.
6. Log in as the separate **Personal sandbox buyer** and approve.
7. Return to Leash.
8. Click **The buyer approved it: finish payment**.
9. Check the green paid card and updated spending total.

A successful PayPal sandbox payment has been completed and demonstrated in the browser.

## Command-line payment workflow

As an alternative to the dashboard:

```bash
python -m leash sandbox order --amount 24.50
python -m leash mandate "Max $50 per order, $150 a week at My Sandbox Shop, ask me above $30" --owner YourName
python -m leash sandbox pay --order ORDER_ID --mandate mandate.json
```

Replace `ORDER_ID` with the returned order ID. Follow any owner-approval instructions, open checkout, and approve as the sandbox buyer.

Then finalize using the request ID printed by Leash:

```bash
python -m leash sandbox finalize --request REQUEST_ID
```

In PowerShell, use single quotes around rule text containing dollar signs.

## Disputes and evidence

### Demo dispute

In demo mode:

1. Reset the demo.
2. Click **Agent buys team lunch**.
3. Click **A dispute arrives**.
4. Open **Evidence** on the completed payment.

This is a simulated dispute.

### PayPal sandbox dispute

In the tested setup, automated dispute creation returned:

```text
No permissions to set target_client_id
```

The sandbox dispute was therefore created manually, and its evidence report was generated.

To repeat the manual workflow:

1. Sign in at https://sandbox.paypal.com/ as the sandbox buyer.
2. Open the payment in Activity.
3. Use the available problem-reporting flow to create a test dispute.
4. Retrieve disputes and payment request IDs:

```bash
python -m leash sandbox disputes
python -m leash list
```

5. Generate the report:

```bash
python -m leash evidence --dispute PP-D-XXXX --request REQUEST_ID --out evidence.md
```

Replace both placeholders with actual identifiers.

If automatic dispute linking is unavailable, generate a purchase report directly:

```bash
python -m leash evidence --request REQUEST_ID --out evidence.md
```

Evidence reports summarize recorded facts. They do not guarantee a dispute outcome.

## Audit verification and recording history

Verify the default audit log:

```bash
python -m leash verify
```

The log is **tamper-evident, not tamper-proof**. Optional keyed signing is configured with:

```dotenv
LEASH_AUDIT_KEY=your_private_signing_secret
```

Keep the key safe and use the appropriate key when verifying signed logs. Preserve independent copies of verification head fingerprints to help detect removal of entries from the end of a log.

For a separate sandbox recording history:

```bash
python -m leash dashboard --sandbox --log recording_sandbox_take1.jsonl
```

Use a filename that does not already exist for a fresh local history. Previous log files remain unchanged.

This does not delete or reverse transactions in PayPal Sandbox. A new log also starts a separate local spending history; it should not be used to bypass an existing spending budget.

When inspecting a custom history through other commands, consult the command’s `--help` and select the same log where supported.

## Testing

Run the Python suite and included attack scenarios:

```bash
python -m pytest
python -m leash attack
```

Optional JavaScript browser-simulation tests require Node.js:

```bash
cd tests/js
npm install
npm test
```

See [TESTING.md](TESTING.md) for the manual checklist.

### Validation status

| Area | Status |
|---|---|
| PayPal sandbox payment | Successfully completed |
| Groq API connection | Successfully tested |
| AI rule preview | Displayed “Understood using: AI” |
| Normal dashboard startup with `.env` | Successfully tested |
| Real-browser dashboard interaction | Manually exercised and recorded |
| Included attack suite | Previously reported all 22 scenarios passed |
| Sandbox dispute | Created manually; evidence report generated |
| Automated sandbox dispute creation | Restricted by PayPal permissions |
| Live Claude integration | Not confirmed |
| AI-assisted evidence drafting | Not confirmed |

The latest supplied Python test output showed **270 passed, 2 failed, and 1 skipped**. The failures concerned Windows text decoding in a test and environment-variable isolation in the no-key AI test. Fixes were identified, but a subsequent passing run has not been confirmed in this documentation.

Run the suite on the submitted revision and use its actual output when reporting results.

## Security and limitations

- **Local prototype:** the dashboard has no user login. Keep it on `127.0.0.1`; do not expose it publicly.
- **Owner identity:** approval uses a name label rather than production authentication.
- **Single process:** concurrent deployments would require transactional shared storage.
- **Local secrets:** PayPal and AI credentials remain in the local configuration. The intended agent interface does not require giving them to the agent.
- **Audit protection:** hash chaining detects edits; it does not prevent a person with sufficient local access from replacing files.
- **Kill-switch scope:** it blocks new requests and Leash-controlled captures. It cannot reverse completed payments or guarantee control over capture by another authorized party.
- **Sandbox architecture:** the demonstration app creates merchant orders and handles agent requests. Production integration would need a separately designed merchant, authorization, and webhook workflow.
- **Supported scope:** the prototype targets single-item orders and the configured supported currencies: USD, EUR, GBP, AUD, and CAD.
- **Production readiness:** a successful sandbox demonstration does not establish readiness for live payments.

The dashboard includes per-run action tokens, host checks, a Content Security Policy, and plain-text rendering of agent-supplied content. These controls do not replace authentication or a production security review.

## Configuration reference

| Variable | Purpose |
|---|---|
| `PAYPAL_CLIENT_ID` | PayPal sandbox app client ID |
| `PAYPAL_CLIENT_SECRET` | PayPal sandbox app secret |
| `PAYPAL_BASE_URL` | Sandbox API endpoint |
| `LEASH_AI_API_KEY` | OpenAI-compatible provider key |
| `LEASH_AI_BASE_URL` | OpenAI-compatible provider endpoint |
| `LEASH_AI_MODEL` | Model identifier |
| `ANTHROPIC_API_KEY` | Optional Anthropic provider key |
| `LEASH_MODEL` | Optional Anthropic model setting |
| `LEASH_AUDIT_KEY` | Optional audit signing secret |

Keep actual credentials in `.env`. Commit only empty fields or placeholders in `.env.example`.

## Project layout

| File | Responsibility |
|---|---|
| `leash/policy.py` | Deterministic spending rules |
| `leash/guard.py` | Payment request orchestration and enforcement |
| `leash/audit.py` | Audit logging and verification |
| `leash/evidence.py` | Evidence reports |
| `leash/paypal_api.py` | PayPal sandbox integration |
| `leash/mock_paypal.py` | Simulated provider |
| `leash/mandate_parser.py` | Plain-English rule interpretation |
| `leash/llm.py` | AI provider adapters |
| `leash/scenarios.py` | Included attack scenarios |
| `leash/web.py` | Local dashboard server |
| `leash/cli.py` | Command-line interface |
| `TESTING.md` | Manual testing checklist |

## Judge walkthrough

1. Clone the repository and install dependencies.
2. Run `python -m leash demo` for the guided offline story.
3. Run `python -m leash dashboard` for interactive scenarios.
4. Run `python -m leash attack` and `python -m pytest`.
5. Configure your own Groq credentials to reproduce AI rule interpretation.
6. Configure your own PayPal sandbox app and accounts to reproduce the payment workflow.

No public deployment is required to run the local application. The offline demonstration needs no external credentials; AI and PayPal sandbox functionality require the corresponding account access.

## License

Licensed under the [MIT License](LICENSE).