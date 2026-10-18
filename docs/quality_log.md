# Accuracy log

Each night (or morning), a random sample of listings shown on the Dashboard,
Steals and Telex pages is checked by hand: is it real studio/pro-audio gear, is
the comparison price (comp) right, and is it still for sale?

| Date | Sample | Relevant gear | Comp right | Still for sale | Notes |
|------|--------|---------------|------------|----------------|-------|
| 2026-10-03 (before fixes) | 70 | 81% (57/70) | ~93% of priced (2 clearly wrong, ~3 doubtful) | 100% (all checked in the last ~6 h) | Junk: 6 Shure mic pouches, 3 Vintage-brand V72 electric guitars, mic clamp part, raw woofer and rack tray (matched "SSL"), phono cartridge. Wrong comps: UA LA-2A reissue compared to 1960s Teletronix ($9.4k); Aiwa VM-12 "RCA 77 style clone" compared to $2.2k |
| 2026-10-03 (after fixes) | same 70 | 100% (all 13 junk now filtered) | both wrong comps fixed (LA-2A now vs UA ~$4.4k; Aiwa vs ~$627) | — | Fixes in scrapers/enrich.py (accessory/junk filters, Teletronix→UA reissue) and scrapers/comps.py (clone-stripped estimates) |
| 2026-10-04 (before fixes) | 60 | ~88% | ~93% | 100% | Junk: speaker voice-coil part, WE mic desk stand, Adam S3H grille, “microphone decor” lot, a few consumer items. Wrong comps: Stam clones naming the original (Sontec 432D9 $14.6k, Pultec $3.3k), NADY TCM-1100 vs $1,250. Also found: steal alerts crashing (KeyError) since launch. |
| 2026-10-04 (after fixes) | same | junk above filtered | Stam clones priced as Stam | — | enrich.py clone makers + part/decor filters; lowest.py alert crash fixed |
| 2026-10-05 (before fixes) | 60 | ~88% | ~87% | 100% (all checked within ~6 h) | Junk: AKG WMS-40 wireless, Phoenix Gold car amp, tube tester, HOSA length-named cable, Rode PSA1 arm, Tascam bass trainer, $25 Realistic console. Wrong comps were rough “similar listings” estimates (SSL 1 vs $618, Lynx Aurora 8 vs ~$3k, Maag PREQ2 vs $2.6k, NADY TCM-1100 vs $1,250). |
| 2026-10-05 (after fixes) | same | junk above filtered | rough estimates can't exceed 40% off | — | enrich.py filters + descriptor rule; comps.believable(); board._is_specific |
| 2026-10-18 Feed (before fixes) | top 40 of the Feed | 82% (33/40) | — | 100% of 37 checked (Reverb API, eBay, Craigslist, Kijiji; 2 Facebook ads not opened) · new ads: half found within 0.3 h, 23/23 within 1 h | Junk: 2 FAE "SSL-175" forestry tillers ($42k/$85k) matching "ssl"; 5 mics/mixer/recorder that only come "with XLR cable" matching "XLR cable" (Shure PGA48, Shure MV7, Shure 55S variant, Zoom H4n, Mackie 1402 bundle). Facebook term searches: Marketplace search paused by Facebook until Oct 18 23:38 UTC, groups only; some terms 13–21 h since last Facebook search. Email digest/alerts failing since Oct 15 (Gmail closes the connection). |
| 2026-10-18 Feed (after fixes) | same 40 | 100% (all 7 filtered) | — | — | enrich.py off-topic: forestry/tiller/FAE; wanted.py: cable terms skip gear that merely comes with a cable (patchbays, wall plates, connectors, cable lots still match) |

Next to watch: vintage dynamic mics with thin Reverb data (Shure 570, EV 674, Astatic 30) — comps may run high.
