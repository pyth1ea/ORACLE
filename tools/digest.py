"""ORACLE digest: a compact, one-page view of ORACLE's state for the daily run (lean mode).

The daily run reads this instead of opening data/oracle.json (about 30k tokens and growing) and the other
data files. Everything a run needs to make its judgments is here; details of any single item are one
--show away.

Usage:
  python3 tools/digest.py                     the daily digest
  python3 tools/digest.py --show P-0003 S-HOUSE C-005    full detail for specific items
  python3 tools/digest.py --week              extra material for the Sunday deep review
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from oracle_time import today as central_today  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOM = {"politics": "pol", "economy": "eco", "unrest": "unr", "foreign": "for"}


def load(name, default=None):
    p = DATA / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def pc(x):
    return "--" if x is None else f"{round(x * 100):d}"


def cut(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def d(s):
    return dt.date.fromisoformat(s)


def next_ids(db, events, today):
    t = today.strftime("%Y%m%d")
    nn = max([int(e["id"].rsplit("-", 1)[1]) for e in events if e["id"].startswith(f"E-{t}-")] + [0]) + 1
    cn = max([int(c["id"].split("-")[1]) for c in db.get("catalysts", []) if c["id"].split("-")[1].isdigit()] + [0]) + 1
    return f"P-{db['meta']['next_id']:04d}", f"E-{t}-{nn:02d}", f"C-{cn:03d}"


def show(db, events, ids):
    pool = {x["id"]: x for x in db.get("predictions", []) + db.get("systems", []) + db.get("catalysts", []) + events}
    for i in ids:
        x = pool.get(i)
        if not x:
            print(f"{i}: not found")
            continue
        x = dict(x)
        if "history" in x:
            x["history"] = x["history"][-3:]
        x.pop("sim_conditional", None)
        print(json.dumps(x, ensure_ascii=False, indent=1))


def week(db, events, today):
    since = today - dt.timedelta(days=7)
    print(f"## Week in review material (since {since})")
    res = [p for p in db["predictions"] if p.get("status") == "resolved" and p.get("resolved_on") and d(p["resolved_on"]) >= since]
    print(f"### Resolved this week ({len(res)})")
    for p in res:
        b = p.get("brier") or {}
        print(f"- {p['id']} {'YES' if p['outcome'] else 'NO'} | ORACLE {pc(p['oracle'])} CLIO {pc(p['clio'])} OMEN {pc(p['omen'])}"
              f" | Brier O {b.get('oracle')} C {b.get('clio')} M {b.get('omen')} | {cut(p['question'], 90)}")
        r = p.get("reasoning") or {}
        print(f"    CLIO said: {cut(r.get('clio'), 160)}\n    OMEN said: {cut(r.get('omen'), 160)}")
    log = db.get("score_log", [])[-8:]
    print("### Score log (last 8 runs): " + " · ".join(f"{r['date'][5:]} O{r.get('brier_oracle')} C{r.get('brier_clio')} M{r.get('brier_omen')}" for r in log))
    print("### Weight changes: " + (" · ".join(f"{w['date'][5:]} {w['domain']} clio {w['clio']}" for w in db.get("weights_history", []) if d(w["date"]) >= since) or "none"))
    print("### Calibration buckets: " + " · ".join(f"{b['range']} n{b['n']} said {pc(b['avg_forecast'])} happened {pc(b['happened'])}" for b in (db.get("stats") or {}).get("calibration", [])))
    print("### This week's scored events (check scoring consistency against the section 3 anchors)")
    for e in sorted((e for e in events if d(e["date"]) >= since), key=lambda e: e["id"]):
        o, c = e["omen"], e["clio"]
        print(f"- {e['id']} {DOM.get(e['domain'], e['domain'])} c{o['chaos']} p{o['polarization']} e{o['economic']} r{c['resistance']} m{c['precedent_match']} | {cut(e['title'], 100)}")
    sw = [r for r in db.get("shock_watch", []) if r.get("outcome") is not None][-7:]
    if sw:
        print("### Shock watch graded: " + " · ".join(f"{r['date'][5:]} said {pc(r['p'])} -> {'shock' if r['outcome'] else 'none'}" for r in sw))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", nargs="+")
    ap.add_argument("--week", action="store_true")
    args = ap.parse_args()
    db = load("oracle.json")
    events = (load("events.json", {"events": []}) or {}).get("events", [])
    today = central_today()
    if args.show:
        show(db, events, args.show)
        return

    m = db["meta"]
    nid_p, nid_e, nid_c = next_ids(db, events, today)
    print(f"# ORACLE digest: run date {today} (Central) · last run {m['last_run']} (#{m['run_count']}) · "
          f"next new ids: {nid_p}, {nid_e}, {nid_c}")

    preds = db.get("predictions", [])
    opn = [p for p in preds if p.get("status") == "open"]
    due = [p for p in opn if d(p["resolves_on"]) < today]
    cats = db.get("catalysts", [])
    cat_due = [c for c in cats if c.get("status", "upcoming") == "upcoming" and d(c.get("window_end") or c["date"]) < today]
    print("\n## Do first")
    if not due and not cat_due:
        print("- nothing is past due")
    for p in due:
        print(f"- RESOLVE {p['id']} (due {p['resolves_on']}): {cut(p['question'], 120)} | check: {cut(p['resolution_source'], 140)}")
    for c in cat_due:
        print(f"- CATALYST OUTCOME {c['id']} {c['date']} {c['name']}: branches " + " | ".join(b["label"] for b in c.get("branches", [])))

    print("\n## Lessons (apply these)")
    for l in db.get("lessons", []):
        print(f"- {cut(l['text'], 300)}")

    w = db.get("weights", {})
    cal = db.get("calibration", {})
    print("\n## Engine weights (CLIO/OMEN): " + " · ".join(f"{k} {v['clio']:.2f}/{v['omen']:.2f}" for k, v in w.items())
          + f" | calibration {'ON' if cal.get('active') else 'off'} ({cal.get('n', 0)}/{db.get('config', {}).get('calib_min', 40)} results)")
    st = (db.get("stats") or {}).get("all", {})
    print(f"Record so far: {st.get('n', 0)} graded, {st.get('hits', 0)} right / {st.get('misses', 0)} wrong, Brier ORACLE {st.get('brier_oracle')}")

    v = db.get("volatility")
    if v:
        hs = db.get("history_scale") or {}
        names = {e["id"]: f"{e['name']} ({e['when']}, {e['V']:.2f})" for e in hs.get("eras", [])}
        print(f"\n## Stability: V {v['V']:.2f} {v['path']} · O {v['O']} C {v['C']} Pm {v['Pm']} · shock watch {pc((v.get('shock') or {}).get('p'))}%"
              f" · closest in level: {', '.join(names.get(i, i) for i in hs.get('closest_level', [])[:2])}"
              f" · in kind: {', '.join(names.get(i, i) for i in hs.get('closest_kind', [])[:2])}")

    print("\n## Systems (pressure vs friction -> status, gauge tip, sandbox, news momentum; scored by)")
    byid = {p["id"]: p for p in preds}
    for s in db.get("systems", []):
        if s.get("retired"):
            continue
        lp = byid.get(s.get("tip_prediction"))
        sim = (s.get("sim") or {}).get("tip")
        print(f"- {s['id']} {s['name']}: P {s['pressure']} / F {s['friction']} -> {s.get('status')} {pc(s.get('tip_probability'))}%"
              f" (sandbox {pc(sim)}, momentum {s.get('momentum', 0):+.1f}) · decides {s['window']} · "
              f"{s.get('tip_prediction')} {pc(lp['oracle']) if lp else '--'}%")
        print(f"    push: {cut('; '.join(s.get('pressure_drivers', [])), 230)}")
        print(f"    hold: {cut('; '.join(s.get('friction_drivers', [])), 230)}")

    print("\n## Upcoming catalysts (next 35 days)")
    for c in sorted(cats, key=lambda c: c["date"]):
        if c.get("status", "upcoming") != "upcoming":
            continue
        start, end = d(c["date"]), d(c.get("window_end") or c["date"])
        if end < today or (start - today).days > 35:
            continue
        br = " | ".join(f"{b['label']} {pc(b['prob'])}" for b in c.get("branches", [])) or "no branches"
        print(f"- {c['id']} {c['date']}{'..' + c['window_end'] if c.get('window_end') else ''} {c['name']} [{c.get('kind')}]: {br}"
              f" · links {','.join(c.get('linked_predictions', [])) or '-'}")

    print("\n## Open predictions: id | domain | horizon | resolves | CLIO OMEN -> ORACLE | market | sandbox | live track | question")
    for p in sorted(opn, key=lambda p: p["resolves_on"]):
        ts = p.get("track_status")
        tr = "-" if not ts else f"{'on' if ts['met'] else 'OFF'} ({ts['value']}{', proxy' if ts.get('proxy') else ''})"
        adj = (p.get("adjustment") or {}).get("value") or 0
        print(f"{p['id']} | {DOM.get(p['domain'], p['domain'])} | {p['horizon']} | {p['resolves_on'][5:]} | {pc(p['clio'])} {pc(p['omen'])} -> "
              f"{pc(p['oracle'])}{f' (adj {adj:+.2f})' if adj else ''} | mkt {pc(p.get('market'))} | sb {pc(p.get('sim'))} | {tr} | {cut(p['question'], 110)}")

    recent = sorted((e for e in events if (today - d(e["date"])).days <= 3), key=lambda e: e["id"])
    print(f"\n## Events already scored in the last 3 days ({len(recent)}). Don't add these again")
    for e in recent:
        o = e["omen"]
        print(f"- {e['id']} c{o['chaos']}/p{o['polarization']}/e{o['economic']} | {cut(e['title'], 110)}")

    rv = db.get("review") or {}
    if rv.get("big_moves"):
        print("\n## Biggest moves vs a week ago: " + " · ".join(f"{x['id']} {pc(x['then'])}->{pc(x['now'])}" for x in rv["big_moves"][:6]))

    mt = load("metrics.json", {}) or {}
    s = mt.get("series", {})
    key = [("SP500", "S&P"), ("DCOILBRENTEU", "Brent spot"), ("GASREGW", "gas"), ("DFEDTARU", "Fed top"), ("DGS10", "10y"),
           ("VIXCLS", "VIX"), ("UNRATE", "unemp")]
    line = [f"{lab} {s[k]['last']} ({s[k]['last_date'][5:]}{', STALE' if s[k].get('stale') else ''})" for k, lab in key if k in s and "last" in s[k]]
    line += [f"{k} {v['value']}" for k, v in (mt.get("derived") or {}).items()]
    print("\n## Key numbers: " + " · ".join(line))
    print("\n(Details of any item: python3 tools/digest.py --show <ID> ... · Apply judgments with tools/update.py, never by editing data/*.json.)")
    if args.week:
        print()
        week(db, events, today)


if __name__ == "__main__":
    main()
