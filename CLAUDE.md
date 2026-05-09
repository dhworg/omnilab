# OmniLab — Claude Code session bootstrap

Read this first. Then [`project-spec-v1.md`](./project-spec-v1.md) for the
full architecture and scope.

## Project summary

OmniLab is a bootc-based immutable Linux OS + CLI giving ROS 2 + Gazebo
developers a zero-setup, dependency-hell-proof environment for sim and
hardware work. Beachhead: e-Yantra teams (~6000 participants/year).
v1 deliverable: bootable ISO that turns a fresh laptop into a working sim
+ hardware dev environment in 15 minutes, no terminal copy-paste.

## Current phase

**Phase A Step 1 — DONE.** Repo scaffold + working CI
(`.github/workflows/build-host-iso.yml`) that produces a minimal host
ISO. The host image at this point is Fedora bootc + XFCE + Podman +
a `hello-omnilab` script. No CLI, no project images, no skill-packs.

> Desktop choice: the spec was amended to **KDE Plasma 6** after Phase
> A.1 shipped (commit "docs: switch desktop choice from XFCE to KDE
> Plasma 6"). The Phase A.1 ISO still has XFCE because it predates the
> amendment; Phase B.5 host rebuild will replace it with KDE.

**Next: Phase A Step 2** (test machine bootstrap — human-driven).
Download ISO from Actions, flash to USB, install on the dGPU machine, set
up Tailscale + tmux + SSH from Mac, validate the live `bootc switch`
loop. After that, Phase B is parallelizable.

See `project-spec-v1.md` "First steps" for the full phase plan
(Phase A → B → C → D).

## Architecture in 5 lines

1. **Host (`omnilab-host`)** — Fedora bootc OCI image, atomic updates via
   `bootc upgrade`. Contains kernel, KDE Plasma 6 on Wayland, Podman +
   nvidia-container-toolkit, GPU drivers, udev rules, the `omnilab` CLI.
   No ROS.
2. **Project images (`omnilab-projects`)** — OCI images with pinned
   ROS 2 Jazzy + Gazebo Harmonic + ros-gz + firmware tools. Referenced by
   SHA256 digest in `omnilab.yaml`. This is the dep-hell cure.
3. **Skill-packs (`omnilab-skills`)** — optional installable extensions
   (v1 ships `llm-log-analyzer`).
4. **CLI (`omnilab`)** — Python; ships in the host image; drives project
   containers via Podman. During dev runs from `/var/home/parth/omnilab/`
   (mutable `/var`); baked in only at release.
5. **Three layers stay separate.** Host is stable. Projects are pinned.
   Skills are opt-in.

## Identity

- GitHub org: `dhworg`
- Repo: `github.com/dhworg/omnilab`
- Image namespace: `ghcr.io/dhworg/`
- License: Apache 2.0
- Default branch: `main`

## Locked stack (do not change without spec amendment)

| Component | Choice |
|---|---|
| Host base | Fedora bootc 42 |
| Image format | OCI via GHCR |
| ISO/qcow2 build tool | `bootc-image-builder` |
| Project base | Ubuntu 24.04 |
| ROS 2 | Jazzy Jalisco (LTS to May 2029) |
| Simulator | Gazebo Harmonic (LTS to Sep 2028) |
| Desktop | KDE Plasma 6 on Wayland |
| Container runtime | Podman + nvidia-container-toolkit |
| GPU tiers | iGPU (Intel/AMD) baseline; NVIDIA proprietary tier |
| CLI language | Python |
| Docs | mkdocs-material |

Switch to Ubuntu bootc only when bootc on Ubuntu is GA. Until then,
ROS lives in the **project container** (Ubuntu 24.04), so the host base
choice is decoupled.

## Conventions

- **Conventional Commits.** Use `feat:`, `fix:`, `chore:`, `ci:`, `docs:`,
  `refactor:`. One change per commit.
- **`main` is the default branch.** Land via squash-merge on PRs (when
  collaborators arrive); direct commits to `main` are fine for solo work.
- **Image refs in `omnilab.yaml` MUST be SHA256 digests, not tags.** That
  pinning is the whole reproducibility story. Tags only in CI for the
  `:latest` and `:sha-<commit>` mirrors.
- **Anything that needs the test machine — leave a `TODO Phase X.Y` with
  a phase-and-step pointer.** Do not stub fake values to make CI green.
- **The `omnilab` CLI runs from `/var/home/parth/omnilab/cli/omnilab/`
  during dev** (mutable `/var`). Install editable on the test machine
  with `pip install --user -e .[dev]`; edits on Mac → rsync → effective
  on next invocation, no host image rebuild. Baked into the host image
  only at release time (Phase B.5+).
- **Defaults that don't suck** (per spec §"v1 must-do" #6): GUI on by
  default, shadows off, single sun, 320x240 @ 15Hz cameras, RMW pinned to
  Cyclone DDS, non-zero `ROS_DOMAIN_ID`, sim caps at 1.0 RTF.

## Stop-and-ask rules

Pause and ask the user when:

- Something contradicts `project-spec-v1.md`.
- Scope creep beyond the current phase step is tempting (defer with a
  `TODO Phase X.Y` and note it in the session summary).
- The first CI run fails for non-obvious reasons (read logs, try **one**
  fix, then ask).
- Choosing between options where the wrong choice creates rework.

Do **not** ask about:

- Style choices the spec or these conventions already cover.
- Implementation details inside a single component.
- Which tool to use when the spec already named one.

## Pointer

Full spec: [`./project-spec-v1.md`](./project-spec-v1.md). Read end-to-end
before substantive work.
