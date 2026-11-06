from scrapers.deal_checks import scam_flags

POS = [
    "I'm out of town, my agent will deliver after payment",
    "Out of the country for work, shipper will come pick it up",
    "Pay first then I ship it to you",
    "Shipping company picks up after you pay",
    "Sold through eBay protection, safe sale",
    "PayPal friends and family only",
    "Please use PayPal F&F",
    "Send the verification code you receive",
    "Google Voice code needed to confirm you are real",
    "Send a deposit to hold it for you",
    "I will overpay and you refund the difference",
]
NEG = [
    "Fender Strat, PayPal accepted",
    "Shipping available, great condition",
    "Sold my friends and family a few of these, great amp",
    "Pickup in town, cash only",
    "Moving sale, local pickup, comes with case",
    "Includes a refund policy within 30 days",
    "Shipping via UPS, buyer pays shipping",
    "Great condition, eBay listing also available",
]


def flags(t):
    return scam_flags({"title": t, "description": "", "source_name": "eBay"}, None, 100.0)


bad = [t for t in POS if not flags(t)] + [t for t in NEG if flags(t)]
for t in bad:
    print("FAIL:", t, flags(t))
print("failures:", len(bad), "of", len(POS) + len(NEG))
raise SystemExit(1 if bad else 0)
