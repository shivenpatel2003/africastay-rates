#!/usr/bin/env python3
"""
AfricaStay live rate fetcher, version 2.

WHY V2 EXISTS
    v1 assumed the booking engine's hotel id could be joined to the public URL
    slug on the widget cards. It cannot. Checked against real data:
        addToCart ids matching a register slug ....... 0 of 16
        card link slugs matching an addToCart id ..... 0 of 6
    The engine identifies hotels in a separate namespace. So the join has to be
    an explicit mapping that a human has confirmed, and anything unmapped must
    show no price rather than a guessed one.

TWO MODES

  1. Build the mapping (do this once, then whenever cards change)

         py rate_fetcher_v2.py --build-map

     Searches every destination, lists every hotel the engine returns, and
     proposes a match for each priced widget card. Writes price_map.csv with a
     confidence column. YOU CONFIRM EACH ROW before it is used. Nothing is
     published from a row until Confirmed is set to Y.

  2. Fetch rates (run this nightly)

         py rate_fetcher_v2.py

     Uses only confirmed rows. Searches several departure dates and takes the
     lowest price per hotel, so "from £X" is a real minimum and not one
     arbitrary date. Writes rates.json for the page script.

WHAT THE PRICE IS
    Total for the room, for the searched occupancy, accommodation only, for the
    board basis of the cheapest available rate. Verified: 2 adults returned £679
    and 1 adult returned £509 for the same room, so it is a room total with a
    single-occupancy discount, NOT a per-person price. Do not label it "pp".
"""

import argparse, csv, json, re, sys, time, difflib
from datetime import date, datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("Install first:  py -m pip install requests")

SITE = "https://www.africastay.co.uk"
UA = {"User-Agent": "AfricaStay internal rate fetcher (sales@africastay.co.uk)",
      "X-Requested-With": "XMLHttpRequest"}

# Each destination is (label, [(search_destination, destination_code), ...]).
#
# A code scopes the search to ONE AREA, not a country. Codes captured from a
# hotel page are that hotel's area only: le-morne-mu is a corner of Mauritius,
# athuruga-mv is one atoll, mahe-island-sc excludes Praslin, mombasa-ke
# excludes Diani. zanzibar-tz happens to be island-wide, which is why Zanzibar
# worked from a single code.
#
# So a destination may need several codes. Add every area your cards cover;
# results are merged and de-duplicated, and the lowest price per hotel wins.
# Each destination is (label, [(search_destination, destination_code), ...]).
#
# A code scopes the search to one AREA unless an umbrella code exists.
# Confirmed by testing:
#     zanzibar-tz        174 hotels   island-wide, covers all 52
#     maldives-all-mv    244 hotels   covers all 70
#     seychelles-all-sc   32 hotels   covers all 31
#     diani-beach-ke      29 hotels   one Kenyan area
#     mombasa-ke                      one Kenyan area
#     le-morne-mu                     one Mauritian area of 18
# Returned zero, so the format is wrong for these: mauritius-mu, mauritius,
# mauritius-all-mu, balaclava-mu, kenya-ke, zimbabwe-zw, praslin-island-sc.
#
# Results across codes are merged and de-duplicated; lowest price per hotel wins.
# (label, [(search_destination, code), ...], nights)
#
# Nights is per destination because the cards differ: Victoria Falls cards
# advertise 3 nights while every other destination advertises 7. Searching
# 7 nights for all of them would put a week's price on a 3-night card.
DESTINATIONS = [
    ("Zanzibar",       [("Zanzibar, Tanzania", "zanzibar-tz")], 7),
    ("Mauritius",      [("Mauritius Island, Mauritius", "mauritius-island-mu")], 7),
    ("Maldives",       [("Maldives (All)", "maldives-all-mv")], 7),
    ("Seychelles",     [("Seychelles (All)", "seychelles-all-sc")], 7),
    ("Kenya",          [("Mombasa, Kenya", "mombasa-ke"),
                        ("Diani-Beach, Kenya", "diani-beach-ke"),
                        ("Malindi, Kenya", "malindi-ke")], 7),
    ("Victoria Falls", [("Victoria-Falls, Zimbabwe", "victoria-falls-zw"),
                        ("Livingstone, Zambia", "livingstone-zm")], 3),
]

# Codes come from the site's own destination autocomplete, which returns
# {"id": "mauritius-island-mu", "value": "Mauritius Island", ...}. Verified
# working: zanzibar-tz (174), maldives-all-mv (244), seychelles-all-sc (32),
# diani-beach-ke (29), mombasa-ke, victoria-falls-zw, mauritius-island-mu.
# Unverified but following the same pattern: malindi-ke, livingstone-zm.
# A code that returns 0 is simply skipped, so a wrong guess costs coverage,
# never correctness.

CC = {"Mauritius": "mu", "Kenya": "ke", "Zimbabwe": "zw", "Zambia": "zm",
      "Seychelles": "sc", "Maldives": "mv", "Tanzania": "tz"}

# A 7-night stay outside this band is a supplier data error, not a rate.
# Zanzibar currently has one at £125,490. Anything outside is never published.
MIN_SANE, MAX_SANE = 200.0, 25000.0

# The feed is GBP only, by decision. The site's currency switcher changes a
# session cookie, so this is forced on every run and verified in every response.
# Anything returned in another currency is dropped, never relabelled.
CURRENCY = "GBP"

# Raw search results, written before any processing, so a failure downstream
# never costs another 30-minute run. Rebuild from it with --from-cache.
CACHE = "rates_cache.json"

# Real poll contract, read off the browser's request:
#   /hotels/results/<search_id>/<hash>/<page>
#       ?results_hash=0987...&results_counter=N&async=true&just_the_rooms=true
# search_id and hash BOTH come from the POST response and both live in the
# path. The results_hash query value is a fixed placeholder, not the hash;
# it is a cache-buster. just_the_rooms=true is what returns the "rooms"
# fragment instead of a whole page.
POLL = "{site}/hotels/results/{sid}/{hash}/{page}"
RESULTS_HASH_CONST = "09876543210987654321098765432109"

H3   = re.compile(r"<h3>\s*(.+?)\s*(\d)\*\s*,\s*(.+?)\s*,\s*([^,<]+?)\s*</h3>")
MINP = re.compile(r"hotel_header_min_price'\)\.html\('.*?prep-curr\">.?</span>\s*([\d,]+)<sup>(\d\d)</sup><span class=\\?\"curr\\?\">([A-Z]{3})")
MEAL = re.compile(r'class="room-meal">.*?</span>\s*([a-z ]+?)\s*</div>', re.S)
CART = re.compile(r'addToCart\(\{"id":"([^"]+)"')


# ----------------------------------------------------------------- searching
def build_payload(destination, code, check_in, nights, adults, rooms):
    p = {"search_type": "hotel", "departure": "", "departure_code": "",
         "destination": destination, "destination_code": code,
         "check_in": check_in.strftime("%d/%m/%Y"),
         "check_out": (check_in + timedelta(days=nights)).strftime("%d/%m/%Y"),
         "number_of_rooms": rooms, "no_nights": nights,
         "transfer_search": 0, "direct_flight": 0, "visitor": ""}
    for r in range(1, 6):
        p[f"room_{r}_no_of_adults"] = adults if r <= rooms else 0
        p[f"room_{r}_no_of_children"] = 0
        for c in ("first", "second", "third", "fourth", "fifth"):
            p[f"room_{r}_{c}_child_age"] = 0
    return p


def parse_hotels(html):
    out = []
    for block in re.split(r"(?=<h3>)", html or ""):
        h, m = H3.search(block), MINP.search(block)
        if not (h and m):
            continue
        meal, cart = MEAL.search(block), CART.search(block)
        out.append({
            "engine_id": cart.group(1) if cart else "",
            "engine_name": h.group(1).replace("&amp;", "&").strip(),
            "stars": h.group(2), "engine_area": h.group(3).strip(),
            "price": float(m.group(1).replace(",", "")) + int(m.group(2)) / 100,
            "currency": m.group(3),
            "board": (meal.group(1).strip() if meal else ""),
        })
    return out


def _dump(tag, resp, args):
    """Write a raw response to disk so the real contract can be read off it."""
    if not args.debug:
        return
    import os
    os.makedirs("debug", exist_ok=True)
    path = os.path.join("debug", tag)
    body = resp.content if hasattr(resp, "content") else b""
    with open(path, "wb") as fh:
        fh.write(body)
    ct = resp.headers.get("content-type", "?")
    print(f"      [debug] {tag}  HTTP {resp.status_code}  {ct}  "
          f"{len(body)} bytes -> {path}")
    head = body[:300].decode("utf-8", "ignore").replace("\n", " ")
    print(f"      [debug] starts: {head}")


def search_destination(session, destination, code, check_in, args, nights=None):
    """Returns {engine_id: record} for one destination and one departure date.

    Contract, as observed in the browser:
        POST /hotels/search              starts the search, returns results_hash
        GET  /hotels/search/<page>?results_hash=<hash>   polled until finished

    v2 omitted results_hash and got nothing back. If this still returns zero,
    run with --debug and read debug/*.txt for what the server actually sends.
    """
    payload = build_payload(destination, code, check_in,
                            nights or args.nights, args.adults, args.rooms)
    try:
        hdr = dict(UA); hdr["Referer"] = f"{SITE}/hotels/search"
        r = session.post(f"{SITE}/hotels/search", data=payload,
                         headers=hdr, timeout=90)
        _dump(f"post_{code}_{check_in}.txt", r, args)
        r.raise_for_status()
    except Exception as exc:
        print(f"      search POST failed: {exc}")
        return {}

    # Observed POST response:
    #   {"status":1,"search_id":1455443,"hash":"bcf347e88021bf5b40170c4d1a775ba4"}
    # The key is "hash" on the way out but "results_hash" on the way back in.
    rhash, search_id = "", ""
    try:
        first = r.json()
        rhash = (first.get("hash") or first.get("results_hash")
                 or first.get("search_hash") or "")
        search_id = first.get("search_id", "")
    except Exception:
        m = re.search(r'"(?:results_)?hash"\s*:\s*"([A-Za-z0-9]+)"', r.text)
        if m:
            rhash = m.group(1)
    if not rhash:
        print("      no hash in the POST response.")
        if not args.debug:
            print("      re-run with --debug to dump it.")
        return {}

    print(f" hash {rhash[:10]}...", end="", flush=True)

    found, page, waited, counter, data = {}, 1, 0, 1, {}
    while page <= args.max_pages and waited < args.timeout:
        stable = 0
        while waited < args.timeout:
            try:
                pr = session.get(POLL.format(site=SITE, sid=search_id,
                                             hash=rhash, page=page),
                                 params={"results_hash": RESULTS_HASH_CONST,
                                         "results_counter": counter,
                                         "async": "true",
                                         "just_the_rooms": "true"},
                                 headers=hdr, timeout=90)
                counter += 1
                if waited == 0:
                    _dump(f"poll_{code}_{check_in}_p{page}.txt", pr, args)
                data = pr.json()
            except Exception:
                time.sleep(2); waited += 2; continue
            before = len(found)
            for h in parse_hotels(data.get("rooms", "")):
                key = h["engine_id"] or h["engine_name"]
                if key not in found or h["price"] < found[key]["price"]:
                    found[key] = h
            finished = str(data.get("search_finished", "")).lower() == "true"
            if len(found) == before:
                stable += 1
            else:
                stable = 0
            if finished and stable >= 2:
                break
            time.sleep(2); waited += 2
        total = data.get("total_no_of_hotels")
        if total and len(found) >= int(total):
            break
        if not data.get("rooms"):
            break
        page += 1
    return found


def gather(session, args, dates):
    """Lowest price per hotel across every searched departure date."""
    best = {}
    targets = (DESTINATIONS if args.all_destinations
               else [(args.destination, [(args.destination, args.code)], args.nights)])
    for label, codes, nights in targets:
        for search_dest, code in codes:
            for d in dates:
                print(f"   {label:16s} {code:20s} {nights}n {d:%d %b %Y} ...",
                      end="", flush=True)
                got = search_destination(session, search_dest, code, d,
                                         args, nights=nights)
                print(f" {len(got):4d} hotels")
                for k, h in got.items():
                    h["searched_destination"] = label
                    h["searched_code"] = code
                    h["nights"] = nights
                    h["cheapest_on"] = d.isoformat()
                    if k not in best or h["price"] < best[k]["price"]:
                        best[k] = h
                time.sleep(args.pause)
    return best


# ------------------------------------------------------------- mapping build
def write_csv(path, rows, fields=None):
    """Write a CSV, falling back to a timestamped name if the file is locked.

    On Windows an open Excel window locks the file, and crashing here throws
    away a search that has already cost 40 seconds.
    """
    if not rows:
        return None
    fields = fields or list(rows[0].keys())
    for attempt in (path, None):
        target = attempt or (path.rsplit(".", 1)[0]
                             + datetime.now().strftime("_%H%M%S") + ".csv")
        try:
            with open(target, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                w.writeheader(); w.writerows(rows)
            if target != path:
                print(f"  {path} was locked (close it in Excel). "
                      f"Wrote {target} instead.")
            return target
        except PermissionError:
            continue
    print(f"  could not write {path}")
    return None



def build_map(session, args, dates):
    engine = gather(session, args, dates[:1])   # one date is enough to enumerate
    if not engine:
        sys.exit("No hotels returned. Confirm destination_code values by running "
                 "a real search with the Network tab open and reading the payload.")

    cards = {}
    try:
        with open(args.widgets, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if not row.get("Price shown (GBP)"):
                    continue                       # only cards that show a price
                if "/search-hotels" in row.get("Page URL", ""):
                    continue    # the platform's global country tiles, out of scope
                slug = row["Linked URL"].rstrip("/").split("/")[-1]
                cards.setdefault(slug, {
                    "card_slug": slug,
                    "card_label": row.get("Label on widget", "").strip(),
                    "is_tile": "tile" in row.get("Widget variant", "").lower(),
                    "pages": set()})["pages"].add(row["Page URL"])
    except FileNotFoundError:
        sys.exit(f"{args.widgets} not found. Run the widget crawler first.")

    # Matching. Three rules, each of which the previous version got wrong.
    #
    # 1. HARD DESTINATION FILTER. A Kenya card may only match a Kenya hotel.
    #    Without it, "Baobab Beach Resort, Diani Beach, Kenya" matched
    #    "ZANZIBAR BEACH RESORT" at full confidence, five times over.
    # 2. SYMMETRIC SCORE. Measuring only "how much of the engine name appears
    #    in the card" lets a short generic engine name match anything. Score
    #    the overlap against the union of both instead.
    # 3. KEEP DESTINATION WORDS. They are the most discriminating tokens
    #    there are; stripping them caused rule 1's failure.
    import math
    from collections import Counter

    GENERIC = {"the", "and", "for", "from", "nights", "night", "board", "half",
               "all", "inclusive", "flight", "hotel", "stars", "star",
               "resort", "spa"}

    def toks(text):
        return set(t for t in re.split(r"[^a-z0-9]+", (text or "").lower())
                   if len(t) > 2 and t not in GENERIC)

    DEST_KEYS = {
        "zanzibar": "zanzibar", "tanzania": "zanzibar",
        "mauritius": "mauritius", "maldives": "maldives",
        "seychelles": "seychelles", "kenya": "kenya", "mombasa": "kenya",
        "diani": "kenya", "malindi": "kenya", "bamburi": "kenya",
        "zimbabwe": "victoria falls", "zambia": "victoria falls",
        "victoria": "victoria falls", "falls": "victoria falls",
    }

    def dest_of(*texts):
        for text in texts:
            for t in re.split(r"[^a-z]+", (text or "").lower()):
                if t in DEST_KEYS:
                    return DEST_KEYS[t]
        return ""

    searched = {dest_of(v.get("searched_destination", "")) for v in engine.values()}
    searched.discard("")

    df = Counter()
    for v in engine.values():
        df.update(toks(v["engine_name"]))
    N = max(1, len(engine))
    idf = lambda t: math.log((N + 1) / (df.get(t, 0) + 1)) + 1.0

    def sym(a, b):
        ta, tb = toks(a), toks(b)
        if not ta or not tb:
            return 0.0
        return (sum(idf(t) for t in ta & tb) / sum(idf(t) for t in ta | tb))

    rows = []
    for slug, c in sorted(cards.items()):
        cdest = dest_of(slug, c["card_label"], " ".join(c["pages"]))
        pool = slug.replace("-", " ") + " " + c["card_label"]

        # Destination tiles ("Maldives From £1,899pp") are not one hotel, so
        # they cannot be matched to one. They need the destination minimum.
        if c["is_tile"]:
            rows.append({"card_slug": slug, "card_label": c["card_label"][:70],
                         "appears_on": "; ".join(sorted(c["pages"])),
                         "engine_id": "", "engine_name": "", "engine_area": "",
                         "engine_stars": "", "sample_price": "",
                         "area_agrees": "", "confidence": "DESTINATION TILE",
                         "score": "", "Confirmed (Y/N)": "",
                         "Notes": "needs the cheapest price across "
                                  f"{cdest or 'the destination'}, not one hotel"})
            continue

        if cdest and cdest not in searched:
            rows.append({"card_slug": slug, "card_label": c["card_label"][:70],
                         "appears_on": "; ".join(sorted(c["pages"])),
                         "engine_id": "", "engine_name": "", "engine_area": "",
                         "engine_stars": "", "sample_price": "",
                         "area_agrees": "", "confidence": "NOT SEARCHED",
                         "score": "", "Confirmed (Y/N)": "",
                         "Notes": f"{cdest} has not been searched yet"})
            continue

        pool_dest = [(k, v) for k, v in engine.items()
                     if not cdest or dest_of(v.get("searched_destination", "")) == cdest]
        scored = sorted(((sym(pool, v["engine_name"]), k) for k, v in pool_dest),
                        reverse=True)
        (s1, k1), (s2, _) = (scored + [(0, ""), (0, "")])[:2]
        e = engine.get(k1, {})
        area_ok = bool(e.get("engine_area")) and toks(e["engine_area"]) <= toks(pool)
        rows.append({
            "card_slug": slug, "card_label": c["card_label"][:70],
            "appears_on": "; ".join(sorted(c["pages"])),
            "engine_id": k1, "engine_name": e.get("engine_name", ""),
            "engine_area": e.get("engine_area", ""),
            "engine_stars": e.get("stars", ""),
            "sample_price": e.get("price", ""),
            "area_agrees": "yes" if area_ok else "NO - check",
            "confidence": ("HIGH" if s1 >= .55 and s1 - s2 >= .15
                           else "MEDIUM" if s1 >= .35 else "LOW"),
            "score": round(s1, 2), "Confirmed (Y/N)": "", "Notes": "",
        })
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "DESTINATION TILE": 3, "NOT SEARCHED": 4}
    rows.sort(key=lambda r: order.get(r["confidence"], 0))

    map_path = write_csv(args.map, rows)
    write_csv("engine_hotels.csv", list(engine.values()))

    c = Counter(r["confidence"] for r in rows)
    print(f"\n  {len(engine)} hotels enumerated -> engine_hotels.csv")
    print(f"  {len(rows)} priced cards to map -> {map_path or args.map}")
    print(f"     HIGH {c['HIGH']}   MEDIUM {c['MEDIUM']}   LOW {c['LOW']}"
          f"   tiles {c['DESTINATION TILE']}   not searched {c['NOT SEARCHED']}")
    print("\n  Open the file, check every row, set Confirmed to Y.")
    print("  Rows left blank or set to N will show NO price on the card.")


# -------------------------------------------------------------- rate fetching
def fetch_rates(session, args, dates):
    try:
        with open(args.map, newline="", encoding="utf-8-sig") as fh:
            mapping = [r for r in csv.DictReader(fh)
                       if r.get("Confirmed (Y/N)", "").strip().upper() == "Y"]
    except FileNotFoundError:
        sys.exit(f"{args.map} not found. Run --build-map first.")
    if not mapping:
        sys.exit(f"No confirmed rows in {args.map}. Nothing will be published "
                 "until rows are checked and marked Y.")

    # A list, not a dict keyed on engine_id. Several cards legitimately point
    # at the same hotel (Sea Cliff appears on /zanzibar and /group/, Maritim
    # and InterContinental each have two slugs). Keying by engine_id made the
    # later card overwrite the earlier one and silently lose its price.
    confirmed = [r for r in mapping if r.get("engine_id")]

    if args.from_cache:
        try:
            best = json.load(open(CACHE, encoding="utf-8"))
            print(f"  reusing {len(best)} cached rates from {CACHE}\n")
        except FileNotFoundError:
            sys.exit(f"{CACHE} not found. Run without --from-cache first.")
    else:
        best = gather(session, args, dates)
        # Write the raw search results straight away. Everything after this
        # is local processing that can fail; a 30-minute search should never
        # be lost to a bug in the ten lines that follow it.
        try:
            json.dump(best, open(CACHE, "w", encoding="utf-8"), indent=1)
            print(f"\n  raw results cached to {CACHE} "
                  f"({len(best)} hotels). Rebuild without re-searching "
                  f"using --from-cache.")
        except Exception as exc:
            print(f"  could not cache results: {exc}")

    rates, skipped = {}, []
    for row in confirmed:
        eid = row["engine_id"]
        h = best.get(eid)
        if not h:
            skipped.append((row["card_slug"], "no rate returned")); continue
        if h.get("currency") and h["currency"] != CURRENCY:
            skipped.append((row["card_slug"],
                            f"returned in {h['currency']}, not {CURRENCY}"))
            continue
        if not (MIN_SANE <= h["price"] <= MAX_SANE):
            skipped.append((row["card_slug"], f"price out of band £{h['price']:,.0f}"))
            continue
        rates[row["card_slug"]] = {
            "price": round(h["price"]),
            "board": h["board"], "nights": h.get("nights", args.nights),
            "adults": args.adults, "rooms": args.rooms,
            "basis": "total for the room, accommodation only",
            "cheapest_departure": h["cheapest_on"],
            "hotel": h["engine_name"],
        }

    out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "currency": CURRENCY,
        "search": {"nights": args.nights, "adults": args.adults,
                   "rooms": args.rooms,
                   "departures_scanned": [d.isoformat() for d in dates]},
        "rates": rates,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)

    shared = len(confirmed) - len({r["engine_id"] for r in confirmed})
    print(f"\n  {len(rates)} card prices written -> {args.out}"
          f"  (from {len(confirmed)} confirmed cards"
          + (f", {shared} sharing a hotel with another card)" if shared else ")"))
    if skipped:
        print(f"  {len(skipped)} confirmed cards produced NO price "
              f"(the page will hide the price line for these):")
        for slug, why in skipped:
            print(f"     {slug[:52]:52s} {why}")



def probe(session, args):
    """Try candidate poll contracts against a real search and report which works.

    The POST is confirmed:
        POST /hotels/search -> {"status":1,"search_id":N,"hash":"..."}
    The poll is not. The browser showed a request named "1?results_hash=..."
    but that is only the tail of the URL, so the path and the full parameter
    set are unknown. This enumerates the sensible options instead of guessing.
    """
    dest = args.destination or "Zanzibar, Tanzania"
    code = args.code or "zanzibar-tz"
    check_in = date.today() + timedelta(days=args.first_lead_days)
    payload = build_payload(dest, code, check_in, args.nights, args.adults, args.rooms)

    ref = f"{SITE}/hotels/search"
    hdr = dict(UA); hdr["Referer"] = ref
    print(f"POST /hotels/search  ({dest}, {check_in:%d %b %Y})")
    r = session.post(f"{SITE}/hotels/search", data=payload, headers=hdr, timeout=90)
    print(f"   HTTP {r.status_code}  {r.text[:120]}")
    try:
        j = r.json()
    except Exception:
        return print("   POST did not return JSON; nothing further to try.")
    h = j.get("hash", ""); sid = j.get("search_id", "")
    if not h:
        return print("   No hash returned.")

    print(f"   hash={h}  search_id={sid}\n")
    print("Waiting 12s for the engine to start producing results...\n")
    time.sleep(12)

    cands = [
        ("GET",  f"/hotels/search/1", {"results_hash": h}),
        ("GET",  f"/hotels/search/1", {"results_hash": h, "search_id": sid}),
        ("GET",  f"/hotels/search/1", {"search_id": sid}),
        ("GET",  f"/hotels/search/1", {"hash": h, "search_id": sid}),
        ("GET",  f"/hotels/search/results/1", {"results_hash": h}),
        ("GET",  f"/hotels/search_results/1", {"results_hash": h}),
        ("GET",  f"/hotels/results/1", {"results_hash": h}),
        ("GET",  f"/hotels/search/{sid}/1", {"results_hash": h}),
        ("GET",  f"/hotels/search/1/{h}", {}),
        ("POST", f"/hotels/search/1", {"results_hash": h, "search_id": sid}),
        ("POST", f"/hotels/search/1", {"results_hash": h}),
    ]
    best = None
    for method, path, params in cands:
        try:
            if method == "GET":
                pr = session.get(SITE + path, params=params, headers=hdr, timeout=60)
            else:
                pr = session.post(SITE + path, data=params, headers=hdr, timeout=60)
            body = pr.text
            hotels = len(parse_hotels(
                (pr.json().get("rooms", "") if body.lstrip().startswith("{") else body) or ""))
        except Exception as exc:
            print(f"   {method:4s} {path:34s} {str(params)[:44]:44s} EXCEPTION {exc}")
            continue
        tag = f"{len(body)}b"
        note = f"{hotels} hotels" if hotels else body[:70].replace("\n", " ")
        star = "  <== WORKS" if hotels else ""
        print(f"   {method:4s} {path:34s} {str(params)[:44]:44s} {pr.status_code} {tag:>8s} {note}{star}")
        if hotels and not best:
            best = (method, path, params)

    print()
    if best:
        print(f"WORKING CONTRACT: {best[0]} {best[1]} params={list(best[2])}")
        print("Send me this line and I will lock it into the fetcher.")
    else:
        print("None returned hotels. Get the exact poll URL from the browser:")
        print("  DevTools > Network > click a '1?results_hash=...' row >")
        print("  Headers tab > copy the full Request URL, and the Payload tab if present.")




def find_codes(session, args):
    """Test destination codes derived from the content master's own URLs.

    Every hotel URL is /accommodation/in-<Area>/<Country>/<slug>/, so the real
    area names are already known. Turning those into codes beats guessing.
    """
    import collections
    try:
        with open(args.register, newline="", encoding="utf-8-sig") as fh:
            recs = list(csv.DictReader(fh))
    except FileNotFoundError:
        return print(f"{args.register} not found; cannot derive candidates.")

    areas = collections.defaultdict(collections.Counter)
    for r in recs:
        m = re.search(r"/accommodation/in-([^/]+)/([^/]+)/", r["Public URL"])
        if m:
            areas[r["Destination"]][(m.group(1), m.group(2))] += 1

    wanted = [d.strip() for d in (args.only_dest or "").split(",") if d.strip()]
    check_in = date.today() + timedelta(days=args.first_lead_days)
    print("Codes derived from the content master's own hotel URLs.\n")

    results = collections.defaultdict(list)
    for dest, counter in areas.items():
        if wanted and dest not in wanted:
            continue
        print(f"{dest}")
        for (area, country), n in counter.most_common():
            code = f"{area.lower().replace('_', '-').replace('(', '').replace(')', '')}-{CC.get(country, '??')}"
            label = f"{area}, {country}"
            print(f"   {label:44s} {code:26s} ...", end="", flush=True)
            got = search_codes_quiet(session, label, code, check_in, args)
            flag = "  <-- use" if got else ""
            print(f" {got:4d} hotels ({n} in master){flag}")
            if got:
                results[dest].append((label, code, got))
            time.sleep(args.pause)
        print()

    print("\nSuggested DESTINATIONS entries:\n")
    for dest, found in results.items():
        found.sort(key=lambda x: -x[2])
        pairs = ", ".join(f'("{l}", "{c}")' for l, c, _ in found)
        print(f'    ("{dest}", [{pairs}]),')


def search_codes_quiet(session, label, code, check_in, args):
    try:
        return len(search_destination(session, label, code, check_in, args))
    except Exception:
        return 0



# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-map", action="store_true")
    ap.add_argument("--destination"); ap.add_argument("--code")
    ap.add_argument("--all-destinations", action="store_true", default=True)
    ap.add_argument("--nights", type=int, default=7)
    ap.add_argument("--adults", type=int, default=2)
    ap.add_argument("--rooms", type=int, default=1)
    ap.add_argument("--scan-months", type=int, default=6,
                    help="how many monthly departure dates to price, so that "
                         "'from' is a real minimum rather than one date")
    ap.add_argument("--first-lead-days", type=int, default=35)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--max-pages", type=int, default=15)
    ap.add_argument("--pause", type=float, default=3.0)
    ap.add_argument("--only-dest", default="",
                    help="comma-separated destinations for --find-codes")
    ap.add_argument("--find-codes", action="store_true",
                    help="test candidate destination codes and report coverage")
    ap.add_argument("--probe", action="store_true",
                    help="find the working poll contract, then stop")
    ap.add_argument("--debug", action="store_true",
                    help="dump raw server responses to debug/")
    ap.add_argument("--widgets", default="widget_inventory.csv")
    ap.add_argument("--register", default="hotel_register.csv")
    ap.add_argument("--map", default="price_map.csv")
    ap.add_argument("--out", default="rates.json")
    ap.add_argument("--from-cache", action="store_true",
                    help="rebuild rates.json from the last search, no re-fetch")
    args = ap.parse_args()
    if args.destination:
        args.all_destinations = False

    start = date.today() + timedelta(days=args.first_lead_days)
    dates = [start + timedelta(days=30 * i) for i in range(args.scan_months)]

    session = requests.Session()
    session.get(SITE, headers=UA, timeout=30)
    # Currency is a session cookie, not a request parameter. Set it explicitly:
    # if the account default ever changes, rates would come back in another
    # currency and be published under a GBP label.
    session.get(f"{SITE}/home/change_currency/{CURRENCY}", headers=UA, timeout=30)

    print(f"{args.nights} nights, {args.adults} adults, {args.rooms} room")
    print(f"Departures scanned: {', '.join(d.strftime('%d %b') for d in dates)}\n")

    if args.find_codes:
        find_codes(session, args)
        return

    if args.probe:
        probe(session, args)
        return

    if args.build_map:
        build_map(session, args, dates)
    else:
        fetch_rates(session, args, dates)


if __name__ == "__main__":
    main()
