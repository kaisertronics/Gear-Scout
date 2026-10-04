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

Next to watch: vintage dynamic mics with thin Reverb data (Shure 570, EV 674, Astatic 30) — comps may run high.
