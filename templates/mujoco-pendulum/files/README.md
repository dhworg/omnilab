# {{project_name}}

MuJoCo pendulum starter for OmniLab. A damped pendulum starts displaced
90°, swings, and settles — with a ROS 2 bridge publishing `/clock` and
`/joint_states` so the OmniLab agent loop works end to end.

```sh
omnilab up            # start the ros-jazzy-mujoco container
omnilab sim           # bridge + viewer (add --headless for no window)
omnilab observe       # swinging → settled via observers.yaml
omnilab record        # bag /clock + /joint_states
```

## Layout

- `sim/pendulum.xml` — the MJCF model. Edit damping/masses freely.
- `sim/mujoco_bridge.py` — steps physics in real time, publishes ROS
  topics. Swap in your own model and it keeps working as long as the
  model has hinge/slide joints.
- `observers.yaml` — motion classes (`swinging`, `settled`) and a
  `runaway_spin` anomaly for `omnilab observe`.

## MuJoCo caveats (v1)

- No frame capture: `omnilab observe` reports numeric signatures only
  (`verification_mode: no_image_source`), never visually verified ones.
- The `ros-jazzy-mujoco` image is sim-only — no hardware toolchain or
  micro-ROS agent. Use the `ros-jazzy-gz-harmonic` image for hardware.
