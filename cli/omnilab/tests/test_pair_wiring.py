"""Tests for the `omnilab pair` -> container wiring.

`pair join` used to write .omnilab/cyclonedds.xml and stop there: nothing
set CYCLONEDDS_URI, and nothing persisted the derived domain into the
manifest. The container then started on `ros.domain_id` and ignored the
config entirely, so two correctly-paired peers still couldn't see each
other — indistinguishable from pairing having done nothing.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnilab.manifest import OmnilabManifest, PairConfig, write_pair_config
from omnilab.podman import (
    PAIR_CONFIG_CONTAINER_PATH,
    PAIR_CONFIG_RELATIVE_PATH,
    HostContext,
    build_run_args,
    detect_host_context,
)

IMAGE = "ghcr.io/dhworg/ros-jazzy-gz-harmonic:latest"


def _manifest(**overrides) -> OmnilabManifest:
    base = {"name": "test-proj", "image": IMAGE}
    base.update(overrides)
    return OmnilabManifest.model_validate(base)


def _ctx(**overrides) -> HostContext:
    base = {"gpu": "igpu", "wayland_display": None, "project_dir": Path("/tmp/proj")}
    base.update(overrides)
    return HostContext(**base)  # type: ignore[arg-type]


# ---- effective domain ---------------------------------------------------


def test_unpaired_manifest_uses_ros_domain_id():
    assert _manifest().effective_domain_id == 42


def test_paired_domain_overrides_manifest_default():
    m = _manifest(ros={"domain_id": 42}, pair={"domain_id": 73})
    assert m.effective_domain_id == 73


def test_container_runs_on_the_paired_domain():
    """The bug: container started on 42 while the XML said 73."""
    m = _manifest(ros={"domain_id": 42}, pair={"domain_id": 73})
    args = build_run_args(m, _ctx(pair_config_present=True))
    assert "ROS_DOMAIN_ID=73" in args
    assert "ROS_DOMAIN_ID=42" not in args


# ---- CYCLONEDDS_URI -----------------------------------------------------


def test_cyclonedds_uri_set_when_pair_config_present():
    args = build_run_args(_manifest(), _ctx(pair_config_present=True))
    assert f"CYCLONEDDS_URI=file://{PAIR_CONFIG_CONTAINER_PATH}" in args


def test_no_cyclonedds_uri_when_unpaired():
    args = build_run_args(_manifest(), _ctx(pair_config_present=False))
    assert not any("CYCLONEDDS_URI" in a for a in args)


def test_pair_config_path_is_inside_the_workspace_mount():
    """The URI must point at the in-container path, not the host path."""
    args = build_run_args(_manifest(), _ctx(pair_config_present=True))
    uri = next(a for a in args if a.startswith("CYCLONEDDS_URI="))
    assert "/workspace/" in uri
    assert str(PAIR_CONFIG_RELATIVE_PATH) in uri


def test_detect_host_context_finds_written_pair_config(tmp_path: Path):
    assert detect_host_context(tmp_path).pair_config_present is False
    xml = tmp_path / PAIR_CONFIG_RELATIVE_PATH
    xml.parent.mkdir(parents=True)
    xml.write_text("<CycloneDDS/>")
    assert detect_host_context(tmp_path).pair_config_present is True


# ---- manifest persistence -----------------------------------------------


def _write_manifest(d: Path) -> Path:
    p = d / "omnilab.yaml"
    p.write_text(yaml.safe_dump({"name": "proj", "image": IMAGE, "gpu": "auto"}))
    return p


def test_write_pair_config_persists_and_revalidates(tmp_path: Path):
    _write_manifest(tmp_path)
    write_pair_config(tmp_path, PairConfig(domain_id=73, config="discovery_server"))

    reloaded = OmnilabManifest.from_yaml(tmp_path / "omnilab.yaml")
    assert reloaded.pair is not None
    assert reloaded.pair.domain_id == 73
    assert reloaded.pair.config == "discovery_server"
    assert reloaded.effective_domain_id == 73


def test_write_pair_config_preserves_other_keys(tmp_path: Path):
    _write_manifest(tmp_path)
    write_pair_config(tmp_path, PairConfig(domain_id=5))
    data = yaml.safe_load((tmp_path / "omnilab.yaml").read_text())
    assert data["name"] == "proj"
    assert data["image"] == IMAGE
    assert data["gpu"] == "auto"


def test_write_pair_config_is_idempotent(tmp_path: Path):
    _write_manifest(tmp_path)
    write_pair_config(tmp_path, PairConfig(domain_id=5))
    first = (tmp_path / "omnilab.yaml").read_text()
    write_pair_config(tmp_path, PairConfig(domain_id=5))
    assert (tmp_path / "omnilab.yaml").read_text() == first


def test_rejoining_updates_the_existing_pair_block(tmp_path: Path):
    _write_manifest(tmp_path)
    write_pair_config(tmp_path, PairConfig(domain_id=5))
    write_pair_config(tmp_path, PairConfig(domain_id=200, config="discovery_server"))
    reloaded = OmnilabManifest.from_yaml(tmp_path / "omnilab.yaml")
    assert reloaded.pair is not None
    assert reloaded.pair.domain_id == 200
    assert reloaded.pair.config == "discovery_server"


# ---- end-to-end: derived domain matches what the XML declares -----------


def test_derived_domain_matches_generated_xml(tmp_path: Path):
    """pair.derive_domain_id, the XML, and ROS_DOMAIN_ID must all agree."""
    from omnilab.pair import cyclonedds_xml, derive_domain_id, generate_pairing_code

    code = generate_pairing_code()
    domain = derive_domain_id(code)
    xml = cyclonedds_xml(domain_id=domain, mode="simple_discovery", interface="eth0")
    assert f"<Domain id='{domain}'>" in xml

    _write_manifest(tmp_path)
    write_pair_config(tmp_path, PairConfig(domain_id=domain))
    m = OmnilabManifest.from_yaml(tmp_path / "omnilab.yaml")
    args = build_run_args(m, _ctx(pair_config_present=True))
    assert f"ROS_DOMAIN_ID={domain}" in args
