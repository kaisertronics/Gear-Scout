# Working on Gear Scout

Gear Scout is the owner's self-hosted used pro-audio gear deal finder. Two Docker
containers from the same image: `scout` (main.py — scheduler + scrapers in scrapers/)
and `dashboard` (dashboard.py — Flask on http://localhost:8420, templates/, static/),
plus `ngrok` for the phone link. Data lives in the Docker volume at /data
(SQLite /data/seen_listings.db). Settings and secrets are in config.yaml and .env.

## The owner
- Not technical: explain everything in plain language, no jargon. Work like a
  proactive employee — find and fix problems before they're pointed out, suggest
  ideas, and just do things rather than asking how.
- Buys gear for their own studio (not flipping), usually $500–$2,000. Wants FRESH
  ads (posted in the last few days); Facebook Marketplace and Craigslist first
  (local sellers price lowest and many ship), eBay/Reverb also fine.
- Uses a mix of alerts (phone push via ntfy + email) and browsing on phone/computer.
- Only uses their Claude subscription — never build features that need a paid
  Anthropic API key. Gemini via their own free key (Settings → AI) is fine.

## Rules
- Deploy ONLY with `scripts/safe_deploy.sh [dashboard|scout|both]` — it waits while
  a search/scrape is running or the owner is using the site. Never bare
  `docker compose build/up` on a running install.
- Never commit config.yaml or .env (they hold passwords/keys; the repo is PUBLIC).
  Never print or log secrets.
- Never add "Co-Authored-By: Claude" or any Claude/AI attribution to commits or code.
- Don't change global git config. Don't delete the owner's data (favorites, hidden,
  Telex List/groups, settings) unless they clearly ask.
- Be gentle with the owner's Facebook account and eBay's API (~5,000 calls/day).
  Never bypass captchas, logins or bot protection (Guitar Center and Sweetwater
  block automation — leave them).
- Keep the Mac's load modest; prefer caching and background work over making a
  page wait. Test pages with header `X-GearScout-Test: 1` (not counted as visits).
- User-visible changes get an entry at the top of static/changelog.json (What's new).

## Before and after every change
Walk the pages as the owner would (Dashboard, Deals, Steals, Telex List, Price Board
at phone width): are the top ads fresh, in budget, real studio gear, priced right,
still for sale, loading fast? Fix what looks wrong.

## Nightly routine
docs/nightly-routine.md — accuracy audit (scores in docs/quality_log.md), health
check, one research angle, 2–5 improvements, changelog, docs/ideas.md, commit +
push, short morning push to the owner. Set it up to run nightly (~1:45 AM) on
whatever computer runs Gear Scout.

## Moving / reinstalling
"Start Gear Scout.command" builds everything, restores a migration bundle's data
(migration/gear_scout_data.tgz) the first time only, and starts it.
