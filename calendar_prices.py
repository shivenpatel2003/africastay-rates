#!/usr/bin/env python3
"""
Price the widget cards from each hotel's own page.

WHY THIS RATHER THAN A DESTINATION SEARCH

A destination search never returns some hotels: The Wallow's own search results
list Tatenda Safaris and The Crescent, not The Wallow. But every hotel page
carries a price calendar with the numbers in the HTML:

    <div class="acqua-cal-price">...<span class="value">577</span>GBP

One fetch per hotel, no search, no supplier codes, no name matching. It cannot
be the wrong hotel because it is that hotel's own page.

The calendar is per night for two adults, so the card price is the chosen night
multiplied by the number of nights. That is a "from" price, which is what the
card says.

    py calendar_prices.py --all            price every card
    py calendar_prices.py --all --dry-run  report only, write nothing
    py calendar_prices.py --export-urls    rebuild card_urls.csv, then stop
    py calendar_prices.py --compare        against cards that already have a
                                           search price, to see if they agree
"""

import argparse, csv, json, os, re, statistics, sys, time

try:
    import requests
except ImportError:
    sys.exit("py -m pip install requests")

SITE = "https://www.africastay.co.uk"
UA = {"User-Agent": "AfricaStay internal rate fetcher (sales@africastay.co.uk)"}

# a day cell in the calendar
CAL = re.compile(r'acqua-cal-price.{0,220}?<span class="value">\s*([\d,]+)\s*</span>', re.S)
# the month tabs carry that month's cheapest
TAB = re.compile(r'graph-price.{0,220}?<span class="value">\s*([\d,]+)\s*</span>', re.S)
# the figure the page shows as its own "from"
HEAD = re.compile(r'id="hotel_header_min_price".{0,300}?([\d,]+)<sup>(\d\d)</sup>', re.S)
# structured data, a useful cross-check
LD = re.compile(r'"priceRange"\s*:\s*"GBP\s*([\d,]+)\s*-\s*GBP\s*([\d,]+)"')

# Per-night bounds for reading the calendar. GBP 34 a night at a four-star
# Zanzibar resort is genuine, confirmed against the page itself.
MIN_NIGHT, MAX_NIGHT = 20.0, 4000.0

# Bounds on the published weekly total. One room was live at GBP 125,490 due to
# a supplier data error, so nothing outside this band is published.
MIN_SANE, MAX_SANE = 200.0, 25000.0

# Nights below this fraction of the median are stray values, not low season.
# Measured: JW Marriott 49 against a median of 676 (0.07) and Medhufushi 41
# against 284 (0.14) are both wrong; Amaan 34 against 37 (0.92) and Azao 82
# against 92 (0.89) are both right.
OUTLIER_FLOOR = 0.35

# Where rates.json already holds a search-based price, the calendar should land
# within this fraction of it. Measured across 11 hotels: 0.63 to 0.78 is normal
# for a "from" discount; far below that means a misreading.
RATIO_FLOOR = 0.40


def num(s):
    return float(str(s).replace(",", ""))


def read_page(session, url):
    """Read a hotel page and work out a defensible nightly rate."""
    try:
        html = session.get(url, headers=UA, timeout=45).text
    except Exception as exc:
        return None, f"fetch failed: {exc}"

    nights = [num(x) for x in CAL.findall(html)]
    nights = [n for n in nights if MIN_NIGHT <= n <= MAX_NIGHT]
    months = [num(x) for x in TAB.findall(html)]
    months = [n for n in months if MIN_NIGHT <= n <= MAX_NIGHT]
    pool = nights or months
    if not pool:
        return None, "no calendar prices on the page (not bookable)"

    # WHICH NIGHT TO TAKE
    #
    # Not the outright cheapest. JW Marriott's calendar holds one night at
    # GBP 49 among 95 whose median is 676; taking the minimum produced a week
    # at GBP 343 against a real search price of GBP 4,714. Median x7 came to
    # 4,732, so the calendar was right and the minimum was a stray cell.
    #
    # So drop nights far below the median first. That keeps a genuine
    # low-season figure while discarding the outlier. A short calendar has no
    # reliable median, so fall back to the minimum there.
    med = statistics.median(pool)
    if len(pool) >= 12:
        plausible = [n for n in pool if n >= med * OUTLIER_FLOOR]
        cheapest = min(plausible) if plausible else med
        dropped = len(pool) - len(plausible)
    else:
        cheapest = min(pool)
        dropped = 0

    head = HEAD.search(html)
    header = num(head.group(1)) + int(head.group(2)) / 100 if head else None
    ld = LD.search(html)
    ld_low = num(ld.group(1)) if ld else None

    # The page's own header should agree with the calendar. Where it does not,
    # one of the two is wrong, and it is not always the calendar: LUX* Grand
    # Gaube displays "from GBP 12.85" against a calendar showing GBP 721 a
    # night, and JW Marriott displays GBP 49 against a calendar median of 676
    # whose x7 matches the booking search to within GBP 20.
    #
    # So trust the calendar when it is well evidenced, meaning a good number of
    # nights read and a chosen night close to their median. Otherwise there is
    # nothing to choose between the two figures and neither is published.
    disagrees = []
    for other in (header, ld_low):
        if other and abs(other - cheapest) / max(other, cheapest) > 0.25:
            disagrees.append(other)

    if disagrees:
        well_evidenced = len(pool) >= 20 and cheapest >= med * 0.30
        if not well_evidenced:
            return None, (f"calendar says {cheapest:,.0f} but the page says "
                          f"{disagrees[0]:,.0f}, and the calendar is too thin "
                          "to prefer; not publishing")
        # keep going, but say so in the output
        note_disagree = (f"page header says {disagrees[0]:,.0f}, "
                         f"calendar {cheapest:,.0f} from {len(pool)} nights")
    else:
        note_disagree = ""

    return {"per_night": cheapest, "outright_cheapest": min(pool),
            "median_of_pool": med, "dropped": dropped,
            "nights_found": len(nights), "months_found": len(months),
            "header": header, "disagrees": note_disagree}, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", default="price_map.csv")
    ap.add_argument("--rates", default="rates.json")
    ap.add_argument("--urls", default="card_urls.csv")
    ap.add_argument("--nights", type=int, default=7)
    ap.add_argument("--pause", type=float, default=1.0)
    ap.add_argument("--all", action="store_true",
                    help="price every card from its own page, needing no mapping")
    ap.add_argument("--export-urls", action="store_true",
                    help="rebuild card_urls.csv from the crawl, then stop")
    ap.add_argument("--compare", action="store_true",
                    help="run against cards that already have a search price")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    need = [args.rates] if (args.all or args.export_urls) else [args.map, args.rates]
    for f in need:
        if not os.path.exists(f):
            sys.exit(f"{f} not found.")

    # ---- where each card points
    #
    # Only the widget cards that carry a price, not every hotel link on the
    # site. Taking all of them once gave 1,128 "cards" including anchor
    # fragments such as "#A".
    url_for = {}
    if os.path.exists(args.urls):
        with open(args.urls, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                url_for[r["slug"]] = r["url"]

    def usable(u):
        if not u or "/accommodation/" not in u or "#" in u:
            return False
        tail = [x for x in u.split("/accommodation/")[-1].split("/") if x]
        return len(tail) >= 3

    if not url_for and os.path.exists("widget_inventory.csv"):
        with open("widget_inventory.csv", newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if not r.get("Widget variant", "").startswith("Widget"):
                    continue
                if not (r.get("Price shown (GBP)") or "").strip():
                    continue
                u = r.get("Linked URL", "")
                if usable(u):
                    url_for.setdefault(u.rstrip("/").split("/")[-1], u)

    # anything relinked since the crawl, so the new URLs are covered too
    if os.path.exists("link_worklist.csv"):
        with open("link_worklist.csv", newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                u = r.get("REPLACE with", "")
                if usable(u):
                    url_for.setdefault(u.rstrip("/").split("/")[-1], u)

    if args.export_urls:
        if not url_for:
            sys.exit("No crawl data to export URLs from.")
        with open(args.urls, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh); w.writerow(["slug", "url"])
            for k, v in sorted(url_for.items()):
                w.writerow([k, v])
        print(f"  {len(url_for)} card URLs -> {args.urls}")
        return

    feed = json.load(open(args.rates, encoding="utf-8"))
    rates = feed.setdefault("rates", {})
    previous = {k: v.get("price") for k, v in rates.items() if v.get("price")}

    rows = []
    if os.path.exists(args.map):
        with open(args.map, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))

    if args.compare:
        todo = [r for r in rows
                if r.get("confidence") != "DESTINATION TILE"
                and r["card_slug"] in rates
                and not rates[r["card_slug"]].get("is_destination_tile")][:12]
        if not todo:
            sys.exit("No priced cards to compare against.")
    elif args.all:
        # The guard was 120, on the assumption of ~41 priced cards. The relink
        # work added the new bookable records alongside the old URLs, so ~130
        # is now correct. It stays, to catch a runaway export.
        if len(url_for) > 400:
            sys.exit(f"{len(url_for)} card URLs found, far more than expected. "
                     "Check card_urls.csv before running with --all.")
        todo = [{"card_slug": s, "card_label": s} for s in sorted(url_for)]
        rates.clear()
    else:
        todo = [r for r in rows
                if r.get("confidence") != "DESTINATION TILE"
                and r["card_slug"] not in rates]
        if not todo:
            sys.exit("Every card already has a price.")

    session = requests.Session()
    session.get(SITE, headers=UA, timeout=30)
    session.get(f"{SITE}/home/change_currency/GBP", headers=UA, timeout=30)

    # Victoria Falls cards advertise 3 nights, everything else 7
    def nights_for(slug):
        return 3 if re.search(r"zimbabwe|zambia|victoria", slug, re.I) else args.nights

    comparisons = []
    if args.compare:
        print(f"  comparing {len(todo)} cards that already have a search price\n")
    else:
        print(f"  {len(todo)} cards to price\n")

    added, failed = 0, []
    for r in todo:
        slug = r["card_slug"]
        label = re.sub(r"[\u2605\u2606]", "", r.get("card_label", ""))[:38].strip()
        url = url_for.get(slug)
        print(f"   {label:40s} ", end="", flush=True)
        if not url:
            print("no hotel URL in the crawl"); failed.append(label); continue

        info, err = read_page(session, url)
        time.sleep(args.pause)
        if not info:
            print(err); failed.append(label); continue

        n = nights_for(slug)
        total = round(info["per_night"] * n)

        if args.compare:
            was = rates[slug]["price"]
            diff = (total - was) / was * 100 if was else 0
            flag = "  <-- big gap" if abs(diff) > 30 else ""
            print(f"search GBP {was:>7,.0f}   calendar GBP {total:>7,.0f}   "
                  f"{diff:+6.0f}%{flag}")
            comparisons.append(diff)
            continue

        if not (MIN_SANE <= total <= MAX_SANE):
            print(f"weekly total {total:,} outside "
                  f"{MIN_SANE:,.0f}-{MAX_SANE:,.0f}, not publishing")
            failed.append(label); continue

        was = previous.get(slug)
        if was and total < was * RATIO_FLOOR:
            print(f"reads {total:,} but the search gave {was:,} "
                  f"(x{total / was:.2f}); too far apart to publish")
            failed.append(label); continue

        note = f", {info['dropped']} outliers dropped" if info["dropped"] else ""
        if info.get("disagrees"):
            note += f"  [{info['disagrees']}]"
        print(f"GBP {total:>7,.0f}   {n}n at {info['per_night']:,.0f}/night "
              f"(low {info['outright_cheapest']:,.0f}, "
              f"med {info['median_of_pool']:,.0f}{note})   {info['nights_found']}d")

        if not args.dry_run:
            rates[slug] = {
                "price": total, "board": "", "nights": n, "adults": 2, "rooms": 1,
                "basis": "total for the room, accommodation only",
                "source": "hotel page calendar",
                "per_night": info["per_night"],
            }
        added += 1

    if args.compare:
        if comparisons:
            comparisons.sort()
            print(f"\n  {len(comparisons)} compared, median difference "
                  f"{comparisons[len(comparisons) // 2]:+.0f}%")
        return

    if args.dry_run:
        print(f"\n  dry run: {added} would be priced, {len(failed)} not, "
              "nothing written")
        return

    # Stamp the file. The page hides every price if the feed is more than 36
    # hours old, and an earlier version left this untouched, so a fresh run
    # published Saturday's timestamp and the site suppressed everything.
    from datetime import datetime, timezone
    feed["generated_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    json.dump(feed, open(args.rates, "w", encoding="utf-8"), indent=1)
    print(f"\n  {added} priced, {len(rates)} keys now in {args.rates}")
    print(f"  stamped {feed['generated_utc']}")
    if failed:
        print(f"  {len(failed)} without a price:")
        for f in failed[:20]:
            print(f"     {f}")
    print("\n  Next:  py destination_tile_prices.py")
    print("         py make_snippet.py --feed <raw github url>")


if __name__ == "__main__":
    main()
