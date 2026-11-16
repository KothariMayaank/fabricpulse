"""Per-tick link telemetry, the coarse "polled" view of the same data, and an
online health monitor that turns error counters into quarantine decisions."""

from __future__ import annotations

import numpy as np


class Telemetry:
    """Records offered rate (Gbps), queue depth (Gbit) and error rate (Gbps)
    for every link, once every ``every`` ticks."""

    def __init__(self, n_links: int, dt: float, every: int = 1):
        self.n_links, self.dt, self.every = n_links, dt, every
        self.n = 0
        self._cap = 4096
        self._arrival = np.zeros((self._cap, n_links), dtype=np.float32)
        self._queue = np.zeros((self._cap, n_links), dtype=np.float32)
        self._err = np.zeros((self._cap, n_links), dtype=np.float32)

    def record(self, tick: int, arrival: np.ndarray, queue: np.ndarray, err: np.ndarray) -> None:
        if tick % self.every:
            return
        if self.n == self._cap:
            self._cap *= 2
            for name in ("_arrival", "_queue", "_err"):
                old = getattr(self, name)
                new = np.zeros((self._cap, self.n_links), dtype=np.float32)
                new[: self.n] = old
                setattr(self, name, new)
        nl = self.n_links
        self._arrival[self.n] = arrival[:nl]
        self._queue[self.n] = queue[:nl]
        self._err[self.n] = err[:nl]
        self.n += 1

    @property
    def period(self) -> float:
        return self.dt * self.every

    @property
    def t(self) -> np.ndarray:
        return np.arange(self.n) * self.period

    @property
    def arrival(self) -> np.ndarray:
        return self._arrival[: self.n]

    @property
    def queue(self) -> np.ndarray:
        return self._queue[: self.n]

    @property
    def err(self) -> np.ndarray:
        return self._err[: self.n]

    def polled(self, series: np.ndarray, interval_s: float) -> np.ndarray:
        """What a poller averaging over ``interval_s`` would report."""
        k = max(1, int(round(interval_s / self.period)))
        n = (series.shape[0] // k) * k
        if n == 0:
            return series.mean(axis=0, keepdims=True)
        return series[:n].reshape(n // k, k, -1).mean(axis=1)

    def resolution_gap(self, cap: np.ndarray, kmin: float, poll_s: float = 1.0) -> dict:
        """Compare the full-resolution view with a polled view on the link
        that spent the most time congested. This is the 'what coarse polling
        hides' number."""
        util = self.arrival / cap[: self.n_links]
        congested = (self.queue > kmin).mean(axis=0)
        link = int(congested.argmax())
        polled = self.polled(util[:, [link]], poll_s)
        return {
            "link": link,
            "resolution_us": round(self.period * 1e6),
            "poll_s": poll_s,
            "peak_util_full_res": float(util[:, link].max()),
            "peak_util_polled": float(polled.max()),
            "time_congested_frac": float(congested[link]),
            "congestion_episodes": int((np.diff((self.queue[:, link] > kmin).astype(np.int8)) == 1).sum()),
        }


class HealthMonitor:
    """EWMA of each link's error ratio (errored / offered). A link whose ratio
    crosses ``err_thr`` while carrying real traffic is declared sick. The
    verdict is sticky: once traffic is steered away there is nothing left to
    measure, so clearing it is an operator decision."""

    def __init__(self, n_links: int, err_thr: float = 0.02, gain: float = 0.5, min_gbps: float = 5.0):
        self.err_thr, self.gain, self.min_gbps = err_thr, gain, min_gbps
        self.a = np.zeros(n_links + 1)
        self.e = np.zeros(n_links + 1)
        self.sick = np.zeros(n_links + 1, dtype=bool)

    def update(self, arrival: np.ndarray, err: np.ndarray) -> np.ndarray:
        """Feed one sample; returns indices of links newly declared sick."""
        live = arrival > 0          # only learn while the link carries traffic
        k = self.gain
        self.a[live] += k * (arrival[live] - self.a[live])
        self.e[live] += k * (err[live] - self.e[live])
        ratio = self.e / np.maximum(self.a, 1e-9)
        new = (ratio > self.err_thr) & (self.a > self.min_gbps) & ~self.sick
        self.sick |= new
        return np.flatnonzero(new)

    def ratio(self) -> np.ndarray:
        return self.e / np.maximum(self.a, 1e-9)
