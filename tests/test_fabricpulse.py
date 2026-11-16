import numpy as np
import pytest

from fabricpulse import Dlb, Ecmp, LeafSpine, Sim, TelemetryAware, TrainingJob
from fabricpulse.attribution import attribute
from fabricpulse.scenarios import SCENARIOS, build
from fabricpulse.telemetry import HealthMonitor


def small():
    return LeafSpine(n_leaf=2, n_spine=2, hosts_per_leaf=4, host_gbps=400, uplink_gbps=800)


def run_flows(sim, flows, until=1.0):
    done = []
    for src, dst, size, key in flows:
        sim.add_flow(src, dst, size, key, on_done=done.append)
    sim.run(until)
    return done


def test_topology_indices_are_unique_and_paths_have_four_links():
    t = LeafSpine()
    idx = np.concatenate([t.host_up, t.host_dn, t.up.ravel(), t.dn.ravel()])
    assert sorted(idx) == list(range(t.n_links))
    assert t.path(0, 1, 0)[2:] == (t.NULL, t.NULL)          # same leaf
    assert t.NULL not in t.path(0, t.hosts_per_leaf, 1)     # across leaves


def test_single_flow_finishes_in_ideal_time():
    sim = Sim(small(), Ecmp())
    (m,) = run_flows(sim, [(0, 4, 8.0, b"a")])
    assert m["fct"] == pytest.approx(8.0 / 400, abs=2 * sim.dt)


def test_goodput_never_exceeds_capacity_and_flows_share():
    # Three 400G senders into one 400G receiver NIC.
    sim = Sim(small(), Ecmp())
    done = run_flows(sim, [(0, 7, 8.0, b"a"), (1, 7, 8.0, b"b"), (2, 7, 8.0, b"c")])
    assert len(done) == 3
    last = max(m["fct"] for m in done)
    assert last >= 24.0 / 400 - 2 * sim.dt        # cannot beat the bottleneck
    assert last < 1.5 * 24.0 / 400                # congestion control wastes < 50%


def test_gray_link_scales_goodput_without_building_a_queue():
    sim = Sim(small(), Ecmp())
    link = int(sim.topo.host_up[0])
    sim.set_link(link, gamma=0.5)
    (m,) = run_flows(sim, [(0, 4, 8.0, b"a")])
    assert m["fct"] == pytest.approx(2 * 8.0 / 400, abs=2 * sim.dt)
    assert sim.telemetry.queue[:, link].max() == 0
    assert sim.telemetry.err[:, link].max() == pytest.approx(200.0)


def test_down_link_stalls_ecmp_until_reconvergence_but_not_telemetry():
    fct = {}
    for policy in (Ecmp(reconverge_s=0.1), TelemetryAware()):
        sim = Sim(small(), policy)
        sim.add_flow(0, 4, 8.0, b"a", on_done=lambda m, p=policy: fct.__setitem__(p.name, m["fct"]))
        spine = sim.active_flows()[0]["spine"]
        sim.schedule(0.005, lambda s=sim, sp=spine: s.set_link(int(s.topo.up[0, sp]), up=False))
        sim.run(0.5)
    assert fct["ecmp"] > 0.1
    assert fct["telemetry"] < 0.03


def test_dlb_balances_local_uplinks():
    sim = Sim(small(), Dlb())
    for i in range(4):
        sim.add_flow(i, 4 + i, 8.0, f"k{i}".encode())
    assert sim.load[sim.topo.up[0]].tolist() == [800.0, 800.0]


def test_telemetry_policy_avoids_overload_on_both_hops():
    t = LeafSpine(n_leaf=4, n_spine=2, hosts_per_leaf=4)
    sim = Sim(t, TelemetryAware())
    # Leaves 0 and 1 each send two flows into leaf 2: the spine->leaf hop is
    # where local-only balancing can collide.
    for i, src in enumerate((0, 1, 4, 5)):
        sim.add_flow(src, 8 + i, 8.0, f"k{i}".encode())
    assert (sim.load[t.is_fabric] <= t.uplink_gbps).all()


def test_health_monitor_flags_only_the_erroring_link():
    mon = HealthMonitor(4)
    arrival = np.array([400.0, 400.0, 0.0, 400.0, 0.0])
    err = np.array([0.0, 120.0, 0.0, 0.0, 0.0])
    new = np.concatenate([mon.update(arrival, err) for _ in range(10)])
    assert new.tolist() == [1]
    assert mon.sick.tolist() == [False, True, False, False, False]


def test_flow_array_grows_past_initial_capacity():
    sim = Sim(small(), Ecmp())
    done = run_flows(sim, [(i % 4, 4 + i % 4, 0.01, f"k{i}".encode()) for i in range(600)], until=0.5)
    assert len(done) == 600
    assert np.allclose(sim.load, 0)


def test_training_job_reaches_ideal_mfu_on_a_clean_fabric():
    sim = Sim(small(), TelemetryAware())
    job = TrainingJob("j", [0, 4, 1, 5])
    sim.add_workload(job)
    sim.run(1.0)
    s = job.stats()
    assert s["iterations"] >= 10
    assert s["mfu"] == pytest.approx(s["ideal_mfu"], rel=0.01)


@pytest.mark.parametrize("scenario", ["gray_link", "bad_nic"])
def test_attribution_names_the_faulted_component(scenario):
    b = build(scenario, "ecmp", seed=0)
    b["sim"].run(2.0)
    post = [it for it in b["job"].iterations if it["t_start"] > b["t_fault"]]
    suspects = attribute(post, b["sim"].topo, b["job"].ideal_comm_s)
    assert suspects[0]["component"] == b["fault"]


@pytest.mark.parametrize("scenario", ["gray_link", "flapping_link", "bad_nic"])
def test_telemetry_policy_detects_fault_without_false_alarms(scenario):
    b = build(scenario, "telemetry", seed=0)
    b["sim"].run(2.0)
    assert [a["component"] for a in b["sim"].advisories] == [b["fault"]]
    if scenario == "bad_nic":
        assert len(b["job"].migrations) == 1


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_telemetry_policy_is_never_worse_than_ecmp(scenario):
    mfu = {}
    for policy in ("ecmp", "telemetry"):
        b = build(scenario, policy, seed=1)
        b["sim"].run(2.0)
        mfu[policy] = b["job"].stats(t0=1.0)["mfu"]
    assert mfu["telemetry"] >= mfu["ecmp"] - 1e-9


# ---- insight (the functions behind the MCP server) -----------------------
from fabricpulse.insight import Session


def test_insight_blames_the_gray_link_and_error_counters_agree():
    s = Session("gray_link", "ecmp", seed=0, duration=2.0)
    out = s.explain_job_slowdown()
    top = out["suspects"][0]
    assert top["component"] == s._truth["fault"]
    assert top["corroborated_by_error_counters"]
    assert "faulty component" in out["verdict"]
    health = s.fabric_health()
    assert health["links_with_errors"] == 1
    assert health["needs_attention"][0]["component"] == s._truth["fault"]


def test_insight_calls_a_healthy_slow_job_congestion_not_a_fault():
    out = Session("healthy", "ecmp", seed=0, duration=2.0).explain_job_slowdown()
    assert out["suspects"] == []
    assert "congestion" in out["verdict"]
    assert out["worst_path_sharing"] > 1.0


def test_insight_placement_avoids_the_bad_nic():
    s = Session("bad_nic", "ecmp", seed=0, duration=2.0)
    bad = s._truth["fault"].split(":")[1]
    rec = s.recommend_placement(4)
    assert bad in rec["avoid_hosts"]
    assert bad not in rec["recommended_hosts"]
    assert len(rec["recommended_hosts"]) == 4
