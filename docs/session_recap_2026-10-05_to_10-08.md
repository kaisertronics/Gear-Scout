# Gear Scout — session recap (Oct 5–8, 2026)

What we did together in this chat, in plain English. Every change is also in
the project history (`git log`) and on the in-app **What's new** page.

## 1. Moving to the new Mac
- Gear Scout now runs on this Mac ("blake's MacBook Pro (2)"). Your listings,
  favorites, price history and Facebook login were restored (~34,000 listings).
- The phone link (ngrok) works from this Mac. Keep Gear Scout off on the old one.
- You were given commands to keep the Mac awake (they need your password, so you
  run them): `sudo pmset -c sleep 0 disksleep 0 standby 0 powernap 0` and, to stay
  on with the lid closed, `sudo pmset -a disablesleep 1`.
- Turn on Docker Desktop → Settings → General → "Start Docker Desktop when you sign in".
- Remote Control is on for this chat, so you can follow it from the Claude app.

## 2. The big redesign (Oct 6)
Your complaints: missing new gear, stale/repeating results, sold ads still showing,
pages disagreeing with each other. Decisions you made:
- **Wanted list = only your Telex List.** The 1,000+ keyword list and the imported
  Cole Picks model group no longer put listings in front of you (the Cole Picks
  group was later removed entirely; a backup of your settings from before that is
  in `migration/config.yaml.before-removing-cole-picks`).
- **Built all at once.**

What changed:
- **Feed** is the home page: every listing for sale that matches your Telex List,
  at any price, newest post first, "New since you last looked" on top.
- **Faster searching:** every term is searched directly on Reverb, Craigslist,
  Kijiji, ShopGoodwill, Long & McQuade, OfferUp every 30 min, eBay every 2 h,
  Facebook Marketplace + groups + group posts in rotation all day.
- **Phone push** (with photo) for every new matching ad within ~30 minutes.
- **Sold Facebook ads** are now detected (a deleted ad shows an empty page).
- **Health** page: when each term was last searched on each site, and how fast
  new ads reach you.

## 3. Everything follows your list (Oct 6)
- Auction alerts, digest email, steal / price-drop alerts, AI briefing, Deals and
  Steals pages all use your Telex List only (auction alerts went from 16 random
  items to 2 relevant ones).
- One deal calculation everywhere — a listing shows the same "% under" on every
  page and in every email.
- Telex page shows every match at any price (it used to hide anything not 10%+
  under — 31 of 126 new ads were shown).
- Scrape now (on the Feed) re-reads every site and searches every term.

## 4. Fixes from the line-by-line review (Oct 6)
- Ads were purged by first-seen date (long-running ads came back as "new") — now by last sighting.
- Manual scrape deleted everything older than 30 days — fixed.
- Reverb/Kijiji term searches weren't newest-first; eBay only searched Pro Audio — fixed.
- A bug I introduced dropped all Wanted ads for ~45 min (exclude-word check) — fixed, nothing lost.
- "Still listed" only shows when a check really confirmed it.
- Facebook: one shared pause for all Facebook searching if Facebook asks to slow down.

## 5. Scheduled tasks (run while the Mac is awake and the Claude app is open)
- **2:10 AM — Gear Scout nightly improvements:** accuracy/freshness check, small fixes, What's new note.
- **4:00 AM — Gear Scout daily code review:** reads every changed line plus a rotation of whole
  files (whole codebase about weekly), fixes confirmed bugs, logs in `docs/code_review_log.md`.

## 6. Cleaning up results (Oct 7)
- Broad terms ("DIY", "Golden age") and short codes ("se8") only match real audio gear.
- Filtered: tractors/loaders/trailers ("SSL attach"), guitar pickups (Seymour Duncan SSL-1),
  guitars by model, clothing, furniture ("Mart Stam" chairs), records ("Whirr – Distressor"),
  repair jobs, add-on cards/ROMs/displays/grills, plug-ins.
- Tips: "LA-2A" also finds copies — use "Teletronix LA-2A"/"UA LA-2A"; "se8" also finds a
  Samson monitor — use "sE Electronics sE8"; "ssl"/"stam" are broad — consider
  "Solid State Logic", "SSL Fusion", "Stam Audio".
- "sabatron" was a misspelling of **Sebatron** — removed; Sebatron searched everywhere.

## 7. Cheapest listings (Oct 7–8)
- **Price Board** (top menu): per term, the cheapest working one still for sale anywhere,
  best deal this week, and price trend.
- The cheapest for each term **pulses gold** ("💲 Lowest") and is first in that term on the
  Telex page and when you filter the Feed by a term.
- For terms covering several models, the **cheapest of each model** comes first, cheapest
  model first, with a gold "💲 Lowest <model>" tag (d12 → D12VR, D12, D12E…; Lexicon pcm →
  PCM60, PCM70, PCM91…).
- No age limit: any working ad still for sale counts, however old.
- Spellings are normalised (Avalon "VT-737SP" = "737 sp" = "737"; Distressor EL8 / EL8X / EL8XS).
- Each pick is re-checked to still be for sale; if it's gone, the next cheapest takes over.

## 8. Other
- Favorites: pink titles, and a pink ★ Favorites link in the top bar.
- Site hang (Oct 7) fixed: Scrape now runs separately from the website; the Feed never
  makes you wait while it refreshes.
