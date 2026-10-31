# Code review log

Daily line-by-line review (scheduled task "Gear Scout daily code review", 4 AM):
new changes since the last reviewed commit, plus a rotating full read of the
whole codebase (about once a week).

Last reviewed commit: the "Code review (Oct 31)" commit (2026-10-31: reviewed 8a400d7..eb3c2f0) — next run's diff starts there

## Files — last fully read

| File | Last fully read | Notes |
|------|-----------------|-------|
| scrapers/reverb_api.py (curl fallback diff), market.py, mic_focus.py deep_crawl, sold_check.check_url, shipping_backfill.py | 2026-10-31 (diff) | reverb_get fallback correct; sold_check now returns "unknown" on any non-OK answer |
| scrapers/reverb_api.py, shopify_store.py, geo.py, image_cache.py | 2026-10-31 | whole files; no bugs (geo._load never retries after one failed download until restart — noted) |
| scrapers/html_scraper.py | 2026-10-31 (600–1140) | file now fully read this week; Reverb page, eBay, Audiogon, Vintage King, USAM, forums |
| scrapers/assistant.py | 2026-10-31 (355–456) | verdict ad-text fetch |
| scrapers/sold_check.py | 2026-10-30 | whole file; Reverb's own 250/hour slots verified live (368–388 checked per run, ~5 min) |
| scrapers/lowest.py, price_drops.py, auctions.py, analytics.py | 2026-10-30 | no bugs; lowest.py's alert email doesn't escape the title (cosmetic) |
| scrapers/html_scraper.py | 2026-10-30 (1–600) | Guitar Center, Sweetwater, Craigslist, start of Reverb; next chunk 600–1140 |
| scrapers/mic_focus.py | 2026-10-30 (diff + find, deep_crawl) | pair total fix and deep-read "still listed" update both correct; every stored Reverb URL has the -slug so the new LIKE is safe |
| scrapers/store.py | 2026-10-29 | |
| scrapers/runner.py | 2026-10-29 | one-term = 1 FB worker |
| scrapers/savings_slides.py | 2026-10-29 | |
| scrapers/mic_focus.py | 2026-10-29 (diff) | Distressor caps, deep_crawl, email |
| scrapers/routine_watch.py | 2026-10-29 | new |
| scrapers/base.py (LooseTerm), telex.py, board.py, enrich.py, steals.py, main.py, dashboard.mics_page | 2026-10-29 (diff) | |
| scrapers/dispatch.py | 2026-10-27 | |
| scrapers/reverb_api.py | 2026-10-27 | term search newest first |
| scrapers/ebay_api.py | 2026-10-27 | ebay_lowest now drops overseas |
| scrapers/kijiji.py | 2026-10-27 | |
| scrapers/price_check.py | 2026-10-27 | FB favorites respect pause |
| scrapers/manual_scrape.py | 2026-10-27 | |
| scripts/safe_deploy.sh | 2026-10-27 | |
| scrapers/comps.py | 2026-10-25 | |
| scrapers/wanted.py | 2026-10-28 | gear check, cable terms |
| scrapers/notify.py | 2026-10-25 | push photo |
| scrapers/mic_focus.py | 2026-10-28 | 8 new brands, deal_pct, 20% floor |
| scrapers/fb_posts_background.py | 2026-10-25 | facebook_paused, quiet hours |
| scrapers/watches.py | 2026-10-25 | |
| scrapers/live_search.py | 2026-10-25 | |
| scrapers/timefmt.py, run_status.py | 2026-10-25 | |
| templates/mics.html, sweetwater.html | 2026-10-25 | new |
| templates/base.html, steals.html, settings.html, health.html | 2026-10-25 (diff) | |
| dashboard.py | 2026-10-28 (diff) | mics_page: per-model table, model_lowest |
| scrapers/board.py | 2026-10-28 | aligned with Feed; check_fb respects the pause |
| scrapers/sold_check.py | partly | checker + _record + candidates |
| scrapers/telex.py | partly | sweep + matchers |
| scrapers/market.py | partly | lookups, load_market |
| scrapers/enrich.py | partly | filters 300–520, price_context |
| scrapers/facebook_scraper.py | partly | Marketplace region scraper |
| scrapers/fb_posts.py | partly | 1–230 |
| scrapers/emailer.py | partly | 28–200 |
| dashboard.py | partly | Feed, Health, Telex, deals cache |
| main.py | 2026-10-27 | full scrape respects FB pause |
| scrapers/steals.py | 2026-10-28 | |
| scrapers/models.py | 2026-10-28 | |
| scrapers/brand.py | 2026-10-28 | |
| scrapers/savings.py | 2026-10-28 | also Mic Focus's model prices |
| scrapers/savings_slides.py | 2026-10-18 | next in rotation |
| scrapers/shipping_backfill.py | 2026-10-28 | |
| templates/mics.html, static/style.css (mic-models) | 2026-10-28 (diff) | |
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
| 2026-10-27 | Scheduled full scrape, "Scrape now" and the favorites price check ignored facebook_paused() (8 Marketplace regions + 3 groups + every FB favorite opened while paused); only the hourly refresh checked it | Facebook sources / FB favorites skipped while paused (tested with the pause forced on and off) |
| 2026-10-27 | Lowest Price trackers: ebay_lowest didn't drop overseas sellers (live: 12 of 38 "neve 1073" results from GB/KR/CN) | is_overseas check; country + shipping now recorded (26 results, 0 overseas) |
| 2026-10-27 | main.py comment said steals are 60%+; manual_scrape return type said 2 values (returns 3) | comments fixed |
| 2026-10-28 | Mic Focus's 8 new brands (8d56455): the "is a mic" check was written with doubled backslashes (`\\bmic…\\b` inside r"…"), so "mic", "microphone", "tube", "fet", "large diaphragm" never matched; only titles with a model code got in (JZ's match had the same problem). 408 stored listings misjudged | One shared _MIC_WORDS pattern. On a DB copy: ~120 more listed, 18 more in the price range (Soyuz 013/011 FET, Josephson C551FA/C42MP, Telefunken tube mic, plain "Neumann mic" ads…), nothing dropped. Added _STUDIO_ONLY so the fix doesn't bring in "V76 Mic Pre", Telefunken direct boxes or loose "vacuum tube" ads; a first try that also left out dynamics/stands was reverted (owner searches "telefunken m80"; "MA-50 bundle with stand" is a real mic) |
| 2026-10-28 | steals.py docstring said 60%+ (MIN_PCT is 50); board.py comment said the Feed window is 45 days (it's 90) | comments fixed |
| 2026-10-29 | Mic Focus Distressor: a pair priced "$1,300 each" got total = $1,300 (qty 2), so it passed the $2,400 pair cap as a "hit" (reproduced with price_basis: basis 'each', qty 2) | total = unit × qty + shipping |
| 2026-10-29 | mic_focus.deep_crawl refreshed last_seen only; a Reverb mic wrongly marked sold stayed sold while the crawl saw it live. Its LIKE '%item/123%' also could match a longer id | also confirmed_live_at, un-marks sold; pattern '%item/ID-%' (all 19,649 stored Reverb links have the -slug) |

| 2026-10-31 | The AI "verdict" button read a Reverb ad's full text with plain requests, which Cloudflare has been turning away (the Oct 31 curl fallback skipped it); it silently fell back to the short stored text | assistant._full_ad_text uses reverb_get. Tested live on a DB-free copy; all 30 dashboard GET pages return 200 |

### Left for the owner (2026-10-31)
- Nothing needing a decision. AudioKarma is still blocked (per the nightly's note).

### Left for the owner (2026-10-29)
- Routine watchdog pushes/emails when the nightly or code-review log is over 30 h old; it uses the log files' modified times, so any edit to those logs resets it. Fine as is.
- Deep Reverb read does one `UPDATE … LIKE '%…%'` per live mic it already knows (a table scan each, a few hundred every 2 h). Works; if it ever feels slow, index by Reverb id.
- Non-"pair" wording like "Stereo" alone ("EL8X Distressor … Stereo") counts as a single in the Distressor quantity check. Noted only.
- Deploy: safe_deploy.sh both at ~20:05 PT from ac1a8f7 (normal build worked); /login and / return 200, no tracebacks in either container.
- Read and fine: LooseTerm (only for one-term searches ending in "microphone"; survives run_sources untouched), mics_page `listed` sort key, Distressor classify (plugins, brochures, 'sold' titles out), scrapers/store.py, runner.py, savings_slides.py in full.

### Left for the owner (2026-10-28)
- With the new-brand fix, these now show in the brand sections too: AKG/Mojave/Soyuz/Telefunken **dynamic** mics (AKG D5/D7, Mojave MA-D packs, Soyuz V1, Telefunken D19, DD5 drum kit) and AKG boundary/paging mics (PCC160, PZM). Dynamics were kept on purpose because "telefunken m80" is one of your searches. Say if the big-brand sections should be studio condensers/ribbons only.
- Mic Focus Deals/Steals (steals.py) check a mic's own limit against the listing price; the Mics page checks price + shipping. Small difference, not changed.
- Uncommitted edits from another session were in the working tree (dashboard.py "newest ad first" sort on the Mics page, enrich.py Sound Storm car-amp filter, mics.html header text, docs/studio-wiring/). Not part of this review and not committed or deployed by it; read and they look fine.
- Reminder: the dashboard sign-in gate is switched off (dashboard.require_login returns early, per your earlier request), so anyone with the ngrok link can open Settings.
- Price Board trend: "a week / month ago" takes the last value in database order, not date order. It works because days are added in order; noted only.
- Deploy: this review built overlay images from 1c3c567, but other sessions committed (7d35c51…993cae1) and redeployed before they were used. Both running containers already have this fix (mic_focus.py identical to HEAD), so the older overlay was not deployed and `:latest` points at the running images. /login and / return 200, no tracebacks.

### Left for the owner (2026-10-27)
- The 4 daily full scrapes don't follow the 1–7 AM Facebook quiet hours (the hourly refresh does). With the schedule at 7 AM, noon, 5 PM, 9 PM none falls inside them, so nothing changed; only matters if the schedule moves.
- Kijiji listings don't record a country (they're all Canadian). Noted only.
- Deploy: the nightly task rebuilt scout at 14:10 PT after this review's commit, so scout already had these fixes. The dashboard was deployed by this review with safe_deploy.sh (it also carries the nightly's sold_check change in 073a4c8, read and fine). Rollback images: gear-scout-{scout,dashboard}:pre-review-1027.

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
