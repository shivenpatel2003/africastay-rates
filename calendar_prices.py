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

MIN_NIGHT, MAX_NIGHT = 25.0, 4000.0


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

    cheapest = min(pool)
    # the page's own header figure and the structured data should agree with
    # the calendar. If they do not, something has been misread.
    for other in (header, ld_low):
        if other and abs(other - cheapest) / max(other, cheapest) > 0.25:
            return None, (f"calendar says {cheapest:,.0f} but the page says "
                          f"{other:,.0f}; not publishing a figure I cannot reconcile")

    return {"per_night": cheapest, "nights_found": len(nights),
            "months_found": len(months), "header": header,
            "median_night": statistics.median(pool)}, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", default="price_map.csv")
    ap.add_argument("--rates", default="rates.json")
    ap.add_argument("--nights", type=int, default=7)
    ap.add_argument("--pause", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--compare", action="store_true",
                    help="run against cards that already have a search price, "
                         "and show both, to see whether the two agree")
    args = ap.parse_args()

    for f in (args.map, args.rates):
        if not os.path.exists(f):
            sys.exit(f"{f} not found.")

    url_for = {}
    for src in ("widget_inventory.csv", "link_worklist.csv"):
        if not os.path.exists(src):
            continue
        with open(src, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                for col in ("Linked URL", "REPLACE with", "FIND this link"):
                    u = r.get(col, "")
                    if u and "/accommodation/" in u:
                        url_for.setdefault(u.rstrip("/").split("/")[-1], u)

    feed = json.load(open(args.rates, encoding="utf-8"))
    rates = feed.setdefault("rates", {})

    with open(args.map, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if args.compare:
        todo = [r for r in rows
                if r.get("confidence") != "DESTINATION TILE"
                and r["card_slug"] in rates
                and not rates[r["card_slug"]].get("is_destination_tile")][:12]
        if not todo:
            sys.exit("No priced cards to compare against.")
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
