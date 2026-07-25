#!/usr/bin/env python3
"""
Set the destination tile prices from the card prices already in rates.json.

The earlier version read price_map.csv and the search cache, which broke
whenever price_map.csv was rebuilt. rates.json already holds every priced
card, however it was priced, so take the cheapest per destination from there.

The destination comes from the card's own slug, so nothing needs matching.

    py destination_tile_prices.py
    py destination_tile_prices.py --dry-run
"""

import json, os, re, sys, shutil, collections

RATES = "rates.json"
if not os.path.exists(RATES):
    sys.exit(f"{RATES} not found.")
dry = "--dry-run" in sys.argv

# tile slug -> words that identify a card as belonging to that destination
DEST = [
    ("zanzibar",       ("zanzibar", "tanzania")),
    ("mauritius",      ("mauritius",)),
    ("maldives",       ("maldives",)),
    ("seychelles",     ("seychelles",)),
    ("kenya",          ("kenya", "mombasa", "diani", "malindi", "bamburi")),
    ("victoria-falls", ("zimbabwe", "zambia", "victoria-falls", "livingstone")),
]
TILE_SLUGS = {d for d, _ in DEST}

feed = json.load(open(RATES, encoding="utf-8"))
rates = feed.setdefault("rates", {})

by_dest = collections.defaultdict(list)
for slug, v in rates.items():
    if slug in TILE_SLUGS or v.get("is_destination_tile"):
        continue                       # do not feed tiles back into themselves
    for dest, words in DEST:
        if any(w in slug.lower() for w in words):
            by_dest[dest].append((slug, v))
            break

added, missing = [], []
for dest, _ in DEST:
    cards = by_dest.get(dest, [])
    if not cards:
        missing.append(dest); continue
    slug, v = min(cards, key=lambda x: x[1]["price"])
    rates[dest] = {
        "price": v["price"], "board": v.get("board", ""),
        "nights": v.get("nights", 7), "adults": v.get("adults", 2), "rooms": 1,
        "basis": "total for the room, accommodation only",
        "hotel": v.get("hotel", ""), "from_card": slug,
        "is_destination_tile": True,
    }
    added.append((dest, v["price"], v.get("nights", 7), len(cards), v.get("hotel", "")))

print(f"  cheapest priced card per destination, from {len(rates)} entries\n")
for dest, price, nights, n, hotel in added:
    print(f"   {dest:16s} GBP {price:>7,.0f}  {nights}n   "
          f"(cheapest of {n} priced cards)  {hotel[:26]}")
if missing:
    print(f"\n  {len(missing)} destinations have no priced card, tile stays blank:")
    for d in missing:
        print(f"     {d}")

if dry:
    print("\n  dry run, nothing written")
else:
    shutil.copy(RATES, RATES.replace(".json", "_before_tiles.json"))
    json.dump(feed, open(RATES, "w", encoding="utf-8"), indent=1)
    print(f"\n  {len(rates)} keys now in {RATES}")
    print("  Next:  py make_snippet.py --feed <raw github url>")
