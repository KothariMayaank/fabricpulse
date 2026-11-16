# Architecture

## The idea in one paragraph

Synchronous training alternates between computing and exchanging gradients. During the exchange every GPU in the job is idle, and the exchange is only finished when its slowest transfer is. So the network sets a floor on idle time, and any single slow path raises that floor for the whole job. FabricPulse models exactly that chain: fabric state → transfer times → iteration time → MFU. Everything else exists to perturb the fabric state or to react to it.

## Layers

```
            ┌──────────────────────────────────────────────────────────┐
 surfaces   │  dashboard (static web)        MCP server                │
            │  dashboard/ + serve.py         mcp_server.py, insight.py │
            └───────────────▲──────────────▲───────────────▲───────────┘
                            │ reads results/*.json, or runs a scenario
            ┌───────────────┴──────────────┴───────────────┴───────────┐
 analysis   │ run.py        experiment matrix, latency sweep, traces   │
            │ attribution.py   job-level fault localisation            │
            └───────────────▲──────────────────────────────────────────┘
            ┌───────────────┴──────────────────────────────────────────┐
 control    │ policies.py   Ecmp · Dlb · TelemetryAware                │
            │ telemetry.py  Telemetry recorder · HealthMonitor         │
            └───────────────▲──────────────────────────────────────────┘
            ┌───────────────┴──────────────────────────────────────────┐
 world      │ sim.py        fluid engine, DCQCN-style rate control     │
            │ topology.py   leaf-spine, flat link indexing             │
            │ workloads.py  TrainingJob · InferenceBursts              │
            │ faults.py     gray link · flapping link · bad NIC        │
            │ scenarios.py  named, seeded experiments                  │
            └──────────────────────────────────────────────────────────┘
```

The world layer knows nothing about policies beyond three calls: `choose()` when a flow starts, `on_link_event()` when a link changes state, and `on_tick()` at the policy's own control period.

## The engine (`sim.py`)

Fixed tick of 100 µs, with an event heap for everything discrete (job phases, faults, delayed routing updates). All link and flow state is in flat numpy arrays, so a tick is about a dozen vectorised operations regardless of how many flows are active. A 4-second run is 40,000 ticks and takes about 1.5 s.

Per tick:

1. **Offered rate per link** = sum of the sending rates of flows crossing it (`np.bincount` over the path matrix).
2. **Queue** integrates `offered − capacity`, clipped to the buffer. It is a congestion signal, not a store of bytes.
3. **Goodput per flow** = rate × the worst `capacity / offered` on its path × the product of the links' goodput factors `gamma`. This is the lossless (PFC) assumption: excess demand is paused back to the sender, not dropped.
4. **Rate control**: queue above `kmin` marks ECN. Marked flows cut by `alpha · m / 2`; unmarked flows recover halfway to their target and the target creeps up. `alpha` follows DCQCN's EWMA.
5. **Counters**: per-link cumulative offered and errored volume, like switch octet and error counters. Controllers read deltas at their own cadence.

Paths are always four link indices. Flows inside a leaf pad with a `NULL` link of infinite capacity, which keeps every per-flow operation a fixed-shape gather.

### Two kinds of broken

- **Hard down** (`link_up = False`): capacity is zero, flows on it stall until someone moves them.
- **Gray** (`gamma < 1`): the port is up at nominal speed, offered load looks normal, no queue forms. Goodput drops and the error counter climbs. This is the one load-based balancing cannot see, and the reason the health monitor reads error counters instead of utilisation.

## Policies (`policies.py`)

Every policy keeps its own **view** of link liveness, updated a delay after the truth changes. That delay is the modelled difference between waiting on a routing protocol and reacting from telemetry.

- **`Ecmp`**: CRC of the flow key modulo the usable spines. A RoCE queue pair keeps its 5-tuple, so the key (and the collision) is the same every iteration. A membership change remaps unrelated flows too (hash-mod-N).
- **`Dlb`**: each new flowlet picks the least-loaded local uplink. Sees its own uplinks fail in 1 ms. Blind to the spine→leaf hop, where flows from different leaves converge, and to gray failures.
- **`TelemetryAware`**:
  - placement minimises the worse of the two fabric hops, using fabric-wide load;
  - `HealthMonitor` keeps an EWMA of each link's error ratio from counter deltas and quarantines a link that crosses 2% while carrying traffic;
  - flap dampening: two down events inside a second quarantine the link, so it is not trusted the moment it comes back;
  - mid-flow rebalancing of fabric links whose placed demand exceeds capacity;
  - a NIC cannot be routed around, so a sick host link becomes an **advisory**. `TrainingJob` checks advisories at each iteration boundary and swaps the worker onto a spare host.
  - `control_s` is the single knob for how fast the loop runs. The latency sweep turns it from 1 ms to 30 s.

Quarantine is sticky on purpose. Once traffic is steered off a link there is nothing left to measure, so clearing it is an operator decision. Every quarantine is logged in `sim.advisories` with its evidence. That list is the human-in-the-loop surface, and it is what the MCP server exposes.

## Surfaces

- **Dashboard** (`dashboard/`, served by `fabricpulse/serve.py`): static HTML, CSS and JavaScript with no build step. It fetches `results/summary.json`, `results/latency_sweep.json` and the per-run traces, and draws everything as SVG.
- **Insight** (`insight.py`): a `Session` wraps one finished run and answers read-only questions about it (fabric health, advisories, why the job is slow, where to place a job). No MCP dependency, so it is unit-tested directly.
- **MCP server** (`mcp_server.py`): a thin wrapper that registers the `Session` methods as tools on the `mcp` SDK (v2, `MCPServer`), over stdio.

## Attribution (`attribution.py`)

Localises a fault from the job's point of view alone: transfer times plus the paths the transfers took.

1. Transfers of one All-Reduce step are concurrent, so the paths say how many shared each link and how slow each transfer *should* have been. Dividing that out leaves the unexplained slowdown. Without this step a plain hash collision outranks a real fault.
2. Components are scored by `precision × total unexplained excess`, so a healthy link shared by one slow and several fast transfers is diluted.

It is the fallback for faults with no switch-side signal, and the cross-check for the ones that have one.

## Metrics

- **MFU** = `eta × compute time / wall time`, with `eta = 0.55` (utilisation while actually computing). On a perfect network that gives 41.2%, inside the 33–45% range quoted for real training clusters.
- **Loss vs ideal** = `1 − MFU / ideal MFU`. This is the network's share of the waste.
- **Iteration p50 / p99**, **inference flow slowdown p99**, **time to detect**, **reroutes**, **migrations**.
- **Resolution gap** (`Telemetry.resolution_gap`): peak utilisation and congestion episodes at full resolution against the same link as a poller would report it.

## Data contracts (for the dashboard and the MCP server)

`results/summary.json`
```
{ assumptions: {...},
  aggregate: [ {scenario, policy, seeds, mfu_pct, ideal_mfu_pct, mfu_loss_rel_pct,
                iter_ms_p50, iter_ms_p99, reroutes, migrations, detect_ms, detected,
                false_advisories, attributed, faulted_runs,
                inference_p99_slowdown, annual_cost_usd} ],
  runs: [ one row per (scenario, policy, seed), incl. resolution_gap ] }
```

`results/runs/<scenario>__<policy>.json` (seed 0; regenerated by `python -m fabricpulse.run`, not committed)
```
{ meta, topology: {n_leaf, n_spine, hosts_per_leaf, link_names, components, ...},
  fabric_links: [link index per column],
  bucket_ms: 10,
  t_ms: [T],                     # bucket start times
  util_peak, util_mean: [T][F],  # fraction of uplink capacity, per fabric link
  queue_peak_gbit: [T][F],
  err_mean_gbps: [T][F],
  iterations: [ {t_start, t_end, comm_s} ],
  link_events: [ [t, link, up] ],
  advisories: [ {t, component, kind, action, evidence} ],
  migrations: [ {t, from, to} ],
  suspects: [ {component, score, slow_transfers, transfers} ] }
```

`results/latency_sweep.json`: `[ {scenario, control_period_s, mfu_pct, mfu_loss_rel_pct, detected, runs, detect_ms} ]`

## Known limits

See [What these numbers are and are not](../README.md#what-these-numbers-are-and-are-not) in the README.
