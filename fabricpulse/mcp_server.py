"""MCP server: exposes simulated network state to an LLM agent or a job scheduler.

    python -m fabricpulse.mcp_server          # stdio transport

Configure an MCP-compatible client to launch the command above from the
repository root using stdio transport.

Every tool is read-only with respect to the network. The agent can look,
explain and recommend; it cannot change routing. `run_scenario` only replaces
which simulated run the other tools describe.

Requires the `serve` extra (mcp >= 2).
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .insight import Session
from .policies import POLICIES
from .scenarios import SCENARIOS

server = MCPServer(
    "fabricpulse",
    instructions=(
        "Network state for a simulated AI training cluster (leaf-spine fabric, one training job). "
        "Call run_scenario first to choose what to look at; the default is a gray link under ECMP. "
        "Then use explain_job_slowdown for 'why is the job slow', get_fabric_health for link state, "
        "get_advisories for what the control loop has flagged, and recommend_placement before placing a job. "
        "All tools are advisory. Report evidence with numbers, and say when a suspect is not corroborated."
    ),
)

_session: Session | None = None


def _s() -> Session:
    global _session
    if _session is None:
        _session = Session()
    return _session


@server.tool()
def list_scenarios() -> dict:
    """List the fault scenarios and routing policies that can be simulated."""
    return {"scenarios": SCENARIOS, "policies": {
        "ecmp": "static hash, no awareness of load or health",
        "dlb": "per-leaf dynamic load balancing over local uplinks",
        "telemetry": "closed loop over fabric-wide telemetry with a 1 ms control period"}}


@server.tool()
def run_scenario(scenario: str = "gray_link", policy: str = "ecmp", seed: int = 0) -> dict:
    """Simulate 4 seconds of the cluster under a scenario and policy, and make
    that run the subject of the other tools. Returns the headline numbers."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; choose from {list(SCENARIOS)}")
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}; choose from {list(POLICIES)}")
    global _session
    _session = Session(scenario, policy, seed)
    return _session.summary()


@server.tool()
def get_fabric_health(top: int = 8) -> dict:
    """Links that need attention (down, quarantined, erroring, flapping or
    congested), worst first, with fabric-wide counts."""
    return _s().fabric_health(top)


@server.tool()
def get_advisories() -> list[dict]:
    """Advisories raised by the telemetry control loop, each with its evidence.
    Empty under policies that have no health monitor."""
    return _s().advisories()


@server.tool()
def explain_job_slowdown() -> dict:
    """Explain why the training job is slower than ideal: MFU lost to the
    network, suspected components from job-level attribution, whether error
    counters corroborate them, and a one-paragraph verdict."""
    return _s().explain_job_slowdown()


@server.tool()
def recommend_placement(n_workers: int) -> dict:
    """Recommend hosts for a new job of n_workers from current network health,
    and list hosts and leaves to avoid. Advice only."""
    return _s().recommend_placement(n_workers)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
