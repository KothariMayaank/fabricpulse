"""Named, reproducible experiments. A scenario builds a Sim with workloads and
faults; the same seed gives every policy the same placement and the same
fault, so differences in the result come from the policy alone."""

from __future__ import annotations

import numpy as np

from . import faults
from .policies import POLICIES
from .sim import Sim
from .topology import LeafSpine
from .workloads import InferenceBursts, TrainingJob

FAULT_T = 1.0        # faults start here; results are measured from here on
DURATION = 4.0

SCENARIOS = {
    "healthy": "No faults. Shows the cost of hash collisions alone.",
    "gray_link": "A spine->leaf optic degrades to 30% goodput while staying up.",
    "flapping_link": "A leaf->spine link bounces: 30 ms down every 250 ms, 10 times.",
    "bad_nic": "One training host's NIC degrades to 40% goodput.",
    "noisy_neighbor": "Bursty inference fan-out shares the fabric with training.",
}


def build(scenario: str, policy: str, seed: int = 0, topo: LeafSpine | None = None,
          record_every: int = 1) -> dict:
    if scenario not in SCENARIOS:
        raise KeyError(f"unknown scenario {scenario!r}; choose from {list(SCENARIOS)}")
    topo = topo or LeafSpine()
    rng = np.random.default_rng(1000 + seed)
    sim = Sim(topo, POLICIES[policy](salt=seed), seed=seed, record_every=record_every)

    # A fragmented cluster: the job's workers are scattered across leaves, as
    # they are after a scheduler has been filling and freeing nodes for a while.
    hosts = rng.permutation(topo.n_hosts)
    n_job = (topo.n_hosts * 3) // 4
    job_hosts, rest = hosts[:n_job].tolist(), hosts[n_job:].tolist()
    spares, inf_hosts = rest[:4], rest[4:]
    job = TrainingJob("train-A", job_hosts, spares=spares)
    sim.add_workload(job)
    out = {"sim": sim, "job": job, "inference": None, "fault": None}

    # Fault a link the job actually uses, chosen from its ring.
    a, b = next((s, d) for s, d in zip(job_hosts, job_hosts[1:])
                if topo.leaf_of(s) != topo.leaf_of(d))
    spine = int(rng.integers(topo.n_spine))
    # Jitter the onset so it does not always land in the same job phase.
    t_fault = FAULT_T + float(rng.uniform(0.0, 0.1))
    out["t_fault"] = t_fault
    if scenario == "gray_link":
        link = int(topo.dn[topo.leaf_of(b), spine])
        faults.gray_link(sim, link, t_fault)
        out["fault"] = topo.components[topo.link_component[link]]
    elif scenario == "flapping_link":
        link = int(topo.up[topo.leaf_of(a), spine])
        faults.flapping_link(sim, link, t_fault)
        out["fault"] = topo.components[topo.link_component[link]]
    elif scenario == "bad_nic":
        faults.bad_nic(sim, a, t_fault)
        out["fault"] = f"nic:h{a}"
    elif scenario == "noisy_neighbor":
        inf = InferenceBursts(inf_hosts, seed=seed)
        sim.add_workload(inf)
        out["inference"] = inf
    return out
