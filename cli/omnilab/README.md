# cli/omnilab/

The `omnilab` CLI v0 (Phase B sub-step 3 per project-spec-v1.md).
Drives project containers on the OmniLab host via Podman.

## v0 commands

| Command | What it does |
|---|---|
| `omnilab new <name> [-t TEMPLATE]` | Scaffold a project directory with a starter `omnilab.yaml` |
| `omnilab up` | Start the project container (`podman run` with GPU passthrough, Wayland mount, ROS network, project dir mount) |
| `omnilab down` | Stop the project container |
| `omnilab sim [--headless]` | Launch the demo TurtleBot3 + nav2 simulation in the running container |
| `omnilab doctor` | Health check: podman, GPU, image reachability, manifest validity |
| `omnilab version` | Print CLI version |

## Install

### Dev workflow (Phase B; on the OmniLab test machine)

The CLI lives at `/var/home/parth/omnilab/cli/omnilab/` on the test machine
(mutable `/var`, fast iteration). Install editable so changes on the Mac
sync via rsync and run immediately:

```sh
cd /var/home/parth/omnilab/cli/omnilab
pip install --user -e .[dev]
```

After this, `omnilab` is on `$PATH` and edits to `cli/omnilab/omnilab/*.py`
take effect on the next invocation — no host image rebuild.

### Release-time (later phase)

Bake into the host image via `pip install` from the source tree as part
of `host/Containerfile`. Phase B.5 wires this in; v0 is editable-only.

## Tests

```sh
cd cli/omnilab
pip install -e .[dev]
pytest          # unit tests for manifest schema + run-args builder + `new` command
ruff check .    # lint
```

CI runs both on every push that touches `cli/**` via
`.github/workflows/test-cli.yml`.

## Architecture (v0)

```
omnilab/
├── __init__.py     # __version__
├── __main__.py     # console-script entry
├── cli.py          # typer app — registers all commands
├── manifest.py     # OmnilabManifest pydantic schema for omnilab.yaml
├── podman.py       # subprocess wrapper + build_run_args()
├── gpu.py          # detect_gpu() / resolve_gpu_mode()
└── templates/
    └── ros-jazzy-gz-harmonic.yaml   # `omnilab new --template …` source
```

Pure functions are heavily favored — `build_run_args` (in `podman.py`)
takes a manifest + host context and returns a list of `podman` args, which
makes it trivial to unit-test without a real container runtime.

## What v0 does NOT do (deferred per spec)

- `omnilab perf-check`, `omnilab hw scan`, `omnilab hw flash`,
  `omnilab micro-ros`, `omnilab skill install`, `omnilab freeze`
  (Phase B.future / Phase D)
- Real digest-pinning at `omnilab new` time (template ships `:latest`;
  user pins manually until `omnilab freeze` lands)
- USB device passthrough for hardware flashing (Phase B.future)
- Tab completion, man pages, fancy progress UI

## Spec pointers

- Manifest schema: `project-spec-v1.md` § "Manifest schema"
- CLI surface: `project-spec-v1.md` § "CLI surface"
- v1 must-do #6 (defaults that don't suck): drives the `RosConfig` /
  `GazeboDefaults` defaults in `manifest.py`
