"""Fault injection. Each helper schedules state changes on a Sim."""

from __future__ import annotations


def gray_link(sim, link: int, t: float, gamma: float = 0.3) -> None:
    """Dirty optic / marginal cable: port stays up, goodput drops to ``gamma``."""
    sim.schedule(t, lambda: sim.set_link(link, gamma=gamma))


def flapping_link(sim, link: int, t: float, period_s: float = 0.25, down_s: float = 0.03,
                  count: int = 10) -> None:
    """Link bounces: down for ``down_s`` once every ``period_s``."""
    for i in range(count):
        t0 = t + i * period_s
        sim.schedule(t0, lambda: sim.set_link(link, up=False))
        sim.schedule(t0 + down_s, lambda: sim.set_link(link, up=True))


def bad_nic(sim, host: int, t: float, gamma: float = 0.4) -> None:
    """A NIC degrades in both directions. No route avoids it."""
    def apply():
        sim.set_link(int(sim.topo.host_up[host]), gamma=gamma)
        sim.set_link(int(sim.topo.host_dn[host]), gamma=gamma)
    sim.schedule(t, apply)
