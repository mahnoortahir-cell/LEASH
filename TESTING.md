# What to test (Windows, VS Code, Command Prompt)

Tick the boxes. If anything fails, send me: the step number, what you did, and a screenshot or the red text.

## A. Install (5 min)
- [ ] Extract the zip into a NEW folder. Copy your old `.env` and `merchants.json` into it if you made them.
- [ ] Open the folder in VS Code, then Terminal, then Command Prompt:
      `python -m venv .venv`, `.venv\Scripts\activate`, `pip install -r requirements.txt`
- [ ] `python -m leash --version` shows `leash 0.2.2`
- [ ] `python -m leash attack` ends with "All 22 scenarios passed"
- [ ] `python -m pytest` ends with "passed" (one skipped is normal)

## B. Demo dashboard (15 min): `python -m leash dashboard`
Click the scenario buttons on the left, one by one:
- [ ] 1 Agent buys team lunch: a GREEN "Paid" card.
- [ ] 2 A web page tricks the agent: RED "Blocked", with "The agent said" and "PayPal says" boxes that differ.
- [ ] 3 Look-alike merchant: RED "Blocked", "Looks like a fake of an allowed merchant".
- [ ] 4 A big purchase: AMBER "Needs your OK". Press Approve: it turns green. Run it again, press Reject: red "You said no".
- [ ] 5 **Price changes while you decide** (the new one): AMBER card showing $85. Press **Approve**.
      It must turn RED: "The order changed after you saw it, so nothing was charged". The budget must NOT move.
- [ ] 6 Merchant raises the price: RED "Blocked", "The order changed after you saw it".
- [ ] 7 Agent pays the same order twice: one green card, one red. Charged once.
- [ ] 8 Agent goes rogue: red cards, and the top-right button says "Kill switch is ON". Click lunch: blocked.
      Click the top-right button to release. Click lunch: green again.
- [ ] 9 Press "Reset the demo", run lunch, run "A dispute arrives", then click **Evidence** on the green card:
      a report opens with a "The dispute" part. Try Download.
- [ ] 10 Someone edits the log: a message says TAMPERING FOUND in a COPY. The top badge stays green.
- [ ] 11 Reset, then click lunch 4 times: the 4th is amber, "Looks like one big purchase split into pieces".
- [ ] 12 Open "Change the rules", type `Max $20 per order, $60 a week at Pizza Palace`, press Preview
      (it says "at most $20.00"), press "Yes, use these rules", click lunch: red, over the limit.
- [ ] 13 Type `Spend $99999 a week at Pizza Palace` and Preview: "Refused for safety".
      Type `$100 a week at Blue Bottle` and Preview: it lists your merchants.
- [ ] 14 The audit log table at the bottom shows rows. If it looks empty, click "Show as simple table".
- [ ] 15 Turn off Wi-Fi, restart the dashboard: the demo still works.

## C. Real PayPal sandbox (45 min)
- [ ] developer.paypal.com: switch to **Sandbox**, Apps & Credentials, Create App (type Merchant). Copy Client ID and Secret.
- [ ] Testing tools, Sandbox accounts: note the email and password of the **Personal** (buyer) account.
- [ ] `copy .env.example .env`, open `.env`, paste the two keys (no spaces, no quotes), save.
- [ ] `python -m leash sandbox selftest`: all PASS (the disputes line may fail for now). **Send me the output.**
- [ ] Copy `merchants.example.json` to `merchants.json`; put one id from selftest's "For merchants.json" line under "My Sandbox Shop".
- [ ] `python -m leash dashboard --sandbox`: the badge says SANDBOX.
- [ ] Change the rules: `Max $50 per order, $150 a week at My Sandbox Shop, ask me above $30`, Preview, Yes.
- [ ] Normal payment: Create test order (24.50), Ask Leash to pay, amber card, "Open PayPal checkout", log in as the
      BUYER, approve (an "Example Domain" page afterwards is normal), then press "The buyer approved it: finish payment". GREEN.
- [ ] Lying agent: new order; "Agent claims merchant" = `pizza-palace@example.com`, amount = `5.00`: RED "story does not match".
- [ ] Big payment: order 45.00: amber "Needs your OK", Approve, then checkout and finish like above.
- [ ] Unknown order: ask Leash to pay `ORDERDOESNOTEXIST1`: RED "Could not check this order at PayPal".
- [ ] Wrong shop: change the rules to Pizza Palace only, pay a new order: RED "This merchant is not on your list".
- [ ] Kill switch: press it, pay an order: blocked. Release it.
- [ ] Evidence button on a green card: a report opens.
- [ ] Stop the dashboard (Ctrl+C), start it again with `--sandbox`: your cards and budget are still there.

## D. Dispute (do it last)
- [ ] Log in at sandbox.paypal.com as the BUYER, Activity, open the payment, "Report a problem", item not received.
- [ ] `python -m leash sandbox disputes` shows the PP-D- number.
- [ ] `python -m leash evidence --dispute PP-D-XXXX --request REQUESTID --out evidence.md`, then open `evidence.md`.
      (`python -m leash list` shows REQUESTID.)
- [ ] Optional: `python -m leash sandbox dispute ...` may be refused by PayPal ("No permissions to set target_client_id").
      That is a PayPal limit; Leash then prints the hand-made steps above.

## E. Command line
- [ ] `python -m leash verify` says "Log is intact".
- [ ] Copy `leash_audit.jsonl`, change one number in the copy with Notepad, `python -m leash verify --log copy.jsonl`: TAMPERING FOUND.

## F. Before you upload to GitHub and submit
- [ ] Put your name in `LICENSE`. Never upload `.env` (run `git status` and check it is not listed).
- [ ] Make the repo public; the license shows in its About box.
- [ ] Clone it into a new folder and repeat section A: this is what the judges will do.
- [ ] Video: public on YouTube, under 3 minutes, no music you don't own. Submit by Nov 11.
