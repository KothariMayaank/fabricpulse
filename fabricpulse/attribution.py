"""Job-level fault localisation.

The job knows which transfers were slow; the fabric knows which links each
transfer crossed. Joining the two points at the faulty component without any
link-level error counter, which is the fallback when a fault is invisible to
the switch (or when all you have is the application's view).

Two steps:

1. Explain away congestion. Transfers of one All-Reduce step run at the same
   time, so the paths alone say how many of them shared each link and
   therefore how slow each *should* have been on a healthy fabric. Dividing
   the measured time by that expectation leaves the unexplained slowdown.
   Without this step, an ordinary hash collision outranks a real fault.

2. Score components on what is left. A transfer is "slow" if its unexplained
   slowdown exceeds ``slow_factor``. For each component

       score = precision * (total unexplained excess of slow transfers crossing it)

   where precision is the share of the component's transfers that were slow.
   A healthy link carrying one slow and several fast transfers is diluted by
   precision; among components that only ever carry slow transfers, the one
   that explains the most slowness wins.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import numpy as np

from .topology import LeafSpine


def attribute(iterations: list[dict], topo: LeafSpine, ideal_comm_s: float,
              top: int = 5, slow_factor: float = 1.3) -> list[dict]:
    samples: dict[int, list[float]] = defaultdict(list)
    for it in iterations:
        sharing = Counter(l for f in it["flows"] for l in f["path"] if l != topo.NULL)
        for f in it["flows"]:
            links = [l for l in f["path"] if l != topo.NULL]
            expected = max(max(1.0, sharing[l] * topo.host_gbps / topo.cap[l]) for l in links)
            unexplained = f["fct"] / (ideal_comm_s * expected)
            for comp in {int(topo.link_component[l]) for l in links}:
                samples[comp].append(unexplained)
    ranked = []
    for comp, v in samples.items():
        v = np.asarray(v)
        slow = v > slow_factor
        if slow.any():
            score = slow.mean() * float((v[slow] - 1.0).sum()) / len(iterations)
            ranked.append((score, comp, int(slow.sum()), len(v)))
    ranked.sort(reverse=True)
    return [{"component": topo.components[c], "score": round(s, 3),
             "slow_transfers": ns, "transfers": n} for s, c, ns, n in ranked[:top]]
