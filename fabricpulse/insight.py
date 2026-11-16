"""Read-only questions about a finished run, answered from network state.

These are the functions the MCP server exposes. They are kept free of any MCP
dependency so they can be tested and used from plain Python. Nothing here
changes routing: the answers are advice for an operator or a job scheduler.
"""

from __future__ import annotations

import numpy as np

from .attribution import attribute
from .scenarios import DURATION, FAULT_T, SCENARIOS, build

ERR_THR = 0.02       # same threshold the online health monitor uses


class Session:
    """One simulated run plus the questions that can be asked of it."""

    def __init__(self, scenario: str = "gray_link", policy: str = "ecmp", seed: int = 0,
                 duration: float = DURATION):
        self.scenario, self.policy, self.seed, self.duration = scenario, policy, seed, duration
        b = build(scenario, policy, seed)
        self.sim, self.job, self.inference = b["sim"], b["job"], b["inference"]
        self._truth = {"fault": b["fault"], "t_fault": b.get("t_fault")}
        self.sim.run(duration)

    # ---- helpers --------------------------------------------------------
    def _err_ratio(self) -> np.ndarray:
        s = self.sim
        return s.cum_err / np.maximum(s.cum_arrival, 1e-9)

    def _quarantined(self) -> np.ndarray:
        q = getattr(self.sim.policy, "quarantined", None)
        return q if q is not None else np.zeros(self.sim.topo.n_links + 1, dtype=bool)

    def _link_row(self, link: int) -> dict:
        s, t, tel = self.sim, self.sim.topo, self.sim.telemetry
        util = tel.arrival[:, link] / t.cap[link]
        return {
            "link": t.link_names[link],
            "component": t.components[t.link_component[link]],
            "up": bool(s.link_up[link]),
            "quarantined": bool(self._quarantined()[link]),
            "error_ratio": round(float(self._err_ratio()[link]), 4),
            "down_events": sum(1 for _, l, up in s.link_events if l == link and not up),
            "peak_utilisation": round(float(util.max()), 2),
            "mean_utilisation": round(float(util.mean()), 3),
            "time_congested_frac": round(float((tel.queue[:, link] > s.cc.kmin).mean()), 4),
        }

    # ---- questions ------------------------------------------------------
    def summary(self) -> dict:
        js = self.job.stats(t0=FAULT_T)
        out = {"scenario": self.scenario, "description": SCENARIOS[self.scenario],
               "policy": self.policy, "seed": self.seed, "duration_s": self.duration,
               "job": self.job.name, "workers": len(self.job.hosts),
               "mfu_pct": round(100 * js["mfu"], 2), "ideal_mfu_pct": round(100 * js["ideal_mfu"], 2),
               "mfu_lost_to_network_pct": round(100 * js["mfu_loss_rel"], 1),
               "iteration_ms_p50": round(js["iter_ms_p50"], 1) if js["iterations"] else None,
               "ideal_iteration_ms": round(js["ideal_iter_ms"], 1),
               "reroutes": self.sim.n_reroutes, "advisories": len(self.sim.advisories)}
        if self.inference:
            out["inference"] = self.inference.stats(t0=FAULT_T)
        return out

    def fabric_health(self, top: int = 8) -> dict:
        """Links that need attention, worst first, plus fabric-wide counts."""
        s, t = self.sim, self.sim.topo
        rows = [self._link_row(l) for l in range(t.n_links)]
        bad = lambda r: (not r["up"], r["quarantined"], r["error_ratio"] > ERR_THR, r["down_events"],
                         r["time_congested_frac"])
        rows.sort(key=bad, reverse=True)
        flagged = [r for r in rows if not r["up"] or r["quarantined"] or r["error_ratio"] > ERR_THR
                   or r["down_events"] or r["time_congested_frac"] > 0.01]
        return {
            "links_total": t.n_links,
            "links_with_errors": sum(r["error_ratio"] > ERR_THR for r in rows),
            "links_that_flapped": sum(r["down_events"] > 0 for r in rows),
            "links_quarantined": sum(r["quarantined"] for r in rows),
            "links_congested_over_1pct_of_time": sum(r["time_congested_frac"] > 0.01 for r in rows),
            "needs_attention": flagged[:top],
        }

    def advisories(self) -> list[dict]:
        return list(self.sim.advisories)

    def explain_job_slowdown(self) -> dict:
        """Why the job is slower than its ideal, with the evidence."""
        js = self.job.stats(t0=FAULT_T)
        post = [it for it in self.job.iterations if it["t_end"] > FAULT_T]
        suspects = attribute(post, self.sim.topo, self.job.ideal_comm_s, top=3)
        ratio, t = self._err_ratio(), self.sim.topo
        for sp in suspects:
            links = [l for l in range(t.n_links) if t.components[t.link_component[l]] == sp["component"]]
            sp["error_ratio"] = round(float(max(ratio[l] for l in links)), 4)
            sp["corroborated_by_error_counters"] = sp["error_ratio"] > ERR_THR
        # Congestion: how much of the slowdown path sharing alone accounts for.
        shared = []
        for it in post:
            counts: dict[int, int] = {}
            for f in it["flows"]:
                for l in f["path"]:
                    if l != t.NULL:
                        counts[l] = counts.get(l, 0) + 1
            shared.append(max(max(1.0, n * t.host_gbps / t.cap[l]) for l, n in counts.items()))
        worst_share = float(np.median(shared)) if shared else 1.0
        slowdown = (js["iter_ms_p50"] or 0) / js["ideal_iter_ms"] if js["iterations"] else None
        if js["mfu_loss_rel"] < 0.02:
            verdict = "The job is running within 2% of its ideal. The network is not holding it back."
        elif suspects and suspects[0]["corroborated_by_error_counters"]:
            verdict = (f"A faulty component is the main cause: {suspects[0]['component']}. Transfers crossing it "
                       f"are slower than path sharing explains, and its error counters agree.")
        elif suspects:
            verdict = (f"Transfers crossing {suspects[0]['component']} are slower than path sharing explains, "
                       f"but its error counters are clean. Treat as a lead, not a finding.")
        else:
            verdict = (f"No faulty component. The slowdown is congestion: on the busiest link, transfers of one "
                       f"All-Reduce step ask for {worst_share:.2f}x its capacity, and the whole job waits for them.")
        return {"job": self.job.name, "mfu_pct": round(100 * js["mfu"], 2),
                "mfu_lost_to_network_pct": round(100 * js["mfu_loss_rel"], 1),
                "iteration_slowdown": round(slowdown, 2) if slowdown else None,
                "worst_path_sharing": round(worst_share, 2),
                "suspects": suspects, "migrations": self.job.migrations,
                "advisories": self.advisories(), "verdict": verdict}

    def recommend_placement(self, n_workers: int) -> dict:
        """Hosts a scheduler should prefer or avoid for a new job, from
        current network health. Advisory only."""
        t, ratio, quar = self.sim.topo, self._err_ratio(), self._quarantined()
        avoid = {}
        for h in range(t.n_hosts):
            r = max(ratio[t.host_up[h]], ratio[t.host_dn[h]])
            if h in self.sim.drain_hosts or r > ERR_THR:
                avoid[h] = f"NIC error ratio {r:.2f}"
        leaf_score = {}
        for l in range(t.n_leaf):
            links = list(t.up[l]) + list(t.dn[l])
            healthy = sum(1 for k in links if self.sim.link_up[k] and not quar[k] and ratio[k] <= ERR_THR)
            leaf_score[l] = healthy / len(links)
        busy = set(self.job.hosts)
        free = [h for h in range(t.n_hosts) if h not in busy and h not in avoid]
        # Healthiest leaves first, and pack by leaf: transfers inside a leaf
        # never touch the spine layer.
        free.sort(key=lambda h: (-leaf_score[t.leaf_of(h)], t.leaf_of(h), h))
        chosen = free[:n_workers]
        return {"requested": n_workers, "recommended_hosts": [f"h{h}" for h in chosen],
                "enough_capacity": len(chosen) == n_workers,
                "avoid_hosts": {f"h{h}": why for h, why in avoid.items()},
                "degraded_leaves": {f"L{l}": f"{s:.0%} of fabric links healthy"
                                    for l, s in leaf_score.items() if s < 1.0},
                "note": "Packs workers onto the fewest, healthiest leaves. Advice only; nothing was changed."}
