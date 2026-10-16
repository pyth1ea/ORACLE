"""ORACLE feed: the daily intake.

Pulls headlines from a balanced set of RSS feeds (wire services, outlets across the spectrum,
official sources) and key numbers from FRED, then de-duplicates and ranks stories so the daily
run reads ~40 story clusters instead of hundreds of articles.

The keyword "pre-scores" here are crude and only used for ranking. The real event scores
(chaos, polarization, economic disruption, precedent, institutional resistance) are assigned by
the daily run using the rubric in ORACLE.md and saved to data/events.json.

Usage:  python tools/feed.py [--hours 30] [--top 45]
Writes: data/feed/YYYY-MM-DD.json, data/feed/YYYY-MM-DD.md, data/metrics.json
"""
import argparse
import csv
import datetime as dt
import html
import io
import json
import re
import sys
import time
from pathlib import Path

import feedparser
import requests

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = {"User-Agent": "Mozilla/5.0 (ORACLE personal forecasting research)"}
TIMEOUT = 20

# name, approximate lean, url. Google News RSS is used for outlets without a public feed.
SOURCES = [
    ("AP", "center", "https://news.google.com/rss/search?q=when:1d+site:apnews.com&hl=en-US&gl=US&ceid=US:en"),
    ("Reuters", "center", "https://news.google.com/rss/search?q=when:1d+site:reuters.com&hl=en-US&gl=US&ceid=US:en"),
    ("C-SPAN", "center", "https://news.google.com/rss/search?q=when:2d+site:c-span.org&hl=en-US&gl=US&ceid=US:en"),
    ("BBC US", "center", "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"),
    ("BBC World", "center", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("PBS NewsHour", "center", "https://www.pbs.org/newshour/feeds/rss/headlines"),
    ("The Hill", "center", "https://thehill.com/news/feed/"),
    ("Axios", "center", "https://api.axios.com/feed/"),
    ("NPR", "center-left", "https://feeds.npr.org/1001/rss.xml"),
    ("NPR Politics", "center-left", "https://feeds.npr.org/1014/rss.xml"),
    ("NPR Business", "center-left", "https://feeds.npr.org/1006/rss.xml"),
    ("Politico", "center-left", "https://rss.politico.com/politics-news.xml"),
    ("The Guardian US", "left", "https://www.theguardian.com/us-news/rss"),
    ("Fox News", "right", "https://moxie.foxnews.com/google-publisher/politics.xml"),
    ("Washington Examiner", "right", "https://www.washingtonexaminer.com/feed/"),
    ("Federal Reserve", "official", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("BLS", "official", "https://www.bls.gov/feed/bls_latest.rss"),
]

# FRED series pulled without an API key (public CSV endpoint).
FRED = {
    "SP500": ("S&P 500", "index"),
    "VIXCLS": ("VIX volatility index", "index"),
    "DCOILBRENTEU": ("Brent crude", "$/bbl"),
    "DCOILWTICO": ("WTI crude", "$/bbl"),
    "GASREGW": ("US regular gasoline", "$/gal"),
    "DFEDTARU": ("Fed funds target (upper)", "%"),
    "DGS10": ("10-year Treasury yield", "%"),
    "T10Y2Y": ("10y-2y yield spread", "pts"),
    "CPIAUCSL": ("CPI (index, seasonally adjusted)", "index"),
    "CPIAUCNS": ("CPI (index, not seasonally adjusted - matches headline y/y)", "index"),
    "CPILFESL": ("Core CPI (index)", "index"),
    "PAYEMS": ("Nonfarm payrolls", "thousands"),
    "UNRATE": ("Unemployment rate", "%"),
    "ICSA": ("Initial jobless claims", "claims"),
    "UMCSENT": ("Consumer sentiment (UMich)", "index"),
}

DOMAIN_WORDS = {
    "politics": ["election", "midterm", "senate", "house", "congress", "trump", "democrat", "republican",
                 "poll", "vote", "voter", "ballot", "campaign", "governor", "supreme court", "justice department",
                 "white house", "speaker", "shutdown", "impeach", "lawmaker", "gop", "administration", "judge",
                 "doj", "fbi", "press", "candidate", "debate"],
    "economy": ["inflation", "cpi", "federal reserve", "the fed", "fed ", "interest rate", "rate hike", "jobs",
                "payroll", "unemployment", "gdp", "recession", "stocks", "stock market", "wall street", "oil",
                "gas price", "gasoline", "tariff", "economy", "earnings", "bank", "dow", "s&p", "nasdaq",
                "treasury", "debt", "prices", "wages", "housing", "mortgage"],
    "unrest": ["protest", "riot", "shooting", "police", "national guard", "unrest", "violence", "rally",
               "march", "arrest", "raid", "ice ", "immigration", "deport", "measles", "outbreak", "no kings",
               "clash", "gunman", "extremis"],
    "foreign": ["iran", "israel", "ukraine", "russia", "china", "hormuz", "yemen", "houthi", "nato", "missile",
                "war", "tanker", "saudi", "ceasefire", "military", "troops", "strike", "pentagon", "gaza",
                "taiwan", "sanction", "diplomat", "venezuela"],
}
CHAOS = ["war", "attack", "killed", "dead", "crisis", "collapse", "riot", "emergency", "explosion", "shooting",
         "raid", "crash", "plunge", "chaos", "strike", "missile", "violence", "seize", "escalat", "evacuat"]
POLAR = ["partisan", "impeach", "indict", "raid", "fraud", "rigged", "censor", "ban", "protest", "extrem",
         "lawsuit", "blocks", "retaliat", "purge", "loyal", "unconstitutional", "probe", "investigat"]
ECON = ["inflation", "recession", "layoff", "rate hike", "oil", "prices", "tumble", "plunge", "slump", "jobs",
        "gdp", "tariff", "default", "shutdown", "gasoline", "stagflation", "debt", "bankrupt"]
STOP = set("""a an the and or but of to in on for at by with from as is are was were be been it its this that
these those after before over under into about amid says said say new will would could may might than then
more most just how why what who when where which while not no yes you your our their his her they them we
he she us out up down off all any can has have had do does did""".split())


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def clean_title(title, source):
    t = re.sub(r"\s+", " ", html.unescape(title or "")).strip()
    # Google News appends " - Outlet name"
    if " - " in t and source in ("AP", "Reuters", "C-SPAN"):
        t = t.rsplit(" - ", 1)[0]
    return t


def tokens(text):
    words = re.findall(r"[a-z0-9&']+", text.lower())
    return {w for w in words if len(w) > 2 and w not in STOP}


def count_hits(text, words):
    t = " " + text.lower() + " "
    return sum(1 for w in words if w in t)


def pre_score(text):
    return {
        "chaos": min(10, 1 + 2 * count_hits(text, CHAOS)),
        "polarization": min(10, 1 + 2 * count_hits(text, POLAR)),
        "economic": min(10, 1 + 2 * count_hits(text, ECON)),
    }


def classify(text):
    scores = {d: count_hits(text, w) for d, w in DOMAIN_WORDS.items()}
    best = max(scores, key=scores.get)
    return (best if scores[best] > 0 else None), scores


def fetch_feeds(hours):
    cutoff = now_utc() - dt.timedelta(hours=hours)
    items, status = [], []
    for name, lean, url in SOURCES:
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            n = 0
            for e in parsed.entries:
                ts = e.get("published_parsed") or e.get("updated_parsed")
                when = dt.datetime(*ts[:6], tzinfo=dt.timezone.utc) if ts else None
                if when and when < cutoff:
                    continue
                title = clean_title(e.get("title", ""), name)
                if not title:
                    continue
                summary = html.unescape(re.sub(r"<[^>]+>", " ", e.get("summary", "") or ""))
                summary = re.sub(r"\s+", " ", summary).strip()[:400]
                if summary.lower().startswith(title.lower()[:40]):
                    summary = ""  # Google News repeats the headline as the summary
                items.append({
                    "title": title, "summary": summary, "link": e.get("link", ""),
                    "source": name.split(" ")[0] if name.startswith(("NPR", "BBC")) else name, "lean": lean,
                    "published": when.isoformat() if when else None,
                })
                n += 1
            status.append({"source": name, "ok": True, "items": n})
        except Exception as ex:  # one dead feed must not stop the run
            status.append({"source": name, "ok": False, "error": str(ex)[:160]})
        time.sleep(0.3)
    return items, status


def cluster(items):
    clusters = []
    for it in items:
        tk = tokens(it["title"])
        if not tk:
            continue
        best, best_sim = None, 0.0
        for c in clusters:
            inter = len(tk & c["tokens"])
            sim = inter / max(1, len(tk | c["tokens"]))
            if (sim >= 0.42 or inter >= 5) and sim > best_sim:
                best, best_sim = c, sim
        if best:
            best["items"].append(it)
            best["tokens"] |= tk
        else:
            clusters.append({"tokens": set(tk), "items": [it]})
    return clusters


def rank(clusters):
    out = []
    for c in clusters:
        its = c["items"]
        text = " ".join(i["title"] + " " + i["summary"] for i in its)
        domain, dscores = classify(text)
        official = any(i["lean"] == "official" for i in its)
        if domain is None and not official:
            continue
        sources = sorted({i["source"] for i in its})
        leans = sorted({i["lean"] for i in its})
        ps = pre_score(text)
        importance = (len(sources) * 3 + (2 if official else 0) + max(ps.values()) * 0.5
                      + min(3, sum(dscores.values())) * 0.5)
        lead = sorted(its, key=lambda i: (i["lean"] != "center", i["published"] or ""))[0]
        out.append({
            "headline": lead["title"],
            "summary": lead["summary"],
            "link": lead["link"],
            "domain": domain or "economy",
            "sources": sources,
            "leans": leans,
            "n_sources": len(sources),
            "pre_scores": ps,
            "importance": round(importance, 2),
            "other_headlines": [i["title"] for i in its if i is not lead][:4],
        })
    out.sort(key=lambda x: -x["importance"])
    return out


def fetch_fred(start):
    series = {}
    for sid, (name, units) in FRED.items():
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            rows = list(csv.reader(io.StringIO(r.text)))[1:]
            obs = [(d, float(v)) for d, v in rows if v not in (".", "")]
            if not obs:
                continue
            last_d, last_v = obs[-1]
            prev = obs[-2][1] if len(obs) > 1 else None
            series[sid] = {
                "name": name, "units": units, "last_date": last_d, "last": last_v,
                "prev": prev, "change": None if prev is None else round(last_v - prev, 4),
                "history": obs[-130:],
            }
        except Exception as ex:
            series[sid] = {"name": name, "units": units, "error": str(ex)[:160]}
        time.sleep(0.2)

    derived = {}
    for sid, label in (("CPIAUCNS", "cpi_yoy"), ("CPILFESL", "core_cpi_yoy")):
        h = series.get(sid, {}).get("history") or []
        if not h:
            continue
        # match the same calendar month a year earlier (months can be missing, e.g. Oct 2025 shutdown)
        by_date = dict(h)
        last_d, last_v = h[-1]
        year_ago = f"{int(last_d[:4]) - 1}{last_d[4:]}"
        if year_ago in by_date:
            derived[label] = {"date": last_d, "value": round((last_v / by_date[year_ago] - 1) * 100, 2), "units": "%"}
    h = series.get("PAYEMS", {}).get("history") or []
    if len(h) >= 2:
        derived["payrolls_change"] = {"date": h[-1][0], "value": round(h[-1][1] - h[-2][1], 1), "units": "thousands"}
    return series, derived


def write_md(path, date, ranked, status):
    ok = [s for s in status if s["ok"]]
    bad = [s for s in status if not s["ok"]]
    lean_counts = {}
    for c in ranked:
        for l in c["leans"]:
            lean_counts[l] = lean_counts.get(l, 0) + 1
    lines = [f"# ORACLE feed shortlist — {date}", "",
             f"{len(ranked)} story clusters from {len(ok)} working sources."
             + (f" Failed: {', '.join(s['source'] for s in bad)}." if bad else ""),
             "Lean coverage (clusters touching each lean): "
             + ", ".join(f"{k} {v}" for k, v in sorted(lean_counts.items())), "",
             "Pre-scores are keyword guesses for ranking only. Score events yourself with the ORACLE.md rubric.", ""]
    for dom in ("politics", "economy", "unrest", "foreign"):
        group = [c for c in ranked if c["domain"] == dom]
        if not group:
            continue
        lines.append(f"## {dom.title()}")
        for c in group:
            p = c["pre_scores"]
            lines.append(f"- **{c['headline']}** — {c['n_sources']} src ({', '.join(c['sources'])}) "
                         f"· pre c{p['chaos']}/p{p['polarization']}/e{p['economic']} · {c['link']}")
            if c["summary"]:
                lines.append(f"  - {c['summary'][:220]}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=30)
    ap.add_argument("--top", type=int, default=45)
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--no-metrics", action="store_true")
    args = ap.parse_args()

    date = dt.date.today().isoformat()
    (DATA / "feed").mkdir(parents=True, exist_ok=True)

    if not args.no_news:
        items, status = fetch_feeds(args.hours)
        ranked = rank(cluster(items))[: args.top]
        out = {"date": date, "generated": now_utc().isoformat(), "hours": args.hours,
               "n_items": len(items), "sources": status, "clusters": ranked}
        (DATA / "feed" / f"{date}.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        write_md(DATA / "feed" / f"{date}.md", date, ranked, status)
        ok = sum(1 for s in status if s["ok"])
        print(f"News: {len(items)} items from {ok}/{len(status)} sources -> {len(ranked)} clusters "
              f"(data/feed/{date}.md)")
        for s in status:
            if not s["ok"]:
                print(f"  feed failed: {s['source']}: {s['error']}")

    if not args.no_metrics:
        start = (dt.date.today() - dt.timedelta(days=500)).isoformat()
        series, derived = fetch_fred(start)
        (DATA / "metrics.json").write_text(json.dumps(
            {"updated": now_utc().isoformat(), "source": "FRED (fred.stlouisfed.org)", "series": series,
             "derived": derived}, indent=1, ensure_ascii=False), encoding="utf-8")
        good = [k for k, v in series.items() if "last" in v]
        print(f"Metrics: {len(good)}/{len(FRED)} FRED series updated (data/metrics.json)")
        for k in good:
            v = series[k]
            print(f"  {v['name']}: {v['last']} ({v['last_date']})")
        for k, v in derived.items():
            print(f"  {k}: {v['value']}{v['units'] if v['units'] == '%' else ''} ({v['date']})")


if __name__ == "__main__":
    main()
