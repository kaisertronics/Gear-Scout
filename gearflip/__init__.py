"""Gear Flip: a buy-to-resell finder for used pro-audio gear.

Give it a starting budget. It keeps scanning listings, works out what each one
would really earn after you buy it, fix it (if it needs fixing), pay the
selling fees and ship it, and picks the best set of buys that fits your cash.
New strong flips are pushed to your phone with the numbers.

It lives inside Gear Scout for now (its own tab at /gearflip) but is built to
be lifted out into its own product:

  settings.py   — its own settings file (/data/gearflip/settings.json)
  economics.py  — the profit math: fees, shipping, repair risk, time to sell,
                  confidence and score. Pure functions, no Gear Scout imports.
  portfolio.py  — the best combination of buys for your cash (pure).
  ledger.py     — what you bought and sold, and your bankroll (its own file).
  source.py     — THE ONLY FILE THAT TALKS TO GEAR SCOUT (listings database,
                  market values, repair wording, scam checks, push, email).
                  Swap this one file to run Gear Flip on its own.
  scour.py      — its own extra searches (Reverb and eBay) for flip-friendly
                  gear and broken gear, stored with Gear Scout's listings.
  engine.py     — turns listings into ranked opportunities.
  alerts.py     — phone pushes and the daily email, with the analytics.
  jobs.py       — one background pass: scour, scan, alert.
  web.py        — the /gearflip tab (a Flask blueprint).

Suggestions only: Gear Flip never buys, posts, lists, messages or contacts
anyone. All dollar figures are estimates."""

__version__ = "0.1.0"
