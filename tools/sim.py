"""ORACLE sandbox: a toy agent-based simulation of the political ecosystem.

Ten-ish generic agents (the swing voter, each party's base and leadership, the protest movement,
hardliners, Wall Street, the Fed, the courts, a foreign adversary) each carry three gauges:
  anger (A)          how upset they are with the status quo
  mobilization (M)   how ready they are to act on it (follows anger with a lag)
  trust (T)          trust in institutions (erodes under shocks, slowly recovers)

Step 1 (memory): newly scored events from data/events.json are replayed to move the agents.
Step 2 (futures): thousands of possible next months are simulated. Each day draws random events
from the recent mix of real scored events (with noise and occasional reversals), and each catalyst
in the registry plays out one of its branches. Agents react, and each system's pressure drifts with
them. On a system's decision date, the pressure-vs-friction tipping curve gives its chance of
breaking in that run. Averaging over runs gives the sandbox's own forecast, plus "IF branch A /
IF branch B" numbers for every catalyst.

This is a toy model with hand-set parameters (data/model.json). Its forecasts are recorded and
scored separately ("sim") so it has to earn trust like everything else.

Usage:  python tools/sim.py [--runs 1000] [--seed N] [--date YYYY-MM-DD]
Reads:  data/model.json, data/oracle.json, data/events.json, data/agents_state.json
Writes: data/agents_state.json, data/sim.json
"""
import argparse
import datetime as dt
import json
import math
import random
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def d(s):
    return dt.date.fromisoformat(s)


def clamp(x, lo=0.0, hi=100.0):
    return lo if x < lo else hi if x > hi else x


def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def stimulus(agent, ev, chaos, polar, econ):
    """How much one event stirs one agent. Scores are centred on 4 (an ordinary news day);
    institutional resistance damps the reaction (friction at work)."""
    dw = agent.get("domains", {}).get(ev["domain"], agent.get("other_domains", 1.0))
    if dw == 0:
        return 0.0
    s = agent["sens"]

    def lift(x):  # above-normal scores stir people; quiet news calms them only a little
        return (x - 4) if x >= 4 else (x - 4) / 3.0

    raw = (s["chaos"] * lift(chaos) + s["polarization"] * lift(polar) + s["economic"] * lift(econ)) / 3.0
    damp = 1.0 - 0.4 * ev["clio"]["resistance"] / 10.0
    return dw * raw * damp


def step_agents(vals, agents, day_events, P):
    """Advance every agent one day. day_events: list of (event, chaos, polar, econ)."""
    for a in agents:
        v = vals[a["id"]]
        b = a["baseline"]
        stim = sum(stimulus(a, ev, c, z, e) for ev, c, z, e in day_events)
        v["A"] = clamp(v["A"] + P["alpha"] * stim + P["decay"] * (b["A"] - v["A"]))
        v["M"] = clamp(v["M"] + P["m_follow"] * (v["A"] - v["M"]) + P["m_decay"] * (b["M"] - v["M"]))
        v["T"] = clamp(v["T"] - P["t_hit"] * max(0.0, stim) + P["t_recover"] * (b["T"] - v["T"]))


def apply_agent_deltas(vals, deltas):
    for aid, dd in (deltas or {}).items():
        if aid in vals:
            for k, x in dd.items():
                vals[aid][k] = clamp(vals[aid][k] + x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--date", default=None)
    args = ap.parse_args()
    today = d(args.date) if args.date else dt.date.today()

    model = load(DATA / "model.json")
    db = load(DATA / "oracle.json")
    events = (load(DATA / "events.json", {"events": []}) or {}).get("events", [])
    P = model["params"]
    k = db.get("config", {}).get("k", 0.6)
    agents = model["agents"]
    runs = args.runs or P.get("runs", 1000)
    rng = random.Random(args.seed if args.seed is not None else int(today.strftime("%Y%m%d")))

    # ---------- step 1: memory — replay newly scored events ----------
    st = load(DATA / "agents_state.json")
    if not st:
        st = {"date": today.isoformat(),  # starting values already reflect today's situation
              "agents": {a["id"]: dict(a.get("start", a["baseline"])) for a in agents}, "history": []}
    for a in agents:  # agents added to the model later start at their starting values
        st["agents"].setdefault(a["id"], dict(a.get("start", a["baseline"])))
    last = d(st["date"])
    replayed = 0
    day = last + dt.timedelta(days=1)
    while day <= today:
        todays = [(e, e["omen"]["chaos"], e["omen"]["polarization"], e["omen"]["economic"])
                  for e in events if e["date"] == day.isoformat()]
        replayed += len(todays)
        step_agents(st["agents"], agents, todays, P)
        day += dt.timedelta(days=1)
    st["date"] = today.isoformat()
    snap = {"date": today.isoformat(), "agents": {aid: {k2: round(v, 1) for k2, v in vals.items()}
                                                   for aid, vals in st["agents"].items()}}
    st["history"] = [h for h in st.get("history", []) if h["date"] != snap["date"]] + [snap]
    st["history"] = st["history"][-120:]
    now_vals = {aid: dict(v) for aid, v in st["agents"].items()}

    # ---------- step 2: futures ----------
    pool = [e for e in events if 0 <= (today - d(e["date"])).days <= 30]
    if pool:
        span = (today - min(d(e["date"]) for e in pool)).days + 1
        lam = min(P["max_daily_events"], max(P["min_daily_events"], len(pool) / span))
    else:
        lam = 0.0

    systems = []
    for s in db.get("systems", []):
        w = d(s["window"])
        day_i = (w - today).days
        if day_i < 0:
            continue
        systems.append({"s": s, "day": min(day_i, P["horizon_days"])})
    horizon = max([x["day"] for x in systems] + [30])

    catalysts = []
    for c in db.get("catalysts", []):
        if c.get("status", "upcoming") != "upcoming" or not c.get("branches"):
            continue
        a0 = (d(c["date"]) - today).days
        a1 = (d(c.get("window_end") or c["date"]) - today).days
        if a1 < 0 or a0 > horizon:
            continue
        catalysts.append({"c": c, "lo": max(0, a0), "hi": min(horizon, max(0, a1))})

    decay_e = 0.5 ** (1.0 / P["event_halflife_days"])
    sys_ids = [x["s"]["id"] for x in systems]
    tip_sum = {sid: 0.0 for sid in sys_ids}
    p_samples = {sid: [] for sid in sys_ids}
    cond = {x["c"]["id"]: {b["label"]: {"n": 0, "tips": {}} for b in x["c"]["branches"]} for x in catalysts}
    proj30 = {a["id"]: {"A": 0.0, "M": 0.0} for a in agents}

    def poisson(l):
        L, kk, p = math.exp(-l), 0, 1.0
        while True:
            p *= rng.random()
            if p <= L:
                return kk
            kk += 1

    def jitter(x):
        return max(1, min(10, x + round(rng.gauss(0, P["score_noise"]))))

    for _ in range(runs):
        vals = {aid: dict(v) for aid, v in now_vals.items()}
        epress = {sid: 0.0 for sid in sys_ids}
        cdp = {sid: 0.0 for sid in sys_ids}
        cdf = {sid: 0.0 for sid in sys_ids}
        cat_days = {x["c"]["id"]: rng.randint(x["lo"], x["hi"]) for x in catalysts}
        chosen = {}
        evaluated = {}
        for day_i in range(0, horizon + 1):
            # 1) systems whose decision date is today are judged before today's catalysts land
            for x in systems:
                if x["day"] == day_i:
                    s = x["s"]
                    Ps = s["pressure"] + epress[s["id"]] + cdp[s["id"]]
                    for aid, var, wgt in s.get("agent_links", []):
                        Ps += wgt * (vals[aid][var] - now_vals[aid][var])
                    Fs = s["friction"] + cdf[s["id"]]
                    tip = sigmoid(k * (Ps - Fs))
                    tip_sum[s["id"]] += tip
                    p_samples[s["id"]].append(Ps)
                    evaluated[s["id"]] = (day_i, tip)
            # 2) catalysts play out one branch
            for x in catalysts:
                c = x["c"]
                if cat_days[c["id"]] != day_i:
                    continue
                r, acc, br = rng.random(), 0.0, c["branches"][-1]
                for b in c["branches"]:
                    acc += b["prob"]
                    if r <= acc:
                        br = b
                        break
                chosen[c["id"]] = (day_i, br["label"])
                eff = br.get("effects", {})
                for sid, dd in (eff.get("systems") or {}).items():
                    if sid in cdp:
                        cdp[sid] += dd.get("pressure", 0.0)
                        cdf[sid] += dd.get("friction", 0.0)
                apply_agent_deltas(vals, eff.get("agents"))
            # 3) background news
            todays = []
            if day_i >= 1 and pool and lam > 0:
                for _e in range(poisson(lam)):
                    ev = rng.choice(pool)
                    sign = -1 if rng.random() < P["reversal_prob"] else 1
                    c_, z_, e_ = (jitter(ev["omen"]["chaos"]), jitter(ev["omen"]["polarization"]),
                                  jitter(ev["omen"]["economic"]))
                    todays.append((ev, c_, z_, e_))
                    intensity = (c_ + z_ + e_) / 15.0
                    for sid, imp in (ev.get("systems") or {}).items():
                        if sid in epress:
                            epress[sid] += P["event_pressure"] * sign * imp * intensity
            # 4) agents react; event pressure fades
            step_agents(vals, agents, todays, P)
            for sid in epress:
                epress[sid] *= decay_e
            if day_i == 30:
                for aid in proj30:
                    proj30[aid]["A"] += vals[aid]["A"]
                    proj30[aid]["M"] += vals[aid]["M"]
        # conditional tallies: systems judged after a catalyst, grouped by the branch that happened
        for cid, (cday, label) in chosen.items():
            slot = cond[cid][label]
            slot["n"] += 1
            for sid, (sday, tip) in evaluated.items():
                if sday > cday:  # only systems judged after this catalyst landed
                    acc_ = slot["tips"].setdefault(sid, [0.0, 0])
                    acc_[0] += tip
                    acc_[1] += 1

    def pct(vals_, q):
        if not vals_:
            return None
        s = sorted(vals_)
        return round(s[min(len(s) - 1, int(q * len(s)))], 2)

    out_sys, out_pred = {}, {}
    for x in systems:
        s = x["s"]
        tip = tip_sum[s["id"]] / runs
        out_sys[s["id"]] = {"tip": round(tip, 3), "eval_date": (today + dt.timedelta(days=x["day"])).isoformat(),
                            "p10": pct(p_samples[s["id"]], 0.1), "p50": pct(p_samples[s["id"]], 0.5),
                            "p90": pct(p_samples[s["id"]], 0.9)}
        if s.get("tip_prediction"):
            out_pred[s["tip_prediction"]] = round(tip if s.get("tip_means", "yes") == "yes" else 1 - tip, 3)
    out_cat = {}
    for x in catalysts:
        cid = x["c"]["id"]
        br_out = {}
        for b in x["c"]["branches"]:
            slot = cond[cid][b["label"]]
            # mean tip chance for each later-judged system, among runs where this branch happened first;
            # skip thin samples so a handful of runs can't produce a misleading number
            br_out[b["label"]] = {"share": round(slot["n"] / runs, 3),
                                  "systems": {sid: round(s_ / n_, 3) for sid, (s_, n_) in slot["tips"].items()
                                              if n_ >= 25}}
        out_cat[cid] = br_out

    sim = {
        "date": today.isoformat(), "runs": runs, "events_per_day": round(lam, 2), "event_pool": len(pool),
        "horizon_days": horizon, "replayed_events": replayed,
        "systems": out_sys, "predictions": out_pred, "catalysts": out_cat,
        "agents_now": snap["agents"],
        "agents_day30": {aid: {"A": round(v["A"] / runs, 1), "M": round(v["M"] / runs, 1)} for aid, v in proj30.items()},
    }
    (DATA / "agents_state.json").write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")
    (DATA / "sim.json").write_text(json.dumps(sim, indent=2, ensure_ascii=False), encoding="utf-8")

    names = {a["id"]: a["name"] for a in agents}
    print(f"Sandbox — {today}: {runs} runs, {horizon} days ahead, {lam:.1f} events/day from {len(pool)} recent "
          f"scored events; replayed {replayed} new events into agent memory.")
    for sid, r in out_sys.items():
        print(f"  {sid}: sim tip {r['tip']:.0%} on {r['eval_date']} (pressure p10/p50/p90 {r['p10']}/{r['p50']}/{r['p90']})")
    for aid, v in snap["agents"].items():
        p30 = sim["agents_day30"][aid]
        print(f"  {names.get(aid, aid)}: anger {v['A']:.0f} mob {v['M']:.0f} trust {v['T']:.0f} "
              f"-> day 30 anger {p30['A']:.0f} mob {p30['M']:.0f}")


if __name__ == "__main__":
    main()
