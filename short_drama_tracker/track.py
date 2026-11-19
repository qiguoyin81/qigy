#!/usr/bin/env python3
"""Short-drama trend tracker for ReelShort and DramaBox (public web pages only).

Usage:
    python3 track.py snapshot            # fetch both platforms, save data/snapshot_<utc>.json
    python3 track.py report              # compare the two most recent snapshots
    python3 track.py report --from A --to B   # compare two specific snapshot files
    python3 track.py list                # list saved snapshots

Growth = change in each title's cumulative view count between two snapshots,
measured on the SAME title id, divided by elapsed days. The two platforms use
different view-count units, so they are always reported separately and never
mixed. Run snapshots at least a few days apart for the numbers to mean anything.

Standard library only. Respects HTTPS_PROXY and uses a CA bundle from
SSL_CERT_FILE / REQUESTS_CA_BUNDLE / /root/.ccr/ca-bundle.crt when present.
"""
import argparse
import collections
import datetime
import glob
import html
import json
import os
import re
import ssl
import statistics
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = os.path.join(HERE, "data")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
PAUSE = 1.0  # seconds between requests

REELSHORT_BASE = "https://www.reelshort.com"
REELSHORT_SHELVES = {
    "top": "top-short-movies-dramas-51004673",
    "new": "new-release-short-movies-dramas-51004672",
}
DRAMABOX_TRENDING = "https://www.dramaboxdb.com/channel/trending"

# Generic labels that say nothing about genre.
SKIP_TAGS = {"All Ages", "Female", "Male", "Series", "All", "Popular", "F-Drama", "M-Drama"}
# Merge spelling variants so one genre is not split across names.
ALIASES = {
    "Werewolves": "Werewolf",
    "Comeback Story": "Comeback",
    "Reborn": "Rebirth",
    "Strong Heroine": "Strong Female Lead",
    "Sweet Love": "Sweet Romance",
    "Billionaires": "Billionaire",
}

NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


# ---------------------------------------------------------------- fetching
def _ssl_context():
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(var)
        if path and os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    if os.path.exists("/root/.ccr/ca-bundle.crt"):
        return ssl.create_default_context(cafile="/root/.ccr/ca-bundle.crt")
    return ssl.create_default_context()


_CTX = None


def fetch(url, retries=3):
    global _CTX
    _CTX = _CTX or _ssl_context()
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
            with urllib.request.urlopen(req, timeout=30, context=_CTX) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:  # network/HTTP errors: back off and retry
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"fetch failed: {url}: {last}")


def page_props(text):
    m = NEXT_DATA.search(text)
    if not m:
        raise RuntimeError("no __NEXT_DATA__ found; the page layout may have changed")
    return json.loads(m.group(1))["props"]["pageProps"]


def clean_tags(names):
    out = []
    for n in names:
        n = html.unescape(str(n)).strip()
        if not n or n in SKIP_TAGS:
            continue
        n = ALIASES.get(n, n)
        if n not in out:
            out.append(n)
    return out


def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- scrapers
def scrape_reelshort():
    items = {}
    for shelf, path in REELSHORT_SHELVES.items():
        page, seen_here = 1, set()
        while True:
            url = f"{REELSHORT_BASE}/shelf/{path}" + ("" if page == 1 else f"/{page}")
            pp = page_props(fetch(url))
            lst = pp.get("list", [])
            fresh = [x for x in lst if x["book_id"] not in seen_here]
            for x in fresh:
                seen_here.add(x["book_id"])
                it = items.setdefault(x["book_id"], {
                    "platform": "reelshort", "id": x["book_id"], "title": x["book_title"],
                    "episodes": x.get("chapter_count"), "views": as_int(x.get("read_count")),
                    "collects": as_int(x.get("collect_count")),
                    "tags": clean_tags(a["tag_name"] for a in x.get("theme_list", [])),
                    "shelves": [],
                })
                if shelf not in it["shelves"]:
                    it["shelves"].append(shelf)
            total = pp.get("total") or len(seen_here)
            # Some shelves return every item on every page; stop as soon as a page adds nothing new.
            if not fresh or len(seen_here) >= total or page >= (pp.get("totalPage") or 1):
                break
            page += 1
            time.sleep(PAUSE)
        print(f"  reelshort/{shelf}: {len(seen_here)} titles")
        time.sleep(PAUSE)
    return list(items.values())


def scrape_dramabox():
    items, page, pages = {}, 1, 1
    while page <= pages:
        url = DRAMABOX_TRENDING if page == 1 else f"{DRAMABOX_TRENDING}/{page}"
        pp = page_props(fetch(url))
        pages = pp.get("pages") or 1
        for pos, x in enumerate(pp["moreData"]["items"], 1):
            names = list(x.get("typeTwoNames") or []) + list(x.get("tags") or []) + list(x.get("labels") or [])
            items[x["bookId"]] = {
                "platform": "dramabox", "id": x["bookId"], "title": x["bookName"],
                "episodes": x.get("chapterCount"), "views": as_int(x.get("viewCount")),
                "collects": None, "tags": clean_tags(names), "shelves": ["trending"],
                "page": page, "pos": pos, "status": x.get("lastUpdateTimeDisplay"),
            }
        page += 1
        time.sleep(PAUSE)
    print(f"  dramabox/trending: {len(items)} titles")
    return list(items.values())


# ---------------------------------------------------------------- snapshot
def cmd_snapshot(args):
    os.makedirs(args.data_dir, exist_ok=True)
    items = []
    for name, fn in (("ReelShort", scrape_reelshort), ("DramaBox", scrape_dramabox)):
        print(f"Fetching {name} ...")
        try:
            items += fn()
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
    if not items:
        sys.exit("No data fetched; nothing saved.")
    ts = datetime.datetime.now(datetime.timezone.utc)
    path = os.path.join(args.data_dir, f"snapshot_{ts.strftime('%Y%m%dT%H%M%SZ')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"taken_at": ts.isoformat(timespec="seconds"), "items": items}, fh, ensure_ascii=False)
    print(f"Saved {len(items)} titles -> {path}")


# ---------------------------------------------------------------- report
def snapshots(data_dir):
    return sorted(glob.glob(os.path.join(data_dir, "snapshot_*.json")))


def load(path):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    d["taken"] = datetime.datetime.fromisoformat(d["taken_at"])
    return d


def cmd_list(args):
    for p in snapshots(args.data_dir):
        d = load(p)
        c = collections.Counter(i["platform"] for i in d["items"])
        print(os.path.basename(p), d["taken_at"], dict(c))


def pick(args):
    files = snapshots(args.data_dir)
    if args.from_ and args.to:
        return load(args.from_), load(args.to)
    if len(files) < 2:
        sys.exit(f"Need at least 2 snapshots in {args.data_dir} (found {len(files)}). Run `snapshot` again later.")
    return load(files[-2]), load(files[-1])


def fmt(n):
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def cmd_report(args):
    a, b = pick(args)
    days = (b["taken"] - a["taken"]).total_seconds() / 86400
    print(f"Comparing {a['taken_at']}  ->  {b['taken_at']}  ({days:.1f} days)")
    if days < 3:
        print("WARNING: under 3 days apart. Per-day numbers are noisy and cache effects can dominate.")
    if days <= 0:
        sys.exit("Snapshots must be in chronological order.")
    for platform in ("reelshort", "dramabox"):
        old = {i["id"]: i for i in a["items"] if i["platform"] == platform}
        new = {i["id"]: i for i in b["items"] if i["platform"] == platform}
        if not old or not new:
            print(f"\n=== {platform}: missing in one of the snapshots, skipped ===")
            continue
        rows = []
        for i, n in new.items():
            o = old.get(i)
            if not o or o["views"] is None or n["views"] is None:
                continue
            d = n["views"] - o["views"]
            rows.append({"t": n["title"], "tags": n["tags"], "from": o["views"], "to": n["views"],
                         "d": d, "pd": d / days, "pct": d / o["views"] * 100 if o["views"] else 0})
        entered = [n for i, n in new.items() if i not in old]
        left = [o for i, o in old.items() if i not in new]
        print(f"\n=== {platform.upper()}  matched {len(rows)} titles | new on list {len(entered)} | dropped {len(left)} ===")
        if not rows:
            print("No overlapping titles to compare.")
            continue
        total = sum(r["d"] for r in rows)
        print(f"Total view gain: {fmt(total)}  ({fmt(total / days)}/day)")

        print("\nTop titles by gain per day:")
        for r in sorted(rows, key=lambda r: -r["pd"])[:args.top]:
            print(f"  {fmt(r['pd']):>8}/day  {r['pct']:5.1f}%  {r['t']}  [{', '.join(r['tags'][:4])}]")

        by = collections.defaultdict(list)
        for r in rows:
            for t in r["tags"]:
                by[t].append(r)
        stats = []
        for t, rs in by.items():
            if len(rs) < args.min_titles:
                continue
            fr = sum(r["from"] for r in rs)
            gain = sum(r["d"] for r in rs)
            stats.append((t, len(rs), gain, gain / fr * 100 if fr else 0,
                          statistics.median(r["pd"] for r in rs)))
        print(f"\nGenres by relative growth (tags with >= {args.min_titles} titles):")
        print(f"  {'tag':<24}{'titles':>7}{'gain':>10}{'growth%':>9}{'median/day':>12}{'share of gain':>15}")
        for t, n, g, pct, med in sorted(stats, key=lambda s: -s[3])[:args.top]:
            share = g / total * 100 if total else 0
            print(f"  {t:<24}{n:>7}{fmt(g):>10}{pct:>8.1f}%{fmt(med):>12}{share:>14.0f}%")

        if entered:
            c = collections.Counter(t for n in entered for t in n["tags"])
            print(f"\nNew entrants this period ({len(entered)}), most common tags:",
                  ", ".join(f"{t}({k})" for t, k in c.most_common(8)))
    print("\nNote: views are cumulative and platform-reported; titles that are old and large gain more in"
          " absolute terms, so compare the growth% column and check the share against the number of titles.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=DEFAULT_DATA)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot")
    sub.add_parser("list")
    r = sub.add_parser("report")
    r.add_argument("--from", dest="from_")
    r.add_argument("--to")
    r.add_argument("--min-titles", type=int, default=3)
    r.add_argument("--top", type=int, default=12)
    args = ap.parse_args()
    {"snapshot": cmd_snapshot, "list": cmd_list, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
