# OmniLab

Bootable, immutable Linux OS + CLI for ROS 2 + Gazebo developers.
Zero-setup, dependency-hell-proof. Also: the first Linux OS where AI
agents can perceive, act on, and verify robot state — see
`omnilab observe` in the spec.

> **Status:** Phase B.4 in progress (rev 3 architecture). Host is KDE
> Plasma 6 on Wayland, project image (ROS 2 Jazzy + Gazebo Harmonic)
> is published to GHCR, CLI v0 ships five commands. See
> [`project-spec-v1.md`](./project-spec-v1.md) and
> [`CLAUDE.md`](./CLAUDE.md) for what each phase adds.

## What this is

A bootc-based Linux distribution targeted at:

1. **e-Yantra Stage 1 participants** (~6000+/year) — beachhead user.
2. **Any ROS 2 / Gazebo developer** who wants `dd → boot → working sim` in
   15 minutes flat.
3. **AI agents driving robotics dev** — designed-for from day 1.

The host stays small and stable; projects live in pinned OCI containers
(referenced by SHA256 digest, not tag); both update atomically via
`bootc upgrade`.

## Install

Two ISO variants are produced by CI; you almost certainly want the first.

### Interactive (default — production / physical machine)

Anaconda runs its full UI. **You create your own user, password, and disk
choice during install.** No credentials are baked into the image. This is
the variant emitted by every push to `main`, every tag, and every PR.

1. Open the latest run of the **build-host-iso** workflow on
   [Actions](https://github.com/dhworg/omnilab/actions/workflows/build-host-iso.yml).
2. Download the **`omnilab-host-iso-interactive`** artifact from the run
   page.
3. Flash to USB (`dd if=…iso of=/dev/diskN bs=4m status=progress` on
   macOS, or balenaEtcher on any platform).
4. Boot the target machine from USB, click through Anaconda, install,
   reboot, eject the medium.

### Dev variant (auto-install with placeholder creds — VM iteration only)

For fast iteration on the OS itself in a local VM. Auto-installs with
placeholder credentials from `host/config.toml.dev`; **never use this for
production or shareable builds**.

1. Trigger via [`workflow_dispatch`](https://github.com/dhworg/omnilab/actions/workflows/build-host-iso.yml)
   with input **`variant: dev`**.
2. Download the **`omnilab-host-iso-dev`** artifact.
3. Flash, install — installer does not prompt for user/password.

## Documentation

`docs/` contains the mkdocs-material source. Build locally:

```sh
pip install mkdocs-material
mkdocs serve
```

Hosted docs site: TODO (Phase D, per `project-spec-v1.md`).

## Architecture (TL;DR)

Three layers + one cross-cutting pillar:

- **`omnilab-host`** — Fedora bootc immutable OS. Stable, atomically
  updated via `bootc upgrade`. Contains kernel, KDE Plasma 6 on Wayland,
  Podman, GPU drivers, udev rules, the `omnilab` CLI. No ROS.
- **`omnilab-projects`** — pinned OCI containers with ROS 2 Jazzy +
  Gazebo Harmonic + extras. Referenced by SHA256 digest in `omnilab.yaml`.
  The dep-hell cure: identical bytes everywhere.
- **`omnilab-skills`** — optional installable extensions
  (LLM log analyzer, future vendor SDKs).
- **Agent perception (`omnilab observe`)** — the differentiator.
  Reads spatial/physical robot state in real time so AI agents can drive
  the dev loop. Companion to `omnilab tune` (action) and `omnilab record`
  (memory).

Full spec: [`project-spec-v1.md`](./project-spec-v1.md).

## License

Apache 2.0 — see [`LICENSE`](./LICENSE).
