#!/bin/bash
# tests/sim/run.sh — simulator acceptance for the v2 controller (docs/CONTRACT.md §13).
#
# Generates every --gen-trace scenario for seeds 1..10 (one hour each,
# deterministic per seed), runs each through `thinkpad-fan-controld --simulate`
# with the default config, and checks the §13 assertions on the mean across
# seeds unless a rule says "any seed".  Then replays the recorded mini-eq trace
# through the v2 controller and through the v1 rules (--set legacy=1) and
# requires at least 3x fewer transitions.  Prints a table; exits 1 on any
# failed assertion.
#
# Nothing here touches hardware, /run, /var or the sound system: --simulate
# and --gen-trace only use the fake clock and the fake fan.
#
# Environment: SIM_SEEDS (default "1 2 3 4 5 6 7 8 9 10"),
#              SIM_SECONDS (default 3600), SIM_KEEP=1 keeps the work dir.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
DAEMON="$REPO/daemon/thinkpad-fan-controld"
REAL_TRACE="$HERE/traces/real-2026-09-23-mini-eq-1hz.csv"
SEEDS=${SIM_SEEDS:-"1 2 3 4 5 6 7 8 9 10"}
RUN_SECONDS=${SIM_SECONDS:-3600}
SCENARIOS=(idle load12 load15 load18 stop ramp acflip sensorloss override)

# The daemon is plain Python; keep bytecode caches out of the repository.
export PYTHONDONTWRITEBYTECODE=1

WORK=$(mktemp -d "${TMPDIR:-/tmp}/fan-sim.XXXXXX")
if [[ ${SIM_KEEP:-0} == 1 ]]; then
    echo "work dir kept: $WORK"
else
    trap 'rm -rf "$WORK"' EXIT
fi

[[ -f $DAEMON ]] || { echo "missing $DAEMON" >&2; exit 1; }
[[ -f $REAL_TRACE ]] || { echo "missing $REAL_TRACE" >&2; exit 1; }

started=$SECONDS
for scenario in "${SCENARIOS[@]}"; do
    for seed in $SEEDS; do
        python3 "$DAEMON" --gen-trace "$scenario" --seconds "$RUN_SECONDS" --seed "$seed" \
            > "$WORK/$scenario-$seed.csv"
        python3 "$DAEMON" --simulate "$WORK/$scenario-$seed.csv" --json --detail \
            > "$WORK/$scenario-$seed.json"
    done
done

# Determinism: the same seed must produce the same trace byte for byte.
first_seed=${SEEDS%% *}
python3 "$DAEMON" --gen-trace load15 --seconds "$RUN_SECONDS" --seed "$first_seed" > "$WORK/determinism.csv"
if cmp -s "$WORK/determinism.csv" "$WORK/load15-$first_seed.csv"; then
    determinism=ok
else
    determinism=FAIL
fi

python3 "$DAEMON" --simulate "$REAL_TRACE" --json --detail > "$WORK/real-v2.json"
python3 "$DAEMON" --simulate "$REAL_TRACE" --json --detail --set legacy=1 > "$WORK/real-v1.json"

python3 - "$WORK" "$determinism" "$((SECONDS - started))" $SEEDS <<'PY'
import csv
import json
import math
import statistics
import sys

work, determinism, elapsed = sys.argv[1], sys.argv[2], sys.argv[3]
seeds = [int(s) for s in sys.argv[4:]]
RANK = {"auto": -1, "disengaged": 8, "full-speed": 8, **{str(i): i for i in range(8)}}
failures = []
rows_out = []


def load(scenario, seed):
    with open(f"{work}/{scenario}-{seed}.json", encoding="utf-8") as f:
        sim = json.load(f)
    with open(f"{work}/{scenario}-{seed}.csv", encoding="utf-8", newline="") as f:
        trace = list(csv.DictReader(f))
    return sim, trace


def check(ok, scenario, seed, message):
    if not ok:
        failures.append(f"{scenario} seed {seed}: {message}")
    return ok


def reseat_index(curve, fast):
    return max(i for i, s in enumerate(curve) if s["temp"] <= fast)


def active_curve(sim, tick):
    cfg = sim["config"]
    return cfg["battery_curve"] if tick["curve"] == "battery" else cfg["curve"]


def row(scenario, sims, notes):
    cph = [s["changes_per_hour"] for s in sims]
    gaps = [s["min_gap_opposite_s"] for s in sims if s["min_gap_opposite_s"] is not None]
    rows_out.append((scenario, statistics.mean(cph), max(cph), min(gaps) if gaps else None,
                     statistics.mean(s["alerts"] for s in sims),
                     statistics.mean(s["seconds_at_or_above_critical"] for s in sims), notes))
    return statistics.mean(cph), max(cph)


# ── rate scenarios ───────────────────────────────────────────────────────────
for scenario, limit in (("idle", 10), ("load12", 10), ("load15", 30), ("load18", None)):
    sims = [load(scenario, s)[0] for s in seeds]
    mean, worst = row(scenario, sims, "")
    if limit is not None:
        check(mean <= limit, scenario, "mean", f"{mean:.1f} changes/h > {limit}")
    if scenario == "load15":
        check(worst <= 40, scenario, "max", f"one seed at {worst:.1f} changes/h > 40")
        for seed, sim in zip(seeds, sims):
            gap = sim["min_gap_opposite_s"]
            check(gap is None or gap >= 10, scenario, seed, f"reversal only {gap} s apart")

# ── stop: first DOWN within 60 s of the load end, no UP during the cascade ──
sims, notes = [], []
for seed in seeds:
    sim, trace = load("stop", seed)
    sims.append(sim)
    power = [float(r["p_w"]) for r in trace]
    t_end = next(i for i in range(1, len(power)) if power[i] < power[i - 1] - 5)
    after = [tr for tr in sim["transition_list"] if tr["t"] >= t_end]
    downs = [tr for tr in after if RANK[tr["to"]] < RANK[tr["from"]]]
    if not check(bool(downs), "stop", seed, "no DOWN step after the load ended"):
        continue
    first = downs[0]["t"] - t_end
    notes.append(first)
    check(first <= 60, "stop", seed, f"first DOWN {first:.0f} s after the load ended (> 60)")
    final = sim["final_level"]
    reach = next((tr["t"] for tr in after if tr["to"] == final), None)
    cascade = [tr for tr in after if reach is None or tr["t"] <= reach]
    ups = [tr for tr in cascade if RANK[tr["to"]] > RANK[tr["from"]]]
    check(not ups, "stop", seed,
          "UP during the cascade: " + ", ".join(f"{u['from']}->{u['to']} at +{u['t'] - t_end:.0f} s" for u in ups))
row("stop", sims, f"first DOWN +{min(notes):.0f}..+{max(notes):.0f} s" if notes else "")

# ── ramp: entry within 2 samples of the second >= critical reading, one alert
#    per cooldown, exit only after the hold ─────────────────────────────────
sims = []
for seed in seeds:
    sim, _trace = load("ramp", seed)
    sims.append(sim)
    cfg, ticks = sim["config"], sim["ticks"]
    crit = cfg["critical_temp"]
    hot = [i for i, tk in enumerate(ticks) if tk["raw"] is not None and tk["raw"] >= crit]
    entry = next((i for i, tk in enumerate(ticks) if tk["critical"]), None)
    if not check(len(hot) >= 2 and entry is not None, "ramp", seed, "critical never entered"):
        continue
    check(entry <= hot[1] + 2, "ramp", seed,
          f"critical entered at sample {entry}, second reading >= {crit} at {hot[1]}")
    exit_i = next((i for i in range(entry, len(ticks)) if not ticks[i]["critical"]), None)
    if not check(exit_i is not None, "ramp", seed, "critical never cleared"):
        continue
    te, tx = ticks[entry]["t"], ticks[exit_i]["t"]
    hold, limit = cfg["critical_exit_hold_s"], crit - cfg["critical_exit_margin"]
    check(tx - te >= hold and ticks[exit_i]["slow"] < limit, "ramp", seed,
          f"exit after {tx - te:.0f} s at slow {ticks[exit_i]['slow']}")
    prev = ticks[exit_i - 1]
    check(not (prev["slow"] < limit and prev["t"] - te >= hold), "ramp", seed,
          "exit later than the first tick that allowed it")
    alerts = sim["alert_times"]
    expected = math.ceil((tx - te) / cfg["alert_cooldown"])
    check(len(alerts) == expected, "ramp", seed, f"{len(alerts)} alerts, expected {expected}")
    check(bool(alerts) and alerts[0] == te, "ramp", seed, "no alert at critical entry")
    gaps = [b - a for a, b in zip(alerts, alerts[1:])]
    check(all(cfg["alert_cooldown"] <= g <= cfg["alert_cooldown"] + cfg["sample_interval"] for g in gaps),
          "ramp", seed, f"alert spacing {gaps}")
    check(all(te <= a < tx for a in alerts), "ramp", seed, "alert outside the critical episode")
    log = sim["log"]
    check(any("CRITICAL entered" in l for l in log) and any("CRITICAL cleared" in l for l in log)
          and sum("ALERT: critical since" in l for l in log) == len(alerts)
          and not any("at or above" in l for l in log),
          "ramp", seed, "critical/alert log wording")
row("ramp", sims, "")

# ── acflip: one transition per real flip, none for a bounce ────────────────
sims = []
for seed in seeds:
    sim, trace = load("acflip", seed)
    sims.append(sim)
    n = len(trace)
    flips = [n // 3 + 2, 2 * n // 3 + 2]        # third identical reading switches
    ac_events = [e for e in sim["events"] if e[1] == "ac"]
    check([round(e[0]) for e in ac_events] == flips, "acflip", seed,
          f"power switches at {[round(e[0]) for e in ac_events]}, expected {flips}")
    curves = [tk["curve"] for tk in sim["ticks"]]
    switches = sum(1 for a, b in zip(curves, curves[1:]) if a != b)
    check(switches == 2, "acflip", seed, f"curve switched {switches} times")
    by_cause = [tr for tr in sim["transition_list"] if tr["cause"] == "ac"]
    check(len(by_cause) <= 2 and all(round(tr["t"]) in flips for tr in by_cause), "acflip", seed,
          f"power-caused transitions {[(tr['t'], tr['from'], tr['to']) for tr in by_cause]}")
    for f in flips:
        at = [tr for tr in sim["transition_list"] if round(tr["t"]) == f]
        check(len(at) <= 1, "acflip", seed, f"{len(at)} transitions at the switch t={f}")
        tick = next(tk for tk in sim["ticks"] if round(tk["t"]) == f)
        curve = active_curve(sim, tick)
        check(tick["idx"] == reseat_index(curve, tick["fast"]), "acflip", seed,
              f"not re-seated at t={f}")
row("acflip", sims, "")

# ── sensorloss: auto after 3 missing samples, one log line, re-seat on recovery
sims = []
for seed in seeds:
    sim, trace = load("sensorloss", seed)
    sims.append(sim)
    n = len(trace)
    ticks = {round(tk["t"]): tk for tk in sim["ticks"]}
    lost = n // 3
    kinds = [e[1] for e in sim["events"]]
    check(kinds.count("sensor_lost") == 1 and kinds.count("sensor_ok") == 1, "sensorloss", seed,
          f"events {kinds.count('sensor_lost')} lost / {kinds.count('sensor_ok')} ok")
    check(sum("no valid temperature" in l for l in sim["log"]) == 1, "sensorloss", seed,
          "sensor loss logged more than once")
    before = ticks[lost - 1]["level"]
    check(all(ticks[t]["level"] == before and ticks[t]["reason"] != "sensor_lost"
              for t in (lost, lost + 1)), "sensorloss", seed, "acted before the 3rd missing sample")
    third = ticks[lost + 2]
    check(third["level"] == "auto" and third["reason"] == "sensor_lost", "sensorloss", seed,
          f"3rd missing sample: {third['level']} [{third['reason']}]")
    short = [n // 6, n // 4, n // 4 + 1] + list(range(int(n * 0.2), int(n * 0.2) + 5))
    check(all(ticks[t]["reason"] != "sensor_lost" for t in short), "sensorloss", seed,
          "a 1-2 sample gap or a partial loss triggered sensor_lost")
    back = ticks[lost + 30]
    curve = active_curve(sim, back)
    check(back["reason"] != "sensor_lost" and back["idx"] == reseat_index(curve, back["fast"])
          and back["fast"] == back["slow"] == back["raw"], "sensorloss", seed,
          f"recovery tick not re-seeded/re-seated: {back}")
row("sensorloss", sims, "")

# ── override: held through 85 C, suspended above the ceiling, dropped at
#    critical, re-seated on expiry ───────────────────────────────────────────
sims = []
for seed in seeds:
    sim, trace = load("override", seed)
    sims.append(sim)
    cfg, ticks = sim["config"], sim["ticks"]
    n = len(trace)
    ceiling = cfg["override_ceiling_temp"]
    phase_b = int(n * 0.25)
    early = [tk for tk in ticks if 60 <= tk["t"] < phase_b]
    check(any(tk["raw"] >= 85 and tk["level"] == "3" and tk["reason"] == "override" for tk in early),
          "override", seed, "no tick held level 3 through a >= 85 C reading")
    check(not any(tk["suspended"] for tk in early), "override", seed,
          "suspended by a Tctl spike before the load rose")
    susp = [i for i, tk in enumerate(ticks) if tk["suspended"] and (i == 0 or not ticks[i - 1]["suspended"])]
    check(bool(susp) and all(ticks[i]["fast"] >= ceiling for i in susp), "override", seed,
          "never suspended, or suspended below the ceiling")
    for i in susp:
        end = next((j for j in range(i, len(ticks)) if not ticks[j]["suspended"]), None)
        if end is not None and ticks[end]["override"] is not None:
            check(ticks[end]["t"] - ticks[i]["t"] >= 30
                  and ticks[end]["slow"] < ceiling - cfg["hysteresis"], "override", seed,
                  f"resumed after {ticks[end]['t'] - ticks[i]['t']:.0f} s at slow {ticks[end]['slow']}")
    crit = [tk for tk in ticks if tk["critical"]]
    check(bool(crit) and all(tk["level"] == "disengaged" and tk["reason"] == "critical" for tk in crit)
          and any(tk["override"] == "3" for tk in crit), "override", seed,
          "critical did not take precedence over the hold")
    exp = [e for e in sim["events"] if e[1] == "override_end"]
    if check(len(exp) == 1 and "expired" in exp[0][2], "override", seed, f"expiry events {exp}"):
        tick = next(tk for tk in ticks if tk["t"] >= exp[0][0])
        curve = active_curve(sim, tick)
        want = reseat_index(curve, tick["fast"])
        check(tick["override"] is None and tick["idx"] == want and tick["reason"] in ("curve", "critical")
              and (tick["reason"] != "curve" or tick["level"] == curve[want]["level"]),
              "override", seed, f"not re-seated on expiry: {tick}")
row("override", sims, "")

# ── real trace: v2 against the v1 rules ─────────────────────────────────────
with open(f"{work}/real-v2.json", encoding="utf-8") as f:
    v2 = json.load(f)
with open(f"{work}/real-v1.json", encoding="utf-8") as f:
    v1 = json.load(f)
ok = v2["transitions"] * 3 <= v1["transitions"] and v1["transitions"] > 0
check(ok, "real-trace", "-", f"v2 {v2['transitions']} vs v1 {v1['transitions']} transitions (need >= 3x fewer)")
check(determinism == "ok", "gen-trace", seeds[0], "same seed produced a different trace")

# ── table ────────────────────────────────────────────────────────────────────
print(f"seeds {seeds[0]}..{seeds[-1]} ({len(seeds)}), {elapsed} s wall time for generation + simulation\n")
print(f"{'scenario':<11} {'mean ch/h':>9} {'max ch/h':>9} {'min rev s':>9} {'alerts':>7} {'>=crit s':>9}  notes")
for name, mean, worst, gap, alerts, crit_s, notes in rows_out:
    gap_s = "-" if gap is None else f"{gap:.0f}"
    print(f"{name:<11} {mean:>9.1f} {worst:>9.1f} {gap_s:>9} {alerts:>7.1f} {crit_s:>9.1f}  {notes}")
print(f"\nreal trace (900 s, open loop): v2 {v2['transitions']} transitions "
      f"({v2['changes_per_hour']}/h), v1 rules {v1['transitions']} ({v1['changes_per_hour']}/h)"
      f" -> {'ok' if ok else 'FAIL'}")
print(f"gen-trace determinism (load15, seed {seeds[0]}): {determinism}")
if failures:
    print(f"\n{len(failures)} FAILED assertion(s):")
    for line in failures:
        print("  - " + line)
    sys.exit(1)
print("\nall assertions passed")
PY
