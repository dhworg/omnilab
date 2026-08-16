# OmniLab

Container + CLI for ROS 2 + Gazebo developers. Zero-setup,
dependency-hell-proof, **on the Linux machine you already have** — no
reformat, no dual-boot surgery. Also where AI agents can perceive, act on,
and verify robot state — see `omnilab observe`.

> **Status:** architecture rev 5 (container-first delivery). The bootc
> host ISO is deferred to v2 as an optional appliance — see
> [why](#why-not-an-iso). Project image (ROS 2 Jazzy + Gazebo Harmonic) is
> published to GHCR; the CLI ships 15 commands. See
> [`project-spec-v1.md`](./project-spec-v1.md) and
> [`CLAUDE.md`](./CLAUDE.md).

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/dhworg/omnilab/main/install.sh | bash
```

That's it. The installer **never partitions, formats, or otherwise touches
your disk layout**, and never runs `sudo` without asking first.

It installs the `omnilab` CLI onto your existing distribution, then checks
podman, GPU, `dialout` group membership, and the NVIDIA container toolkit,
printing the exact fix command for anything missing.

**Requirements:** Linux (Ubuntu 22.04+, Fedora 40+, Arch), Python 3.11+,
podman. macOS and Windows are out of scope for v1 — `podman run
--network host` behaves differently under a podman machine VM, which breaks
DDS discovery and therefore `omnilab pair`.

<details>
<summary>Alternatives to the one-liner</summary>

```sh
# via pipx
pipx install "omnilab[tui] @ git+https://github.com/dhworg/omnilab@main#subdirectory=cli/omnilab"

# or pip
python3 -m pip install --user "omnilab[tui] @ git+https://github.com/dhworg/omnilab@main#subdirectory=cli/omnilab"

# install from a branch or fork
OMNILAB_REF=my-branch OMNILAB_REPO=https://github.com/you/omnilab bash install.sh
```

The `[tui]` extra pulls in `textual`, which powers the `omnilab gpu` and
`omnilab inspect` interfaces. `OMNILAB_NO_TUI=1` skips it.

</details>

## Quickstart

```sh
omnilab doctor              # verify the host is ready
omnilab new my-robot        # scaffold a project
cd my-robot
omnilab up                  # start the pinned ROS 2 + Gazebo container
omnilab sim                 # launch the demo TurtleBot3 + nav2 sim
```

## GPU

If your dGPU "doesn't work", it is usually not a missing driver. It is a
driver that is installed while **nothing is using it** — the card asleep in
D3cold, `nvidia_uvm` never loaded (it loads lazily, so CUDA sees nothing
while `nvidia-smi` looks fine), missing `/dev/nvidia*` nodes, a CDI spec
gone stale after an update, or everything green and the app still rendering
on llvmpipe.

```sh
omnilab gpu                 # interactive UI: Wake GPU / Apply fixes / Re-probe
omnilab gpu --fix           # headless: apply every auto-fixable repair
omnilab gpu --json          # structured output for agents
```

```
! GPU power state       runtime-suspended (D3cold) — nothing has woken it
✗ NVIDIA kernel modules missing: nvidia_uvm
✗ CDI spec              stale — generated for driver 550.120, running 580.65.06
✗ PRIME render offload  renderer is 'llvmpipe' — rendering on the CPU

4 issue(s) can be fixed automatically — run `omnilab gpu --fix`.
```

Auto-fixable: waking the GPU, loading modules, creating device nodes,
regenerating the CDI spec. Deliberately **not** auto-fixed, because no CLI
can: Secure Boot rejecting unsigned modules, kernel cmdline changes,
firmware/MUX settings, and vendor daemons (`system76-power`, `supergfxctl`,
`optimus-manager`) — those get named alongside their switch command. For
those the win is a 30-second diagnosis instead of a four-hour one.

## Commands

| Group | Commands |
|---|---|
| Project | `new`, `template list/show/install`, `up`, `down`, `clean` |
| Sim & introspection | `sim`, `inspect`, `observe` |
| GPU & health | `gpu`, `doctor` |
| Recording | `record`, `replay` |
| Networking | `pair init/join/status` |
| Tuning | `tune` |

Every read-only command takes `--json`; destructive ones take `--dry-run`
and `--yes`. Exit codes: `0` ok · `1` generic · `2` invalid args · `3` state
· `4` network/auth · `5` permission. The CLI is the agent API.

## Architecture

- **Host** — your own Linux distro. OmniLab supplies the CLI and diagnoses
  host prerequisites; it does not own your kernel, desktop, or GPU driver.
- **`omnilab-projects`** — pinned OCI containers with ROS 2 Jazzy + Gazebo
  Harmonic, referenced by SHA256 digest in `omnilab.yaml`. The dep-hell
  cure: identical bytes everywhere.
- **`omnilab-skills`** — optional installable extensions.
- **Agent perception (`omnilab observe`)** — the differentiator. Reads
  spatial/physical robot state in real time so AI agents can drive the dev
  loop. Companion to `omnilab tune` (action) and `omnilab record` (memory).

Full spec: [`project-spec-v1.md`](./project-spec-v1.md).

## Why not an ISO

OmniLab originally shipped as a bootc-based immutable OS. That was dropped
in [architecture rev 5](./project-spec-v1.md).

bootc is a **whole-machine model and cannot dual-boot safely**. It lays down
its own ESP + boot + ostree layout and bootupd expects to own the
bootloader, so the ceiling for even a correctly-configured interactive
installer is "pick a disk to take over" — not "install alongside your
existing OS". For a student with one laptop and one disk, those are the same
sentence. This is structural; no amount of installer polish fixes it.

A container inherits the OS you already run and sidesteps the question
entirely. The accepted cost is that a container cannot ship a kernel driver,
so the NVIDIA driver and container toolkit become a documented prerequisite
— which is exactly what `omnilab gpu` exists to diagnose and repair.

The bootc host image still builds (manual dispatch only) and is retained for
a possible v2 appliance for users who want the turnkey box. **v1 publishes
no ISO artifacts.**

## Development

```sh
cd cli/omnilab
pip install -e ".[dev,tui]"
pytest          # 288 tests
ruff check .
```

## Documentation

`docs/` contains the mkdocs-material source:

```sh
pip install mkdocs-material
mkdocs serve
```

Hosted docs site: TODO (Phase D).

## License

Apache 2.0 — see [`LICENSE`](./LICENSE).
