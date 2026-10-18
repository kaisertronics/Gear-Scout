# Code review log

Daily line-by-line review (scheduled task "Gear Scout daily code review", 4 AM):
new changes since the last reviewed commit, plus a rotating full read of the
whole codebase (about once a week).

Last reviewed commit: 795fb94 (reviewed 2026-10-18; the review's own fix commit follows it)

## Files — last fully read

| File | Last fully read | Notes |
|------|-----------------|-------|
| scrapers/store.py | 2026-10-06 | purge by last_seen, price raise, sold undo |
| scrapers/runner.py | 2026-10-06 | |
| scrapers/dispatch.py | 2026-10-06 | |
| scrapers/reverb_api.py | 2026-10-06 | term search newest first |
| scrapers/ebay_api.py | 2026-10-06 | specific terms on all of eBay |
| scrapers/comps.py | 2026-10-06 | |
| scrapers/wanted.py | 2026-10-18 | gear check, cable terms |
| scrapers/notify.py | 2026-10-06 | push photo |
| scrapers/board.py | 2026-10-18 (diff: build, verify_cheapest) | aligned with Feed |
| scrapers/sold_check.py | partly | checker + _record + candidates |
| scrapers/telex.py | partly | sweep + matchers |
| scrapers/market.py | partly | lookups, load_market |
| scrapers/enrich.py | partly | filters 300–520, price_context |
| scrapers/facebook_scraper.py | partly | Marketplace region scraper |
| scrapers/fb_posts.py | partly | 1–230 |
| scrapers/emailer.py | partly | 28–200 |
| dashboard.py | partly | Feed, Health, Telex, deals cache |
| main.py | 2026-10-05 (session) | |
| scrapers/steals.py | 2026-10-18 | |
| scrapers/models.py | 2026-10-18 | new |
| scrapers/brand.py | 2026-10-18 | new |
| scrapers/savings.py | 2026-10-18 | new |
| scrapers/savings_slides.py | 2026-10-18 | new |
| scrapers/shipping_backfill.py | 2026-10-18 | new |
| everything else | never | |

## Findings

| Date | Finding | Fix |
|------|---------|-----|
| 2026-10-06 | Manual "Scrape now" purged everything over 30 days; didn't search Wanted terms | Fixed |
| 2026-10-06 | Wanted fallback in keyword_match caught exclude-word checks (all Wanted ads dropped ~45 min) | ScrapeKeywords only |
| 2026-10-06 | Feed / Telex / Price Board / digest used different deal formulas and matching | One Wanted list, one deal calc |
| 2026-10-06 | Telex page hid everything under 10% off (31 of 126 new ads shown) | Built from Feed listings |
| 2026-10-06 | Reverb/Kijiji term searches sorted by relevance; eBay only Pro Audio category | Newest first; all eBay for specific terms |
| 2026-10-18 | Gear check for broad/short-code terms (added Oct 7) hid real gear whose title has no gear word and no glued model code: SSL Big Six/Fusion/AWS 948, Peluso SR 14, Audioscape, Stam Audio, BeesNeez, Sebatron, Warm Audio WA 19, Golden Age GA 47 (~84 listings) | wanted._looks_like_gear: spaced models ("SR 14") count; a title that starts with a one-word maker term counts. Re-run on real rows: 0 lost, 84 gained, junk among them (comics, car stereo, anhydrous applicator) filtered in enrich._OFF_TOPIC |
| 2026-10-18 | Feed with a term + Deals/Steals/distance: pinned "cheapest per model" skipped the filters (ssl + Steals: ~48 ads, 1 steal) | pinned limited to the filtered view |
| 2026-10-18 | Price Board partial rebuilds (verify_cheapest, term adds) reset "built", so the hourly full rebuild was put off (03:18 → 07:10 overnight) | partial builds keep the last full build time |
| 2026-10-18 | enrich._JUNK "(LP)" / "[LP]" patterns could never match (wrapped in \b…\b) | lp(?=[)\]]) |
| 2026-10-18 | Saved: "NS10s" / "C414s" counted as 1 (docstring said pair) | letter+digits+s = pair, searched without the s (note: a model really ending in S, e.g. Yamaha HS8S sub, would read as a pair) |

### Left for the owner (2026-10-18)
- Steals threshold: steals.MIN_PCT = 50 (your Oct 11 choice). The review checklist still says 60% — tell me if 50 stands so the checklist can be updated.
- Price Board "still for sale" check (board.verify_cheapest, every 30 min) opens every unconfirmed cheapest Facebook ad (~70 of the ~300 it watches) in the Facebook browser every ~3 h. That's extra Facebook load; it could be limited to each term's overall cheapest if you want it gentler.
- Deployed 2026-10-18 ~06:40 as code-only overlay images built from d6428ea, which also includes the nightly fixes (8d22518) and the merged "inbox front page" branch (451a7a3). Those two weren't part of this review; the next run's diff from 795fb94 covers them. Rollback images: gear-scout-{scout,dashboard}:pre-review-1018.
- Ran in parallel with the nightly accuracy task, which had its own uncommitted edits (cable terms, forestry filter, email status on Health). This review committed only its own changes.
- Small, not changed: steals.MIN_REF comment still says "60% off"; Saved slides "PIECES OF GEAR" counts rows, not pieces; Saved running-total chart assumes the total stays positive.
