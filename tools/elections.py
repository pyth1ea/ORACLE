"""ORACLE elections: the data behind the app's Elections tab (2026 midterms).

Commands (run from the repository root):
  python3 tools/elections.py refresh   Re-download every race's candidates, incumbent and expert ratings from
                                       Wikipedia's 2026 election pages (which collect Cook, Inside Elections, Sabato
                                       and others), plus the 2024 county results once. Keeps everything the runs added.
                                       Free: no judgment needed. If a download fails, the old data is kept.
  python3 tools/elections.py status    One compact page for the run: spotlight states, close races, which still
                                       need an ORACLE forecast, early-vote and results freshness, rating changes.
  python3 tools/elections.py patch FILE [--dry-run]
                                       Apply the run's judgments from a JSON file (forecast links, notes, polls,
                                       betting odds, early vote, counties to watch, results). Everything is
                                       validated first; on any error nothing changes.
  python3 tools/elections.py check     Validate data/elections.json.

Files: data/elections.json (races, spotlight states) and data/elections_2024.json (2024 presidential result by
county, fixed). tools/build.py packages both into the app as elections.js.

Rating scale used everywhere: each expert rating becomes a number (Safe/Solid 3, Likely 2, Lean 1, Tilt 0.5,
Toss-up 0; negative = Democratic side, positive = Republican side) and the race's "score" is the average.
"""
import argparse
import copy
import csv
import datetime as dt
import html
import io
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from oracle_time import today as central_today  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FILE = DATA / "elections.json"
FILE24 = DATA / "elections_2024.json"
UA = {"User-Agent": "ORACLE-personal-forecaster/1.0 (https://github.com/pyth1ea/ORACLE)"}
WIKI = "https://en.wikipedia.org/api/rest_v1/page/html/"
PAGES = {"senate": "2026_United_States_Senate_elections", "governor": "2026_United_States_gubernatorial_elections",
         "house": "2026_United_States_House_of_Representatives_elections",
         "house_ratings": "2026_United_States_House_of_Representatives_election_ratings"}
RESULTS_2024 = ("https://raw.githubusercontent.com/tonmcg/US_County_Level_Election_Results_08-24/master/"
                "2024_US_County_Level_Presidential_Results.csv")
ELECTION_DAY = "2026-11-03"
DEFAULT_SPOTLIGHT = ["TX", "MI", "CA", "ME", "OH", "AK", "GA", "IA"]
KEY_RATERS = ["Cook", "IE", "Sabato"]
MIN_RACES = {"senate": 30, "governor": 30, "house": 400}

STATES = {
    "Alabama": ("AL", "01"), "Alaska": ("AK", "02"), "Arizona": ("AZ", "04"), "Arkansas": ("AR", "05"),
    "California": ("CA", "06"), "Colorado": ("CO", "08"), "Connecticut": ("CT", "09"), "Delaware": ("DE", "10"),
    "District of Columbia": ("DC", "11"), "Florida": ("FL", "12"), "Georgia": ("GA", "13"), "Hawaii": ("HI", "15"),
    "Idaho": ("ID", "16"), "Illinois": ("IL", "17"), "Indiana": ("IN", "18"), "Iowa": ("IA", "19"),
    "Kansas": ("KS", "20"), "Kentucky": ("KY", "21"), "Louisiana": ("LA", "22"), "Maine": ("ME", "23"),
    "Maryland": ("MD", "24"), "Massachusetts": ("MA", "25"), "Michigan": ("MI", "26"), "Minnesota": ("MN", "27"),
    "Mississippi": ("MS", "28"), "Missouri": ("MO", "29"), "Montana": ("MT", "30"), "Nebraska": ("NE", "31"),
    "Nevada": ("NV", "32"), "New Hampshire": ("NH", "33"), "New Jersey": ("NJ", "34"), "New Mexico": ("NM", "35"),
    "New York": ("NY", "36"), "North Carolina": ("NC", "37"), "North Dakota": ("ND", "38"), "Ohio": ("OH", "39"),
    "Oklahoma": ("OK", "40"), "Oregon": ("OR", "41"), "Pennsylvania": ("PA", "42"), "Rhode Island": ("RI", "44"),
    "South Carolina": ("SC", "45"), "South Dakota": ("SD", "46"), "Tennessee": ("TN", "47"), "Texas": ("TX", "48"),
    "Utah": ("UT", "49"), "Vermont": ("VT", "50"), "Virginia": ("VA", "51"), "Washington": ("WA", "53"),
    "West Virginia": ("WV", "54"), "Wisconsin": ("WI", "55"), "Wyoming": ("WY", "56"),
}
ABBR = {a: (name, f) for name, (a, f) in STATES.items()}
PARTIES = {"D", "R", "I", "L", "G", "O"}
RUN_FIELDS = {"prediction", "yes_party", "note", "polls", "market", "result"}


# ---------------------------------------------------------------------------------------------- downloading

def fetch(url, timeout=60):
    import requests
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.text


class _Tables(HTMLParser):
    """Every <table> on a page as a grid of cell texts, with rowspan/colspan expanded, plus the heading above it."""

    def __init__(self):
        super().__init__()
        self.tables, self.stack, self.cell, self.skip, self.heading, self.in_h = [], [], None, 0, "", False

    def handle_starttag(self, tag, a):
        a = dict(a)
        if tag in ("h2", "h3", "h4"):
            self.in_h, self.heading = True, ""
        if tag in ("style", "script", "sup"):
            self.skip += 1
        if tag == "table":
            self.stack.append({"rows": [], "heading": self.heading.strip()})
        elif tag == "tr" and self.stack:
            self.stack[-1]["rows"].append([])
        elif tag in ("td", "th") and self.stack and self.stack[-1]["rows"]:
            num = lambda v: int(re.sub(r"\D", "", v or "1") or 1)  # noqa: E731
            self.cell = {"text": "", "rs": num(a.get("rowspan")), "cs": num(a.get("colspan"))}
        elif tag == "br" and self.cell is not None:
            self.cell["text"] += " / "

    def handle_endtag(self, tag):
        if tag in ("h2", "h3", "h4"):
            self.in_h = False
        if tag in ("style", "script", "sup"):
            self.skip = max(0, self.skip - 1)
        if tag in ("td", "th") and self.cell is not None and self.stack:
            self.stack[-1]["rows"][-1].append(self.cell)
            self.cell = None
        elif tag == "table" and self.stack:
            self.tables.append(self.stack.pop())

    def handle_data(self, d):
        if self.skip:
            return
        if self.in_h:
            self.heading += d
        if self.cell is not None:
            self.cell["text"] += d


def tables(page_html):
    p = _Tables()
    p.feed(page_html)
    out = []
    for t in p.tables:
        grid, pending = [], {}
        for raw in t["rows"]:
            row, col, it = [], 0, iter(raw)
            while True:
                if pending.get(col, (0,))[0] > 0:
                    n, txt = pending[col]
                    row.append(txt)
                    pending[col] = (n - 1, txt)
                    col += 1
                    continue
                c = next(it, None)
                if c is None:
                    break
                txt = re.sub(r"\[[^\]]*\]", "", " ".join(html.unescape(c["text"]).split())).strip()
                for _ in range(c["cs"]):
                    row.append(txt)
                    if c["rs"] > 1:
                        pending[col] = (c["rs"] - 1, txt)
                    col += 1
            grid.append(row)
        out.append({"heading": t["heading"], "grid": grid})
    return out


# ---------------------------------------------------------------------------------------------- parsing

def party(s):
    s = (s or "").strip().lower()
    if s.startswith(("democratic", "democrat", "dfl")):
        return "D"
    if s.startswith("republican"):
        return "R"
    if s.startswith("independent"):
        return "I"
    if s.startswith("libertarian"):
        return "L"
    if "green" in s:
        return "G"
    return "O"


def candidates(cell, incumbent=None):
    """Candidates, Democrats first, then Republicans, independents, others; the incumbent first within a party."""
    out = [{"name": n.strip(), "party": party(p)} for n, p in re.findall(r"▌\s*([^▌]+?)\s*\(([^()]+)\)\s*(?=▌|$)", cell)]
    order = {"D": 0, "R": 1, "I": 2}
    return sorted(out, key=lambda c: (order.get(c["party"], 3), c["name"] != incumbent, c["name"]))


def rating_score(r):
    r = (r or "").lower().replace("toss-up", "tossup")
    if "tossup" in r:
        return 0.0
    m = re.match(r"(safe|solid|likely|lean|tilt)\s+(dfl|d|r)\b", r)
    if not m:
        return None
    v = {"safe": 3, "solid": 3, "likely": 2, "lean": 1, "tilt": 0.5}[m.group(1)]
    return v if m.group(2) == "r" else -v


def label(score):
    if score is None:
        return "Not rated"
    a, side = abs(score), ("D" if score < 0 else "R")
    if a < 0.5:
        return "Toss-up"
    return ("Lean " if a < 1.5 else "Likely " if a < 2.5 else "Safe ") + side


def state_of(cell):
    """'Florida / (special)' -> ('Florida', True); 'Texas 7' -> ('Texas', '7'); 'Alaska at-large' -> ('Alaska', 'AL')."""
    base = cell.split(" / ")[0].strip()
    if base in STATES:
        return base, ("special" in cell.lower() or "class 3" in cell.lower())
    m = re.match(r"(.+?)\s+(\d+|at-large)$", base)
    if m and m.group(1) in STATES:
        return m.group(1), ("AL" if m.group(2) == "at-large" else m.group(2))
    return None, None


def headers(grid):
    """Column names: the last header row before the data starts."""
    hdr = []
    for row in grid:
        if row and state_of(row[0])[0]:
            break
        if any(row):
            hdr = row
    return hdr


def col(hdr, *names, default=None):
    for i, h in enumerate(hdr):
        if any(h.lower().startswith(n.lower()) for n in names):
            return i
    return default


def ratings_from(grid):
    """{(state, key): {"ratings": {rater: text}, "pvi": .., "last": ..}} from a Wikipedia ratings table."""
    hdr = headers(grid)
    out = {}
    for row in grid:
        name, key = state_of(row[0]) if row else (None, None)
        if not name or len(row) < 5:
            continue
        rs = {}
        for i in range(4, min(len(row), len(hdr))):
            rater = hdr[i].split(" / ")[0].strip().rstrip(".")
            if rater and rating_score(row[i]) is not None:
                rs[rater] = row[i]
        out[(name, key)] = {"ratings": rs, "pvi": row[1], "incumbent": row[2].split(" / ")[0], "last": row[3]}
    return out


def score_fields(rs):
    vals = [rating_score(v) for v in rs.values()]
    vals = [v for v in vals if v is not None]
    s = round(sum(vals) / len(vals), 2) if vals else None
    return {"score": s, "rating": label(s), "n_raters": len(vals),
            "ratings": {k: rs[k] for k in KEY_RATERS if k in rs}}


def is_open(status):
    return not re.search(r"\b(renominated|advanced to general|nominated)\b", (status or "").lower())


def find(tabs, heading, need=None):
    for t in tabs:
        if t["heading"].lower().startswith(heading.lower()) and (need is None or any(need in " ".join(r) for r in t["grid"][:2])):
            return t["grid"]
    return []


def build_senate(tabs):
    races = {}
    preds = ratings_from(find(tabs, "Predictions", "Ratings"))
    for heading in ("Elections leading to the next Congress", "Special elections during the preceding Congress"):
        grid = find(tabs, heading)
        hdr = headers(grid)
        ci = {k: col(hdr, *n) for k, n in {"sen": ("Senator", "Incumbent"), "party": ("Party",), "status": ("Results", "Status"),
                                         "cands": ("Candidates",)}.items()}
        if ci["cands"] is None:
            continue
        for row in grid:
            name, special = state_of(row[0]) if row else (None, None)
            if not name or len(row) <= ci["cands"]:
                continue
            a, _ = STATES[name]
            special = special or heading.startswith("Special")
            rid = f"{a}-SEN" if f"{a}-SEN" not in races else f"{a}-SEN2"
            pr = preds.get((name, True)) or preds.get((name, False)) or {}
            status = row[ci["status"]] if ci["status"] is not None else ""
            inc = pr.get("incumbent") or row[ci["sen"]].split(" / ")[0]
            races[rid] = {"state": a, "office": "senate", "special": special,
                          "incumbent": {"name": inc, "party": party(row[ci["party"]] if ci["party"] is not None else "")},
                          "status": status, "open": is_open(status), "candidates": candidates(row[ci["cands"]], inc),
                          "pvi": pr.get("pvi"), "last": pr.get("last"), **score_fields(pr.get("ratings", {}))}
    return races


def build_governor(tabs):
    races = {}
    preds = ratings_from(find(tabs, "Predictions", "Ratings"))
    grid = find(tabs, "States")
    hdr = headers(grid)
    ci = {"gov": col(hdr, "Governor"), "party": col(hdr, "Party"), "status": col(hdr, "Status"), "cands": col(hdr, "Candidates")}
    for row in grid:
        name, _ = state_of(row[0]) if row else (None, None)
        if not name or ci["cands"] is None or len(row) <= ci["cands"]:
            continue
        a, _ = STATES[name]
        pr = preds.get((name, False)) or {}
        status = row[ci["status"]]
        races[f"{a}-GOV"] = {"state": a, "office": "governor", "special": False,
                             "incumbent": {"name": row[ci["gov"]], "party": party(row[ci["party"]])},
                             "status": status, "open": is_open(status), "candidates": candidates(row[ci["cands"]], row[ci["gov"]]),
                             "pvi": pr.get("pvi"), "last": pr.get("last"), **score_fields(pr.get("ratings", {}))}
    return races


def build_house(tabs, rating_tabs):
    races = {}
    preds = ratings_from(find(rating_tabs, "Latest published ratings"))
    for t in tabs:
        grid = t["grid"]
        hdr = headers(grid)
        if t["heading"] not in STATES or col(hdr, "Candidates") is None or col(hdr, "Member") is None:
            continue
        ci = {"pvi": col(hdr, "2026"), "mem": col(hdr, "Member"), "party": col(hdr, "Party"), "status": col(hdr, "Status"),
              "cands": col(hdr, "Candidates")}
        for row in grid:
            name, d = state_of(row[0]) if row else (None, None)
            if not name or not isinstance(d, str) or len(row) <= ci["cands"]:
                continue
            a, _ = STATES[name]
            rid = f"{a}-AL" if d == "AL" else f"{a}-{int(d):02d}"
            pr = preds.get((name, d))
            pvi = row[ci["pvi"]] if ci["pvi"] is not None else ""
            inc_party = party(row[ci["party"]]) if "None" not in row[ci["party"]] else None
            status = row[ci["status"]] if ci["status"] is not None else ""
            if pr:
                sf = score_fields(pr["ratings"])
            else:  # not on any expert's competitive list: safe for the side the district leans to
                m = re.match(r"([DR])\+(\d+)", pvi)
                side = m.group(1) if m else inc_party
                sf = {"score": (-3.0 if side == "D" else 3.0) if side in ("D", "R") else None, "n_raters": 0, "ratings": {}}
                sf["rating"] = label(sf["score"])
            races[rid] = {"state": a, "office": "house", "district": d, "special": False,
                          "incumbent": {"name": row[ci["mem"]] if inc_party else None, "party": inc_party},
                          "status": status, "open": is_open(status) or not inc_party,
                          "candidates": candidates(row[ci["cands"]], row[ci["mem"]] if inc_party else None),
                          "pvi": pvi, "last": pr.get("last") if pr else None, **sf}
    return races


def fetch_2024():
    rows = csv.DictReader(io.StringIO(fetch(RESULTS_2024)))
    out = {}
    for r in rows:
        f = r["county_fips"].zfill(5)
        try:
            out[f] = [round(float(r["per_gop"]) - float(r["per_dem"]), 4), int(float(r["total_votes"]))]
        except ValueError:
            continue
    return {"source": "2024 presidential result by county (tonmcg/US_County_Level_Election_Results_08-24)",
            "note": "m = Republican share minus Democratic share (positive = Trump won); v = total votes",
            "counties": out}


def auto_watch(st, e24):
    """Counties worth watching in a state, from the 2024 result: the biggest vote, and the closest big places."""
    pre = ABBR[st][1]
    cs = [(f, m, v) for f, (m, v) in e24["counties"].items() if f.startswith(pre)]
    if not cs:
        return []
    total = sum(v for _, _, v in cs)
    big = sorted(cs, key=lambda x: -x[2])[:3]
    close = sorted((c for c in cs if c[2] >= 0.015 * total and c not in big), key=lambda x: abs(x[1]))[:3]
    won = lambda m: f"{'Trump' if m > 0 else 'Harris'} won it by {abs(m) * 100:.0f} points"  # noqa: E731
    return ([{"fips": f, "why": f"one of the biggest vote counts: {v:,} votes in 2024; {won(m)}"} for f, m, v in big] +
            [{"fips": f, "why": f"closely split in 2024: {won(m)}; {v:,} votes"} for f, m, v in close])


# ---------------------------------------------------------------------------------------------- the data file

def load():
    if FILE.exists():
        return json.loads(FILE.read_text(encoding="utf-8"))
    return {"meta": {"election_day": ELECTION_DAY, "refreshed": None,
                     "control": {"house": "P-0001", "senate": "P-0002"},
                     "sources": {"ratings": "Wikipedia's 2026 election pages, which collect Cook, Inside Elections (IE), "
                                            "Sabato's Crystal Ball and others; the score is the average across raters",
                                 "map": "US Census county and state shapes (us-atlas)"}},
            "spotlight": {s: {} for s in DEFAULT_SPOTLIGHT}, "races": {}, "changes": []}


def save(db):
    db["meta"]["states"] = {a: [n, f] for n, (a, f) in STATES.items()}
    FILE.write_text(json.dumps(db, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def refresh(db):
    today = central_today().isoformat()
    try:
        tabs = {k: tables(fetch(WIKI + v)) for k, v in PAGES.items()}
    except Exception as ex:  # noqa: BLE001
        print(f"WARNING: couldn't download the race pages ({type(ex).__name__}: {ex}). Kept the old race data.")
        tabs = None
    if tabs:
        fresh = {"senate": build_senate(tabs["senate"]), "governor": build_governor(tabs["governor"]),
                 "house": build_house(tabs["house"], tabs["house_ratings"])}
        for office, races in fresh.items():
            if len(races) < MIN_RACES[office]:
                print(f"WARNING: only {len(races)} {office} races parsed (page layout changed?). Kept the old {office} data.")
                continue
            for rid, r in races.items():
                old = db["races"].get(rid)
                if old:
                    if old.get("rating") != r["rating"] and old.get("rating"):
                        db["changes"].append({"date": today, "race": rid, "from": old["rating"], "to": r["rating"]})
                    r.update({k: v for k, v in old.items() if k in RUN_FIELDS})
                db["races"][rid] = r
        db["changes"] = db["changes"][-40:]
        db["meta"]["refreshed"] = today
        n = {o: sum(1 for r in db["races"].values() if r["office"] == o) for o in MIN_RACES}
        print(f"Races: {n['senate']} Senate, {n['governor']} governor, {n['house']} House (ratings as of {today}).")
    if not FILE24.exists():
        try:
            FILE24.write_text(json.dumps(fetch_2024(), separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
            print("Saved the 2024 county results.")
        except Exception as ex:  # noqa: BLE001
            print(f"WARNING: couldn't download the 2024 county results ({ex}).")
    if FILE24.exists():
        e24 = json.loads(FILE24.read_text(encoding="utf-8"))
        for st, sp in db["spotlight"].items():
            sp["auto_watch"] = auto_watch(st, e24)


def validate(db):
    errs = []
    preds = {p["id"] for p in json.loads((DATA / "oracle.json").read_text(encoding="utf-8")).get("predictions", [])}
    date = lambda s: bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(s)))  # noqa: E731
    for st in db["spotlight"]:
        if st not in ABBR:
            errs.append(f"spotlight: unknown state {st}")
    for rid, r in db["races"].items():
        w = lambda m: errs.append(f"{rid}: {m}")  # noqa: E731
        if r.get("state") not in ABBR or r.get("office") not in ("senate", "governor", "house"):
            w("bad state or office")
        if r.get("prediction") and r["prediction"] not in preds:
            w(f"prediction {r['prediction']} doesn't exist in data/oracle.json")
        if r.get("yes_party", "D") not in PARTIES:
            w("yes_party must be one of " + "/".join(sorted(PARTIES)))
        if r.get("note") is not None and (not isinstance(r["note"], str) or len(r["note"]) > 400):
            w("note must be text, at most 400 characters")
        for key, lo, hi in (("polls", 0, 100), ("market", 0, 1)):
            x = r.get(key)
            if x is None:
                continue
            if not isinstance(x, dict) or not x.get("source") or not date(x.get("as_of")):
                w(f"{key} needs a source and an as_of date")
            for p, v in (x or {}).items():
                if p in ("source", "as_of"):
                    continue
                if p not in PARTIES or not isinstance(v, (int, float)) or not lo <= v <= hi:
                    w(f"{key}.{p} must be a party letter with a number from {lo} to {hi}")
        res = r.get("result")
        if res is not None:
            if res.get("winner") not in PARTIES | {None} or not isinstance(res.get("called"), bool) or not date(res.get("as_of")):
                w("result needs winner (party letter or null), called (true/false) and as_of")
            for p, v in (res.get("pct") or {}).items():
                if p not in PARTIES or not 0 <= v <= 100:
                    w("result.pct must map party letters to 0-100")
            for f, sh in (res.get("counties") or {}).items():
                if not f.startswith(ABBR.get(r.get("state"), ("", "??"))[1]) or len(f) != 5:
                    w(f"result county {f} isn't in {r.get('state')}")
                if not all(p in PARTIES and 0 <= v <= 100 for p, v in sh.items()):
                    w(f"result county {f}: shares must be party letter -> 0-100")
    for st, sp in db["spotlight"].items():
        ev = sp.get("early_vote")
        if ev:
            if not ev.get("source") or not date(ev.get("as_of")) or not isinstance(ev.get("total"), int) or ev["total"] < 0:
                errs.append(f"spotlight {st}: early_vote needs source, as_of and a whole-number total")
            if ev.get("share") is not None and not 0 <= ev["share"] <= 1:
                errs.append(f"spotlight {st}: early_vote share must be 0-1 (share of registered voters)")
            for f, v in (ev.get("counties") or {}).items():
                if not f.startswith(ABBR.get(st, ("", "??"))[1]) or not isinstance(v, int) or v < 0:
                    errs.append(f"spotlight {st}: early_vote county {f} must be in the state, with a whole number")
        for wv in sp.get("watch") or []:
            if not str(wv.get("fips", "")).startswith(ABBR.get(st, ("", "??"))[1]) or not wv.get("why"):
                errs.append(f"spotlight {st}: watch entries need fips (in the state) and why")
        if sp.get("headline") is not None and len(sp["headline"]) > 220:
            errs.append(f"spotlight {st}: headline must be at most 220 characters")
    return errs


def patch(db, p):
    allowed = {"races", "spotlight", "add_spotlight", "remove_spotlight"}
    bad = set(p) - allowed
    if bad:
        return [f"unknown patch keys: {', '.join(sorted(bad))} (allowed: {', '.join(sorted(allowed))})"]
    errs = []
    for st in p.get("add_spotlight", []):
        if st not in ABBR:
            errs.append(f"add_spotlight: unknown state {st}")
        else:
            db["spotlight"].setdefault(st, {})
    for st in p.get("remove_spotlight", []):
        db["spotlight"].pop(st, None)
    if FILE24.exists() and p.get("add_spotlight"):
        e24 = json.loads(FILE24.read_text(encoding="utf-8"))
        for st in p["add_spotlight"]:
            if st in db["spotlight"]:
                db["spotlight"][st]["auto_watch"] = auto_watch(st, e24)
    for rid, ch in (p.get("races") or {}).items():
        if rid not in db["races"]:
            errs.append(f"races: unknown race id {rid} (ids look like TX-SEN, MI-GOV, CA-22, AK-AL)")
            continue
        bad = set(ch) - RUN_FIELDS
        if bad:
            errs.append(f"{rid}: only these fields can be set: {', '.join(sorted(RUN_FIELDS))} (not {', '.join(sorted(bad))})")
            continue
        for k, v in ch.items():
            if v is None:
                db["races"][rid].pop(k, None)
            else:
                db["races"][rid][k] = v
    for st, ch in (p.get("spotlight") or {}).items():
        if st not in db["spotlight"]:
            errs.append(f"spotlight: {st} isn't a spotlight state (add it with add_spotlight)")
            continue
        bad = set(ch) - {"headline", "early_vote", "watch"}
        if bad:
            errs.append(f"spotlight {st}: only headline, early_vote and watch can be set")
            continue
        for k, v in ch.items():
            if v is None:
                db["spotlight"][st].pop(k, None)
            else:
                db["spotlight"][st][k] = v
    return errs + validate(db)


def status(db):
    today = central_today()
    days = (dt.date.fromisoformat(ELECTION_DAY) - today).days
    oracle = {p["id"]: p for p in json.loads((DATA / "oracle.json").read_text(encoding="utf-8")).get("predictions", [])}
    R = db["races"]
    print(f"# Elections · Election Day {ELECTION_DAY} ({days} days) · ratings refreshed {db['meta'].get('refreshed')}")
    sp = list(db["spotlight"])
    print("Spotlight states: " + ", ".join(sp))
    close = sorted((rid for rid, r in R.items() if r["office"] != "house" and r.get("score") is not None and abs(r["score"]) < 1.5),
                   key=lambda rid: (R[rid]["state"] not in sp, abs(R[rid]["score"])))

    def line(rid):
        r = R[rid]
        c = " v ".join(f"{x['name']} ({x['party']})" for x in r["candidates"][:2])
        p = oracle.get(r.get("prediction"))
        pol, mk = r.get("polls"), r.get("market")
        return (f"- {rid}{'*' if r['state'] in sp else ''} {r['rating']} ({r['score']:+.1f}) {c}"
                f" · ORACLE {p['id'] + ' ' + str(round(p['oracle'] * 100)) + '% ' + r.get('yes_party', 'D') if p else 'none'}"
                f" · polls {'/'.join(f'{k}{v}' for k, v in pol.items() if k not in ('source', 'as_of')) + ' (' + pol['as_of'][5:] + ')' if pol else '-'}"
                f" · market {'/'.join(f'{k}{round(v * 100)}' for k, v in mk.items() if k not in ('source', 'as_of')) + ' (' + mk['as_of'][5:] + ')' if mk else '-'}")
    print(f"\n## Close Senate and governor races ({len(close)}; * = spotlight; score: − leans D, + leans R)")
    for rid in close:
        print(line(rid))
    need = [rid for rid in close if not R[rid].get("prediction")]
    print(f"\n## Close races with no ORACLE forecast yet ({len(need)}). Create up to 8 per run, in this order: "
          + (", ".join(need) or "none"))
    house_close = sorted((rid for rid, r in R.items() if r["office"] == "house" and r.get("score") is not None and abs(r["score"]) < 0.5),
                         key=lambda rid: abs(R[rid]["score"]))
    print(f"House toss-ups ({len(house_close)}): " + ", ".join(house_close))
    print("\n## Spotlight extras")
    for st in sp:
        s = db["spotlight"][st]
        ev = s.get("early_vote")
        print(f"- {st}: headline {'set' if s.get('headline') else 'MISSING'} · early vote "
              f"{(str(ev['total']) + ' as of ' + ev['as_of']) if ev else 'none'} · watch list "
              f"{'custom' if s.get('watch') else 'automatic (2024 result)'}")
    res = [rid for rid, r in R.items() if r.get("result")]
    print(f"\n## Results recorded: {len(res)} races" + (" (record winners from the Nov 4 run on)" if not res else ""))
    ch = [c for c in db.get("changes", []) if (today - dt.date.fromisoformat(c["date"])).days <= 7]
    if ch:
        print("## Rating changes in the last 7 days: " + "; ".join(f"{c['race']} {c['from']} -> {c['to']} ({c['date'][5:]})" for c in ch))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["refresh", "status", "patch", "check"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    db = load()
    if a.cmd == "refresh":
        refresh(db)
        errs = validate(db)
        if errs:
            print("ERROR (not saved): " + "; ".join(errs[:10]))
            sys.exit(1)
        save(db)
        print("OK: data/elections.json saved.")
    elif a.cmd == "status":
        status(db)
    elif a.cmd == "check":
        errs = validate(db)
        print("OK" if not errs else "ERRORS:\n- " + "\n- ".join(errs))
        sys.exit(1 if errs else 0)
    else:
        if not a.file:
            sys.exit("usage: python3 tools/elections.py patch FILE [--dry-run]")
        try:
            p = json.loads(Path(a.file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as ex:
            print(f"ERROR (nothing was changed): can't read {a.file}: {ex}")
            sys.exit(1)
        work = copy.deepcopy(db)
        errs = patch(work, p)
        if errs:
            print("ERROR (nothing was changed):\n- " + "\n- ".join(errs))
            sys.exit(1)
        if a.dry_run:
            print("OK (dry run): the patch is valid; nothing was saved.")
            return
        save(work)
        print(f"OK: applied {len(p.get('races') or {})} race updates and {len(p.get('spotlight') or {})} spotlight updates.")


if __name__ == "__main__":
    main()
