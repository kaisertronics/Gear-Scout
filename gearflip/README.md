# Gear Flip

A buy-to-resell finder for used pro-audio gear. It's the 💹 Gear Flip tab (`/gearflip`) on the Gear Scout site, but it's built so it can be split off into its own product later.

## What it does
- **Your starting budget.** Set it in the tab's Settings. Your bankroll is the budget, plus profit from flips you've sold, minus money tied up in flips you haven't sold yet.
- **Constant scanning.** Gear Scout's scheduler calls `jobs.tick()` every 5 minutes, and a pass runs every *Scan every N minutes* (default 15). Each pass:
  1. **Scours.** Two Reverb searches walk through about 80 models that resell well, one Reverb read pulls the newest broken or for-parts pro audio, and every other pass there's one eBay search (about 50 a day). There's no Facebook and no logins.
  2. **Scans.** Everything Gear Scout has collected (Facebook Marketplace, Craigslist, eBay, Reverb, auctions...) goes through the profit math.
  3. **Alerts.** A strong new flip gets a phone push (ntfy) with all the numbers. A morning email carries the plan.
- **Per-flip numbers.** All-in buy price (sales tax online, a pickup trip for local), resale, fees, shipping by size, repair cost and the odds it works, net profit (the average of fixed and not fixable), worst case, ROI, ROI per month, days to sell, confidence, score, and a walk-away price.
- **Best buys.** A 0/1 knapsack picks the set with the most confidence-weighted profit that fits the cash you have free.
- **My flips.** A ledger tracks what you bought, repaired and sold, and compares the estimates with what really happened.

## Files and data
| File | Job |
|---|---|
| `economics.py` | Fees, shipping, repairs, confidence, score. Pure. |
| `portfolio.py` | The budget plan. Pure. |
| `settings.py` | `/data/gearflip/settings.json` |
| `ledger.py` | `/data/gearflip/ledger.json` |
| `engine.py` | Ranked opportunities saved to `/data/gearflip/scan.json` |
| `scour.py` | Extra searches. State in `scour_state.json` |
| `alerts.py` | Pushes and email. Sent log in `alerted.json` |
| `jobs.py` | One background pass. Status in `jobs.json` |
| `web.py` + `templates/` | The tab, plus JSON at `/gearflip/api/*.json` |
| `source.py` | **The only file that imports Gear Scout** |

Everything Gear Flip keeps lives in `/data/gearflip/`. It doesn't touch `config.yaml` (except to read the email and ntfy settings) or the older Flips tab.

## Spinning it off
Copy this folder and re-implement `source.py` against the new app's listings store and notifier. Give it a `base.html` with a `content` block, and register `web.bp` on a Flask app. Then call `jobs.tick()` on a timer.

## Tests
`python -m pytest gearflip/tests`
