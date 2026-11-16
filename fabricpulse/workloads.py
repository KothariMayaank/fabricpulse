"""Workloads that generate traffic and report what the network cost them."""

from __future__ import annotations

import numpy as np


class TrainingJob:
    """Synchronous distributed training.

    Each iteration is a compute phase (GPUs busy, network idle) followed by a
    ring All-Reduce step: every worker sends ``msg_gbit`` to its ring
    neighbour, and nobody resumes computing until the *slowest* transfer has
    finished. That barrier is why a single bad link or NIC taxes the whole job.

    MFU = eta * compute_time / wall_time, where ``eta`` is the utilisation the
    accelerators reach while they are actually computing.
    """

    def __init__(self, name: str, hosts, compute_s: float = 0.06, msg_gbit: float = 8.0,
                 eta: float = 0.55, spares=(), start: float = 0.0, follow_advisories: bool = True):
        self.name, self.hosts = name, list(hosts)
        self.compute_s, self.msg_gbit, self.eta = compute_s, msg_gbit, eta
        self.spares, self.start = list(spares), start
        self.follow_advisories = follow_advisories
        self.iterations: list[dict] = []
        self.migrations: list[dict] = []

    def attach(self, sim) -> None:
        self.sim = sim
        self.ideal_comm_s = self.msg_gbit / sim.topo.host_gbps
        sim.schedule(self.start, self._begin_compute)

    def _begin_compute(self) -> None:
        sim = self.sim
        if self.follow_advisories and sim.drain_hosts:
            for i, h in enumerate(self.hosts):
                if h in sim.drain_hosts and self.spares:
                    new = self.spares.pop(0)
                    self.hosts[i] = new
                    self.migrations.append({"t": round(sim.now, 6), "from": h, "to": new})
        self._t0 = sim.now
        sim.schedule(sim.now + self.compute_s, self._begin_comm)

    def _begin_comm(self) -> None:
        sim, n = self.sim, len(self.hosts)
        self._t_comm = sim.now
        self._flows: list[dict] = []
        self._pending = n
        for i, src in enumerate(self.hosts):
            dst = self.hosts[(i + 1) % n]
            sim.add_flow(src, dst, self.msg_gbit, key=f"{self.name}:{src}>{dst}".encode(),
                         on_done=self._flow_done, tag=self.name)

    def _flow_done(self, m: dict) -> None:
        self._flows.append({"src": m["src"], "dst": m["dst"], "path": m["path"],
                            "fct": m["fct"], "reroutes": m["reroutes"]})
        self._pending -= 1
        if self._pending == 0:
            now = self.sim.now
            self.iterations.append({"t_start": self._t0, "t_end": now, "compute_s": self.compute_s,
                                    "comm_s": now - self._t_comm, "flows": self._flows})
            self._begin_compute()

    # ---- reporting ------------------------------------------------------
    def stats(self, t0: float = 0.0, t1: float = np.inf) -> dict:
        # An iteration counts if it *ends* in the window, so the iteration a
        # fault lands in is charged to the post-fault result.
        its = [it for it in self.iterations if t0 < it["t_end"] <= t1]
        ideal_iter = self.compute_s + self.ideal_comm_s
        ideal_mfu = self.eta * self.compute_s / ideal_iter
        if not its:
            # No iteration completed in the window: the job was stalled.
            return {"iterations": 0, "mfu": 0.0, "ideal_mfu": ideal_mfu, "mfu_loss_rel": 1.0,
                    "iter_ms_p50": None, "iter_ms_p99": None, "comm_ms_p50": None,
                    "ideal_iter_ms": ideal_iter * 1e3}
        wall = np.array([it["t_end"] - it["t_start"] for it in its])
        comm = np.array([it["comm_s"] for it in its])
        mfu = self.eta * self.compute_s * len(its) / wall.sum()
        return {"iterations": len(its), "mfu": float(mfu), "ideal_mfu": float(ideal_mfu),
                "mfu_loss_rel": float(1 - mfu / ideal_mfu),
                "iter_ms_p50": float(np.percentile(wall, 50) * 1e3),
                "iter_ms_p99": float(np.percentile(wall, 99) * 1e3),
                "comm_ms_p50": float(np.percentile(comm, 50) * 1e3),
                "ideal_iter_ms": ideal_iter * 1e3}


class InferenceBursts:
    """Bursty inference traffic: at Poisson-distributed instants, ``fanout``
    prefill->decode host pairs each start a KV-cache transfer at once. The
    pairs are disjoint, so any slowdown over an empty network comes from the
    fabric and not from two transfers sharing one NIC."""

    def __init__(self, hosts, burst_rate_hz: float = 20.0, fanout: int = 4,
                 size_gbit=(0.4, 4.0), seed: int = 0, start: float = 0.0):
        self.hosts = list(hosts)
        self.rate, self.fanout, self.size, self.start = burst_rate_hz, fanout, size_gbit, start
        self.rng = np.random.default_rng(seed)
        self.done: list[dict] = []
        self._n = 0

    def attach(self, sim) -> None:
        self.sim = sim
        sim.schedule(self.start + self.rng.exponential(1 / self.rate), self._burst)

    def _burst(self) -> None:
        sim = self.sim
        k = min(self.fanout, len(self.hosts) // 2)
        picks = self.rng.choice(self.hosts, size=2 * k, replace=False)
        for src, dst in zip(picks[:k], picks[k:]):
            self._n += 1
            sim.add_flow(int(src), int(dst), float(self.rng.uniform(*self.size)),
                         key=f"inf:{self._n}".encode(), on_done=self._flow_done, tag="inference")
        sim.schedule(sim.now + self.rng.exponential(1 / self.rate), self._burst)

    def _flow_done(self, m: dict) -> None:
        ideal = m["size"] / self.sim.topo.host_gbps
        self.done.append({"t": m["start"], "fct": m["fct"], "slowdown": m["fct"] / ideal})

    def stats(self, t0: float = 0.0, t1: float = np.inf) -> dict:
        s = np.array([d["slowdown"] for d in self.done if t0 <= d["t"] <= t1])
        if not s.size:
            return {"flows": 0, "slowdown_p50": None, "slowdown_p99": None}
        return {"flows": int(s.size), "slowdown_p50": float(np.percentile(s, 50)),
                "slowdown_p99": float(np.percentile(s, 99))}
