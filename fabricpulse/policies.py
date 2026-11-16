"""Path-selection policies. In a leaf-spine fabric the only routing decision
for a flow between two leaves is which spine it crosses.

    Ecmp            static 5-tuple hash. RoCE flows have little entropy and a
                    queue pair keeps its tuple, so a bad hash is permanent.
    Dlb             dynamic load balancing with local knowledge: each leaf
                    spreads new flowlets over its own uplinks by load, but is
                    blind to the spine->leaf hop and to gray failures.
    TelemetryAware  closed loop over fabric-wide telemetry: global load on
                    both hops, error-counter health, flap dampening, millisecond
                    reaction, mid-flow rebalancing and advisories to the
                    job scheduler for faults routing cannot fix.

Every policy keeps its own *view* of link liveness, updated some delay after
the truth changes. That delay is the difference between waiting on routing
protocol reconvergence and reacting from telemetry.
"""

from __future__ import annotations

import zlib

import numpy as np

from .telemetry import HealthMonitor


class Policy:
    name = "base"
    control_every = 0        # ticks between on_tick() calls; 0 disables

    def __init__(self, reconverge_s: float = 0.1, salt: int = 0):
        self.reconverge_s, self.salt = reconverge_s, salt

    def attach(self, sim) -> None:
        self.sim, self.topo = sim, sim.topo
        self.alive = np.ones(sim.topo.n_links + 1, dtype=bool)

    def delay(self, link: int) -> float:
        return self.reconverge_s

    def on_link_event(self, link: int, up: bool) -> None:
        self.sim.schedule(self.sim.now + self.delay(link), lambda: self._apply(link, up))

    def _apply(self, link: int, up: bool) -> None:
        self.alive[link] = up
        self.on_view_change()

    def usable(self, ls: int, ld: int) -> np.ndarray:
        return self.alive[self.topo.up[ls]] & self.alive[self.topo.dn[ld]]

    def _hash(self, key: bytes) -> int:
        return zlib.crc32(key, self.salt)

    def _stranded(self) -> list[int]:
        """Active inter-leaf flows whose current path is dead in our view."""
        out = []
        for m in self.sim.active_flows():
            if m["inter"]:
                ls, ld = self.topo.leaf_of(m["src"]), self.topo.leaf_of(m["dst"])
                if not self.usable(ls, ld)[m["spine"]]:
                    out.append(m["fid"])
        return out

    def on_view_change(self) -> None:
        for fid in self._stranded():
            self.sim.reroute(fid)

    def on_tick(self) -> None:
        pass

    def choose(self, src: int, dst: int, key: bytes, current: int | None = None) -> int:
        raise NotImplementedError


class Ecmp(Policy):
    name = "ecmp"

    def choose(self, src, dst, key, current=None):
        ok = np.flatnonzero(self.usable(self.topo.leaf_of(src), self.topo.leaf_of(dst)))
        if not ok.size:
            ok = np.arange(self.topo.n_spine)
        return int(ok[self._hash(key) % ok.size])

    def on_view_change(self):
        # Hash-mod-N: a membership change remaps flows that were not on the
        # failed path as well.
        for m in self.sim.active_flows():
            if m["inter"]:
                self.sim.reroute(m["fid"])


class Dlb(Policy):
    name = "dlb"

    def __init__(self, reconverge_s: float = 0.1, local_s: float = 1e-3, salt: int = 0):
        super().__init__(reconverge_s, salt)
        self.local_s = local_s

    def delay(self, link):
        # A leaf sees its own uplink go down immediately; the far hop needs
        # the routing protocol.
        t = self.topo
        local = t.up.min() <= link <= t.up.max()
        return self.local_s if local else self.reconverge_s

    def choose(self, src, dst, key, current=None):
        t, sim = self.topo, self.sim
        ls, ld = t.leaf_of(src), t.leaf_of(dst)
        ok = self.usable(ls, ld)
        if not ok.any():
            ok = np.ones(t.n_spine, dtype=bool)
        up = t.up[ls]
        cost = sim.load[up] / t.uplink_gbps + sim.queue[up] / sim.cc.kmax
        cost = cost + 1e-6 * ((np.arange(t.n_spine) + self._hash(key)) % t.n_spine)
        cost[~ok] = np.inf
        return int(cost.argmin())


class TelemetryAware(Policy):
    name = "telemetry"

    def __init__(self, react_s: float | None = None, control_s: float = 1e-3, err_thr: float = 0.02,
                 flap_count: int = 2, flap_window_s: float = 1.0, salt: int = 0):
        # control_s is the telemetry/control period. By default link events
        # are also acted on with that latency, so one knob sweeps the loop
        # from "millisecond agent" to "30-second poller".
        super().__init__(control_s if react_s is None else react_s, salt)
        self.control_s, self.err_thr = control_s, err_thr
        self.flap_count, self.flap_window_s = flap_count, flap_window_s

    def attach(self, sim):
        super().attach(sim)
        self.control_every = max(1, int(round(self.control_s / sim.dt)))
        self.monitor = HealthMonitor(sim.topo.n_links, err_thr=self.err_thr)
        self.quarantined = np.zeros(sim.topo.n_links + 1, dtype=bool)
        self._seen_arrival = sim.cum_arrival.copy()
        self._seen_err = sim.cum_err.copy()
        self._downs: dict[int, list[float]] = {}

    # ---- view -----------------------------------------------------------
    def _apply(self, link, up):
        if not up:
            ts = [x for x in self._downs.get(link, []) if x > self.sim.now - self.flap_window_s]
            ts.append(self.sim.now)
            self._downs[link] = ts
            if len(ts) >= self.flap_count and not self.quarantined[link]:
                self._quarantine(link, "flapping", downs_in_window=len(ts), window_s=self.flap_window_s)
        super()._apply(link, up)

    def usable(self, ls, ld):
        t = self.topo
        alive = self.alive[t.up[ls]] & self.alive[t.dn[ld]]
        clean = alive & ~self.quarantined[t.up[ls]] & ~self.quarantined[t.dn[ld]]
        return clean if clean.any() else alive

    def _quarantine(self, link: int, kind: str, **evidence) -> None:
        self.quarantined[link] = True
        t, sim = self.topo, self.sim
        comp = t.components[t.link_component[link]]
        host = t.host_of_link(link)
        if host is None:
            sim.advise(comp, kind, "quarantined link, traffic steered away; inspect optic/cable", **evidence)
        elif host not in sim.drain_hosts:
            # Routing cannot avoid a NIC. Tell the scheduler instead.
            sim.drain_hosts.add(host)
            sim.advise(comp, kind, "cannot route around a NIC; advising scheduler to drain host", **evidence)

    # ---- decisions ------------------------------------------------------
    def choose(self, src, dst, key, current=None):
        t, sim = self.topo, self.sim
        ls, ld = t.leaf_of(src), t.leaf_of(dst)
        ok = self.usable(ls, ld)
        if not ok.any():
            ok = np.ones(t.n_spine, dtype=bool)
        r = t.host_gbps
        cost = np.maximum(sim.load[t.up[ls]] + r, sim.load[t.dn[ld]] + r) / t.uplink_gbps
        cost = cost + 1e-6 * ((np.arange(t.n_spine) + self._hash(key)) % t.n_spine)
        if current is not None and current >= 0:
            cost[current] -= 1e-3          # stickiness: do not move on ties
        cost[~ok] = np.inf
        return int(cost.argmin())

    def on_tick(self):
        sim, t = self.sim, self.topo
        # Counter deltas since the last control tick, as average rates.
        span = self.control_every * sim.dt
        d_arrival = (sim.cum_arrival - self._seen_arrival) / span
        d_err = (sim.cum_err - self._seen_err) / span
        self._seen_arrival[:] = sim.cum_arrival
        self._seen_err[:] = sim.cum_err
        new = self.monitor.update(d_arrival, d_err)
        if new.size:
            ratios = self.monitor.ratio()
            for link in new:
                self._quarantine(int(link), "errors", err_ratio=round(float(ratios[link]), 3))
            self.on_view_change()
        # Rebalance: relieve fabric links whose placed demand exceeds capacity.
        over = np.flatnonzero(t.is_fabric & (sim.load > t.cap * 1.01))
        for link in over:
            for fid in sim.flows_on(int(link)):
                if sim.load[link] <= t.cap[link] * 1.01:
                    break
                sim.reroute(fid)


POLICIES = {"ecmp": Ecmp, "dlb": Dlb, "telemetry": TelemetryAware}
