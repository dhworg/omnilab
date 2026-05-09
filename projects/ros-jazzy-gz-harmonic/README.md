# projects/ros-jazzy-gz-harmonic/

The default OmniLab project image: Ubuntu 24.04 + ROS 2 Jazzy desktop +
Gazebo Harmonic + ros-gz + nav2 + turtlebot3 + firmware toolchain +
micro-ROS agent. End users pin it by SHA256 digest from `omnilab.yaml`.

Built and published by `.github/workflows/build-project-images.yml` on
push to `main`. Tags published to GHCR:
- `ghcr.io/dhworg/ros-jazzy-gz-harmonic:latest`
- `ghcr.io/dhworg/ros-jazzy-gz-harmonic:sha-<commit>`

## What's in the image

| Layer | Contents |
|---|---|
| Base | Ubuntu 24.04 (noble) + locale + build-essential + cmake + git + pipx |
| ROS 2 Jazzy | `ros-jazzy-desktop`, `ros-jazzy-rmw-cyclonedds-cpp`, `ros-jazzy-ros-gz`, `ros-jazzy-nav2-bringup`, `ros-jazzy-turtlebot3*`, `python3-colcon-common-extensions`, `python3-rosdep` (initialized) |
| Gazebo Harmonic | `gz-harmonic` (apt repo from packages.osrfoundation.org) |
| Hardware tools | `arduino-cli`, `platformio` (pipx), `esptool` (pipx), `dfu-util`, `stm32flash`, `picotool` (built from source), `picocom`, `screen`, `v4l-utils` |
| micro-ROS agent | Built from source at `/opt/micro-ros-agent/`; sourced from `/etc/bash.bashrc` |
| Defaults | `RMW_IMPLEMENTATION=rmw_cyclonedds_cpp`, `ROS_DOMAIN_ID=42`, ROS + micro-ROS auto-sourced in interactive bash |

Self-test ships at `/usr/local/bin/smoke-test.sh`.

## Verifying the image

```sh
podman pull ghcr.io/dhworg/ros-jazzy-gz-harmonic:latest
podman run --rm ghcr.io/dhworg/ros-jazzy-gz-harmonic:latest \
    bash /usr/local/bin/smoke-test.sh
```

Inside the OmniLab host VM, the same image is pulled by `omnilab up`
(Phase B sub-step 3 — CLI). For now, `podman run -it` is the way to
poke at it interactively.

## Pinning by digest

Once published, copy the `:sha-<commit>` digest into your project's
`omnilab.yaml`:

```yaml
image: ghcr.io/dhworg/ros-jazzy-gz-harmonic@sha256:<digest>
```

(Per spec § Manifest schema — tags are CI conveniences; digests are the
real reproducibility story.)

<!-- TODO Phase B.3.future — split into ros-jazzy-base + this image
extending it (per spec § Layer 2 listing all three default project
images). For now this image is built standalone from ubuntu:24.04. -->
