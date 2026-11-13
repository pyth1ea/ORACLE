"""ORACLE breaking-news watcher: free, no AI. Runs every 30 minutes as a GitHub Action (.github/workflows/breaking.yml).

It reads Google News' US top stories, keeps the recent ones that sound serious (outages, attacks, disasters,
resignations, market shocks...), and checks how widely each is being covered in the last few hours. A story that
clears the bar is pushed to the phone through ntfy and logged in incoming/breaking.json, which the next ORACLE run
reads (tools/feed.py prints it) so it can score the event and add forecasts.

The ntfy channel comes from the NTFY_TOPIC environment variable (a GitHub Actions secret). It is never stored in the
repository. Without it the script only prints what it would send.

Usage:  python3 tools/breaking.py [--dry-run] [--show]
"""
import argparse
import datetime as dt
import email.utils
import json
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "incoming" / "breaking.json"
UA = {"User-Agent": "Mozilla/5.0 (ORACLE personal forecasting research)"}
TOP = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
SEARCH = "https://news.google.com/rss/search?"

FRESH_HOURS = 4          # only stories first published this recently
WINDOW = "when:3h"       # breadth is measured over this window
MIN_MAJOR = 16           # major national outlets (last 3 hours) needed for a serious-sounding story
MIN_MAJOR_ANY = 24       # ...or this many for any story at all
MAJOR = re.compile(
    r"^(AP|Associated Press|AP News|Reuters|The New York Times|The Washington Post|The Wall Street Journal|CNN|"
    r"NBC News|CBS News|ABC News|Fox News|NPR|PBS|BBC|Bloomberg|Politico|POLITICO|The Hill|Axios|The Guardian|"
    r"USA Today|USA TODAY|CNBC|Al Jazeera|Los Angeles Times|MSNBC|MS NOW|Financial Times|Time|TIME|Newsweek|"
    r"Forbes|Business Insider|NBC|CBS|ABC|Fox Business|Yahoo News|The Atlantic|Vox|Semafor|The Economist)\b", re.I)
MAX_PER_DAY = 3          # never more than this many alerts in 24 hours
REPEAT_HOURS = 48        # a story similar to one already sent is skipped for this long

SERIOUS = re.compile(
    r"\b(ground stop|grounded|nationwide|outage|blackout|cyber ?attack|hack(ed|ers)?|ransomware|explosion|blast|"
    r"attack|shooting|gunman|terror\w*|bomb\w*|missile|airstrikes?|invasion|invades?|war\b|ceasefire|coup|martial law|"
    r"state of emergency|evacuat\w*|earthquake|tsunami|hurricane|wildfire|tornado|flood\w*|collapse[sd]?|derail\w*|"
    r"crash(es|ed)?|plunge[sd]?|tumble[sd]?|sell-?off|default|emergency rate|shutdown|impeach\w*|resign\w*|"
    r"fired|ousted|indict\w*|arrested|assassinat\w*|dies|died|dead|killed|pandemic|outbreak|nuclear|sanctions|"
    r"tariffs?|strike\b|riots?|insurrection)\b", re.I)
STOP = set("""a an the and or of to in on at for from by with as is are was were be been after before over under into
amid about says said say new live updates update what we know how why who when where will would could may might
its it this that these those than then their there here his her he she they them us u.s. vs report reports latest
breaking just first more most up down out off amid against no timeline resolving climb climbs continues exclusive""".split())


def now():
    return dt.datetime.now(dt.timezone.utc)


def rss(url):
    r = requests.get(url, timeout=30, headers=UA)
    r.raise_for_status()
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        title = (it.findtext("title") or "").strip()
        src = it.find("source")
        try:
            when = email.utils.parsedate_to_datetime(it.findtext("pubDate"))
        except (TypeError, ValueError):
            continue
        outlet = (src.text if src is not None and src.text else title.rsplit(" - ", 1)[-1]).strip()
        out.append({"title": title, "outlet": outlet, "when": when, "link": it.findtext("link") or ""})
    return out


def headline(title):
    return title.rsplit(" - ", 1)[0].strip()


def words(title):
    return [w for w in re.findall(r"[a-z0-9'.-]+", headline(title).lower()) if w not in STOP and len(w) > 2]


def similar(a, b):
    wa, wb = set(words(a)), set(words(b))
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.5


def breadth(title):
    """Distinct outlets covering this story in the last few hours. Searches a few versions of its key words
    (outlets word headlines differently) and keeps the best one."""
    key = words(title)
    terms = [m.group(0).lower() for m in SERIOUS.finditer(headline(title))]
    rest = [w for w in key if w not in " ".join(terms)]
    queries = {" ".join(key[:3]), " ".join(key[:5])}
    if terms and rest:
        queries.add(" ".join(f'"{t}"' if " " in t else t for t in terms[:2]) + " " + " ".join(rest[:2]))
    best = []
    for q in queries:
        if len(q.split()) < 2:
            continue
        try:
            hits = rss(SEARCH + urllib.parse.urlencode({"q": q + " " + WINDOW, "hl": "en-US", "gl": "US",
                                                        "ceid": "US:en"}))
        except Exception:  # noqa: BLE001
            continue
        outlets = sorted({h["outlet"] for h in hits if h["when"] > now() - dt.timedelta(hours=3)})
        if sum(1 for o in outlets if MAJOR.match(o)) > sum(1 for o in best if MAJOR.match(o)):
            best = outlets
    return len(best), best


def load_log():
    try:
        return json.loads(LOG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"note": "Breaking-news alerts from tools/breaking.py. The next ORACLE run scores these.", "alerts": []}


def push(topic, alert):
    body = (f"{alert['headline']}\n\nCovered by {alert['outlets']} outlets ({alert['major']} major) in the last 3 hours "
            f"({', '.join(alert['sample'][:4])}...).\n\nLogged for the next ORACLE run (Mon/Wed/Fri).")
    requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), timeout=20,
                  headers={"Title": "ORACLE breaking news", "Tags": "rotating_light", "Priority": "high",
                           "Click": "https://pyth1ea.github.io/ORACLE/"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't push or write the log")
    ap.add_argument("--show", action="store_true", help="print every candidate and its score")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    log = load_log()
    recent = [a for a in log["alerts"] if dt.datetime.fromisoformat(a["sent"]) > now() - dt.timedelta(hours=24)]
    if len(recent) >= MAX_PER_DAY:
        print(f"Daily limit reached ({MAX_PER_DAY}); nothing sent.")
        return
    lately = [a for a in log["alerts"] if dt.datetime.fromisoformat(a["sent"]) > now() - dt.timedelta(hours=REPEAT_HOURS)]

    stories = [s for s in rss(TOP)[:20] if s["when"] > now() - dt.timedelta(hours=FRESH_HOURS)]
    new = []
    for s in stories:
        if any(similar(s["title"], a["headline"]) for a in lately + new):
            continue
        serious = bool(SERIOUS.search(headline(s["title"])))
        n, outlets = breadth(s["title"])
        major = [o for o in outlets if MAJOR.match(o)]
        hit = len(major) >= MIN_MAJOR_ANY or (serious and len(major) >= MIN_MAJOR)
        if args.show:
            print(f"{'ALERT' if hit else '     '} {n:3d} outlets ({len(major)} major)  serious={serious!s:5}  "
                  f"{headline(s['title'])[:90]}")
        outlets = major + [o for o in outlets if o not in major]
        if hit:
            new.append({"sent": now().isoformat(timespec="minutes"), "published": s["when"].isoformat(timespec="minutes"),
                        "headline": headline(s["title"]), "outlet": s["outlet"], "outlets": n, "sample": outlets[:8], "major": len(major)})
        if len(recent) + len(new) >= MAX_PER_DAY:
            break

    if not new:
        print("No breaking stories.")
        return
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    for a in new:
        print(f"BREAKING: {a['headline']} ({a['outlets']} outlets)")
        if topic and not args.dry_run:
            try:
                push(topic, a)
            except requests.RequestException as ex:
                print("push failed:", ex)
    if not args.dry_run:
        log["alerts"] = (log["alerts"] + new)[-200:]
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text(json.dumps(log, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
