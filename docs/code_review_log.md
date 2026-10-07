# Code review log

Daily line-by-line review (scheduled task "Gear Scout daily code review", 4 AM):
new changes since the last reviewed commit, plus a rotating full read of the
whole codebase (about once a week).

Last reviewed commit: 60bed1b

## Files — last fully read

| File | Last fully read | Notes |
|------|-----------------|-------|
| scrapers/store.py | 2026-10-06 | purge by last_seen, price raise, sold undo |
| scrapers/runner.py | 2026-10-06 | |
| scrapers/dispatch.py | 2026-10-06 | |
| scrapers/reverb_api.py | 2026-10-06 | term search newest first |
| scrapers/ebay_api.py | 2026-10-06 | specific terms on all of eBay |
| scrapers/comps.py | 2026-10-06 | |
| scrapers/wanted.py | 2026-10-06 | new module |
| scrapers/notify.py | 2026-10-06 | push photo |
| scrapers/board.py | 2026-10-06 (build) | aligned with Feed |
| scrapers/sold_check.py | partly | checker + _record + candidates |
| scrapers/telex.py | partly | sweep + matchers |
| scrapers/market.py | partly | lookups, load_market |
| scrapers/enrich.py | partly | filters 300–520, price_context |
| scrapers/facebook_scraper.py | partly | Marketplace region scraper |
| scrapers/fb_posts.py | partly | 1–230 |
| scrapers/emailer.py | partly | 28–200 |
| dashboard.py | partly | Feed, Health, Telex, deals cache |
| main.py | 2026-10-05 (session) | |
| everything else | never | |

## Findings

| Date | Finding | Fix |
|------|---------|-----|
| 2026-10-06 | Manual "Scrape now" purged everything over 30 days; didn't search Wanted terms | Fixed |
| 2026-10-06 | Wanted fallback in keyword_match caught exclude-word checks (all Wanted ads dropped ~45 min) | ScrapeKeywords only |
| 2026-10-06 | Feed / Telex / Price Board / digest used different deal formulas and matching | One Wanted list, one deal calc |
| 2026-10-06 | Telex page hid everything under 10% off (31 of 126 new ads shown) | Built from Feed listings |
| 2026-10-06 | Reverb/Kijiji term searches sorted by relevance; eBay only Pro Audio category | Newest first; all eBay for specific terms |
