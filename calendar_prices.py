#!/usr/bin/env python3
"""
Price the remaining cards from each hotel's own page.

WHY THIS WORKS WHERE THE SEARCH DID NOT

A destination search never returns some of these hotels. The Wallow's own
search results listed Tatenda Safaris and The Crescent, not The Wallow. But
its page carries a price calendar, and those numbers are in the HTML:

    <div class="acqua-cal-price">...<span class="value">577</span>GBP
    <span class="price" id="hotel_header_min_price">...496<sup>70</sup>

So we read the hotel's own page. One fetch each, no search, no supplier codes,
no name matching. It cannot be the wrong hotel because it is that hotel's page.

The calendar is per night for two adults, so the card price is the cheapest
night multiplied by the number of nights. That is a "from" price, which is
what the card says.

    py calendar_prices.py              price every card that has none
    py calendar_prices.py --nights 3   for destinations sold on 3 nights
"""

import argparse, csv, json, os, re, sys, time, statistics

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
# the header figure the page shows as its own "from"
HEAD = re.compile(r'id="hotel_header_min_price".{0,300}?([\d,]+)<sup>(\d\d)</sup>', re.S)
# structured data, a useful cross-check
LD = re.compile(r'"priceRange"\s*:\s*"GBP\s*([\d,]+)\s*-\s*GBP\s*([\d,]+)"')

# Per-night bounds. GBP 34 a night at a four-star Zanzibar resort is genuine,
# confirmed against the page itself, so the floor stays low.
# Per-night bounds, for reading the calendar. GBP 34 a night at a four-star
# Zanzibar resort is genuine, confirmed against the page itself.
MIN_NIGHT, MAX_NIGHT = 20.0, 4000.0

# Bounds on the published weekly total. One room was live at GBP 125,490 due
# to a supplier data error, so nothing outside this band is published.
MIN_SANE, MAX_SANE = 200.0, 25000.0

# Cross-check against the search-based price where rates.json already has one.
# Measured across 11 hotels with both: the calendar sits at 0.63 to 0.78 of the
# search price, which is a sensible "from" discount. Two were far outside that
# (JW Marriott 0.07, Medhufushi 0.18) because something on those pages reads as
# a nightly rate and is not one. Anything below this floor is not published.
RATIO_FLOOR = 0.40

# Which night to take as the "from" price.
#   0.0  the outright cheapest, which is what the hotel page displays as its
#        own "from" figure. Amaan shows GBP 34.12 a night this way.
#   0.2  the 20th-percentile night, closer to what a real week costs. Amaan's
#        search-based price for the same week was GBP 572 against GBP 238 from
#        the cheapest night, so the two methods do not otherwise agree.
# Set this from measured data rather than by guesswork.
PERCENTILE = 0.0


def num(s):
    return float(str(s).replace(",", ""))


def read_page(session, url):
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

    head = HEAD.search(html)
    header = num(head.group(1)) + int(head.group(2)) / 100 if head else None
    ld = LD.search(html)
    ld_low = num(ld.group(1)) if ld else None

    # PERCENTILE controls which night is taken. 0.0 is the outright cheapest,
    # which matches the "from" figure the hotel page itself displays. A higher
    # value gives a night closer to what a real week costs, at the price of no
    # longer matching the page. Set from measured data, not by guesswork.
    ordered = sorted(pool)
    if PERCENTILE > 0 and len(ordered) >= 8:
        cheapest = ordered[max(0, int(len(ordered) * PERCENTILE) - 1)]
    else:
        cheapest = min(ordered)
    # the page's own header figure and the structured data should agree with
    # the calendar. If they do not, something has been misread.
    for other in (header, ld_low):
        if other and abs(other - cheapest) / max(other, cheapest) > 0.25:
            return None, (f"calendar says {cheapest:,.0f} but the page says "
                          f"{other:,.0f}; not publishing a figure I cannot reconcile")

    # A cheapest night far below the typical night on the same page suggests one
    # stray element rather than a genuine low season.


    return {"per_night": cheapest, "outright_cheapest": min(pool),
            "nights_found": len(nights),
            "months_found": len(months), "header": header,
            "median_night": statistics.median(pool)}, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", default="price_map.csv")
    ap.add_argument("--rates", default="rates.json")
    ap.add_argument("--nights", type=int, default=7)
    ap.add_argument("--pause", type=float, default=1.0)
    ap.add_argument("--all", action="store_true",
                    help="price every card from its own page, not just the "
                         "ones without a price. Needs no mapping at all.")
    ap.add_argument("--urls", default="card_urls.csv",
                    help="slug,url for every card; written by --export-urls")
    ap.add_argument("--export-urls", action="store_true",
                    help="write card_urls.csv from the crawl, then stop")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--compare", action="store_true",
                    help="run against cards that already have a search price, "
                         "and show both, to see whether the two agree")
    args = ap.parse_args()

    need = [args.rates] if ("--all" in sys.argv or "--export-urls" in sys.argv) \
           else [args.map, args.rates]
    for f in need:
        if not os.path.exists(f):
            sys.exit(f"{f} not found.")

    # Only the widget cards, not every hotel link on the site. The crawl
    # contains text links, area links and anchor fragments; taking all of
    # them gave 1,128 "cards" including things like "#A".
    url_for = {}
    if os.path.exists(args.urls):
        with open(args.urls, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                url_for[r["slug"]] = r["url"]

    def usable(u):
        if not u or "/accommodation/" not in u or "#" in u:
            return False
        tail = [x for x in u.split("/accommodation/")[-1].split("/") if x]
        return len(tail) >= 3          # in-Area / Country / hotel-slug

    if not url_for and os.path.exists("widget_inventory.csv"):
        with open("widget_inventory.csv", newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                # only cards that carry a price. The crawl holds 1,238 widget
                # cards across 361 pages; the ones this brief is about are the
                # ~41 that display a "from" price on the curated pages.
                if not r.get("Widget variant", "").startswith("Widget"):
                    continue
                if not (r.get("Price shown (GBP)") or "").strip():
                    continue
                u = r.get("Linked URL", "")
                if usable(u):
                    url_for.setdefault(u.rstrip("/").split("/")[-1], u)

    # anything relinked today, so the new URLs are covered too
    if os.path.exists("link_worklist.csv"):
        with open("link_worklist.csv", newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                u = r.get("REPLACE with", "")
                if usable(u):
                    url_for.setdefault(u.rstrip("/").split("/")[-1], u)

    if args.export_urls:
        if not url_for:
            sys.exit("No crawl data found to export URLs from.")
        with open(args.urls, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh); w.writerow(["slug", "url"])
            for k, v in sorted(url_for.items()):
                w.writerow([k, v])
        print(f"  {len(url_for)} card URLs -> {args.urls}")
        print("  Upload that to the repo and the nightly job needs no mapping.")
        return

    feed = json.load(open(args.rates, encoding="utf-8"))
    rates = feed.setdefault("rates", {})
    # keep the pre-existing prices so a new reading can be sanity-checked
    # against whatever produced them
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
        # Was 120, on the assumption of ~41 priced cards. The July relink work
        # added the new bookable records alongside the old URLs, so both sets
        # are present and ~130 is now correct. The guard exists to catch a
        # runaway export (it once found 1,128 including anchor fragments), so
        # it stays, just higher.
        if len(url_for) > 400:
            sys.exit(f"{len(url_for)} card URLs found, far more than expected. "
                     "Check card_urls.csv before running with --all.")
        # every card we have a URL for. No mapping, no search, no matching:
        # each price comes from that card's own hotel page.
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
        print(f"  {len(todo)} cards with no price\n")
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
        if not (MIN_SANE <= total <= MAX_SANE):
            print(f"weekly total {total:,} is outside "
                  f"{MIN_SANE:,.0f}-{MAX_SANE:,.0f}, not publishing")
            failed.append(label); continue

        was = previous.get(slug)
        if was and total < was * RATIO_FLOOR:
            print(f"reads {total:,} but the search gave {was:,} "
                  f"(x{total/was:.2f}); too far apart to publish")
            failed.append(label); continue
        if args.compare:
            was = rates[slug]["price"]
            diff = (total - was) / was * 100 if was else 0
            flag = "  <-- big gap" if abs(diff) > 30 else ""
            print(f"search GBP {was:>7,.0f}   calendar GBP {total:>7,.0f}   "
                  f"{diff:+6.0f}%{flag}")
            comparisons.append(diff)
            continue
        print(f"GBP {total:>7,.0f}   {n}n at {info['per_night']:,.0f}/night   "
              f"({info['nights_found']} days, {info['months_found']} months read)")
        if not args.dry_run:
            rates[slug] = {
                "price": total, "board": "", "nights": n, "adults": 2, "rooms": 1,
                "basis": "total for the room, accommodation only",
                "source": "hotel page calendar, cheapest night",
                "per_night": info["per_night"],
            }
        added += 1

    if args.compare:
        if comparisons:
            comparisons.sort()
            mid = comparisons[len(comparisons) // 2]
            print(f"\n  {len(comparisons)} compared, median difference {mid:+.0f}%")
            print("  Under about 20% and the two methods agree well enough to mix.")
            print("  Consistently lower is expected: the calendar looks across a")
            print("  whole year of nights, the search only at six departure dates.")
        return
    if args.dry_run:
        print(f"\n  dry run: {added} would be priced, nothing written")
        return
    json.dump(feed, open(args.rates, "w", encoding="utf-8"), indent=1)
    print(f"\n  {added} cards priced, {len(rates)} keys now in {args.rates}")
    if failed:
        print(f"  {len(failed)} still without a price:")
        for f in failed:
            print(f"     {f}")
    print("\n  Next:  py destination_tile_prices.py")
    print("         py make_snippet.py --feed <raw github url>")


if __name__ == "__main__":
    main()
