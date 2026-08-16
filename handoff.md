# Session handoff — 2026-05-13

Two-paragraph version for whoever picks up next.

## What just shipped — agent perception/action loop end-to-end

The quadruped at `/home/parth/Downloads/ROS_quadruped` walks. Trot gait,
500+ continuous seconds with body height stable at z = 0.21 m (the design
stand height — slip would drop this toward 0.008 m, which is the prior
failure mode). The corrected controller is
`scripts/gait_v2.py` with `GAIT_PERIOD_S=2.5 THIGH_AMP_RAD=0.10
KNEE_AMP_RAD=0.30`. The fix was a single sign flip: `thigh = -THIGH_SIGN
* THIGH_AMP * cos(phi)` — the previous `+` made the stance half (foot
planted, knee straight) sweep the thigh forward, which is anti-propulsive
and produced 1.5 cm/s of leg-slip instead of walking. Saved as project
memory `project_quadruped_gait_phasing.md`.

In plain English: the robot used to drag its feet in the wrong direction
during the part of the step when feet are on the ground, so it skidded
forward at ~1 inch per second while slowly collapsing. One minus sign
flipped the leg-swing direction so the feet push backward like real
walking. It's been walking for eight minutes straight now.

## Live state on the test machine

- Host: `parth@192.168.29.122` (fedora bootc 42, KDE Plasma Wayland)
- Container: `quadruped_test` (image
  `ghcr.io/dhworg/ros-jazzy-gz-harmonic:latest`), 16+ min uptime
- Sim: `gz sim` running `quadruped_harmonic.world`, single instance
- Gait controller: `python3 /workspace/scripts/gait_v2.py` (PID 3422
  in-container), logging to `/workspace/.gait.log`
- Last pose read: `(0.42, -0.52, 0.21)` rpy ≈ design_orientation
- Architecture: Layer 1 (numeric `omnilab observe`) + Layer 2 (screenshot
  capture, currently degraded — see below) + Layer 3 (derived semantic
  layer in `observe_derived.py` reading `derived_config.yaml`)

## Open issues found this session

1. **`/gui/screenshot` render buffer wedged.** Service returns
   `data: true` and writes new PNG files at fresh paths, but the bytes
   are identical (same MD5) across calls — the gz GUI offscreen render
   target isn't refreshing. User confirms the live window renders the
   bot fine; the wedge is in the capture pipeline only. Affects
   cross-modal verification per `feedback_use_the_loop.md` — for now,
   z-height stability is the slip-vs-walk discriminator. Likely
   Wayland/Ogre rendertarget issue. Not fixed this session.

2. **gz silently no-ops on wrong model name.** `set_pose` with
   `name: "Quadruped_Sim"` (the URDF declaration) returns `data: true`
   but does nothing — the spawned model is named `quadruped`
   (lowercase). Cost ~30 min before catching it. Saved as
   `project_quadruped_model_name.md`. **Always verify `gz model -m
   quadruped` after pose-changing service calls.**

3. **`/gui/follow/offset` uses entity-local frame.** With this bot's
   roll=π/2 design_orientation, "up" in entity-local is "lateral" in
   world. `/gui/move_to/pose` with absolute world coordinates +
   computed look-at quaternion is the only reliable camera control.
   Pitch sign in look-at math: `pitch = atan2(-dz, dist_xy)` (positive
   pitch = look down toward target below camera in gz convention).
   Previous session's note said the opposite — that was wrong, fixed
   here.

4. **firewalld is still off on the test box.** Stopped during the
   gz-transport debugging session. Needs proper rules (only allow
   multicast 224.0.0.7 for gz discovery) before B.5 host hardening
   bakes the host image.

## Files touched this session

- `~/Downloads/ROS_quadruped/scripts/gait_v2.py` (on test machine,
  not in dhworg/omnilab repo) — gait phasing flip
- `~/.claude/projects/.../memory/project_quadruped_gait_phasing.md`
  (new)
- `~/.claude/projects/.../memory/project_quadruped_model_name.md`
  (new)
- `~/.claude/projects/.../memory/MEMORY.md` (index updated)

Nothing in dhworg/omnilab was modified this session. The CLI code under
`cli/omnilab/omnilab/` is unchanged; the bug surface was all in the user's
project workspace.

## Next phase (per `project-spec-v1.md` § Phase status)

**B.5 — host hardening.** Spec says: udev rules, group memberships,
NVIDIA stack, branding (fastfetch, fonts, wallpapers, KDE theming).

Done this session (live on test machine, also baked into
`host/Containerfile`):

- NVIDIA proprietary driver 580.159.03 + CUDA libs +
  nvidia-container-toolkit installed via rpm-ostree layer
- Kernel cmdline: `rd.driver.blacklist=nouveau modprobe.blacklist=nouveau
  nvidia-drm.modeset=1` via `/usr/lib/bootc/kargs.d/10-nvidia.toml`
- `omnilab-nvidia-cdi.service` runs `nvidia-ctk cdi generate` on first
  boot so podman picks up the GPU without manual setup
- Verified: `nvidia-smi` on host shows GTX 1050 with driver 580.159.03,
  and `podman run --device nvidia.com/gpu=all --security-opt=label=disable
  nvidia/cuda:... nvidia-smi` works inside the container

Still pending for B.5:

- Proper firewalld rules covering gz multicast discovery
- Investigate `/gui/screenshot` wedge so the agent-perception pillar's
  visual verification is reliable (B.5 or B.6, not P0 for v1 if numeric
  proof is sufficient for now)
- Custom SELinux module so podman GPU access doesn't need
  `--security-opt=label=disable` (memory:
  project_podman_nvidia_selinux.md)
- udev rules for hardware (FTDI/CP210x/CH340/ST-Link/RP2040)
- Group memberships for default user (dialout/plugdev/video/input)
- Branding overlay (`/etc/os-release`, GRUB theme, wallpaper)
- Rebuild `omnilab-host` image with the NVIDIA changes (Containerfile
  updated; task #32)

## Quick recovery if the gait dies

```bash
# kill any old gait, hold q=0 briefly, reset pose, restart gait
ssh parth@192.168.29.122 'podman exec quadruped_test bash -c "
pkill -f gait_v2 hold_q0 || true
sleep 1
# Park legs at q=0 so reset_pose doesn't immediately collapse
nohup python3 /tmp/hold_q0.py >/tmp/hold_q0.log 2>&1 &
sleep 1
gz service -s /world/default/set_pose --reqtype gz.msgs.Pose --reptype gz.msgs.Boolean \
  --timeout 3000 --req \"name: \\\"quadruped\\\" position { x: 0 y: 0 z: 0.40 } \
  orientation { x: 0.7071068 y: 0 z: 0 w: 0.7071068 }\"
sleep 2
pkill -f hold_q0
export GAIT_PERIOD_S=2.5 THIGH_AMP_RAD=0.10 KNEE_AMP_RAD=0.30
nohup python3 /workspace/scripts/gait_v2.py >/workspace/.gait.log 2>&1 &
"'
```

## Memory pointers worth re-reading

- `feedback_use_the_loop.md` — never claim physical state from numbers
  alone; always pair with image. Followed this session by using z-height
  as the slip-vs-walk discriminator since the screenshot pipeline was
  degraded.
- `feedback_calibration_simultaneity.md` — pause-capture-resume is in
  `observe_sources.LiveStateSource.calibrated_sample`.
- `project_quadruped_q0_stand.md` — q=0 IS the stand pose for this bot.
- `project_quadruped_gait_phasing.md` (new) — propulsion sign.
- `project_quadruped_model_name.md` (new) — `quadruped` not
  `Quadruped_Sim`.
