# FabricPulse

**How much GPU time does the network waste, and how much of it can a telemetry-driven control loop win back?**

FabricPulse is a simulator of an AI-cluster fabric. It runs a distributed training job (and bursty inference traffic) over a leaf-spine network, injects the faults that hurt real clusters, and measures the result in the unit that matters to whoever pays for the GPUs: **Model FLOPS Utilization (MFU)**.

It compares three ways of steering traffic:

| policy | what it knows | how fast it reacts |
|---|---|---|
| `ecmp` | a static hash of the flow | routing reconvergence (100 ms) |
| `dlb` | load on the local leaf's own uplinks | 1 ms locally, 100 ms for the far hop |
| `telemetry` | fabric-wide load, per-link error counters, flap history | 1 ms, from high-resolution telemetry |

The third one is a small closed loop: it measures, detects, steers traffic, and when routing cannot fix the problem (a bad NIC) it issues an advisory that the job scheduler acts on.

## Results

64 GPUs' worth of hosts, 8 leaves, 4 spines, 400G hosts, 800G uplinks (non-blocking). One 48-worker training job doing ring All-Reduce, scattered across the cluster. Mean of 3 seeds, measured from the moment the fault starts. The ideal MFU on a perfect network is 41.2%.

| scenario | policy | MFU % | loss vs ideal % | iteration p50 ms | iteration p99 ms |
|---|---|---|---|---|---|
| healthy | ecmp | 32.6 | 21.0 | 102.0 | 102.0 |
| healthy | dlb | 33.7 | 18.4 | 98.3 | 98.3 |
| healthy | telemetry | 41.2 | 0.0 | 80.0 | 80.0 |
| gray link (optic at 30% goodput) | ecmp | 25.4 | 38.4 | 134.5 | 134.5 |
| gray link | dlb | 28.4 | 31.2 | 118.4 | 118.4 |
| gray link | telemetry | 41.2 | 0.0 | 80.0 | 80.3 |
| flapping link | ecmp | 32.1 | 22.2 | 102.0 | 115.5 |
| flapping link | dlb | 33.7 | 18.4 | 98.3 | 98.3 |
| flapping link | telemetry | 41.2 | 0.0 | 80.0 | 80.3 |
| bad NIC (40% goodput) | ecmp | 28.0 | 32.0 | 118.5 | 118.5 |
| bad NIC | dlb | 29.2 | 29.2 | 113.8 | 113.8 |
| bad NIC | telemetry | 40.9 | 0.8 | 80.0 | 96.1 |
| noisy inference neighbour | ecmp | 32.3 | 21.6 | 102.0 | 107.2 |
| noisy inference neighbour | dlb | 33.6 | 18.6 | 98.3 | 102.8 |
| noisy inference neighbour | telemetry | 41.1 | 0.3 | 80.0 | 82.7 |

Full table, including detection times and inference tail latency: [results/summary.md](results/summary.md).

Three things the numbers say:

1. **Hash collisions alone cost about a fifth of the cluster.** Nothing is broken in the `healthy` row. All-Reduce waits for its slowest transfer, so a few flows hashed onto the same uplink set the pace for all 48 workers.
2. **One sick component taxes every GPU in the job.** A single optic at 30% goodput takes ECMP from 32.6% to 25.4% MFU. The port is up and no queue builds, so load-based balancing does not see it.
3. **Reaction time is the product.** The same control loop, slowed down, loses its benefit:

| gray link, control period | MFU % | loss vs ideal % | time to detect |
|---|---|---|---|
| 1 ms | 41.2 | 0.0 | 24 ms |
| 100 ms | 40.7 | 1.3 | 114 ms |
| 1 s | 36.5 | 11.4 | 947 ms |
| 30 s (no poll lands inside the 3 s window) | 26.4 | 36.0 | not detected |

All three faults: [results/latency_sweep.md](results/latency_sweep.md).

And one on visibility: on the hottest link of the healthy ECMP run, utilisation peaks at **150%** at 100 µs resolution, with 1,564 separate congestion episodes in 4 seconds. The same link averaged over 1-second polls peaks at **34%**. A dashboard fed by polling shows a quiet link.

### On a 2:1 oversubscribed fabric

The fabric above is non-blocking, so a perfect placement exists and the telemetry loop finds it. With the uplinks halved to 400G no placement is perfect, and the gap narrows to something more like a real cluster. Same scenarios, same seeds:

| scenario | ecmp MFU % | dlb MFU % | telemetry MFU % |
|---|---|---|---|
| healthy | 22.7 | 23.7 | 32.2 |
| gray link | 18.2 | 18.6 | 32.2 |
| flapping link | 22.8 | 23.7 | 32.2 |
| bad NIC | 20.7 | 22.9 | 31.9 |
| noisy inference neighbour | 22.3 | 23.4 | 31.9 |

The telemetry loop still loses 22% to the network here, against 42 to 56% for the baselines. Full table: [results/summary_oversub2x.md](results/summary_oversub2x.md).

## Dashboard

```bash
python -m fabricpulse.serve
```

Then open http://localhost:8765/dashboard/. It replays any scenario under any policy: the fabric with every link coloured by utilisation, iteration time for all three policies, the same link seen at 10 ms and at 1 s resolution, the control-latency sweep, and the advisory feed beside the offline attribution. It is plain HTML and JavaScript with no build step, and reads the JSON in `results/`.

## MCP server: network state for agents and schedulers

`fabricpulse/mcp_server.py` exposes a run to an LLM agent or a job scheduler over the Model Context Protocol. Every tool is advisory; nothing changes routing.

| tool | answers |
|---|---|
| `run_scenario` | simulate a scenario and policy, make it the subject of the other tools |
| `get_fabric_health` | which links are down, quarantined, erroring, flapping or congested |
| `get_advisories` | what the control loop has flagged, with evidence |
| `explain_job_slowdown` | why the job is slow, and whether error counters back the suspect |
| `recommend_placement` | which hosts to use or avoid for a new job |

Install the `serve` extra, then configure an MCP-compatible client to launch this command from the repository root using stdio transport:

```bash
python -m fabricpulse.mcp_server
```

Real output of `explain_job_slowdown` for the gray-link scenario under ECMP (trimmed):

```json
{
  "mfu_pct": 20.61,
  "mfu_lost_to_network_pct": 50.0,
  "iteration_slowdown": 2.05,
  "suspects": [
    {"component": "dn:S2->L6", "score": 6.633, "slow_transfers": 54, "transfers": 57,
     "error_ratio": 0.5727, "corroborated_by_error_counters": true},
    {"component": "up:L3->S2", "score": 2.211, "slow_transfers": 18, "transfers": 19,
     "error_ratio": 0.0, "corroborated_by_error_counters": false}
  ],
  "verdict": "A faulty component is the main cause: dn:S2->L6. Transfers crossing it are slower than path sharing explains, and its error counters agree."
}
```

On the healthy scenario the same tool returns no suspects and says the slowdown is congestion, so it does not invent a fault where there is none.

## What these numbers are and are not

They come from a fluid model, not a packet simulator and not hardware. Read them as "the mechanism is real and this is its rough size", not as a benchmark.

- The telemetry policy's 0.0% loss on the non-blocking fabric is an upper bound: a perfect placement exists, and the controller sees the load of every flow the instant it starts. The oversubscribed table is the fairer comparison.
- On the oversubscribed fabric the rebalancer moves flows about 500 times per run because it keeps trying to relieve overload that cannot be relieved. A real controller would need damping.
- Goodput on a congested path uses the single worst link; a flow throttled at one hop still offers its full rate to the next. This overstates congestion slightly.
- The gray-failure model is one number (goodput factor). Real optics degrade through bit errors, FEC, and RoCE retransmission behaviour.
- Congestion control is DCQCN-shaped (ECN marking, multiplicative decrease, staged recovery), not a faithful DCQCN implementation.
- Job-level fault attribution names the right component in 12 of 18 faulted baseline runs. In 3 of the 6 misses the faulted link carried no job traffic, so there was nothing to find.
- The dollar column assumes 10,000 accelerators at $2.50 per hour. It is there to give the percentages a scale.

## Run it

```bash
python -m pip install -e ".[dev,serve]"
```

```bash
pytest
```

```bash
python -m fabricpulse.run
```

```bash
python -m fabricpulse.run --sweep
```

```bash
python -m fabricpulse.run --uplink-gbps 400 --tag oversub2x
```

The full matrix (5 scenarios × 3 policies × 3 seeds) takes about a minute on a laptop. It only needs numpy.

## How it works

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
