"""ORACLE update: apply the daily run's judgments from a small patch file (lean mode).

The daily run never opens or hand-edits data/oracle.json or data/events.json. It writes its judgments
into a small JSON patch (in .work/, which git ignores) and runs this tool, which checks everything first
and changes nothing if any part is invalid. That keeps runs cheap (no re-reading big files) and makes
broken data much less likely.

Usage:  python3 tools/update.py .work/patch.json [--dry-run]

Patch format (every key optional; ids are assigned by the tool for new items):
{
  "resolve":   [{"id": "P-0010", "outcome": true, "note": "BLS: PPI final demand +1.3% m/m"}],
  "void":      [{"id": "P-0012", "note": "why it can't be resolved fairly"}],
  "forecasts": [{"id": "P-0003", "clio": 0.45, "omen": 0.60, "why_clio": "...", "why_omen": "...",
                 "market": 0.55, "market_source": "Polymarket, Oct 17", "adjustment": 0.0, "adjustment_reason": "",
                 "track": {"series": "SP500", "op": ">", "value": 7800, "proxy": false}}],
  "new_predictions": [{"domain": "economy", "horizon": "1w", "question": "...", "resolves_on": "2026-10-24",
                 "resolution_source": "...", "clio": 0.4, "omen": 0.5, "why_clio": "...", "why_omen": "...",
                 "market": null, "market_source": null, "track": null}],
  "systems":   [{"id": "S-HOUSE", "pressure": 7.7, "friction": 4.3, "pressure_drivers": ["..."], "friction_drivers": ["..."]}],
  "new_systems": [{"id": "S-NAME", "name": "...", "domain": "politics", "status_quo": "...", "change": "...",
                 "pressure": 5, "friction": 6, "pressure_drivers": ["..."], "friction_drivers": ["..."],
                 "window": "2026-12-01", "tip_prediction": "P-0030", "tip_means": "yes", "agent_links": []}],
  "retire_systems": [{"id": "S-NAME", "note": "why"}],
  "catalysts": [{"id": "C-001", "status": "resolved", "outcome_branch": "Hot (≥0.5%)"}],
  "new_catalysts": [{"date": "2026-10-30", "window_end": null, "name": "...", "domain": "economy", "kind": "data",
                 "linked_predictions": ["P-0011"], "branches": [{"label": "...", "prob": 0.6, "narrative": "...",
                 "effects": {"systems": {"S-GROWTH": {"pressure": 0.3}}, "agents": {"A-SWING": {"A": 2}}}}]}],
  "events":    [{"date": "2026-10-16", "domain": "economy", "title": "...", "sources": ["AP", "BLS"], "url": "...",
                 "omen": {"chaos": 3, "polarization": 2, "economic": 6},
                 "clio": {"precedent": "...", "precedent_match": 5, "resistance": 6}, "systems": {"S-INFLATION": 2}}],
  "lessons":   [{"text": "..."}]
}
"""
import argparse
import copy
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from oracle_time import today as central_today  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOMAINS = {"politics", "economy", "unrest", "foreign"}
HORIZONS = {"1d", "1w", "1m", "3m", "6m+"}
KINDS = {"data", "decision", "election", "ruling", "deadline", "event", "market"}
OPS = {">", ">=", "<", "<="}
AGENT_VARS = {"A", "M", "T"}


class Bad(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise Bad(msg)


def prob(x, what):
    need(isinstance(x, (int, float)) and not isinstance(x, bool) and 0.0 <= x <= 1.0, f"{what} must be a number from 0 to 1 (got {x!r})")
    return round(float(x), 3)


def gauge(x, what):
    need(isinstance(x, (int, float)) and not isinstance(x, bool) and 0.0 <= x <= 10.0, f"{what} must be a number from 0 to 10 (got {x!r})")
    return round(float(x), 2)


def score(x, what):
    need(isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= 10, f"{what} must be a whole number 1-10 (got {x!r})")
    return x


def date_str(x, what):
    try:
        dt.date.fromisoformat(x)
    except Exception:
        raise Bad(f"{what} must be a date like 2026-10-24 (got {x!r})")
    return x


def text(x, what, allow_empty=False):
    need(isinstance(x, str) and (allow_empty or x.strip()), f"{what} must be non-empty text")
    return x.strip()


def strings(x, what):
    need(isinstance(x, list) and all(isinstance(s, str) and s.strip() for s in x), f"{what} must be a list of text items")
    return [s.strip() for s in x]


def track(x, what):
    if x is None:
        return None
    need(isinstance(x, dict) and isinstance(x.get("series"), str) and x.get("op") in OPS
         and isinstance(x.get("value"), (int, float)), f"{what} must look like {{series, op (> >= < <=), value, proxy}}")
    return {"series": x["series"], "op": x["op"], "value": x["value"], "proxy": bool(x.get("proxy", False))}


def norm_title(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def check_branches(branches, sys_ids, what):
    need(isinstance(branches, list), f"{what}.branches must be a list")
    if not branches:
        return []
    out = []
    for i, b in enumerate(branches):
        bw = f"{what}.branches[{i}]"
        need(isinstance(b, dict), f"{bw} must be an object")
        eff = b.get("effects") or {}
        for sid, dd in (eff.get("systems") or {}).items():
            need(sid in sys_ids, f"{bw} effects mention unknown system {sid}")
            need(isinstance(dd, dict) and set(dd) <= {"pressure", "friction"}, f"{bw} system effects use only pressure/friction")
        for aid, dd in (eff.get("agents") or {}).items():
            need(isinstance(dd, dict) and set(dd) <= AGENT_VARS, f"{bw} agent effects use only A/M/T")
        out.append({"label": text(b.get("label"), f"{bw}.label"), "prob": prob(b.get("prob"), f"{bw}.prob"),
                    "narrative": text(b.get("narrative", ""), f"{bw}.narrative", allow_empty=True),
                    "effects": {"systems": eff.get("systems") or {}, "agents": eff.get("agents") or {}}})
    tot = sum(b["prob"] for b in out)
    need(abs(tot - 1.0) <= 0.02, f"{what} branch probabilities sum to {tot:.2f}, not 1")
    return out


def apply(db, evdoc, patch, today):
    """Validate the whole patch against a copy; raise Bad on the first problem. Returns (db, evdoc, log)."""
    db, evdoc = copy.deepcopy(db), copy.deepcopy(evdoc)
    tstr = today.isoformat()
    log, warn = [], []
    known = {"resolve", "void", "forecasts", "new_predictions", "systems", "new_systems", "retire_systems",
             "catalysts", "new_catalysts", "events", "lessons"}
    extra = set(patch) - known
    need(not extra, f"unknown patch keys: {', '.join(sorted(extra))}")
    preds = {p["id"]: p for p in db["predictions"]}
    systems = {s["id"]: s for s in db["systems"]}
    cats = {c["id"]: c for c in db["catalysts"]}
    events = evdoc["events"]

    def open_pred(pid, what):
        need(pid in preds, f"{what}: unknown prediction {pid}")
        need(preds[pid]["status"] == "open", f"{what}: {pid} is already {preds[pid]['status']}; past forecasts are never changed")
        return preds[pid]

    # 1. resolutions and voids
    for i, r in enumerate(patch.get("resolve", [])):
        p = open_pred(r.get("id"), f"resolve[{i}]")
        need(isinstance(r.get("outcome"), bool), f"resolve[{i}].outcome must be true or false")
        p.update(status="resolved", outcome=r["outcome"], resolved_on=tstr, resolution_note=text(r.get("note"), f"resolve[{i}].note"))
        log.append(f"resolved {p['id']} -> {'YES' if r['outcome'] else 'NO'}")
    for i, r in enumerate(patch.get("void", [])):
        p = open_pred(r.get("id"), f"void[{i}]")
        p.update(status="void", resolved_on=tstr, resolution_note=text(r.get("note"), f"void[{i}].note"))
        log.append(f"voided {p['id']}")

    # 2. new predictions (before systems/catalysts so they can reference the new ids)
    for i, n in enumerate(patch.get("new_predictions", [])):
        w = f"new_predictions[{i}]"
        need(n.get("domain") in DOMAINS, f"{w}.domain must be one of {sorted(DOMAINS)}")
        need(n.get("horizon") in HORIZONS, f"{w}.horizon must be one of {sorted(HORIZONS)}")
        q = text(n.get("question"), f"{w}.question")
        need(all(norm_title(q) != norm_title(p["question"]) for p in preds.values() if p["status"] == "open"),
             f"{w} duplicates an open prediction: {q[:80]}")
        res = date_str(n.get("resolves_on"), f"{w}.resolves_on")
        need(res >= tstr, f"{w}.resolves_on is in the past")
        pid = f"P-{db['meta']['next_id']:04d}"
        db["meta"]["next_id"] += 1
        adj = n.get("adjustment", 0.0) or 0.0
        need(isinstance(adj, (int, float)) and abs(adj) <= 0.10, f"{w}.adjustment must be within ±0.10")
        p = {"id": pid, "created": tstr, "domain": n["domain"], "horizon": n["horizon"], "question": q,
             "resolves_on": res, "resolution_source": text(n.get("resolution_source"), f"{w}.resolution_source"),
             "clio": prob(n.get("clio"), f"{w}.clio"), "omen": prob(n.get("omen"), f"{w}.omen"),
             "adjustment": {"value": round(float(adj), 3), "reason": n.get("adjustment_reason", "") or ""},
             "market": None if n.get("market") is None else prob(n["market"], f"{w}.market"),
             "market_source": n.get("market_source"),
             "reasoning": {"clio": text(n.get("why_clio"), f"{w}.why_clio"), "omen": text(n.get("why_omen"), f"{w}.why_omen")},
             "status": "open", "outcome": None, "resolved_on": None, "resolution_note": None,
             "brier": {"oracle": None, "clio": None, "omen": None, "market": None, "sim": None}, "history": [],
             "sim": None, "oracle": None}
        t = track(n.get("track"), f"{w}.track")
        if t:
            p["track"] = t
        db["predictions"].append(p)
        preds[pid] = p
        log.append(f"new prediction {pid}: {q[:70]}")

    # 3. forecast updates
    for i, f in enumerate(patch.get("forecasts", [])):
        w = f"forecasts[{i}]"
        p = open_pred(f.get("id"), w)
        for k in ("clio", "omen"):
            if k in f:
                p[k] = prob(f[k], f"{w}.{k}")
        r = p.setdefault("reasoning", {"clio": "", "omen": ""})
        if "why_clio" in f:
            r["clio"] = text(f["why_clio"], f"{w}.why_clio")
        if "why_omen" in f:
            r["omen"] = text(f["why_omen"], f"{w}.why_omen")
        if "market" in f:
            p["market"] = None if f["market"] is None else prob(f["market"], f"{w}.market")
        if "market_source" in f:
            p["market_source"] = f["market_source"]
        if "adjustment" in f:
            a = f["adjustment"] or 0.0
            need(isinstance(a, (int, float)) and abs(a) <= 0.10, f"{w}.adjustment must be within ±0.10")
            p["adjustment"] = {"value": round(float(a), 3), "reason": f.get("adjustment_reason", "") or ""}
            need(not p["adjustment"]["value"] or p["adjustment"]["reason"].strip(), f"{w}: a nonzero adjustment needs adjustment_reason")
        if "track" in f:
            t = track(f["track"], f"{w}.track")
            if t:
                p["track"] = t
            else:
                p.pop("track", None)
                p.pop("track_status", None)
        unknown = set(f) - {"id", "clio", "omen", "why_clio", "why_omen", "market", "market_source", "adjustment",
                            "adjustment_reason", "track"}
        need(not unknown, f"{w} has fields this tool doesn't change: {', '.join(sorted(unknown))} (resolution dates and questions are fixed once made)")
        log.append(f"updated {p['id']}")

    # 4. systems
    for i, s in enumerate(patch.get("new_systems", [])):
        w = f"new_systems[{i}]"
        sid = text(s.get("id"), f"{w}.id")
        need(re.fullmatch(r"S-[A-Z0-9-]+", sid) and sid not in systems, f"{w}.id must be new and look like S-NAME")
        need(s.get("domain") in DOMAINS, f"{w}.domain must be one of {sorted(DOMAINS)}")
        tp = s.get("tip_prediction")
        need(tp in preds and preds[tp]["status"] == "open", f"{w}.tip_prediction must be an open prediction id")
        links = s.get("agent_links", [])
        need(isinstance(links, list) and all(isinstance(l, list) and len(l) == 3 and l[1] in ("A", "M") for l in links),
             f"{w}.agent_links must be a list of [agent, 'A' or 'M', weight]")
        new = {"id": sid, "name": text(s.get("name"), f"{w}.name"), "domain": s["domain"],
               "status_quo": text(s.get("status_quo"), f"{w}.status_quo"), "change": text(s.get("change"), f"{w}.change"),
               "pressure": gauge(s.get("pressure"), f"{w}.pressure"), "friction": gauge(s.get("friction"), f"{w}.friction"),
               "pressure_drivers": strings(s.get("pressure_drivers"), f"{w}.pressure_drivers"),
               "friction_drivers": strings(s.get("friction_drivers"), f"{w}.friction_drivers"),
               "window": date_str(s.get("window"), f"{w}.window"), "tip_prediction": tp,
               "tip_means": s.get("tip_means", "yes"), "agent_links": links, "history": []}
        need(new["tip_means"] in ("yes", "no"), f"{w}.tip_means must be yes or no")
        db["systems"].append(new)
        systems[sid] = new
        log.append(f"new system {sid}")
    for i, s in enumerate(patch.get("systems", [])):
        w = f"systems[{i}]"
        need(s.get("id") in systems, f"{w}: unknown system {s.get('id')}")
        x = systems[s["id"]]
        for k in ("pressure", "friction"):
            if k in s:
                new_v = gauge(s[k], f"{w}.{k}")
                if abs(new_v - x[k]) > 1.0:
                    warn.append(f"{x['id']} {k} moves {x[k]} -> {new_v} (more than 1.0 in a day): say why in the briefing")
                x[k] = new_v
        for k in ("pressure_drivers", "friction_drivers"):
            if k in s:
                x[k] = strings(s[k], f"{w}.{k}")
        unknown = set(s) - {"id", "pressure", "friction", "pressure_drivers", "friction_drivers"}
        need(not unknown, f"{w} has fields this tool doesn't change: {', '.join(sorted(unknown))}")
        log.append(f"updated system {x['id']}")
    for i, s in enumerate(patch.get("retire_systems", [])):
        need(s.get("id") in systems, f"retire_systems[{i}]: unknown system {s.get('id')}")
        systems[s["id"]]["retired"] = True
        systems[s["id"]]["retired_note"] = text(s.get("note"), f"retire_systems[{i}].note")
        log.append(f"retired system {s['id']}")

    # 5. catalysts
    sys_ids = set(systems)
    for i, c in enumerate(patch.get("catalysts", [])):
        w = f"catalysts[{i}]"
        need(c.get("id") in cats, f"{w}: unknown catalyst {c.get('id')}")
        x = cats[c["id"]]
        if "branches" in c:
            x["branches"] = check_branches(c["branches"], sys_ids, w)
        for k in ("date", "window_end"):
            if k in c:
                x[k] = None if (k == "window_end" and c[k] is None) else date_str(c[k], f"{w}.{k}")
        if "name" in c:
            x["name"] = text(c["name"], f"{w}.name")
        if "linked_predictions" in c:
            need(all(pid in preds for pid in c["linked_predictions"]), f"{w}.linked_predictions has an unknown id")
            x["linked_predictions"] = c["linked_predictions"]
        if "status" in c:
            need(c["status"] in ("upcoming", "resolved"), f"{w}.status must be upcoming or resolved")
            x["status"] = c["status"]
        if "outcome_branch" in c:
            labels = [b["label"] for b in x.get("branches", [])]
            need(c["outcome_branch"] in labels, f"{w}.outcome_branch must be one of: {labels}")
            x["outcome_branch"] = c["outcome_branch"]
            x["status"] = "resolved"
        unknown = set(c) - {"id", "branches", "date", "window_end", "name", "linked_predictions", "status", "outcome_branch"}
        need(not unknown, f"{w} has unknown fields: {', '.join(sorted(unknown))}")
        log.append(f"updated catalyst {x['id']}")
    nums = [int(c["id"].split("-")[1]) for c in cats.values() if c["id"].split("-")[1].isdigit()]
    nxt = max(nums + [0]) + 1
    for i, c in enumerate(patch.get("new_catalysts", [])):
        w = f"new_catalysts[{i}]"
        need(c.get("domain") in DOMAINS, f"{w}.domain must be one of {sorted(DOMAINS)}")
        need(c.get("kind") in KINDS, f"{w}.kind must be one of {sorted(KINDS)}")
        lp = c.get("linked_predictions", [])
        need(all(pid in preds for pid in lp), f"{w}.linked_predictions has an unknown id")
        cid = f"C-{nxt:03d}"
        nxt += 1
        new = {"id": cid, "date": date_str(c.get("date"), f"{w}.date"),
               "window_end": None if c.get("window_end") is None else date_str(c["window_end"], f"{w}.window_end"),
               "name": text(c.get("name"), f"{w}.name"), "domain": c["domain"], "kind": c["kind"],
               "linked_predictions": lp, "status": "upcoming", "outcome_branch": None,
               "branches": check_branches(c.get("branches", []), sys_ids, w)}
        db["catalysts"].append(new)
        cats[cid] = new
        log.append(f"new catalyst {cid}: {new['name'][:60]}")

    # 6. events
    seen_titles = {(norm_title(e["title"]), e["date"]) for e in events}
    for i, e in enumerate(patch.get("events", [])):
        w = f"events[{i}]"
        edate = date_str(e.get("date"), f"{w}.date")
        need(edate <= tstr, f"{w}.date is in the future")
        need(e.get("domain") in DOMAINS, f"{w}.domain must be one of {sorted(DOMAINS)}")
        title = text(e.get("title"), f"{w}.title")
        if (norm_title(title), edate) in seen_titles:
            warn.append(f"skipped duplicate event: {title[:70]}")
            continue
        o, c = e.get("omen") or {}, e.get("clio") or {}
        om = {k: score(o.get(k), f"{w}.omen.{k}") for k in ("chaos", "polarization", "economic")}
        cl = {"precedent": text(c.get("precedent"), f"{w}.clio.precedent"),
              "precedent_match": score(c.get("precedent_match"), f"{w}.clio.precedent_match"),
              "resistance": score(c.get("resistance"), f"{w}.clio.resistance")}
        srcs = strings(e.get("sources", []), f"{w}.sources")
        need(om["chaos"] < 7 or len(srcs) >= 2, f"{w}: chaos {om['chaos']} needs at least two independent sources (rubric)")
        pushes = e.get("systems") or {}
        need(isinstance(pushes, dict) and all(k in systems and isinstance(v, int) and not isinstance(v, bool) and -3 <= v <= 3
                                              for k, v in pushes.items()),
             f"{w}.systems must map known system ids to whole numbers -3..3")
        pref = f"E-{edate.replace('-', '')}-"
        nn = max([int(x["id"].rsplit("-", 1)[1]) for x in events if x["id"].startswith(pref)] + [0]) + 1
        new = {"id": f"{pref}{nn:02d}", "date": edate, "domain": e["domain"], "title": title, "sources": srcs,
               "url": e.get("url", ""), "omen": om, "clio": cl, "systems": pushes}
        events.append(new)
        seen_titles.add((norm_title(title), edate))
        log.append(f"new event {new['id']}: {title[:60]}")

    # 7. lessons
    for i, l in enumerate(patch.get("lessons", [])):
        db["lessons"].append({"date": tstr, "source": "run", "text": text(l.get("text"), f"lessons[{i}].text")})
        log.append("new lesson")
    if len(db["lessons"]) > 25:
        warn.append(f"{len(db['lessons'])} lessons: merge or retire old ones (ORACLE.md section 9 keeps about 25)")

    # run bookkeeping: one run per Central-time day
    if db["meta"].get("last_run") != tstr:
        db["meta"]["run_count"] = db["meta"].get("run_count", 0) + 1
        db["meta"]["last_run"] = tstr
    return db, evdoc, log, warn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("patch")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", default=None, help="override today's date (testing only)")
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.date) if args.date else central_today()
    try:
        patch = json.loads(Path(args.patch).read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"ERROR: can't read the patch file: {ex}")
        sys.exit(1)
    db = json.loads((DATA / "oracle.json").read_text(encoding="utf-8"))
    evdoc = json.loads((DATA / "events.json").read_text(encoding="utf-8"))
    try:
        db2, ev2, log, warn = apply(db, evdoc, patch, today)
    except Bad as ex:
        print(f"ERROR (nothing was changed): {ex}")
        sys.exit(1)
    if not args.dry_run:
        (DATA / "oracle.json").write_text(json.dumps(db2, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        (DATA / "events.json").write_text(json.dumps(ev2, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(("DRY RUN, nothing saved. " if args.dry_run else "OK: ") + f"{len(log)} changes applied for {today}.")
    for line in log:
        print("  " + line)
    for wline in warn:
        print("WARNING: " + wline)


if __name__ == "__main__":
    main()
