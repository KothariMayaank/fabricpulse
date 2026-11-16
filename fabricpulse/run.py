"""Experiment runner.

    python -m fabricpulse.run                       # full matrix, 3 seeds
    python -m fabricpulse.run --scenarios gray_link --policies ecmp telemetry --seeds 1

Writes results/summary.json, results/summary.md and, for seed 0 of every
(scenario, policy) pair, a results/runs/<scenario>__<policy>.json trace that
the dashboard reads.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from .attribution import attribute
from .policies import POLICIES
from .scenarios import DURATION, FAULT_T, SCENARIOS, build
from .topology import LeafSpine

# Illustrative economics, not a measurement: what a relative MFU loss is worth
# on a cluster of this size at this price.
N_XPU = 10_000
USD_PER_XPU_HOUR = 2.50
TRACE_BUCKET_S = 0.01


def _clean(o):
    """NaN is not valid JSON (browsers reject it); write null instead."""
    if isinstance(o, float):
        return None if o != o else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def dumps(o, **kw) -> str:
    return json.dumps(_clean(o), allow_nan=False, **kw)


def annual_cost_usd(mfu_loss_rel: float) -> float:
    return mfu_loss_rel * N_XPU * USD_PER_XPU_HOUR * 8760


def run_one(scenario: str, policy: str, seed: int, duration: float = DURATION,
            uplink_gbps: float | None = None) -> tuple[dict, dict]:
    topo = LeafSpine(uplink_gbps=uplink_gbps) if uplink_gbps else None
    b = build(scenario, policy, seed, topo=topo)
    sim, job, inf = b["sim"], b["job"], b["inference"]
    t0 = time.perf_counter()
    sim.run(duration)
    js = job.stats(t0=FAULT_T)
    post = [it for it in job.iterations if it["t_end"] > FAULT_T]
    suspects = attribute(post, sim.topo, job.ideal_comm_s, top=3)
    tel = sim.telemetry
    fault, t_fault = b["fault"], b.get("t_fault", FAULT_T)
    advised = [a for a in sim.advisories if a["component"] == fault]
    res = {
        "scenario": scenario, "policy": policy, "seed": seed, "fault": b["fault"],
        "t_fault": round(t_fault, 4) if fault else None,
        **js,
        "reroutes": sim.n_reroutes,
        "advisories": len(sim.advisories),
        # Online detection: time from fault onset to an advisory naming it.
        "detect_ms": round((advised[0]["t"] - t_fault) * 1e3, 2) if advised else None,
        "false_advisories": sum(a["component"] != fault for a in sim.advisories),
        "migrations": len(job.migrations),
        # Offline localisation from job-level transfer times alone.
        "top_suspect": suspects[0]["component"] if suspects else None,
        "inference": inf.stats(t0=FAULT_T) if inf else None,
        "resolution_gap": tel.resolution_gap(sim.topo.cap, sim.cc.kmin),
        "wall_s": round(time.perf_counter() - t0, 2),
    }
    # Trace for the dashboard: fabric links only, pooled into 10 ms buckets.
    # Utilisation and queue keep the bucket *peak* so microbursts survive.
    k = max(1, int(round(TRACE_BUCKET_S / tel.period)))
    fab = np.flatnonzero(sim.topo.is_fabric[: sim.topo.n_links])
    n = (tel.n // k) * k
    pool = lambda a: a[:n][:, fab].reshape(n // k, k, fab.size)
    trace = {
        "meta": res, "topology": sim.topo.describe(), "fabric_links": fab.tolist(),
        "bucket_ms": TRACE_BUCKET_S * 1e3,
        "t_ms": (tel.t[:n:k] * 1e3).round(1).tolist(),
        "util_peak": (pool(tel.arrival).max(axis=1) / sim.topo.uplink_gbps).round(2).tolist(),
        "util_mean": (pool(tel.arrival).mean(axis=1) / sim.topo.uplink_gbps).round(2).tolist(),
        "queue_peak_gbit": pool(tel.queue).max(axis=1).round(3).tolist(),
        "err_mean_gbps": pool(tel.err).mean(axis=1).round(1).tolist(),
        "iterations": [{"t_start": it["t_start"], "t_end": it["t_end"], "comm_s": it["comm_s"]}
                       for it in job.iterations],
        "link_events": sim.link_events, "advisories": sim.advisories,
        "migrations": job.migrations, "suspects": suspects,
    }
    return res, trace


def aggregate(rows: list[dict]) -> list[dict]:
    out = []
    for sc in dict.fromkeys(r["scenario"] for r in rows):
        for po in dict.fromkeys(r["policy"] for r in rows):
            g = [r for r in rows if r["scenario"] == sc and r["policy"] == po]
            if not g:
                continue
            mean = lambda k: float(np.mean([r[k] for r in g if r[k] is not None] or [np.nan]))
            inf = [r["inference"]["slowdown_p99"] for r in g if r["inference"] and r["inference"]["flows"]]
            loss = mean("mfu_loss_rel")
            out.append({
                "scenario": sc, "policy": po, "seeds": len(g),
                "mfu_pct": 100 * mean("mfu"), "ideal_mfu_pct": 100 * mean("ideal_mfu"),
                "mfu_loss_rel_pct": 100 * loss,
                "iter_ms_p50": mean("iter_ms_p50"), "iter_ms_p99": mean("iter_ms_p99"),
                "reroutes": mean("reroutes"), "migrations": mean("migrations"),
                "detect_ms": mean("detect_ms"),
                "detected": sum(r["detect_ms"] is not None for r in g),
                "false_advisories": sum(r["false_advisories"] for r in g),
                "attributed": sum(r["fault"] is not None and r["top_suspect"] == r["fault"] for r in g),
                "faulted_runs": sum(r["fault"] is not None for r in g),
                "inference_p99_slowdown": float(np.mean(inf)) if inf else None,
                "annual_cost_usd": annual_cost_usd(loss),
            })
    return out


def to_markdown(agg: list[dict]) -> str:
    f = lambda v, p=1: "-" if v is None or v != v else f"{v:.{p}f}"
    lines = [
        "| scenario | policy | MFU % | loss vs ideal % | iter p50 ms | iter p99 ms | reroutes | "
        "detected online | detect ms | attributed offline | inference p99 slowdown | cost of loss $M/yr |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a in agg:
        n = a["faulted_runs"]
        det = f"{a['detected']}/{n}" if n else "-"
        att = f"{a['attributed']}/{n}" if n else "-"
        lines.append(
            f"| {a['scenario']} | {a['policy']} | {f(a['mfu_pct'])} | {f(a['mfu_loss_rel_pct'])} | "
            f"{f(a['iter_ms_p50'])} | {f(a['iter_ms_p99'])} | {f(a['reroutes'], 0)} | "
            f"{det} | {f(a['detect_ms'], 1)} | {att} | {f(a['inference_p99_slowdown'], 2)} | "
            f"{f((a['annual_cost_usd'] or 0) / 1e6)} |")
    return "\n".join(lines)


def latency_sweep(out: Path, seeds: int, duration: float = DURATION) -> str:
    """How much of the benefit survives as the control loop slows down from a
    millisecond agent to a classic poller. Same policy, one knob."""
    from .policies import TelemetryAware
    periods = [1e-3, 1e-2, 1e-1, 1.0, 30.0]
    rows = []
    for sc in ("gray_link", "flapping_link", "bad_nic"):
        for period in periods:
            POLICIES["_sweep"] = lambda salt=0, p=period: TelemetryAware(control_s=p, salt=salt)
            try:
                rs = [run_one(sc, "_sweep", seed, duration)[0] for seed in range(seeds)]
            finally:
                del POLICIES["_sweep"]
            det = [r["detect_ms"] for r in rs if r["detect_ms"] is not None]
            rows.append({"scenario": sc, "control_period_s": period,
                         "mfu_pct": 100 * float(np.mean([r["mfu"] for r in rs])),
                         "mfu_loss_rel_pct": 100 * float(np.mean([r["mfu_loss_rel"] for r in rs])),
                         "detected": len(det), "runs": len(rs),
                         "detect_ms": float(np.mean(det)) if det else None})
            print(f"sweep {sc:14s} period={period:g}s mfu={rows[-1]['mfu_pct']:.1f}%", flush=True)
    lines = ["| scenario | control period | MFU % | loss vs ideal % | detected | detect ms |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        p = r["control_period_s"]
        label = f"{p * 1e3:g} ms" if p < 1 else f"{p:g} s"
        d = "-" if r["detect_ms"] is None else f"{r['detect_ms']:.0f}"
        lines.append(f"| {r['scenario']} | {label} | {r['mfu_pct']:.1f} | {r['mfu_loss_rel_pct']:.1f} | "
                     f"{r['detected']}/{r['runs']} | {d} |")
    md = "\n".join(lines)
    (out / "latency_sweep.json").write_text(dumps(rows, indent=1))
    (out / "latency_sweep.md").write_text(md + "\n")
    return md


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenarios", nargs="+", default=list(SCENARIOS), choices=list(SCENARIOS))
    ap.add_argument("--policies", nargs="+", default=list(POLICIES), choices=list(POLICIES))
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--duration", type=float, default=DURATION)
    ap.add_argument("--out", default="results")
    ap.add_argument("--uplink-gbps", type=float, default=None,
                    help="override uplink speed, e.g. 400 for a 2:1 oversubscribed fabric")
    ap.add_argument("--tag", default="",
                    help="suffix for the summary files; tagged runs do not write dashboard traces")
    ap.add_argument("--sweep", action="store_true",
                    help="run the control-latency sweep instead of the policy matrix")
    args = ap.parse_args(argv)

    out = Path(args.out)
    (out / "runs").mkdir(parents=True, exist_ok=True)
    if args.sweep:
        print("\n" + latency_sweep(out, args.seeds, args.duration))
        return
    rows = []
    for sc in args.scenarios:
        for po in args.policies:
            for seed in range(args.seeds):
                res, trace = run_one(sc, po, seed, args.duration, args.uplink_gbps)
                rows.append(res)
                if seed == 0 and not args.tag:
                    (out / "runs" / f"{sc}__{po}.json").write_text(dumps(trace))
                print(f"{sc:15s} {po:10s} seed={seed} mfu={100 * res['mfu']:.1f}% "
                      f"iter_p50={res['iter_ms_p50'] or float('nan'):.1f}ms ({res['wall_s']}s)", flush=True)
    agg = aggregate(rows)
    md = to_markdown(agg)
    sfx = f"_{args.tag}" if args.tag else ""
    (out / f"summary{sfx}.json").write_text(dumps({
        "assumptions": {"n_xpu": N_XPU, "usd_per_xpu_hour": USD_PER_XPU_HOUR,
                        "measured_from_s": FAULT_T, "duration_s": args.duration,
                        "uplink_gbps": args.uplink_gbps or LeafSpine().uplink_gbps},
        "aggregate": agg, "runs": rows}, indent=1))
    (out / f"summary{sfx}.md").write_text(md + "\n")
    print("\n" + md)


if __name__ == "__main__":
    main()
