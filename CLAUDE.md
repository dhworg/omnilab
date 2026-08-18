# OmniLab — Claude Code session bootstrap

Read this first. Then [`project-spec-v1.md`](./project-spec-v1.md) for the
full architecture and scope (**architecture rev 5.1 as of 2026-08-18 —
container-first delivery; Gazebo primary + MuJoCo supported**; see the
amendment blocks at the top of the spec).

## Project summary

OmniLab is a container + CLI giving ROS 2 + Gazebo developers a
zero-setup, dependency-hell-proof environment for sim and hardware work
**on the Linux machine they already have**. It's also where AI agents can
perceive, act on, and verify robot state via a built-in agent-perception
primitive (`omnilab observe`). Beachhead: e-Yantra teams (~6000
participants/year). v1 deliverable: one-command install that turns an
existing Linux laptop into a working sim + hardware dev environment in 15
minutes, **without touching the user's disk layout**.

**Why not the ISO (rev 5, 2026-08-16):** bootc is a whole-machine model and
cannot dual-boot — the ISO erased the author's disk. That's structural, not a
hardening bug. The bootc host, ISO, and qcow2 are deferred to v2 as an
optional appliance. Accepted cost: a container can't ship a kernel driver, so
the NVIDIA driver + container-toolkit is now a documented prerequisite that
`omnilab gpu` diagnoses and repairs rather than something we own.

## Current phase

**Phase A — Bootstrap: DONE.** (bootc/ISO work retained for the v2 appliance.)

**Phase B — Core layers (B.1–B.4 DONE; B.5 replaced by rev 5):**
- ✅ **B.2**: `ros-jazzy-gz-harmonic` project image on GHCR
- ✅ **B.3 / B.4**: full CLI surface — `new`, `up`, `down`, `sim`, `doctor`,
  `inspect`, `clean`, `record`/`replay`, `pair`, `template`, `observe`, `tune`
- 🆕 **B.5′ — Host portability (REPLACES host hardening), IN PROGRESS:**
  - ✅ `omnilab gpu` — NVIDIA diagnosis + repair; TUI + `--json` + `--fix`
  - ✅ PRIME render offload in `omnilab up` (passthrough alone left Gazebo on
    llvmpipe on every hybrid laptop)
  - ✅ `omnilab pair` wiring fixed — `CYCLONEDDS_URI` set + `PairConfig`
    persisted, so the container actually starts on the paired domain
  - ✅ `install.sh` one-command installer
  - ⏸ `podman.py` host-distro portability — `label=disable`, `:Z`,
    `--userns keep-id` are Fedora/bootc-shaped; need Ubuntu/AppArmor handling
  - ⏸ udev + `dialout` as `doctor --host` guidance
- 🔄 **B.6**: smoke matrix retargeted to container-on-host-distro

**Phase C — Verification (gates v1):** NVIDIA tier across host distros,
hardware verification, agent-loop verification.

**Phase D — Polish:** `llm-log-analyzer`, mkdocs site (install chapter
rewritten for the container path). GNOME theming deferred to v2.

See `project-spec-v1.md` § "Phase status" for the up-to-date breakdown.

## Architecture (3 layers + 1 pillar)

1. **Host** — the user's own Linux distro (Ubuntu 22.04+, Fedora 40+,
   Arch). OmniLab supplies the CLI via `install.sh` and diagnoses host
   prerequisites; it does not own the kernel, desktop, or GPU driver.
   *(`omnilab-host` bootc image deferred to v2 — see rev 5.)*
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
| Host | User's existing Linux distro (Linux only in v1) |
| Image format | OCI via GHCR |
| ISO/qcow2 build tool | `bootc-image-builder` (v2 appliance only) |
| Project base | Ubuntu 24.04 |
| ROS 2 | Jazzy Jalisco (LTS to May 2029) |
| Simulator | Gazebo Harmonic (primary); MuJoCo (supported, sim-only — rev 5.1) |
| Desktop | n/a in v1 (GNOME deferred to the v2 appliance) |
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
