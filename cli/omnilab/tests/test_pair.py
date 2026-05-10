"""Tests for omnilab.pair — pairing code, mode selector, XML, firewall rules."""

from __future__ import annotations

import pytest

from omnilab.pair import (
    UNREACHABLE_PEER_HINT,
    NetworkProbe,
    cyclonedds_xml,
    derive_domain_id,
    firewall_commands,
    generate_pairing_code,
    is_valid_pairing_code,
    select_pairing_mode,
)

# ---- pairing code generator ---------------------------------------------


def test_generate_pairing_code_format():
    code = generate_pairing_code()
    assert is_valid_pairing_code(code), f"got {code!r}"
    parts = code.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 4
    assert len(parts[2]) == 4
    assert 3 <= len(parts[1]) <= 8


def test_generate_pairing_code_unique_enough():
    codes = {generate_pairing_code() for _ in range(50)}
    # Birthday-paradox safe at this small N.
    assert len(codes) == 50


def test_pairing_code_excludes_ambiguous_chars():
    """Suffix alphabet must not include I, L, O, 0, 1 (visually ambiguous)."""
    for _ in range(20):
        code = generate_pairing_code()
        a, _, b = code.split("-")
        for ch in a + b:
            assert ch not in "ILO01"


def test_is_valid_pairing_code_round_trip():
    for _ in range(10):
        c = generate_pairing_code()
        assert is_valid_pairing_code(c)


def test_is_valid_pairing_code_rejects_bad():
    assert not is_valid_pairing_code("ABC-X-DEF")  # too-short word/suffix
    assert not is_valid_pairing_code("ABCD-NAVY-EFG")  # short suffix
    assert not is_valid_pairing_code("ABCD-NAVY")  # missing trailing block
    assert not is_valid_pairing_code("0000-NAVY-1111")  # excluded chars in suffix


# ---- domain id derivation -----------------------------------------------


def test_derive_domain_id_in_range():
    for _ in range(50):
        c = generate_pairing_code()
        d = derive_domain_id(c)
        assert 1 <= d <= 232


def test_derive_domain_id_deterministic():
    code = "K8X3-NAVY-9PLM"
    assert derive_domain_id(code) == derive_domain_id(code)


def test_derive_domain_id_changes_with_code():
    a = derive_domain_id("K8X3-NAVY-9PLM")
    b = derive_domain_id("K8X3-NAVY-9PLN")
    # Could collide but extremely unlikely with sha256.
    assert a != b


# ---- mode selector ------------------------------------------------------


def test_select_mode_returns_none_for_unreachable():
    assert select_pairing_mode(NetworkProbe()) is None


def test_select_mode_simple_discovery_when_lan_multicast():
    probe = NetworkProbe(peer_reachable=True, can_multicast=True, nat_detected=False)
    assert select_pairing_mode(probe) == "simple_discovery"


def test_select_mode_discovery_server_when_nat():
    probe = NetworkProbe(peer_reachable=True, can_multicast=True, nat_detected=True)
    assert select_pairing_mode(probe) == "discovery_server"


def test_select_mode_discovery_server_when_no_multicast():
    probe = NetworkProbe(peer_reachable=True, can_multicast=False, nat_detected=False)
    assert select_pairing_mode(probe) == "discovery_server"


# ---- Cyclone DDS XML ----------------------------------------------------


def test_cyclonedds_xml_simple_discovery():
    xml = cyclonedds_xml(domain_id=42, mode="simple_discovery", interface="eth0")
    assert "<Domain id='42'>" in xml
    assert "eth0" in xml
    assert "<Peer" not in xml
    assert "AllowMulticast>true" in xml


def test_cyclonedds_xml_discovery_server_includes_peer():
    xml = cyclonedds_xml(
        domain_id=7,
        mode="discovery_server",
        interface="en0",
        peer_ip="10.0.0.42",
    )
    assert "<Peer address='10.0.0.42'/>" in xml
    assert "<Domain id='7'>" in xml


def test_cyclonedds_xml_discovery_server_requires_peer_ip():
    with pytest.raises(ValueError, match="peer_ip"):
        cyclonedds_xml(domain_id=7, mode="discovery_server", interface="en0")


def test_cyclonedds_xml_rejects_out_of_range_domain():
    with pytest.raises(ValueError, match="out of range"):
        cyclonedds_xml(domain_id=999, mode="simple_discovery", interface="eth0")


def test_cyclonedds_xml_requires_interface():
    with pytest.raises(ValueError, match="interface"):
        cyclonedds_xml(domain_id=1, mode="simple_discovery", interface="")


# ---- firewall rules -----------------------------------------------------


def test_firewall_firewalld_opens_correct_band():
    cmds = firewall_commands(domain_id=42, backend="firewalld")
    base = 7400 + 250 * 42
    ports = []
    for cmd in cmds:
        # cmd: ['firewall-cmd', '--add-port', '{port}/udp']
        port_str = cmd[2].split("/")[0]
        ports.append(int(port_str))
    assert ports[0] == base
    assert ports[-1] == base + 15
    assert all(c[0] == "firewall-cmd" for c in cmds)


def test_firewall_nftables_uses_nft():
    cmds = firewall_commands(domain_id=1, backend="nftables")
    assert all(c[0] == "nft" for c in cmds)
    assert any("dport" in c[6] for c in cmds)


def test_firewall_unknown_backend_raises():
    with pytest.raises(ValueError, match="unknown firewall backend"):
        firewall_commands(domain_id=1, backend="iptables")


# ---- failure-mode hint --------------------------------------------------


def test_unreachable_hint_does_not_bundle_underlay():
    """Spec hard requirement: don't ship/install/integrate Tailscale et al."""
    h = UNREACHABLE_PEER_HINT
    assert "does NOT bundle" in h
    # Names alternatives, but only as user-chosen options.
    assert "Tailscale" in h
    assert "WireGuard" in h
    # The wording must not promise installation.
    assert "install" not in h.lower() or "user-chosen" in h.lower() or "yourself" in h.lower()
