# OmniLab

Bootable, immutable Linux OS + CLI for ROS 2 + Gazebo developers.
Zero-setup, dependency-hell-proof.

> **Status:** Phase A bootstrap (pre-alpha). The CI pipeline produces an
> ISO; that ISO is currently a minimal Fedora bootc + XFCE + Podman shell
> with no ROS yet. See [`project-spec-v1.md`](./project-spec-v1.md) for the
> v1 plan and what each phase adds.

## What this is

A bootc-based Linux distribution targeted at:

1. **e-Yantra Stage 1 participants** (~6000+/year) — beachhead user.
2. **Any ROS 2 / Gazebo developer** who wants `dd → boot → working sim` in
   15 minutes flat.

The host stays small and stable; projects live in pinned OCI containers
(referenced by SHA256 digest, not tag); both update atomically via
`bootc upgrade`.

## Quickstart (Phase A — pipeline preview only)

The ISO produced by CI today boots Fedora bootc with XFCE and Podman.
Nothing robotics-specific yet.

1. Open the latest run of the **build-host-iso** workflow in
   [Actions](https://github.com/dhworg/omnilab/actions/workflows/build-host-iso.yml).
2. Download the `omnilab-host-iso` artifact from the run page.
3. Flash to USB (`dd if=…iso of=/dev/sdX bs=4M status=progress` or
   balenaEtcher) and install.

A real install/quickstart for robotics work lands with Phase B.

## Documentation

`docs/` contains the mkdocs-material source. Build locally:

```sh
pip install mkdocs-material
mkdocs serve
```

Hosted docs site: TODO (Phase D, per `project-spec-v1.md`).

## Architecture (TL;DR)

Three layers, deliberately separate:

- **`omnilab-host`** — Fedora bootc immutable OS. Stable, atomically
  updated via `bootc upgrade`. Contains kernel, XFCE, Podman, GPU drivers,
  udev rules, the `omnilab` CLI. No ROS.
- **`omnilab-projects`** — pinned OCI containers with ROS 2 Jazzy +
  Gazebo Harmonic + extras. Referenced by SHA256 digest in `omnilab.yaml`.
  This is the dep-hell cure: identical bytes everywhere.
- **`omnilab-skills`** — optional installable extensions
  (LLM log analyzer, future vendor SDKs, etc.).

Full spec: [`project-spec-v1.md`](./project-spec-v1.md).

## License

Apache 2.0 — see [`LICENSE`](./LICENSE).
