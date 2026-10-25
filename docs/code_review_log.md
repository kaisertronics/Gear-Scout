# Code review log

Daily line-by-line review (scheduled task "Gear Scout daily code review", 4 AM):
new changes since the last reviewed commit, plus a rotating full read of the
whole codebase (about once a week).

Last reviewed commit: 66511fe + the "Code review (Oct 25)" commit right after it (reviewed 2026-10-25: ba581a9..66511fe, 31 commits, incl. all of Mic Focus) — next run's diff starts at the Code review (Oct 25) commit

## Files — last fully read

| File | Last fully read | Notes |
|------|-----------------|-------|
| scrapers/store.py | 2026-10-20 | |
| scrapers/runner.py | 2026-10-20 | one-term = 1 FB worker |
| scrapers/dispatch.py | 2026-10-06 | |
| scrapers/reverb_api.py | 2026-10-06 | term search newest first |
| scrapers/ebay_api.py | 2026-10-06 | specific terms on all of eBay |
| scrapers/comps.py | 2026-10-25 | |
| scrapers/wanted.py | 2026-10-18 | gear check, cable terms |
| scrapers/notify.py | 2026-10-25 | push photo |
| scrapers/mic_focus.py | 2026-10-25 | new (Oct 22–24) |
| scrapers/fb_posts_background.py | 2026-10-25 | facebook_paused, quiet hours |
| scrapers/watches.py | 2026-10-25 | |
| scrapers/live_search.py | 2026-10-25 | |
| scrapers/timefmt.py, run_status.py | 2026-10-25 | |
| templates/mics.html, sweetwater.html | 2026-10-25 | new |
| templates/base.html, steals.html, settings.html, health.html | 2026-10-25 (diff) | |
| dashboard.py | 2026-10-25 (diff) | mics, sweetwater, login hash, inbox done, deals defaults |
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
| scrapers/lowest.py | 2026-10-20 | FB every 6 h, carry-over |
| scrapers/base.py | 2026-10-20 | keyword_match Wanted fallback only for ScrapeKeywords ✓ |
| scrapers/price_drops.py | 2026-10-20 | |
| scrapers/auctions.py | 2026-10-20 | |
| scrapers/analytics.py | 2026-10-20 | new |
| templates/feed.html, feed_all.html | 2026-10-20 (diff) | inbox |
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
| 2026-10-20 | Inbox: ✓ Done on a steal (or any card) brought the same ad straight back under "New since you last looked" until your next visit | Done anywhere keeps it out of Steals/New; a further price cut or an auction ending still shows |
| 2026-10-20 | All-ads page (/?all=1): "All", "Newest first" and "show all terms" sent you back to the inbox | links keep all=1 |
| 2026-10-20 | Price Board trackers (Oct 19 change) skipped Facebook 5 checks in 6 and rebuilt the top 10 without it: the lowest jumped up and a pricier ad could be pushed as a "new lowest" | last Facebook results carried over between Facebook checks (unless sold/pending/hidden) |
| 2026-10-20 | Background tracker / saved-search searches ignored the Facebook pause and the 1–7 AM quiet hours | live search drops Facebook while paused; trackers/watches skip it overnight |
| 2026-10-20 | Analytics: two page loads at once could both start the hourly rebuild | flag set before the thread starts |
| 2026-10-25 | Mic Focus didn't pause Telex pushes: the hourly refresh still called wanted.alert_new (20 pushes in 24 h) and the full scrape still ran check_telex_lowest | Skipped in Mic Focus. These edits (plus ISO-timestamp cutoffs in dashboard._for_you, main.market_job, image_cache.cache_recent) were sitting uncommitted in the working tree since Oct 23 and not deployed; reviewed and committed here |
| 2026-10-25 | Mic auctions ignored per-model caps: a $350 WA-14 auction listed/pushed with "your limit $900" (reproduced with a test row on a DB copy) | auctions() uses the lower model cap |
| 2026-10-25 | Deals/Steals in Mic Focus let Canadian mics through (Mics page is US-only): 2 of 73 | skipped in focus mode |
| 2026-10-25 | "Rolls RM67 Mic/Source Mixer", "Vanguard VM304a … Sound Mixer" classified as focus mics | mixers in _NOT_MIC |
| 2026-10-25 | Saving Mics settings didn't invalidate the Deals/Steals cache (up to 10 min stale) | _data_changed() |
| 2026-10-25 | Mic "fresh" (push window) used last_seen only, not COALESCE(last_seen, first_seen) | COALESCE (0 rows affected today) |

### Left for the owner (2026-10-25)
- In Mic Focus, the scheduled full scrape still sends the digest email, favorite price-drop pushes still go out, and the background Facebook post search (fb_posts_background) still walks the Telex List (its roundup email is off). The Mics page lists what's paused (Telex matches, steals, price drops, auctions, AI briefing) and these aren't on it, so left as they are — say if they should pause too, or if the post search should use the mic terms.
- Mics page "In your price range — $300 and up" header: Warm's own minimum is $150, so a $200 Warm can be listed there. Text only.
- Sign-in: the email check uses secrets.compare_digest on text, which errors (500) on a non-ASCII character typed in the email box. Harmless; not changed.

### Left for the owner (2026-10-20)
- Price-drop alerts and auction alerts judge "deal" with comps.evaluate (15%/40% thresholds), not steals.find. That's separate from the one-deal-calculation rule; it looks deliberate (different thresholds, bids), so left alone — say if they should use the same numbers as Deals.
- Analytics "for sale now" = seen in the last 3 days and not sold (not confirmed_live_at). Fine for counts; noted only.
- A live search you start yourself while Facebook has paused the account now skips Facebook (it used to search it anyway).

### Left for the owner (2026-10-18)
- Steals threshold: steals.MIN_PCT = 50 (your Oct 11 choice). The review checklist still says 60% — tell me if 50 stands so the checklist can be updated.
- Price Board "still for sale" check (board.verify_cheapest, every 30 min) opens every unconfirmed cheapest Facebook ad (~70 of the ~300 it watches) in the Facebook browser every ~3 h. That's extra Facebook load; it could be limited to each term's overall cheapest if you want it gentler.
- Deployed 2026-10-18 ~06:40 as code-only overlay images built from d6428ea, which also includes the nightly fixes (8d22518) and the merged "inbox front page" branch (451a7a3). Those two weren't part of this review; the next run's diff from 795fb94 covers them. Rollback images: gear-scout-{scout,dashboard}:pre-review-1018.
- Ran in parallel with the nightly accuracy task, which had its own uncommitted edits (cable terms, forestry filter, email status on Health). This review committed only its own changes.
- Small, not changed: steals.MIN_REF comment still says "60% off"; Saved slides "PIECES OF GEAR" counts rows, not pieces; Saved running-total chart assumes the total stays positive.

### 2026-10-20 follow-ups (owner said "fix")
- Price Board verify_cheapest: Facebook only for each term's overall cheapest, 6 h window, skipped in marketplace_quiet_hours (tested: 20 of 72 FB ads opened when all were due).
- Trek bike / Rackshelf junk under "ssl" filtered; MIN_REF comment; slide cover counts pieces; running chart scaled with a $0 line.
- Email: working again (digest sent Oct 20); steals threshold 50% kept per owner's Oct 11 choice.
