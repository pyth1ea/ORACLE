"""ORACLE core: all of ORACLE's arithmetic, done the same way every day.

The daily run (Claude) supplies judgments: CLIO and OMEN probabilities, pressure and friction
readings, outcomes of resolved questions. This script does every calculation from them:

- Blends CLIO and OMEN into the ORACLE number using the per-domain engine weights
- Scores resolved predictions (Brier) and updates the engine weights with multiplicative weights:
  after each resolution, each engine's weight is multiplied by exp(-eta * its Brier score), so the
  engine that was closer gains a little say (about 1-3 points per question)
- Turns pressure (OMEN) vs friction (CLIO) into a tipping probability, a status and a projected
  crossing date for every system
- Checks tracked predictions against the latest real numbers (data/metrics.json)
- Builds the 7-day review, calibration table, hit/miss record and the score log

Usage:  python tools/core.py [--date YYYY-MM-DD] [--dry-run]
"""
import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

DEFAULT_CONFIG = {
    "eta": 0.5,            # learning rate for engine weights
    "w_min": 0.2, "w_max": 0.8,
    "k": 0.6,              # steepness of tipping curve: P(tip) = 1 / (1 + exp(-k * (pressure - friction)))
    "history_step": 0.03,  # record a new history point when ORACLE moves at least this much
    "clamp": [0.02, 0.98],
    "adj_max": 0.10,
    # Timeline Volatility Index: V = A / (A + B),  A = w_omen * O * P_m,  B = w_clio * C * I_s
    "inertia": 1.0,          # I_s: US institutional inertia (calibration constant for B)
    "vol_window": 14,        # days of scored events that count
    "vol_halflife": 5,       # recent events count more (weight halves every 5 days)
    "vol_min_events": 3,
    "pm_cap": 0.6,           # P_m can add at most +60% sensitivity
    # Alpha (steady) < 0.40 <= Gamma (tense but holding) < 0.60 <= Beta (breaking point) < 0.75 <= Omega (collapse).
    # Calibrated against the historical eras in data/history_anchors.json (see ORACLE.md section 8).
    "path_bands": [0.40, 0.60, 0.75],
    # Shock watch: P(major shock within 7 days) = sigmoid(a + b * V), re-fitted from results
    "shock_prior": [-4.0, 7.0],
    "shock_min_fit": 20,
    "shock_chaos": 7,        # a scored event with chaos >= this counts as a shock
    "shock_spx_move": 0.025, # so does an S&P 500 daily move of 2.5% or more
    # Automatic calibration of ORACLE numbers (Platt scaling), switched on after enough results
    "calib_min": 40,
    "calib_ridge": 15.0,     # strong pull toward "no correction" so a run of luck can't swing everything
    "calib_a_max": 0.5,      # limits on the correction: shift at most ±0.5 in log-odds...
    "calib_b_range": [0.6, 1.4],  # ...and stretch/shrink confidence by at most 40%
    "ridge": 3.0,
}

# How strongly an upcoming catalyst raises calendar sensitivity (P_m), by kind
CATALYST_WEIGHT = {"election": 0.5, "decision": 0.2, "ruling": 0.2, "deadline": 0.15, "event": 0.1,
                   "data": 0.05, "market": 0.0}


def load(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def d(s):
    return dt.date.fromisoformat(s)


def r2(x):
    """Round half up to 2 decimals (plain round() turns 0.475 into 0.47)."""
    return None if x is None else math.floor(x * 100 + 0.5 + 1e-9) / 100


def strain_label(margin):
    if margin < -2.0:
        return "holding"
    if margin < -0.5:
        return "strained"
    if margin <= 0.5:
        return "critical"
    return "tipping"


def compare(value, op, target):
    return {">": value > target, ">=": value >= target, "<": value < target, "<=": value <= target}[op]


def slope(points):
    """Least-squares slope of (day_index, value) points."""
    n = len(points)
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    den = sum((p[0] - mx) ** 2 for p in points)
    if den == 0:
        return 0.0
    return sum((p[0] - mx) * (p[1] - my) for p in points) / den


def logit(p):
    p = min(0.999, max(0.001, p))
    return math.log(p / (1 - p))


def intensity(e):
    """One number for how hard an event hits: half its worst OMEN score, half its average."""
    s = [e["omen"]["chaos"], e["omen"]["polarization"], e["omen"]["economic"]]
    return 0.5 * max(s) + 0.5 * sum(s) / 3


def fit_logistic(xs, ys, prior, ridge):
    """2-parameter logistic regression y ~ sigmoid(a + b*x), pulled toward `prior` (ridge), Newton steps."""
    a, b = prior
    for _ in range(50):
        ga = gb = 0.0
        haa = hab = hbb = 0.0
        for x, y in zip(xs, ys):
            p = sigmoid(a + b * x)
            ga += p - y
            gb += (p - y) * x
            w = p * (1 - p)
            haa += w
            hab += w * x
            hbb += w * x * x
        ga += ridge * (a - prior[0])
        gb += ridge * (b - prior[1])
        haa += ridge
        hbb += ridge
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if abs(da) < 1e-7 and abs(db) < 1e-7:
            break
    return a, b


def volatility(events, catalysts, today, wo, wc, cfg, domain=None):
    """Timeline Volatility Index for the whole country, or one domain."""
    num = res = den = 0.0
    n = 0
    for e in events:
        if domain and e["domain"] != domain:
            continue
        age = (today - d(e["date"])).days
        if age < 0 or age > cfg["vol_window"]:
            continue
        w = 0.5 ** (age / cfg["vol_halflife"])
        num += w * intensity(e)
        res += w * e["clio"]["resistance"]
        den += w
        n += 1
    if n < cfg["vol_min_events"]:
        return None
    O, C = num / den, res / den
    pm_sum, drivers = 0.0, []
    for c in catalysts:
        if c.get("status", "upcoming") != "upcoming" or (domain and c["domain"] != domain):
            continue
        start = (d(c["date"]) - today).days
        end = (d(c.get("window_end") or c["date"]) - today).days
        if end < 0 or start > 30:
            continue
        s0 = max(0, start)
        if end > s0:  # "any day within a window": use its average closeness, not its nearest day
            near = 10 / (end - s0) * (math.exp(-s0 / 10) - math.exp(-end / 10))
        else:
            near = math.exp(-s0 / 10)
        contrib = CATALYST_WEIGHT.get(c.get("kind"), 0.1) * near
        if contrib >= 0.02:
            drivers.append({"id": c["id"], "name": c["name"], "add": round(contrib, 3)})
        pm_sum += contrib
    Pm = 1 + min(cfg["pm_cap"], pm_sum)
    A = wo * O * Pm
    B = wc * C * cfg["inertia"]
    V = A / (A + B)
    b = cfg["path_bands"]
    path = "alpha" if V < b[0] else "gamma" if V < b[1] else "beta" if (len(b) < 3 or V < b[2]) else "omega"
    drivers.sort(key=lambda x: -x["add"])
    return {"V": round(V, 3), "path": path, "O": round(O, 2), "C": round(C, 2), "Pm": round(Pm, 3),
            "w_omen": round(wo, 3), "w_clio": round(wc, 3), "inertia": cfg["inertia"], "n_events": n,
            "calendar_drivers": drivers[:5]}


def profile(events, today, cfg):
    """Recency-weighted average of each score: the 'shape' of the turmoil (war-like, economic, political...)."""
    acc = {"chaos": 0.0, "polarization": 0.0, "economic": 0.0, "resistance": 0.0}
    den = 0.0
    for e in events:
        age = (today - d(e["date"])).days
        if age < 0 or age > cfg["vol_window"]:
            continue
        w = 0.5 ** (age / cfg["vol_halflife"])
        for k in ("chaos", "polarization", "economic"):
            acc[k] += w * e["omen"][k]
        acc["resistance"] += w * e["clio"]["resistance"]
        den += w
    return {k: round(v / den, 2) for k, v in acc.items()} if den else None


def history_scale(today, wo, wc, cfg, today_vol, today_profile):
    """Runs every historical era in data/history_anchors.json through the same volatility formula as today."""
    src = load(DATA / "history_anchors.json")
    if not src:
        return None
    eras = []
    for era in src["eras"]:
        evs = [{"date": (today - dt.timedelta(days=e["age"])).isoformat(), "domain": "politics",
                "omen": {k: e[k] for k in ("chaos", "polarization", "economic")},
                "clio": {"resistance": e["resistance"]}} for e in era["events"]]
        cal = [{"id": f"{era['id']}-c{i}", "name": c["kind"], "kind": c["kind"], "status": "upcoming", "domain": "politics",
                "date": (today + dt.timedelta(days=c["days"])).isoformat()} for i, c in enumerate(era.get("calendar", []))]
        v = volatility(evs, cal, today, wo, wc, cfg)
        if not v:
            continue
        eras.append({k: era[k] for k in ("id", "name", "when", "place", "featured", "like", "next")} |
                    {"V": v["V"], "path": v["path"], "O": v["O"], "C": v["C"], "Pm": v["Pm"],
                     "profile": profile(evs, today, cfg), "events": [e["title"] for e in era["events"]]})
    eras.sort(key=lambda x: x["V"])
    out = {"eras": eras}
    if today_vol and today_profile:
        V = today_vol["V"]
        out["closest_level"] = [e["id"] for e in sorted(eras, key=lambda e: abs(e["V"] - V))[:3]]

        def mix_sim(p):
            """How alike the MIX of turmoil is (war-like chaos vs. political division vs. economic damage),
            ignoring how intense it is: cosine similarity of each score's excess over a quiet baseline of 1."""
            a = [today_profile[k] - 1 for k in ("chaos", "polarization", "economic")]
            b = [p[k] - 1 for k in ("chaos", "polarization", "economic")]
            na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
            return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0
        # among eras at a broadly similar level, which had the most similar kind of turmoil?
        for width in (0.10, 0.15, 0.25, 1.0):
            pool = [e for e in eras if abs(e["V"] - V) <= width]
            if len(pool) >= 3:
                break
        out["closest_kind"] = [e["id"] for e in sorted(pool, key=lambda e: -mix_sim(e["profile"]))[:3]]
        for e in eras:
            e["mix_similarity"] = round(mix_sim(e["profile"]), 3)
        below = [e for e in eras if e["V"] <= V]
        above = [e for e in eras if e["V"] > V]
        out["between"] = [below[-1]["id"] if below else None, above[0]["id"] if above else None]
        out["today"] = {"V": V, "profile": today_profile}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    today = d(args.date) if args.date else dt.date.today()
    tstr = today.isoformat()

    db = load(DATA / "oracle.json")
    metrics = load(DATA / "metrics.json", {}) or {}
    sim = load(DATA / "sim.json", {}) or {}
    events = (load(DATA / "events.json", {"events": []}) or {}).get("events", [])
    cfg = {**DEFAULT_CONFIG, **db.get("config", {})}
    db["config"] = cfg
    lo, hi = cfg["clamp"]
    weights = db.setdefault("weights", {})
    whist = db.setdefault("weights_history", [])
    preds = db.get("predictions", [])
    notes, warnings = [], []

    # ---------- 1. score new resolutions, update engine weights ----------
    newly = [p for p in preds if p.get("status") == "resolved" and (p.get("brier") or {}).get("oracle") is None]
    newly.sort(key=lambda p: (p.get("resolved_on") or tstr, p["id"]))
    for p in newly:
        if p.get("outcome") is None:
            warnings.append(f"{p['id']} is marked resolved but has no outcome (true/false). Skipped.")
            continue
        o = 1.0 if p["outcome"] else 0.0
        p["brier"] = {k: (None if p.get(k) is None else round((p[k] - o) ** 2, 4))
                      for k in ("oracle", "clio", "omen", "market", "sim")}
        p["hit"] = None if p["oracle"] == 0.5 else ((p["oracle"] > 0.5) == bool(p["outcome"]))
        p["scored_on"] = tstr
        w = weights.setdefault(p["domain"], {"clio": 0.5, "omen": 0.5})
        wc = w["clio"] * math.exp(-cfg["eta"] * p["brier"]["clio"])
        wo = w["omen"] * math.exp(-cfg["eta"] * p["brier"]["omen"])
        new_c = min(cfg["w_max"], max(cfg["w_min"], wc / (wc + wo)))
        old_c = w["clio"]
        w["clio"], w["omen"] = round(new_c, 3), round(1 - new_c, 3)
        whist.append({"date": tstr, "domain": p["domain"], "prediction": p["id"],
                      "clio": w["clio"], "omen": w["omen"], "change_clio": round(w["clio"] - old_c, 3)})
        verdict = "HIT" if p["hit"] else ("MISS" if p["hit"] is False else "no call (50%)")
        notes.append(f"Scored {p['id']} -> {'YES' if p['outcome'] else 'NO'} ({verdict}). "
                     f"Brier ORACLE {p['brier']['oracle']}, CLIO {p['brier']['clio']}, OMEN {p['brier']['omen']}"
                     + (f", market {p['brier']['market']}" if p['brier']['market'] is not None else "")
                     + (f", sim {p['brier']['sim']}" if p['brier']['sim'] is not None else "")
                     + f". {p['domain']} weights now CLIO {w['clio']:.3f} / OMEN {w['omen']:.3f}.")

    # ---------- 1b. automatic calibration (Platt scaling), once there are enough results ----------
    # If ORACLE's 70%s keep happening 55% of the time, this learns the correction and applies it
    # to every new blend. The ridge pull toward "no correction" keeps it gentle with few results.
    done = [p for p in preds if p.get("status") == "resolved" and p.get("outcome") is not None]
    calib = {"active": False, "n": len(done), "a": 0.0, "b": 1.0}
    if len(done) >= cfg["calib_min"]:
        xs = [logit(p.get("blend", p["oracle"])) for p in done]
        ys = [1.0 if p["outcome"] else 0.0 for p in done]
        ca, cb = fit_logistic(xs, ys, (0.0, 1.0), cfg["calib_ridge"])
        ca = max(-cfg["calib_a_max"], min(cfg["calib_a_max"], ca))
        cb = max(cfg["calib_b_range"][0], min(cfg["calib_b_range"][1], cb))
        calib = {"active": True, "n": len(done), "a": round(ca, 4), "b": round(cb, 4), "fitted_on": tstr}
    db["calibration"] = calib

    # ---------- 2. blend open predictions; attach sim numbers and live tracking ----------
    sim_preds = (sim.get("predictions") or {}) if sim.get("date") else {}
    series = metrics.get("series", {})
    derived = metrics.get("derived", {})
    for p in preds:
        if p.get("status") != "open":
            continue
        w = weights.setdefault(p["domain"], {"clio": 0.5, "omen": 0.5})
        adj = (p.get("adjustment") or {}).get("value") or 0.0
        if abs(adj) > cfg["adj_max"]:
            warnings.append(f"{p['id']} judge adjustment {adj} exceeds ±{cfg['adj_max']}; capped.")
            adj = math.copysign(cfg["adj_max"], adj)
        blend = min(hi, max(lo, w["clio"] * p["clio"] + w["omen"] * p["omen"] + adj))
        p["blend"] = r2(blend)
        if calib["active"]:
            blend = sigmoid(calib["a"] + calib["b"] * logit(blend))
        val = r2(min(hi, max(lo, blend)))
        p["oracle"] = val
        if p["id"] in sim_preds:
            p["sim"] = r2(sim_preds[p["id"]])
        hist = p.setdefault("history", [])
        point = {"date": tstr, "clio": p["clio"], "omen": p["omen"], "oracle": val}
        if p.get("sim") is not None:
            point["sim"] = p["sim"]
        if not hist:
            hist.append(point)
        elif hist[-1]["date"] == tstr:
            hist[-1] = point
        elif abs(val - hist[-1]["oracle"]) >= cfg["history_step"] - 1e-9:
            hist.append(point)
        if p.get("resolves_on") and d(p["resolves_on"]) < today:
            notes.append(f"{p['id']} is past its resolution date ({p['resolves_on']}): resolve it if the answer is out.")
        tr = p.get("track")
        if tr:
            src = derived.get(tr["series"]) if tr["series"] in derived else series.get(tr["series"])
            if src and ("last" in src or "value" in src):
                v = src.get("last", src.get("value"))
                p["track_status"] = {"date": src.get("last_date", src.get("date")), "value": v,
                                     "met": compare(v, tr["op"], tr["value"]), "proxy": bool(tr.get("proxy"))}

    # ---------- 3. systems: pressure vs friction ----------
    sim_sys = (sim.get("systems") or {}) if sim.get("date") else {}
    by_id = {p["id"]: p for p in preds}
    for s in db.get("systems", []):
        P, F = float(s["pressure"]), float(s["friction"])
        margin = P - F
        s["margin"] = round(margin, 2)
        s["tip_probability"] = round(sigmoid(cfg["k"] * margin), 3)
        s["status"] = strain_label(margin)
        if s["id"] in sim_sys:
            s["sim"] = {k: sim_sys[s["id"]][k] for k in ("tip", "p10", "p50", "p90") if k in sim_sys[s["id"]]}
        lp = by_id.get(s.get("tip_prediction"))
        s["linked_oracle"] = None if not lp else (lp["oracle"] if s.get("tip_means", "yes") == "yes"
                                                  else round(1 - lp["oracle"], 2))
        hist = s.setdefault("history", [])
        point = {"date": tstr, "pressure": P, "friction": F}
        if not hist:
            hist.append(point)
        elif hist[-1]["date"] == tstr:
            hist[-1] = point
        elif abs(hist[-1]["pressure"] - P) >= 0.1 or abs(hist[-1]["friction"] - F) >= 0.1:
            hist.append(point)
        recent = [h for h in hist if (today - d(h["date"])).days <= 21]
        s["projection"] = None
        if len(recent) >= 2 and (d(recent[-1]["date"]) - d(recent[0]["date"])).days >= 3:
            pts = [((d(h["date"]) - d(recent[0]["date"])).days, h["pressure"] - h["friction"]) for h in recent]
            sl = slope(pts)
            s["trend_per_week"] = round(sl * 7, 2)
            if margin < 0 and sl > 0.005:
                days = -margin / sl
                if days <= 365:
                    s["projection"] = {"kind": "crossing", "date": (today + dt.timedelta(days=round(days))).isoformat(),
                                       "note": "pressure overtakes friction at the current trend"}
            elif margin > 0 and sl < -0.005:
                days = margin / -sl
                if days <= 365:
                    s["projection"] = {"kind": "restabilizing", "date": (today + dt.timedelta(days=round(days))).isoformat(),
                                       "note": "friction regains the upper hand at the current trend"}
        else:
            s["trend_per_week"] = None
        # news momentum: what the last 3 weeks of scored events say about this system
        mom = 0.0
        for e in events:
            imp = (e.get("systems") or {}).get(s["id"])
            age = (today - d(e["date"])).days
            if imp and 0 <= age <= 21:
                mom += imp * intensity(e) / 10 * 0.5 ** (age / 7)
        s["momentum"] = round(mom, 2)
        old = [h for h in hist if (today - d(h["date"])).days >= 5]
        if old:
            dm = margin - (old[-1]["pressure"] - old[-1]["friction"])
            if mom >= 1.0 and dm <= 0:
                notes.append(f"{s['id']}: news momentum is +{mom:.1f} toward change, but the pressure-friction gap "
                             f"hasn't widened in 5+ days. Raise pressure or lower friction, or explain why not.")
            elif mom <= -1.0 and dm >= 0:
                notes.append(f"{s['id']}: news momentum is {mom:.1f} toward the status quo, but the gap hasn't "
                             f"narrowed in 5+ days. Lower pressure or raise friction, or explain why not.")
        if s["linked_oracle"] is not None and abs(s["linked_oracle"] - s["tip_probability"]) > 0.15:
            notes.append(f"{s['id']}: pressure/friction gauge says {s['tip_probability']:.0%} but linked "
                         f"{s['tip_prediction']} says {s['linked_oracle']:.0%}. Re-check the gauge or explain the gap.")

    # ---------- 4. catalysts ----------
    sim_cat = (sim.get("catalysts") or {}) if sim.get("date") else {}
    for c in db.get("catalysts", []):
        if c["id"] in sim_cat:
            c["sim_conditional"] = sim_cat[c["id"]]
        end = c.get("window_end") or c["date"]
        if c.get("status", "upcoming") == "upcoming" and d(end) < today:
            notes.append(f"Catalyst {c['id']} ({c['name']}) has passed: record which branch happened "
                         f"(outcome_branch) and set status to 'resolved'.")
        if c.get("branches"):
            tot = sum(b["prob"] for b in c["branches"])
            if abs(tot - 1) > 0.02:
                warnings.append(f"Catalyst {c['id']} branch probabilities sum to {tot:.2f}, not 1.")

    # ---------- 4b. Timeline Volatility Index + shock watch ----------
    doms = ("politics", "economy", "unrest", "foreign")
    wo_g = sum(weights.get(x, {"omen": 0.5})["omen"] for x in doms) / 4
    wc_g = sum(weights.get(x, {"clio": 0.5})["clio"] for x in doms) / 4
    vol = volatility(events, db.get("catalysts", []), today, wo_g, wc_g, cfg)
    if vol:
        vol["date"] = tstr
        vol["by_domain"] = {}
        for x in doms:
            w = weights.get(x, {"clio": 0.5, "omen": 0.5})
            vd = volatility(events, db.get("catalysts", []), today, w["omen"], w["clio"], cfg, domain=x)
            vol["by_domain"][x] = None if not vd else {k: vd[k] for k in ("V", "path", "O", "C", "Pm", "n_events")}

    # resolve shock-watch entries whose 7-day window has fully passed (one extra day for late scoring)
    sw = db.setdefault("shock_watch", [])
    spx = (series.get("SP500") or {}).get("history") or []
    spx_moves = {spx[i][0]: abs(spx[i][1] / spx[i - 1][1] - 1) for i in range(1, len(spx))}
    for row in sw:
        if row.get("outcome") is not None:
            continue
        start = d(row["date"])
        if (today - start).days < 8:
            continue
        end = start + dt.timedelta(days=7)
        hits = [e["id"] for e in events if start < d(e["date"]) <= end and e["omen"]["chaos"] >= cfg["shock_chaos"]]
        mkt = [k for k, v in spx_moves.items() if start < d(k) <= end and v >= cfg["shock_spx_move"]]
        row["outcome"] = bool(hits or mkt)
        row["evidence"] = hits[:5] + [f"S&P 500 move {k}" for k in mkt][:3]
        row["resolved_on"] = tstr
        row["brier"] = round((row["p"] - (1.0 if row["outcome"] else 0.0)) ** 2, 4)
        notes.append(f"Shock watch {row['date']} resolved: {'SHOCK' if row['outcome'] else 'no shock'} "
                     f"(said {row['p']:.0%}, Brier {row['brier']}).")

    # re-fit the V -> shock mapping from ORACLE's own record once there is enough of it
    rs = [r for r in sw if r.get("outcome") is not None]
    sa, sb = cfg["shock_prior"]
    fitted = False
    if len(rs) >= cfg["shock_min_fit"]:
        sa, sb = fit_logistic([r["V"] for r in rs], [1.0 if r["outcome"] else 0.0 for r in rs],
                              tuple(cfg["shock_prior"]), cfg["ridge"])
        fitted = True
    if vol:
        p_shock = sigmoid(sa + sb * vol["V"])
        vol["shock"] = {"p": round(p_shock, 3), "a": round(sa, 3), "b": round(sb, 3), "fitted": fitted,
                        "n_resolved": len(rs),
                        "brier": round(sum(r["brier"] for r in rs) / len(rs), 4) if rs else None,
                        "hits": sum(1 for r in rs if r["outcome"] == (r["p"] >= 0.5))}
        entry = {"date": tstr, "V": vol["V"], "p": round(p_shock, 3), "outcome": None}
        if sw and sw[-1]["date"] == tstr and sw[-1].get("outcome") is None:
            sw[-1] = entry
        else:
            sw.append(entry)
        db["volatility"] = vol
        vlog = db.setdefault("volatility_log", [])
        vrow = {"date": tstr, "V": vol["V"], "path": vol["path"], "p_shock": round(p_shock, 3),
                "by_domain": {k: (v["V"] if v else None) for k, v in vol["by_domain"].items()}}
        if vlog and vlog[-1]["date"] == tstr:
            vlog[-1] = vrow
        else:
            vlog.append(vrow)
    else:
        warnings.append("Not enough scored events in the last 14 days to compute the volatility index.")

    # historical scale: past eras measured with the same ruler
    hs = history_scale(today, wo_g, wc_g, cfg, vol, profile(events, today, cfg))
    if hs:
        db["history_scale"] = hs

    # ---------- 5. stats, calibration, score log ----------
    scored = [p for p in preds if p.get("status") == "resolved" and (p.get("brier") or {}).get("oracle") is not None]

    def mean_b(rows, key):
        vals = [p["brier"][key] for p in rows if p["brier"].get(key) is not None]
        return (round(sum(vals) / len(vals), 4) if vals else None), len(vals)

    def block(rows):
        out = {"n": len(rows), "hits": sum(1 for p in rows if p.get("hit") is True),
               "misses": sum(1 for p in rows if p.get("hit") is False)}
        for k in ("oracle", "clio", "omen", "market", "sim"):
            out[f"brier_{k}"], out[f"n_{k}"] = mean_b(rows, k)
        return out

    stats = {"all": block(scored), "by_domain": {}}
    for dom in ("politics", "economy", "unrest", "foreign"):
        stats["by_domain"][dom] = block([p for p in scored if p["domain"] == dom])
    buckets = []
    for b_lo in (0.0, 0.2, 0.4, 0.6, 0.8):
        rows = [p for p in scored if b_lo <= p["oracle"] < b_lo + 0.2 or (b_lo == 0.8 and p["oracle"] >= 1.0)]
        buckets.append({"range": f"{int(b_lo*100)}-{int(b_lo*100)+20}%", "n": len(rows),
                        "avg_forecast": r2(sum(p["oracle"] for p in rows) / len(rows)) if rows else None,
                        "happened": r2(sum(1 for p in rows if p["outcome"]) / len(rows)) if rows else None})
    stats["calibration"] = buckets
    db["stats"] = stats

    log = db.setdefault("score_log", [])
    a = stats["all"]
    entry = {"date": tstr, "resolved_today": sum(1 for p in scored if p.get("resolved_on") == tstr),
             "total_resolved": a["n"], "hits": a["hits"], "misses": a["misses"],
             "brier_oracle": a["brier_oracle"], "brier_clio": a["brier_clio"], "brier_omen": a["brier_omen"],
             "brier_market": a["brier_market"], "n_market": a["n_market"],
             "brier_sim": a["brier_sim"], "n_sim": a["n_sim"]}
    if log and log[-1]["date"] == tstr:
        log[-1] = entry
    else:
        log.append(entry)

    # ---------- 6. the 7-day review ----------
    wk = today - dt.timedelta(days=7)
    moved = []
    for p in preds:
        if p.get("status") != "open":
            continue
        past = [h for h in p.get("history", []) if d(h["date"]) <= wk]
        if past:
            delta = p["oracle"] - past[-1]["oracle"]
            if abs(delta) >= 0.05:
                moved.append({"id": p["id"], "question": p["question"], "then": past[-1]["oracle"],
                              "now": p["oracle"], "delta": round(delta, 2)})
    moved.sort(key=lambda x: -abs(x["delta"]))
    week_res = []
    for p in scored:
        if p.get("resolved_on") and d(p["resolved_on"]) >= wk:
            bc, bo = p["brier"]["clio"], p["brier"]["omen"]
            winner = "tie" if abs(bc - bo) < 1e-9 else ("CLIO" if bc < bo else "OMEN")
            week_res.append({"id": p["id"], "question": p["question"], "outcome": p["outcome"],
                             "oracle": p["oracle"], "clio": p["clio"], "omen": p["omen"],
                             "hit": p.get("hit"), "closer_engine": winner})
    tracked = [{"id": p["id"], "question": p["question"], **p["track_status"]}
               for p in preds if p.get("status") == "open" and p.get("track_status")]
    db["review"] = {
        "date": tstr, "since": wk.isoformat(),
        "resolved": week_res,
        "clio_wins": sum(1 for x in week_res if x["closer_engine"] == "CLIO"),
        "omen_wins": sum(1 for x in week_res if x["closer_engine"] == "OMEN"),
        "big_moves": moved[:10],
        "weight_changes": [w for w in whist if d(w["date"]) >= wk],
        "tracked": tracked,
    }

    # ---------- save + summary ----------
    if not args.dry_run:
        (DATA / "oracle.json").write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"ORACLE core — {tstr}{' (dry run, nothing saved)' if args.dry_run else ''}")
    print(f"Open {sum(1 for p in preds if p.get('status') == 'open')} · resolved {a['n']} "
          f"(hits {a['hits']}, misses {a['misses']}) · Brier ORACLE {a['brier_oracle']} CLIO {a['brier_clio']} "
          f"OMEN {a['brier_omen']} market {a['brier_market']} (n={a['n_market']}) sim {a['brier_sim']} (n={a['n_sim']})")
    print("Weights: " + ", ".join(f"{k} {v['clio']:.2f}/{v['omen']:.2f}" for k, v in weights.items()))
    if calib["active"]:
        print(f"Calibration ON (n={calib['n']}): oracle = sigmoid({calib['a']} + {calib['b']} * logit(blend))")
    else:
        print(f"Calibration off until {cfg['calib_min']} results (have {calib['n']}).")
    if vol:
        names = {"alpha": "Path Alpha (status quo)", "gamma": "Path Gamma (synthesis)", "beta": "Path Beta (systemic shock)",
                 "omega": "Path Omega (collapse)"}
        print(f"Volatility V = {vol['V']:.2f} -> {names[vol['path']]} | OMEN intensity {vol['O']} x calendar {vol['Pm']} "
              f"vs CLIO resistance {vol['C']} x inertia {vol['inertia']} ({vol['n_events']} events)")
        print("  by domain: " + ", ".join(f"{k} {v['V']:.2f}" if v else f"{k} n/a" for k, v in vol["by_domain"].items()))
        if hs and hs.get("today"):
            nm = {e["id"]: f"{e['name']} ({e['when']}, {e['V']:.2f})" for e in hs["eras"]}
            print("  closest in level: " + "; ".join(nm[i] for i in hs["closest_level"]))
            print("  closest in kind:  " + "; ".join(nm[i] for i in hs["closest_kind"]))
        sh = vol["shock"]
        print(f"Shock watch: {sh['p']:.0%} chance of a major shock in the next 7 days "
              f"({'fitted' if sh['fitted'] else 'prior'} mapping; {sh['n_resolved']} resolved, Brier {sh['brier']})")
    for s in db.get("systems", []):
        simtxt = f", sim {s['sim']['tip']:.0%}" if s.get("sim") else ""
        proj = f", projected {s['projection']['kind']} {s['projection']['date']}" if s.get("projection") else ""
        print(f"System {s['id']}: P {s['pressure']} vs F {s['friction']} -> {s['status']} "
              f"(tip {s['tip_probability']:.0%}{simtxt}{proj}, news momentum {s['momentum']:+.1f})")
    for t in tracked:
        print(f"Tracking {t['id']}: {t['value']} on {t['date']} -> {'on track' if t['met'] else 'off track'}"
              f"{' (proxy)' if t['proxy'] else ''}")
    for m in moved[:5]:
        print(f"7-day move {m['id']}: {m['then']:.0%} -> {m['now']:.0%}")
    for n in notes:
        print("NOTE: " + n)
    for w in warnings:
        print("WARNING: " + w)


if __name__ == "__main__":
    main()
