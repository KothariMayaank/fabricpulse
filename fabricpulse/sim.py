"""Fluid, time-stepped fabric simulator.

Model (see docs/ARCHITECTURE.md for the reasoning and the known limits):

* Time advances in fixed ticks of ``dt`` (default 100 microseconds). Discrete
  events (job phases, faults, delayed routing updates) live in a heap and fire
  at tick boundaries.
* A flow is a fluid with a sending rate. Each tick, the offered rate on a link
  is the sum of the rates of the flows crossing it. The fabric is lossless
  (PFC-style): a flow's goodput is its rate scaled by the worst
  ``capacity / offered`` ratio on its path.
* Each link keeps a queue that integrates excess demand. Queue depth drives
  ECN marking, and senders react with a DCQCN-style multiplicative decrease
  and staged recovery.
* A link also has a goodput factor ``gamma``. ``gamma < 1`` models a gray
  failure (a dirty optic, a bad NIC): the port stays up at nominal speed, no
  queue builds, but goodput collapses and error counters climb.

Units: seconds, Gbps, Gbit.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .telemetry import Telemetry
from .topology import LeafSpine


@dataclass
class Dcqcn:
    kmin: float = 0.008      # ECN marking starts (Gbit, ~1 MB)
    kmax: float = 0.08       # everything marked (Gbit, ~10 MB)
    buf: float = 0.4         # queue limit (Gbit, ~50 MB)
    g: float = 1 / 16        # alpha gain
    rai_gbps_per_s: float = 200_000.0   # additive increase of the target rate
    rmin: float = 1.0        # rate floor (Gbps)


class Sim:
    def __init__(self, topo: LeafSpine, policy, dt: float = 1e-4, seed: int = 0,
                 cc: Dcqcn | None = None, record_every: int = 1):
        self.topo, self.policy, self.dt = topo, policy, dt
        self.cc = cc or Dcqcn()
        self.rng = np.random.default_rng(seed)
        self.now, self.tick = 0.0, 0
        n1 = topo.n_links + 1

        # Link state (ground truth).
        self.link_up = np.ones(n1, dtype=bool)
        self.gamma = np.ones(n1)
        self.queue = np.zeros(n1)
        self.cap_eff = topo.cap.copy()
        # Nominal demand (line rate per active flow) placed on each link.
        self.load = np.zeros(n1)
        self.last_arrival = np.zeros(n1)
        self.last_err = np.zeros(n1)
        # Monotonic per-link counters (Gbit), like a switch's octet and error
        # counters. Controllers read deltas of these at their own cadence.
        self.cum_arrival = np.zeros(n1)
        self.cum_err = np.zeros(n1)

        # Flow state, struct-of-arrays.
        self._cap_flows = 256
        self.f_active = np.zeros(self._cap_flows, dtype=bool)
        self.f_rate = np.zeros(self._cap_flows)
        self.f_target = np.zeros(self._cap_flows)
        self.f_alpha = np.zeros(self._cap_flows)
        self.f_rem = np.zeros(self._cap_flows)
        self.f_path = np.full((self._cap_flows, 4), topo.NULL, dtype=np.int64)
        self.f_meta: list[dict | None] = [None] * self._cap_flows
        self._free = list(range(self._cap_flows - 1, -1, -1))
        self._idx = np.empty(0, dtype=np.int64)
        self._dirty = False

        self._ev: list = []
        self._seq = itertools.count()

        self.telemetry = Telemetry(topo.n_links, dt, every=record_every)
        self.link_events: list[tuple[float, int, bool]] = []
        self.advisories: list[dict] = []
        self.drain_hosts: set[int] = set()
        self.n_reroutes = 0
        self.workloads: list = []

        policy.attach(self)
        self._ctl = int(getattr(policy, "control_every", 0))

    # ------------------------------------------------------------- events
    def schedule(self, t: float, fn: Callable[[], None]) -> None:
        heapq.heappush(self._ev, (t, next(self._seq), fn))

    def add_workload(self, w) -> None:
        self.workloads.append(w)
        w.attach(self)

    # -------------------------------------------------------------- links
    def set_link(self, link: int, up: bool | None = None, gamma: float | None = None) -> None:
        if gamma is not None:
            self.gamma[link] = gamma
        if up is not None and bool(up) != bool(self.link_up[link]):
            self.link_up[link] = up
            self.cap_eff[link] = self.topo.cap[link] if up else 0.0
            self.link_events.append((self.now, int(link), bool(up)))
            self.policy.on_link_event(int(link), bool(up))

    def advise(self, component: str, kind: str, action: str, **evidence) -> None:
        self.advisories.append({"t": round(self.now, 6), "component": component,
                                "kind": kind, "action": action, "evidence": evidence})

    # -------------------------------------------------------------- flows
    def _grow(self) -> None:
        old, new = self._cap_flows, self._cap_flows * 2
        for name in ("f_active", "f_rate", "f_target", "f_alpha", "f_rem"):
            arr = getattr(self, name)
            grown = np.zeros(new, dtype=arr.dtype)
            grown[:old] = arr
            setattr(self, name, grown)
        path = np.full((new, 4), self.topo.NULL, dtype=np.int64)
        path[:old] = self.f_path
        self.f_path = path
        self.f_meta.extend([None] * old)
        self._free.extend(range(new - 1, old - 1, -1))
        self._cap_flows = new

    def add_flow(self, src: int, dst: int, size_gbit: float, key: bytes,
                 on_done: Callable[[dict], None] | None = None, tag=None) -> int:
        if not self._free:
            self._grow()
        fid = self._free.pop()
        topo = self.topo
        inter = topo.leaf_of(src) != topo.leaf_of(dst)
        meta = {"fid": fid, "src": src, "dst": dst, "size": size_gbit, "key": key,
                "start": self.now, "spine": -1, "inter": inter, "on_done": on_done,
                "tag": tag, "reroutes": 0}
        self.f_meta[fid] = meta
        meta["spine"] = self.policy.choose(src, dst, key) if inter else -1
        self._place(fid, meta)
        line = topo.host_gbps
        self.f_rate[fid] = self.f_target[fid] = line
        self.f_alpha[fid] = 1.0
        self.f_rem[fid] = size_gbit
        self.f_active[fid] = True
        self._dirty = True
        return fid

    def _place(self, fid: int, meta: dict) -> None:
        p = self.topo.path(meta["src"], meta["dst"], meta["spine"])
        self.f_path[fid] = p
        for l in p:
            self.load[l] += self.topo.host_gbps

    def _unplace(self, fid: int) -> None:
        for l in self.f_path[fid]:
            self.load[l] -= self.topo.host_gbps

    def reroute(self, fid: int, spine: int | None = None) -> bool:
        """Move a flow to ``spine``; with None, ask the policy (the flow's own
        load is removed first so the policy sees the fabric without it)."""
        meta = self.f_meta[fid]
        if not meta["inter"]:
            return False
        self._unplace(fid)
        if spine is None:
            spine = self.policy.choose(meta["src"], meta["dst"], meta["key"], current=meta["spine"])
        moved = spine != meta["spine"]
        meta["spine"] = spine
        self._place(fid, meta)
        if moved:
            meta["reroutes"] += 1
            self.n_reroutes += 1
            self.f_rate[fid] = self.f_target[fid] = self.topo.host_gbps
        return moved

    def active_flows(self) -> list[dict]:
        return [self.f_meta[i] for i in np.flatnonzero(self.f_active)]

    def flows_on(self, link: int) -> list[int]:
        idx = np.flatnonzero(self.f_active)
        return [int(i) for i in idx[(self.f_path[idx] == link).any(axis=1)]]

    def _finish(self, fid: int) -> None:
        meta = self.f_meta[fid]
        self._unplace(fid)
        self.f_active[fid] = False
        self.f_meta[fid] = None
        self._free.append(fid)
        self._dirty = True
        meta["fct"] = self.now - meta["start"]
        meta["path"] = tuple(int(x) for x in self.f_path[fid])
        if meta["on_done"]:
            meta["on_done"](meta)

    # --------------------------------------------------------------- loop
    def step(self) -> None:
        ev, dt, cc, topo = self._ev, self.dt, self.cc, self.topo
        while ev and ev[0][0] <= self.now + 1e-12:
            heapq.heappop(ev)[2]()
        if self._ctl and self.tick % self._ctl == 0:
            self.policy.on_tick()
        if self._dirty:
            self._idx = np.flatnonzero(self.f_active)
            self._dirty = False
        idx = self._idx
        n1 = topo.n_links + 1
        cap = self.cap_eff
        q = self.queue

        if idx.size:
            rate = self.f_rate[idx]
            paths = self.f_path[idx]
            arrival = np.bincount(paths.ravel(), weights=np.repeat(rate, 4), minlength=n1)
            arrival[topo.NULL] = 0.0
        else:
            arrival = np.zeros(n1)

        q += (arrival - cap) * dt
        np.clip(q, 0.0, cc.buf, out=q)
        err = np.minimum(arrival, cap) * (1.0 - self.gamma)
        err[topo.NULL] = 0.0
        self.last_arrival, self.last_err = arrival, err
        self.cum_arrival += arrival * dt
        self.cum_err += err * dt

        done = ()
        if idx.size:
            ratio = np.minimum(1.0, cap / np.maximum(arrival, 1e-9))
            eff = ratio[paths].min(axis=1) * self.gamma[paths].prod(axis=1)
            rem = self.f_rem[idx] - rate * eff * dt
            self.f_rem[idx] = rem

            m = np.clip((q[paths].max(axis=1) - cc.kmin) / (cc.kmax - cc.kmin), 0.0, 1.0)
            marked = m > 0
            alpha = (1 - cc.g) * self.f_alpha[idx] + cc.g * marked
            target = self.f_target[idx]
            target = np.where(marked, rate, np.minimum(topo.host_gbps, target + cc.rai_gbps_per_s * dt))
            rate = np.where(marked, rate * (1 - 0.5 * alpha * m), 0.5 * (rate + target))
            np.clip(rate, cc.rmin, topo.host_gbps, out=rate)
            self.f_alpha[idx], self.f_target[idx], self.f_rate[idx] = alpha, target, rate
            done = idx[rem <= 0]

        self.telemetry.record(self.tick, arrival, q, err)
        self.tick += 1
        self.now = self.tick * dt
        for fid in done:
            self._finish(int(fid))

    def run(self, until: float) -> None:
        for _ in range(int(round((until - self.now) / self.dt))):
            self.step()
