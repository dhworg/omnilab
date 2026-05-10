# OmniLab — Claude Code session bootstrap

Read this first. Then [`project-spec-v1.md`](./project-spec-v1.md) for the
full architecture and scope (architecture rev 3 as of 2026-05-10).

## Project summary

OmniLab is a bootc-based immutable Linux OS + CLI giving ROS 2 + Gazebo
developers a zero-setup, dependency-hell-proof environment for sim and
hardware work. It's also the first Linux OS where AI agents can perceive,
act on, and verify robot state via a built-in agent-perception primitive
(`omnilab observe`). Beachhead: e-Yantra teams (~6000 participants/year).
v1 deliverable: bootable interactive ISO that turns a fresh laptop into a
working sim + hardware dev environment in 15 minutes.

## Current phase

**Phase A — Bootstrap: DONE.** Repo scaffold + `build-host-iso.yml` CI;
first ISO built and bootc loop verified end-to-end.

**Phase B — Core layers (B.4 in progress):**
- ✅ **B.1**: Host swapped to KDE Plasma 6 on Wayland
- ✅ **B.2**: `ros-jazzy-gz-harmonic` project image built, on GHCR
- ✅ **B.3**: `omnilab` CLI v0 (5 commands: `new`, `up`, `down`, `sim`,
  `doctor`) with 40 unit tests, two-Python-version CI matrix
- ✅ **B.4 DONE** — spec architecture rev 3:
  - ✅ Switched ISO to interactive install (default; opt-in `dev` variant
    via `workflow_dispatch` with `host/config.toml.dev`)
  - ✅ CLI conventions infra (`--json`, `--dry-run`, `--yes`, documented
    exit codes 0..5; `docs/cli-conventions.md`)
  - ✅ Seven new commands shipped: `inspect`, `clean`, `record`/`replay`,
    `pair init/join/status`, `template list/show/install`, `observe`,
    `tune`. All honor dual-mode + destructive-safety conventions.
  - ✅ Three foundational templates: `nav2-base`, `micro-ros-blink`,
    `quadruped-walker` (with observers.yaml). 3 `docs/examples/observers/`
    YAMLs (quadruped, mobile_2d, arm_6dof).
  - ✅ Agent-loop smoke test (`tests/test_agent_loop.py`) wired into
    `smoke-tests.yml` workflow.
  - ✅ `omnilab doctor --full` for extended health checks.
  - 234 → 240+ unit tests, two Python versions in CI.
- ⏳ **B.5 NEXT**: Host hardening — udev rules, group memberships,
  NVIDIA stack, branding (fastfetch, fonts, wallpapers, KDE theming)
- ⏸ **B.6**: Full smoke-test matrix in CI (Gazebo headless on
  self-hosted dGPU runner)

**Phase C — Verification (gates v1 release):** NVIDIA tier, hardware
verification, agent-loop verification.

**Phase D — Polish:** `llm-log-analyzer` skill-pack, mkdocs docs site,
KDE theming for the Figma-inspired aesthetic.

See `project-spec-v1.md` § "Phase status" for the up-to-date breakdown.

## Architecture (3 layers + 1 pillar)

1. **Host (`omnilab-host`)** — Fedora bootc 42 OCI image, atomic updates
   via `bootc upgrade`. Contains kernel, KDE Plasma 6 on Wayland, Podman
   + nvidia-container-toolkit, GPU drivers, udev rules, the `omnilab`
   CLI. No ROS.
2. **Project images (`omnilab-projects`)** — OCI images with pinned
   ROS 2 Jazzy + Gazebo Harmonic + ros-gz + firmware tools. Referenced
   by SHA256 digest in `omnilab.yaml`. This is the dep-hell cure.
3. **Skill-packs (`omnilab-skills`)** — optional installable extensions.
   v1 ships `llm-log-analyzer`.
4. **🆕 Agent perception pillar (`omnilab observe`)** — cross-cutting
   primitive that exposes spatial/physical robot state to AI agents
   via dev-defined predicates in `observers.yaml`. Companion to
   `omnilab tune` (action) and `omnilab record` (memory): together they
   form the agent-driven dev loop. The differentiator versus other
   robotics OSes.
5. **CLI (`omnilab`)** — Python (Typer + Rich/Textual). Lives in the
   host image; drives project containers via Podman. During dev runs
   from `/var/home/<user>/omnilab/cli/omnilab/` (mutable `/var`); baked
   in only at release time.

## CLI conventions (architecture rev 3)

The CLI is the agent API. Every command honors:

- **Dual-mode output** — read-only commands accept `--json` for agents
  and emit human-readable text/TUI by default. Same data, different
  shape.
- **Destructive safety** — destructive commands (`clean`, `down`,
  `record --stop`, etc.) accept `--dry-run` (preview only) and
  `--yes` (skip confirmation prompt). Default behavior previews and
  asks.
- **Documented exit codes:** `0` success · `1` generic error · `2`
  invalid args · `3` state error (e.g. container not running) ·
  `4` network/auth error · `5` permission error.
- **Predictable structure** — same flag means the same thing across
  commands.

### v1 CLI surface (full — see spec § CLI for grouping)

| Group | Commands |
|---|---|
| Project lifecycle | `new`, `template list/show/install`, `up`, `down`, `freeze`, `clean` |
| Sim & introspection | `sim`, `inspect`, `observe`, `perf-check` |
| Hardware | `hw scan`, `hw flash`, `micro-ros` |
| Recording | `record` (incl. `--start --background` / `--stop <id>`), `replay` |
| Networking | `pair init`, `pair join`, `pair status` |
| Tuning | `tune` (`--set`, `--save`, light TUI) |
| System | `doctor`, `skill install/list` |

### Parked items (deliberately out of v1 scope)

- **`omnilab compete`** — same machinery as `template`; ship when
  competition orgs (e-Yantra etc.) actually adopt OmniLab. No code
  carried until adoption.
- **`omnilab observe --diff` / `--record` (Layer 3)** — baseline
  comparison; agents in v1 poll `observe --json` and reason over the
  timeseries themselves.
- **Multi-node tuning sessions in `omnilab tune`** — single-node only
  in v1.
- **`--shape` flag in `observe`** — public release uses dev-defined
  predicates (Option B); shape templates wait for org collaboration.

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
| CLI language | Python (Typer + Rich/Textual for TUIs) |
| Bag format | MCAP default; sqlite3 fallback |
| DDS | Cyclone DDS (default), Fast DDS supported |
| Docs | mkdocs-material |

Switch to Ubuntu bootc only when bootc on Ubuntu is GA.

## Conventions

- **Conventional Commits.** Use `feat:`, `fix:`, `chore:`, `ci:`, `docs:`,
  `refactor:`, `test:`. One change per commit.
- **`main` is the default branch.** Squash-merge PRs.
- **Image refs in `omnilab.yaml` MUST be SHA256 digests, not tags.**
  Pinning is the whole reproducibility story. Tags only in CI mirrors.
- **No baked credentials in default ISOs.** Default ISO is **interactive**;
  user creates their account during Anaconda. The opt-in `dev` variant
  uses `host/config.toml.dev` for VM auto-install — only triggered
  explicitly via `workflow_dispatch` with `variant: dev`.
- **The `omnilab` CLI runs from `/var/home/<user>/omnilab/cli/omnilab/`
  during dev** (mutable `/var`, fast iteration). Install editable on the
  test machine with `pip install --user -e .[dev]`; edits on Mac → rsync
  → effective on next invocation, no host image rebuild. Baked into the
  host image only at release (Phase B.5+).
- **Test machine = physical, not VM.** x86_64 emulation under UTM on
  Apple Silicon is ~10× wall-clock slower; physical also has the dGPU
  + USB ports needed for NVIDIA + hardware verification. VM remains
  useful for one-off compatibility checks.
- **Defaults that don't suck** (per spec §"v1 must-do" #6): GUI on by
  default, shadows off, single sun, 320x240 @ 15Hz cameras, RMW pinned
  to Cyclone DDS, non-zero `ROS_DOMAIN_ID`, sim caps at 1.0 RTF.

## Stop-and-ask rules

Pause and ask the user when:

- Something contradicts `project-spec-v1.md`.
- Scope creep beyond the current phase step is tempting (defer with a
  `TODO Phase X.Y` and note it in the session summary).
- The first CI run fails for non-obvious reasons (read logs, try **one**
  fix, then ask).
- Choosing between options where the wrong choice creates rework.
- ISO build risks pushing past 8 GB or violating any hard constraint.

Do **not** ask about:

- Style choices the spec or these conventions already cover.
- Implementation details inside a single component.
- Which tool to use when the spec already named one (Python, Typer,
  Textual, MCAP, Cyclone DDS, etc.).

## Pointer

Full spec: [`./project-spec-v1.md`](./project-spec-v1.md). Read end-to-end
before substantive work.
