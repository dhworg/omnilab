# Project Spec — v1

**Working name:** OmniLab (rename later)
**Status:** v1 scope locked, architecture rev 5.2 (container-first; Gazebo primary + MuJoCo supported incl. visual verification)

> **Amendment 2026-08-18b (rev 5.2):** **MuJoCo visual verification
> ships after all.** Rev 5.1 parked Layer 2 capture on "MuJoCo has no
> external capture API" — true but beside the point: the bridge script
> lives *inside* the sim process, so it can pause physics, render the
> exact current step offscreen (EGL/OSMesa, smoke-test-proven), and
> answer a file-based capture protocol under `.omnilab/mujoco_capture/`
> (no exec channel — /workspace is a bind mount). State and image share
> one `mj_step` by construction, a strictly stronger simultaneity
> guarantee than gz's pause-capture-resume. `omnilab observe --capture`
> therefore reaches `verification_mode: verified` on MuJoCo projects
> with a bridge; bridge-less projects still degrade to
> `no_image_source`.

> **Amendment 2026-08-18 (rev 5.1):** **MuJoCo added as a supported
> simulator** alongside Gazebo Harmonic (which stays primary). Scope is
> deliberate: everything OmniLab does at the ROS layer — observe Layer 1
> predicates, tune, record, pair, inspect-via-/clock — is simulator-agnostic
> and works with MuJoCo unchanged; only the edges dispatch on a new
> `simulator:` manifest field (sim launch, inspect's sim panel, clean's
> process patterns, observe's capture gate). Not ported: Layer 2 frame
> capture (gz-service-based; MuJoCo state lives inside the sim process with
> no external capture API — observe degrades honestly to
> `verification_mode: no_image_source`) and the hardware toolchain (the
> `ros-jazzy-mujoco` image is lean and sim-only; hardware work stays on the
> gz-harmonic image). Ships: the image, a `mujoco-pendulum` template whose
> bridge publishes /clock + /joint_states, and an embedded `mujoco` manifest
> template.

> **Amendment 2026-08-16 (rev 5):** Primary v1 delivery vehicle swapped
> **bootable bootc ISO → installable container + host-installed CLI**. The
> `omnilab-host` bootc image, the ISO, and the qcow2 are deferred to v2 as an
> optional "appliance" path; they are not deleted and the Phase A work carries
> forward.
>
> Rationale, in order of weight:
>
> 1. **bootc is a whole-machine model and cannot dual-boot safely.** The ISO
>    erased the author's disk on boot from USB. Regardless of which variant
>    was booted, the ceiling for a correctly-configured interactive Anaconda
>    on the bootc path is "pick a disk to take over", not "install alongside
>    your existing OS": bootc lays down its own ESP + boot + ostree layout and
>    bootupd expects to own the bootloader. Dual-boot on ostree is not
>    categorically impossible (Silverblue manages it), but it is untested in
>    our build path and is an expert maneuver, not a student one. This is
>    structural — no amount of B.5 hardening fixes it.
> 2. **It contradicts the beachhead.** e-Yantra is ~6000 students/year on
>    shared, school-owned, or already-dual-booting laptops. "Reformat your
>    machine with an 8 GB ISO" is a non-starter for that funnel; `omnilab up`
>    on the machine they already have is not.
> 3. **The value is already built and never needed the OS.** The project image
>    is on GHCR (B.2), the CLI is complete with tests (B.3/B.4), and the
>    differentiator — `observe` / `tune` / `record` — is pure CLI + ROS
>    introspection. The remaining roadmap (B.5 hardening, B.6 dGPU matrix,
>    Phase C NVIDIA tier, Phase D GNOME theming) was almost entirely host
>    work: the whole remaining budget spent on the layer that is not the
>    product.
> 4. **Iteration cost.** Container rebuild is minutes; ISO build plus physical
>    install is hours, and § "Development workflow" already rules out VM
>    testing as ~10x too slow.
>
> Accepted cost: "dependency-hell-proof" weakens at exactly one seam — a
> container cannot ship a kernel driver, so the NVIDIA host driver and
> `nvidia-container-toolkit` become a documented prerequisite rather than
> something we own. That is the single nastiest dependency in robotics and we
> are giving up owning it. Mitigation is `omnilab gpu` (diagnosis + repair),
> not pretending the seam isn't there. The category claim also softens from
> "first Linux OS where AI agents can perceive robot state" to a CLI-level
> claim; the underlying capability is unchanged.
>
> Precedent: the 2026-05-09 (XFCE->Plasma) and 2026-05-14 (Plasma->GNOME)
> amendments swapped components *inside* the architecture. This one changes
> the delivery vehicle and is correspondingly larger — it rewrites
> § "v1 deliverable", § "Install behavior", § "Stack", § "Hard constraints",
> § "Non-goals", and § "Phase status".

> **Amendment 2026-05-14:** Desktop swapped **KDE Plasma 6 → GNOME** on Wayland.
> Rationale: the Figma reference design (centred clock + dropdown notification
> centre + Activities-style brand on the left + dock at bottom) lines up with
> GNOME's native shell pattern; Plasma was being forced into that shape via
> custom panels, plasmoids, and a hand-rolled translucent theme that turned out
> to be fragile (a kwinrc enabling a non-existent effect plugin triggered a
> Plasma login loop on 2026-05-13, ~6 hours to diagnose). GNOME also has a
> noticeably smoother Wayland + NVIDIA Optimus story out of the box, which the
> Phase C NVIDIA verification tier benefits from. Precedent: 2026-05-09
> XFCE→Plasma amendment; same shape, opposite direction.

---

## Pitch

A container + CLI that gives ROS 2 + Gazebo developers a zero-setup, dependency-hell-proof environment for sim and hardware work **on the Linux machine they already have** — no reformat, no dual-boot surgery. **Also: the first Linux OS where AI agents can perceive, act on, and verify robot state — closing the agent-driven dev loop that until now required a human in every iteration.** Beachhead: e-Yantra teams. Expansion: any robotics dev.

## Why

Robotics dev today loses hours to setup: wrong Ubuntu version, broken `rosdep`, RMW chaos, missing udev rules, USB serial that doesn't show up, GPU passthrough that won't. e-Yantra Stage 1 is purely simulation; even strong teams suffer because their environment fights them. The same pain hits every ROS 2 dev on a fresh laptop.

Beyond setup, daily robotics dev is fragmented across dozens of tools (`ros2 topic`, `tf2_echo`, `rqt_*`, `pkill`, `ros2 bag`, `ros2 param set`, manual DDS config, etc.). And AI agents driving this work today are blind — they read numbers but can't see what the robot is doing. OmniLab consolidates the fragmented surfaces and adds the agent-perception primitives nobody else has.

## Users

1. **Beachhead:** e-Yantra participants (~6000+/year)
2. **Expansion:** any ROS 2 / Gazebo developer
3. **AI agents driving robotics dev:** Claude Code today, similar tools tomorrow — designed-for from day 1, not retrofit

---

## Architecture

Three layers + one cross-cutting pillar.

### Layer 1 — Host (`omnilab-host`)

A bootc-based immutable Linux image. Defined as a `Containerfile`, distributed as an OCI image, installed via an ISO generated by `bootc-image-builder`. Updates are atomic: `bootc upgrade` pulls a new image and reboots into it; rollback is one command.

The host contains *only*:
- Linux kernel + base userspace (Fedora bootc base)
- Display server (Wayland), GNOME desktop
- GPU drivers (Mesa for iGPU + NVIDIA proprietary; loads conditionally based on detected hardware)
- Container runtime (Podman) + nvidia-container-toolkit
- udev rules for the four target MCUs + USB cameras
- The `omnilab` CLI
- udev/group infrastructure for hardware (Phase B.5)

The host does **not** contain ROS, Gazebo, or any project-specific tooling. That's the whole point.

### Layer 2 — Project images (`omnilab-projects`)

OCI container images with pinned ROS 2 + Gazebo + extras. Each project's `omnilab.yaml` references an image by SHA256 digest, not tag. This is the actual dep-hell cure: identical bytes everywhere.

Default project images shipped:
- `ros-jazzy-base` — minimal ROS 2 Jazzy
- `ros-jazzy-gz-harmonic` — ROS 2 Jazzy + Gazebo Harmonic + ros-gz + nav2

Custom images: any user can write a `Containerfile` extending these.

### Layer 3 — Skill-packs (`omnilab-skills`)

Optional, installable extensions. Each is a directory with manifest + install script + container layers. Pattern is forward-compatible: future vendor SDKs, RT kernel, cloud burst all slot in here without changing the core.

v1 ships one skill-pack:
- `llm-log-analyzer` — Ollama + small model + service that watches Gazebo logs and ROS topics, suggests parameter tweaks. Disabled by default (RAM cost). One command to enable.

### Pillar: Agent perception (`omnilab observe`)

**The differentiator.** A primitive that lets AI agents (or human devs) read the spatial and physical state of a running robot in real-time without writing custom introspection code per project. Companion to `omnilab tune` (action) and `omnilab record` (memory) — together they form the agent-driven dev loop.

Three internal layers within the pillar:

- **Layer 1 — Spatial summary (v1):** structured snapshot of pose, velocity, contact state, computed `motion_class` label, and active `anomalies`. Computed from existing ROS 2 + Gazebo state via dev-defined predicates in `observers.yaml`.
- **Layer 2 — Frame capture (v1):** on-demand rendered frames with overlays (pose, contact indicators, timestamps), for vision-capable agents.
- **Layer 3 — Comparative diff (v2 — parked):** comparison against a recorded baseline. Cut from v1 — agents instead poll `observe --json` repeatedly and reason over the timeseries themselves.

#### `observers.yaml` predicate language

Devs declare what their robot can do in a YAML file. Schema:

```yaml
motion_classes:
  - name: walking_forward
    when: "linear_velocity.x > 0.05 AND num_feet_in_contact >= 2"

  - name: falling
    when: "abs(orientation.roll) > 45 OR abs(orientation.pitch) > 45"
    duration_min: 100ms       # must persist this long before firing

anomalies:
  - name: foot_slip
    when: "foot.in_contact AND foot.lateral_velocity > 0.1"
    duration_min: 50ms
    cooldown: 500ms           # don't re-fire for 500ms after last fire
```

Built-in noise handling (`duration_min`, `cooldown`, `hysteresis`) eliminates >90% of sensor-bounce false positives.

`omnilab observe --validate observers.yaml` lints predicates for obvious mistakes (missing fields, contradictory rules, threshold values that don't make physical sense).

#### Distribution model

- **Public / open release (v1):** Option B — dev-defined predicates with no shape templates. Example YAMLs (`quadruped.yaml`, `mobile_2d.yaml`, `arm_6dof.yaml`) shipped as documentation only at `docs/examples/observers/`. Devs copy from working examples, not blank pages.
- **Future e-Yantra collaboration:** Option C — same engine, examples promoted to first-class shape templates with `--shape quadruped` flag. No code change required — purely a packaging/UX promotion.

---

## CLI

### Conventions (apply to all commands)

- **Dual-mode output:** every read-only command emits both human-readable TUI/text and `--json` for agents. The CLI is the API.
- **Destructive safety:** every destructive command supports `--dry-run`, prompts for confirmation by default, and accepts `--yes` to skip prompts (for scripting).
- **Documented exit codes:** 0 = success, non-zero = specific error class. Agents rely on this.
- **Predictable structure:** same flag means same thing across commands.

### Commands — full v1 surface

**Project lifecycle:**

```
omnilab new <name>                          # scaffold project + manifest
omnilab template list                       # available starter templates
omnilab template show <name>                # preview a template before installing
omnilab template install <name>             # install template into current project
omnilab up | down                           # start/stop project container
omnilab freeze                              # write lockfile from current state
omnilab clean [--all] [--aggressive]        # safe orphan/state cleanup
                  [--dry-run] [--yes]
```

`omnilab clean` design constraints:
- Scoped to current project by default (`--all` is the nuclear opt-in)
- Container-kill primitives (podman kill / rm --force), not just pkill
- Tree-aware: walks descendants, kills children-first to prevent re-orphaning
- D-state detection: detects uninterruptible kernel sleep and reports honestly that reboot is needed instead of silently failing
- Default conservative; `--aggressive` does the SIGTERM/SIGKILL/sudo escalation chain

**Sim & introspection:**

```
omnilab sim [--headless]                    # launch demo
omnilab inspect [--json]                    # live unified dashboard
omnilab observe [--json] [--capture]        # agent perception primitive
                [--validate observers.yaml]
omnilab perf-check <world.sdf> [--json]     # SDF perf analysis
```

`omnilab inspect` shows nodes (with CPU/mem and warnings), topics (with rates and bandwidth), services, parameters, TF tree (with stale/missing-frame warnings), and Gazebo connection state. TUI for humans, `--json` for agents — same data, different shape.

**Hardware:**

```
omnilab hw scan [--json]                    # enumerate connected MCUs
omnilab hw flash <board> <firmware>         # flash an MCU
omnilab micro-ros                           # start micro-ROS agent
```

**Recording:**

```
omnilab record [--with-screencast]          # smart bag recording
               [--with-cameras]
               [--name <label>] [--duration <t>]
               [--topics <list>]
omnilab record --start --background         # for agent workflows
omnilab record --stop <id>
omnilab replay <recording> [--rate <r>]     # playback with env auto-restore
                            [--start-offset <t>] [--loop]
```

`record` defaults: zstd-compressed, auto-named (timestamp + project), excludes spammy topics (`/tf_static`, GUI internals), max-size 1 GB, stored under `.omnilab/recordings/`. Metadata sidecar captures project + image digest + manifest digest so `replay` knows the exact environment to restore.

`--with-screencast` captures a video of the GUI synced with bag timestamps — the underrated debug feature.

**Networking:**

```
omnilab pair init                           # initiate pairing, prints code
omnilab pair join <code>                    # join an existing pairing
omnilab pair status [--json]                # report current pairing state
```

`omnilab pair` design principle — **agnostic to the network underlay**:
- Same-LAN case: works out of the box, zero accounts or external services required (95% of users)
- Cross-network case: failure-mode hint suggests user-chosen underlay (Tailscale, Headscale, WireGuard, etc.) without forcing or installing one
- The OmniLab project does not depend on, bundle, or integrate with any specific underlay

**Tuning (agent-action complement to observe):**

```
omnilab tune <node> [--set <param>=<value>] # live param set
                    [--save] [--json]
omnilab tune <node>                         # light TUI for humans
```

Scoped primarily as an agent primitive. Together `observe` + `tune` form the closed agent loop: agent reads state → adjusts parameters → reads new state → iterates. The full multi-node TUI experience is parked for v2.

**System:**

```
omnilab doctor [--full] [--json]            # diagnose host + project setup
omnilab skill install <name>
omnilab skill list
```

### Manifest schema (`omnilab.yaml`)

```yaml
name: my-project
host_min_version: 0.1.0
image: ghcr.io/dhworg/ros-jazzy-gz-harmonic@sha256:<digest>
ros:
  rmw: rmw_cyclonedds_cpp
  domain_id: 42
simulator: gazebo          # gazebo (default) | mujoco
mujoco:                    # only for simulator: mujoco
  model: sim/model.xml     # MJCF, relative to project dir
  bridge: sim/bridge.py    # publishes /clock + /joint_states
gazebo:
  default_world: turtlebot3_world.sdf
  defaults:
    shadows: false
    camera_fps: 15
    camera_resolution: [320, 240]
gpu: auto                  # auto | igpu | nvidia
hardware:
  micro_ros: enabled
  boards: [arduino_uno, esp32, stm32_blue_pill, rp2040]
observers: observers.yaml  # optional, for use with omnilab observe
skills: []
pair:
  domain_id: 73            # set by omnilab pair init/join, persisted here
  config: simple_discovery # or discovery_server
```

---

## Templates (v1 set)

Three foundational templates ship in v1:

1. **`nav2-base`** — TurtleBot3-style mobile robot with full nav2 stack in a default arena. Most common starting point.
2. **`micro-ros-blink`** — ESP32 publishing IMU data via micro-ROS. Hardware-side first project.
3. **`quadruped-walker`** — quadruped with gait controller + observers.yaml demonstrating the agent-perception story.

Templates live as OCI images in their own namespace (`ghcr.io/dhworg/templates/<name>:<version>`). Anyone can publish a template by pushing to a registry; users add template sources via `~/.omnilab/config.yaml`.

The `omnilab compete` framing/command (same machinery, organizer-blessed competition kits) is **parked** until competition orgs (e-Yantra etc.) actually adopt OmniLab. No code or maintenance burden until adoption is real.

---

## Install behavior

v1 installs onto the user's **existing Linux distribution**. No disk is
partitioned, formatted, or otherwise modified. Two steps:

1. **CLI** — `curl -fsSL https://raw.githubusercontent.com/dhworg/omnilab/main/install.sh | bash`
   (or `pipx install`). The installer never runs `sudo` without asking.
2. **Host prerequisites** — `omnilab doctor` and `omnilab gpu` probe and
   report, with exact fix commands, on: podman, NVIDIA driver +
   `nvidia-container-toolkit`, udev rules for the four supported MCUs, and
   `dialout` group membership.

**Supported hosts (v1):** Linux only — Ubuntu 22.04+, Fedora 40+, Arch. macOS
and Windows are out of scope: `podman run --network host` has different
semantics under a podman machine VM, which breaks DDS discovery and therefore
`omnilab pair`. WSL2 is a separate investigation, not a v1 promise.

**ISO / qcow2 (deferred to v2):** the bootc host image remains buildable for
future appliance use, but v1 publishes **no ISO artifacts**. The `dev` ISO
variant is removed: it auto-installed (wiping the target disk) and carried
baked credentials, and its only purpose was fast VM iteration on the host
image, which is no longer on the critical path.

---

## v1 deliverable

Five artifacts:

1. Project images on GHCR, digest-pinned (`ros-jazzy-base`,
   `ros-jazzy-gz-harmonic`, plus a future `eyrc-template`)
2. `omnilab` CLI, distributed via `install.sh` / pipx / pip
3. `omnilab gpu` — NVIDIA diagnosis and repair on the user's own host
4. `llm-log-analyzer` skill-pack (optional, disabled by default)
5. Documentation site (mkdocs) covering: install, hello-world, hardware
   quickstart, manifest reference, agent perception, networking,
   troubleshooting, observers.yaml schema reference

**Success criterion:** existing Linux laptop → working sim + hardware dev
environment in 15 minutes, **without modifying the user's disk layout.**

---

## Stack

| Component | Choice |
|---|---|
| Host | User's existing Linux distro (Ubuntu 22.04+, Fedora 40+, Arch) |
| Host base (v2 appliance) | Fedora bootc 42 |
| Image format | OCI, distributed via GHCR |
| Build tool | `bootc-image-builder` (v2 appliance path only) |
| CLI distribution | `install.sh` / pipx / pip |
| Project base | Ubuntu 24.04 (required for ROS 2 Jazzy) |
| ROS 2 | Jazzy Jalisco (LTS until May 2029) |
| Simulator | Gazebo Harmonic (primary; LTS, EOL Sep 2028); MuJoCo (supported, sim-only image) |
| Desktop | GNOME on Wayland (gdm + gnome-shell) |
| Container runtime | Podman + nvidia-container-toolkit |
| GPU tiers | iGPU (Intel/AMD) baseline; NVIDIA proprietary tier |
| CLI language | Python (Typer + Rich/Textual for TUIs) |
| Bag format | MCAP (default) with sqlite3 fallback |
| DDS implementations supported | Cyclone DDS (default), Fast DDS |
| Docs | mkdocs-material |

---

## v1 must-do

1. Bootable interactive ISO installs cleanly on commodity laptops, iGPU and NVIDIA both work
2. Host boots to GNOME, default user (created by user during install) has correct group memberships, udev rules active
3. `omnilab` CLI present, all listed commands functional, all conventions respected
4. `omnilab new --template nav2-base` produces a working project in <60s
5. `omnilab sim` opens Gazebo with the demo world + RViz alongside; robot navigates
6. **Defaults that don't suck (GUI on by default, tuned for iGPU):**
   - Tutorial worlds: shadows off, single sun, no extra lights
   - Default camera resolution 320×240 @ 15Hz
   - RMW pinned to Cyclone DDS
   - Non-zero `ROS_DOMAIN_ID` default
   - Sim caps at 1.0 RTF when iterating
   - `--headless` flag is one keystroke away
7. **Hardware-ready out of the box:**
   - Groups: `dialout`, `plugdev`, `video`, `input` for default user
   - udev rules: FTDI, CP210x, CH340, ST-Link, RP2040 BOOTSEL
   - Toolchain in project image: `arduino-cli`, `esptool`, `dfu-util`, `stm32flash`, `picotool`, `picocom`, `screen`
   - PlatformIO preinstalled in project image
   - micro-ROS agent in project image, started by `omnilab micro-ros`
   - V4L2 utilities for USB cameras
8. **NVIDIA tier:** dGPU detected and used by Gazebo automatically; iGPU users see no difference
9. **`omnilab perf-check`:** parses an SDF, flags shadows, light count, camera resolution/rate, suggests tuning
10. Branding: custom `/etc/os-release`, boot splash, wallpaper
11. **`omnilab inspect`:** TUI + `--json` mode, shows nodes/topics/rates/TF/Gazebo in one view
12. **`omnilab pair`:** LAN-first auto-config; cross-network failure mode with underlay hint
13. **`omnilab clean`:** container-kill, tree-aware, D-state honest
14. **`omnilab record`/`replay`:** smart defaults, screencast, agent flags, env auto-restore
15. **`omnilab template`:** 3 foundational templates (nav2-base, micro-ros-blink, quadruped-walker)
16. **`omnilab observe`:** Layer 1 (spatial summary + motion_class + anomalies via observers.yaml) + Layer 2 (frame capture)
17. **`omnilab tune`:** `--set` and `--save` flags solid; light TUI present

---

## Smoke tests

Seven tests run on every ISO build (in CI) and on first boot (locally):

1. **Boot:** ISO installs, host boots to GNOME, login works
2. **CLI:** `omnilab doctor` returns all green; `--json` output is valid JSON
3. **Sim (headless, automated):** `omnilab sim --headless --test` runs nav2 to a goal, asserts goal reached, exits with PASS/FAIL
4. **Sim (GUI demo):** `omnilab sim` opens, GUI renders, robot moves on command (manual verification, scripted screenshot diff in CI)
5. **Hardware:** `omnilab hw scan` detects each of {Arduino Uno/Nano, ESP32, STM32 Blue Pill, RP2040}; `omnilab hw flash` succeeds for each; micro-ROS topic appears in `ros2 topic list`
6. **NVIDIA tier:** on a dGPU host, sim RTF >= 2× iGPU baseline for the demo world
7. **Agent loop:** `omnilab observe --json` returns valid spatial summary; `omnilab tune <node> --set <param>=<value> --json` applies; `omnilab inspect --json` reflects the change

---

## Hardware support scope (v1)

| Board | Toolchain | Flash via |
|---|---|---|
| Arduino Uno / Nano (ATmega328) | arduino-cli, PlatformIO | USB-serial (FTDI / CH340) |
| ESP32 | esptool, PlatformIO, arduino-cli (esp32 core) | USB-serial (CP210x / CH340) |
| STM32 Blue/Black Pill | stm32flash, dfu-util, PlatformIO | UART, DFU, ST-Link |
| Raspberry Pi Pico / RP2040 | picotool, PlatformIO, arduino-cli (arduino-pico) | USB MSC (BOOTSEL), picotool |

---

## Repo layout

```
omnilab/
├── host/
│   ├── Containerfile
│   ├── overlay/                 # /etc files, branding, default user template
│   └── config.toml.dev          # opt-in dev-variant auto-install (NOT default)
├── projects/
│   ├── ros-jazzy-base/
│   └── ros-jazzy-gz-harmonic/
├── templates/                   # v1 foundational templates
│   ├── nav2-base/
│   ├── micro-ros-blink/
│   └── quadruped-walker/
├── cli/
│   └── omnilab/                 # Python package
│       ├── inspect/
│       ├── observe/
│       ├── pair/
│       ├── clean/
│       ├── record/
│       ├── tune/
│       ├── template/
│       └── ...
├── skills/
│   └── llm-log-analyzer/
├── tests/
│   ├── smoke-boot/
│   ├── smoke-sim/
│   ├── smoke-hw/
│   ├── smoke-nvidia/
│   ├── smoke-agent-loop/
│   └── perf-check/
├── docs/                        # mkdocs source
│   └── examples/
│       └── observers/           # quadruped.yaml, mobile_2d.yaml, arm_6dof.yaml
└── README.md
```

---

## Development workflow

**Bootstrap (one-time, done):**

1. Push to `main` triggered GH Actions `build-host-iso.yml`, produced ISO + qcow2
2. ISO installed on test machine (now: physical machine; previously VM)

**Daily loop — Mac drives test machine via SSH (Tailscale + tmux):**

The 80% case (truly live, no rebuild):
- `/etc/udev/rules.d/*` → `udevadm control --reload && udevadm trigger`
- `/etc/systemd/system/*.service` → `systemctl daemon-reload`
- `/etc/environment`, `/etc/profile.d/*.sh` (RMW pinning, `ROS_DOMAIN_ID`)
- Branding (`/etc/os-release`, GRUB themes, wallpapers)
- The `omnilab` CLI — runs from `/var/home/<user>/omnilab/` (a git checkout in mutable `/var`) during dev. Edit on Mac → rsync → run. Bake into the host image only at release time.

The 20% case (host image rebuild — `/usr`, kernel modules, system packages):

- **Fast path (self-hosted, after bootstrap):** test machine builds its own host image with Podman.
  ```
  rsync Containerfile to test machine
  ssh: podman build -t localhost/omnilab-host:dev .
  ssh: bootc switch --transient localhost/omnilab-host:dev
  ssh: systemctl reboot
  → 2–5 min cycle, rollback is `bootc rollback && reboot`
  ```
- **CI path (for shareable builds):** push to a feature branch, GH Actions builds, `bootc switch` to the GHCR-published image. ~10 min cycle but produces a real artifact and runs the full smoke-test matrix.

**Ephemeral probing:** `bootc usr-overlay` mounts a writable overlay on `/usr` for one-off "does this binary even work" tests. Reboot wipes it.

**Why physical machine over VM:**
- x86_64 emulation under UTM on Apple Silicon is slow enough (~10× wall-clock penalty) to make iteration painful for anything beyond minimal smoke-testing
- Physical test machine has dGPU (required for NVIDIA tier verification) and USB ports (required for hardware testing)
- VM remains useful for one-off compatibility checks but not as primary dev target

**CI matrix:**

`build-host-iso.yml` produces interactive ISO + qcow2 on every push to `main`, on PRs, and on tags. Accepts `variant: dev` input for opt-in auto-install builds.

`smoke-tests.yml` runs the seven smoke tests against the qcow2 in a VM. NVIDIA + hardware + agent-loop tests run on a self-hosted runner (the dGPU machine, registered to GH; only runs on tags or label-triggered).

---

## Non-goals (v1)

- **macOS / Windows hosts** — `--network host` semantics break DDS discovery
  under a podman machine VM. Linux only in v1.
- **Dual-boot alongside another OS on an OmniLab-managed disk** — the reason
  for the rev 5 amendment. If the v2 appliance ISO ships, it ships as
  whole-machine-only, stated up front in the installer UI.
- **Docker as an alternative runtime** — see open questions.

Effort isn't the binding constraint anymore — these are parked for *direction* or *external* reasons:

- **Headtracking / custom Wayland compositor** — different project (was in original PDF, unrelated to dep hell)
- **From-scratch distro / LFS** — different project
- **PREEMPT_RT real-time kernel** — requires custom kernel build/test, RT tuning expertise; different engineering category
- **Vendor industrial-arm SDKs (UR, Franka, Robotiq)** — licensing/distribution complexity; will be skill-packs when needed
- **Proprietary sensor SDKs (RealSense, ZED, Velodyne)** — same; skill-pack pattern
- **Cloud GPU burst (`omnilab run --remote`)** — needs cloud account + billing infra; skill-pack later
- **ARM / Raspberry Pi target** — no test hardware on hand
- **Teensy support** — no test hardware on hand
- **CAN bus tooling** — specialized; skill-pack later
- **`omnilab compete` framing/command** — parked until competition organizers adopt; same machinery as `template` so trivial to ship later
- **`omnilab observe --record` / `--diff`** (Layer 3 baseline comparison) — parked for v2; agents poll `observe --json` and reason over timeseries themselves
- **Multi-node tuning sessions in `omnilab tune`** — single-node tuning only in v1
- **Shape-template `--shape` flag in observe** — public release uses Option B (dev-defined YAML with examples in docs); shape templates wait for org collaboration

---

## Hard constraints

- ROS 2 Jazzy ↔ Ubuntu 24.04 (set by upstream) — applies to project image
- Gazebo Harmonic ↔ ROS 2 Jazzy (set by upstream) — applies to project image
- Project image pull ≤ 4 GB compressed (pull time is the new equivalent
  barrier; students on slow or metered connections feel this exactly where
  they used to feel ISO size). ISO size ≤ 8 GB applies to the v2 appliance.
- iGPU baseline must work without dGPU
- No baked credentials in **any** distributed artifact (enforced by removing
  the dev ISO variant, not by build-time discipline)

---

## v2+ ideas (parked, not lost)

- Ubuntu bootc base (when mature enough to replace Fedora)
- PREEMPT_RT kernel option
- Vendor SDK skill-packs (RealSense, ZED, UR, Franka)
- Cloud GPU burst
- ARM / RPi build
- Teensy support
- CAN bus tooling
- `omnilab compete` framing + organizer-blessed templates (e-Yantra collab)
- Shape-template `--shape` flag in observe
- `omnilab observe --record` and `--diff` with statistical baselines
- Multi-node tuning sessions in `omnilab tune`
- Headtracking compositor (separate project)
- **`omnilab-host` bootc appliance ISO** — whole-machine-only, for users who
  want the turnkey box. Phase A + B.1b + Phase D theming work feeds this.
- **GNOME theming / Figma aesthetic** — moves here with the ISO.

---

## Phase status

**Phase A — Bootstrap (DONE):** Repo scaffold, `build-host-iso.yml`, first ISO building in CI, bootc loop verified end-to-end.

**Phase B — Core layers (B.1–B.4 DONE; B.5 REPLACED by rev 5):**
- ✅ B.1 / B.1b: host desktop work — *superseded by rev 5; retained for v2*
- ✅ B.2: `ros-jazzy-gz-harmonic` project image on GHCR — **carries forward**
- ✅ B.3 / B.4: CLI + observe pillar + tune + record + pair — **carries forward**
- 🆕 **B.5′ — Host portability (REPLACES host hardening), IN PROGRESS:**
  - ✅ `omnilab gpu` — NVIDIA diagnosis + repair (power state, modules, device
    nodes, CDI spec, PRIME render offload), TUI + `--json` + `--fix`
  - ✅ PRIME render offload wired into `omnilab up` — passthrough alone left
    Gazebo on llvmpipe on every hybrid laptop
  - ✅ `omnilab pair` wiring fixed — `CYCLONEDDS_URI` now set, and `pair join`
    persists `PairConfig` so the container starts on the paired domain
  - ✅ `install.sh` one-command installer for existing Linux hosts
  - ⏸ `podman.py` host-distro portability: `--security-opt label=disable`,
    `:Z` relabels, and `--userns keep-id` are Fedora/bootc-shaped and need
    conditional handling on Ubuntu/AppArmor hosts
  - ⏸ udev rules + `dialout` membership as `doctor --host` guidance
- 🔄 B.6: smoke-test matrix — retarget from qcow2-in-VM to
  container-on-host-distro (Ubuntu 22.04 / 24.04, Fedora 40+)

**Phase C — Verification (gates v1 release, can't be parallelized):**
- 🔄 NVIDIA tier: now "works against the user's driver + container-toolkit on
  N host distros", not "our NVIDIA layer works"
- ⏸ Hardware verification with all four MCUs (device passthrough + host udev
  rather than baked rules)
- ⏸ Agent-loop verification (observe + tune integration)

**Phase D — Polish:**
- ⏸ `llm-log-analyzer` skill-pack
- ⏸ Docs site — **install chapter rewritten** for the container path
- 🔀 GNOME theming / Figma aesthetic — **deferred to v2** with the appliance ISO

---

## Open questions (decide before shipping)

- Final project name (OmniLab is placeholder)
- License (recommend Apache 2.0 — permissive + patent grant; aligns with ROS / Gazebo licensing)
- Distribution: GHCR for OCI images; GH Releases for ISO/qcow2; mirror to a CDN later
- Versioning scheme (recommend SemVer with monthly minor releases)

- **Docker as an alternative to Podman.** Many students will already have
  Docker installed, and `podman.py` is podman-specific (`--userns keep-id`,
  `:Z` relabels). Supporting Docker widens the beachhead materially but is
  real work and a second test matrix. Decide before the docs site freezes.
- **Rename.** "OmniLab is an OS" is no longer the pitch. The working name was
  always marked "rename later"; rev 5 is the natural moment.
