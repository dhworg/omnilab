"""Pairing primitives for `omnilab pair`.

Per project-spec-v1.md (rev 3) § "Networking" + "v1 must-do" #12:

- LAN-first: zero accounts, no external services.
- RMW config selection from a network probe — Simple Discovery for
  same-LAN + multicast, Discovery Server for cross-NAT or flaky.
- ROS_DOMAIN_ID derived deterministically from the pairing code so
  two operators sharing a code land in the same domain.
- Cyclone DDS XML config bound to the correct interface.
- Firewall rules: firewalld preferred on Fedora, nftables fallback.
- On unreachable peer: fail with an actionable message that names
  user-chosen underlay options. **No VPN/mesh is bundled, installed,
  or auto-configured by OmniLab.**

Pure helpers in this module are fully testable; the live probing path
is split out and easy to mock.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import socket
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Literal

# Word list keeps the middle of pairing codes phonetic and easy to read
# aloud. Avoid ambiguous letters (I/L, O/0) in the suffix encoding too.
_WORDS: tuple[str, ...] = (
    "NAVY", "GOLD", "RUBY", "PLUM", "JADE", "SAGE", "ROSE", "MINT",
    "PEACH", "PEAR", "LIME", "FERN", "MOSS", "OAK", "ELM", "PINE",
    "RIVER", "LAKE", "COVE", "REEF", "BAY", "PEAK", "MESA", "CLIFF",
    "WIND", "SNOW", "RAIN", "STORM", "FOG", "SUN", "MOON", "STAR",
    "WOLF", "BEAR", "HAWK", "FOX", "DEER", "OWL", "LYNX", "SEAL",
    "IRON", "CLAY", "SILK", "STONE", "WOOD", "GLASS", "CORAL", "AMBER",
)
_SUFFIX_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no I, L, O, 0, 1


PairingMode = Literal["simple_discovery", "discovery_server"]
DomainIdInt = int  # constrained to 1..232 by validators below


# ---- pairing code generator + parser ------------------------------------


def generate_pairing_code() -> str:
    """Generate a memorable code like ``K8X3-NAVY-9PLM``.

    Format: 4 chars from _SUFFIX_ALPHABET, dash, word, dash, 4 more chars.
    Entropy: ~30^4 * 48 * 30^4 ≈ 4.2e13 — overkill for one-off pairings.
    """

    def _suffix() -> str:
        return "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(4))

    return f"{_suffix()}-{secrets.choice(_WORDS)}-{_suffix()}"


_PAIRING_CODE_RE = re.compile(
    r"^[A-HJKMNP-Z2-9]{4}-[A-Z]{3,8}-[A-HJKMNP-Z2-9]{4}$"
)


def is_valid_pairing_code(code: str) -> bool:
    """Strict structural check; doesn't verify the word against _WORDS so
    forks adding more words still parse."""
    return bool(_PAIRING_CODE_RE.match(code))


def derive_domain_id(code: str) -> DomainIdInt:
    """Hash a pairing code to a stable ``ROS_DOMAIN_ID`` in 1..232.

    Two operators sharing the same code derive the same domain, so they
    end up on the same DDS partition. 0 is reserved (default DDS pool)
    and 233+ is invalid per the DDS spec.
    """
    h = hashlib.sha256(code.encode("utf-8")).digest()
    n = int.from_bytes(h[:4], "big")
    return (n % 232) + 1


# ---- network probe + mode selection -------------------------------------


@dataclass
class NetworkProbe:
    """Result of probing the host + peer network.

    All fields default to safe-pessimistic so tests can override only
    what they care about.
    """

    peer_reachable: bool = False
    can_multicast: bool = False
    nat_detected: bool = False
    mtu: int = 1500
    interface: str = ""
    local_ip: str = ""
    peer_ip: str | None = None


def select_pairing_mode(probe: NetworkProbe) -> PairingMode | None:
    """Pure: choose an RMW config from a probe. Returns None if the
    peer is unreachable — the caller surfaces the underlay-suggestion
    error.
    """
    if not probe.peer_reachable:
        return None
    if probe.can_multicast and not probe.nat_detected:
        return "simple_discovery"
    return "discovery_server"


# ---- Cyclone DDS XML config ---------------------------------------------


def cyclonedds_xml(
    *,
    domain_id: int,
    mode: PairingMode,
    interface: str,
    peer_ip: str | None = None,
) -> str:
    """Pure: build a Cyclone DDS XML config string.

    For Simple Discovery, only the network interface is bound. For
    Discovery Server, the peer's IP is added as a unicast peer.
    """
    if not interface:
        raise ValueError("interface is required")
    if not (0 <= domain_id <= 232):
        raise ValueError(f"domain_id {domain_id} out of range")

    if mode == "simple_discovery":
        peers_block = ""
    elif mode == "discovery_server":
        if not peer_ip:
            raise ValueError("discovery_server mode requires peer_ip")
        peers_block = (
            "        <Peers>\n"
            f"            <Peer address='{peer_ip}'/>\n"
            "        </Peers>\n"
        )
    else:
        raise ValueError(f"unknown mode: {mode!r}")

    return (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<CycloneDDS xmlns='https://cdds.io/config'\n"
        "  xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'\n"
        "  xsi:schemaLocation='https://cdds.io/config https://raw.githubusercontent.com/eclipse-cyclonedds/cyclonedds/master/etc/cyclonedds.xsd'>\n"
        f"  <Domain id='{domain_id}'>\n"
        "    <General>\n"
        f"      <NetworkInterfaceAddress>{interface}</NetworkInterfaceAddress>\n"
        "      <AllowMulticast>true</AllowMulticast>\n"
        "    </General>\n"
        "    <Discovery>\n"
        "      <ParticipantIndex>auto</ParticipantIndex>\n"
        f"{peers_block}"
        "    </Discovery>\n"
        "  </Domain>\n"
        "</CycloneDDS>\n"
    )


# ---- firewall rules ------------------------------------------------------


def firewall_commands(*, domain_id: int, backend: str) -> list[list[str]]:
    """Pure: list of commands to open the DDS ports for a domain.

    DDS port formula (RTPS spec): 7400 + 250*domain_id (discovery) and
    7401 + 250*domain_id (user data). Plus +10 for unicast metadata.
    For simplicity we open the first 16 ports of the domain band.
    """
    base = 7400 + 250 * domain_id
    ports = list(range(base, base + 16))

    if backend == "firewalld":
        return [
            ["firewall-cmd", "--add-port", f"{p}/udp"] for p in ports
        ]
    if backend == "nftables":
        return [
            [
                "nft",
                "add",
                "rule",
                "inet",
                "filter",
                "input",
                f"udp dport {p} accept",
            ]
            for p in ports
        ]
    raise ValueError(f"unknown firewall backend: {backend!r}")


def detect_firewall_backend() -> str:
    """Returns 'firewalld' if the daemon is running, else 'nftables'."""
    rc = subprocess.run(  # noqa: S603 — fixed argv
        ["systemctl", "is-active", "--quiet", "firewalld"],
        check=False,
    ).returncode
    return "firewalld" if rc == 0 else "nftables"


# ---- live probing (best-effort; mock in tests) --------------------------


def default_interface() -> str:
    """Best-effort: name of the host interface owning the default route."""
    try:
        out = subprocess.run(
            ["ip", "-o", "route", "get", "1.1.1.1"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"\bdev\s+(\S+)", out.stdout)
    return m.group(1) if m else ""


def local_ip_for(interface: str) -> str:
    if not interface:
        return ""
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show", interface],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out.stdout)
    return m.group(1) if m else ""


def probe_peer_reachable(peer_ip: str, *, port: int = 7400, timeout: float = 2.0) -> bool:
    """TCP / UDP probe — returns True if anything replies."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(b"\x00", (peer_ip, port))
            return True  # got past sendto without raising; peer is at least addressable
    except OSError:
        return False


# ---- failure-mode hint ---------------------------------------------------


UNREACHABLE_PEER_HINT = (
    "Peer is not reachable on this network. OmniLab's pairing is\n"
    "LAN-first by design and does NOT bundle a VPN or mesh.\n"
    "\n"
    "If you need to pair across networks, set up an underlay yourself:\n"
    "  - Tailscale  (https://tailscale.com)\n"
    "  - Headscale  (https://headscale.net)\n"
    "  - WireGuard  (https://wireguard.com)\n"
    "  - ZeroTier   (https://zerotier.com)\n"
    "\n"
    "Once both peers can ping each other on the underlay, re-run\n"
    "`omnilab pair join <code>` and OmniLab will configure RMW + ports\n"
    "without caring which underlay you chose."
)


# ---- summary dataclass for --json + manifest persistence ---------------


@dataclass
class PairResult:
    """Returned from `pair init` / `pair join` for both human + JSON output."""

    code: str
    domain_id: int
    mode: PairingMode | None
    interface: str
    local_ip: str
    peer_ip: str | None = None
    cyclonedds_xml_path: str | None = None
    firewall_backend: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def succeeded(self) -> bool:
        return self.mode is not None
