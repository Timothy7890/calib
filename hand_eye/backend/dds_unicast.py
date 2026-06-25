"""Force Unitree DDS (CycloneDDS) to use UNICAST peer discovery.

Why
---
Unitree's SDK builds an inline CycloneDDS config and passes it straight to
``Domain(id, config)``, so the ``CYCLONEDDS_URI`` env var is ignored. On a network
where multicast is dropped (e.g. both devices behind a router/switch instead of a
direct cable), SPDP multicast discovery fails and ``rt/lowstate`` never arrives.

This module monkeypatches the SDK's config strings *before*
``ChannelFactoryInitialize`` runs, disabling multicast and adding explicit unicast
``<Peer>`` entries (the robot's IP). Discovery then happens over unicast.

Usage
-----
    from backend.dds_unicast import enable_unicast_peers
    enable_unicast_peers(["192.168.123.164"])   # call BEFORE any executor/DDS init
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

# Ensure the vendored SDK is importable (repo_root/unitree_sdk2_python).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SDK_DIR = _REPO_ROOT / "unitree_sdk2_python"
for _p in (str(_REPO_ROOT), str(_SDK_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _config_xml(peers: Sequence[str], interface_token: str) -> str:
    peers_xml = "".join(f'<Peer address="{p}"/>' for p in peers)
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>'
        "<CycloneDDS>"
        '<Domain Id="any">'
        "<General>"
        "<Interfaces>"
        f"{interface_token}"
        "</Interfaces>"
        "<AllowMulticast>false</AllowMulticast>"
        "</General>"
        "<Discovery>"
        "<ParticipantIndex>auto</ParticipantIndex>"
        "<MaxAutoParticipantIndex>32</MaxAutoParticipantIndex>"
        f"<Peers>{peers_xml}</Peers>"
        "</Discovery>"
        "</Domain>"
        "</CycloneDDS>"
    )


def enable_unicast_peers(peers: Sequence[str]) -> None:
    """Patch the SDK's DDS config to use unicast discovery to ``peers``.

    Must be called BEFORE ChannelFactoryInitialize / any executor construction.
    """
    if not peers:
        return
    import unitree_sdk2py.core.channel as ch

    # Keep the $__IF_NAME__$ placeholder so an explicit --network-interface still works.
    iface_with_name = '<NetworkInterface name="$__IF_NAME__$" priority="default" multicast="false"/>'
    iface_auto = '<NetworkInterface autodetermine="true" priority="default" multicast="false"/>'

    ch.ChannelConfigHasInterface = _config_xml(peers, iface_with_name)
    ch.ChannelConfigAutoDetermine = _config_xml(peers, iface_auto)
    print(f"[dds] unicast discovery enabled, peers={list(peers)} (multicast disabled)")
