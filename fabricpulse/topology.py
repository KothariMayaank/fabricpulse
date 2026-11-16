"""Two-tier leaf-spine fabric.

Every link is directional and has an integer index, so the simulator can keep
all per-link state in flat numpy arrays:

    host_up[h]   host h -> its leaf          (the NIC, transmit side)
    host_dn[h]   leaf   -> host h            (the NIC, receive side)
    up[l, s]     leaf l -> spine s
    dn[l, s]     spine s -> leaf l

Index ``NULL`` (== n_links) is a dummy link with infinite capacity. Flows that
stay inside one leaf use it to pad their path to a fixed length of 4.
"""

from __future__ import annotations

import numpy as np


class LeafSpine:
    def __init__(self, n_leaf: int = 8, n_spine: int = 4, hosts_per_leaf: int = 8,
                 host_gbps: float = 400.0, uplink_gbps: float = 800.0):
        self.n_leaf, self.n_spine, self.hosts_per_leaf = n_leaf, n_spine, hosts_per_leaf
        self.host_gbps, self.uplink_gbps = host_gbps, uplink_gbps
        self.n_hosts = H = n_leaf * hosts_per_leaf
        L, S = n_leaf, n_spine

        self.host_up = np.arange(H)
        self.host_dn = H + np.arange(H)
        self.up = (2 * H + np.arange(L * S)).reshape(L, S)
        self.dn = (2 * H + L * S + np.arange(L * S)).reshape(L, S)
        self.n_links = 2 * H + 2 * L * S
        self.NULL = self.n_links

        self.cap = np.empty(self.n_links + 1)
        self.cap[: 2 * H] = host_gbps
        self.cap[2 * H: self.n_links] = uplink_gbps
        self.cap[self.NULL] = np.inf

        self.is_fabric = np.zeros(self.n_links + 1, dtype=bool)
        self.is_fabric[2 * H: self.n_links] = True

        # A "component" is the thing an operator would replace: a NIC (both
        # directions of a host link), or one direction of a fabric link.
        self.components: list[str] = [f"nic:h{h}" for h in range(H)]
        self.link_component = np.empty(self.n_links + 1, dtype=int)
        self.link_component[self.host_up] = np.arange(H)
        self.link_component[self.host_dn] = np.arange(H)
        self.link_names = [""] * (self.n_links + 1)
        for h in range(H):
            self.link_names[self.host_up[h]] = f"h{h}->L{self.leaf_of(h)}"
            self.link_names[self.host_dn[h]] = f"L{self.leaf_of(h)}->h{h}"
        for l in range(L):
            for s in range(S):
                for kind, arr, name in (("up", self.up, f"L{l}->S{s}"), ("dn", self.dn, f"S{s}->L{l}")):
                    self.link_component[arr[l, s]] = len(self.components)
                    self.components.append(f"{kind}:{name}")
                    self.link_names[arr[l, s]] = name
        self.link_component[self.NULL] = -1
        self.link_names[self.NULL] = "null"

    def leaf_of(self, host: int) -> int:
        return host // self.hosts_per_leaf

    def host_of_link(self, link: int) -> int | None:
        """Host index if ``link`` is a NIC link, else None."""
        if link < self.n_hosts:
            return int(link)
        if link < 2 * self.n_hosts:
            return int(link - self.n_hosts)
        return None

    def path(self, src: int, dst: int, spine: int) -> tuple[int, int, int, int]:
        """Link indices a flow traverses. ``spine`` is ignored within a leaf."""
        ls, ld = self.leaf_of(src), self.leaf_of(dst)
        if ls == ld:
            return (int(self.host_up[src]), int(self.host_dn[dst]), self.NULL, self.NULL)
        return (int(self.host_up[src]), int(self.up[ls, spine]),
                int(self.dn[ld, spine]), int(self.host_dn[dst]))

    def describe(self) -> dict:
        return {
            "n_leaf": self.n_leaf, "n_spine": self.n_spine,
            "hosts_per_leaf": self.hosts_per_leaf, "n_hosts": self.n_hosts,
            "host_gbps": self.host_gbps, "uplink_gbps": self.uplink_gbps,
            "n_links": self.n_links, "link_names": self.link_names[: self.n_links],
            "components": self.components,
        }
