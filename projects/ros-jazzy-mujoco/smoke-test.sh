#!/usr/bin/env bash
# Smoke test for the ros-jazzy-mujoco project image. Runs in CI via
# `podman run --rm IMAGE bash /usr/local/bin/smoke-test.sh`. End users
# can invoke the same script after pulling the image to verify it.
#
# Exits non-zero with a count of failed checks; otherwise exits 0.
#
# Note: -u (nounset) is intentionally NOT set. ROS's setup.bash references
# AMENT_TRACE_SETUP_FILES without a default and trips nounset; that's an
# upstream pattern we work around rather than fight.
set -o pipefail

source /opt/ros/jazzy/setup.bash

PASS=0
FAIL=0
declare -a FAILURES

green() { printf "\033[32m%s\033[0m\n" "$*" ; }
red()   { printf "\033[31m%s\033[0m\n" "$*" ; }

check() {
    local name="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        green "  ✓ $name"
        PASS=$((PASS + 1))
    else
        red   "  ✗ $name"
        FAILURES+=("$name")
        FAIL=$((FAIL + 1))
    fi
}

echo "=== ROS ==="
check "rclcpp present"          bash -c 'ros2 pkg list | grep -q "^rclcpp$"'
check "rclpy importable"        python3 -c 'import rclpy'
check "rosgraph_msgs (Clock)"   python3 -c 'from rosgraph_msgs.msg import Clock'
check "sensor_msgs (JointState)" python3 -c 'from sensor_msgs.msg import JointState'

echo ""
echo "=== Defaults ==="
check "RMW pinned to cyclonedds" bash -c '[ "${RMW_IMPLEMENTATION:-}" = "rmw_cyclonedds_cpp" ]'
check "ROS_DOMAIN_ID set"        bash -c '[ -n "${ROS_DOMAIN_ID:-}" ]'

echo ""
echo "=== MuJoCo ==="
check "mujoco importable" python3 -c 'import mujoco'

# Physics actually steps: build a one-body model and run 100 steps.
STEP_OUT=$(python3 - <<'PY' 2>&1
import mujoco
m = mujoco.MjModel.from_xml_string(
    "<mujoco><worldbody><body><joint type='free'/>"
    "<geom size='0.1' mass='1'/></body></worldbody></mujoco>"
)
d = mujoco.MjData(m)
for _ in range(100):
    mujoco.mj_step(m, d)
print(f"mujoco {mujoco.__version__} stepped 100, t={d.time:.3f}")
PY
)
if echo "$STEP_OUT" | grep -q "stepped 100"; then
    green "  ✓ physics steps ($STEP_OUT)"
    PASS=$((PASS + 1))
else
    red "  ✗ physics did not step"
    echo "$STEP_OUT" | head -5 | sed 's/^/      /'
    FAILURES+=("physics steps")
    FAIL=$((FAIL + 1))
fi

# Headless CPU render — proves the osmesa backend works with no GPU and
# no display, which is what frame-grabbing bridge scripts rely on in CI.
RENDER_OUT=$(MUJOCO_GL=osmesa python3 - <<'PY' 2>&1
import mujoco
m = mujoco.MjModel.from_xml_string(
    "<mujoco><worldbody><light pos='0 0 3'/><body>"
    "<geom size='0.1'/></body></worldbody></mujoco>"
)
d = mujoco.MjData(m)
mujoco.mj_forward(m, d)
with mujoco.Renderer(m, height=64, width=64) as r:
    r.update_scene(d)
    px = r.render()
print(f"rendered {px.shape}")
PY
)
if echo "$RENDER_OUT" | grep -q "rendered (64, 64, 3)"; then
    green "  ✓ headless render via osmesa ($RENDER_OUT)"
    PASS=$((PASS + 1))
else
    red "  ✗ headless osmesa render failed"
    echo "$RENDER_OUT" | head -5 | sed 's/^/      /'
    FAILURES+=("headless render")
    FAIL=$((FAIL + 1))
fi

echo ""
echo "=== ${PASS} passed, ${FAIL} failed ==="
if [ "$FAIL" -gt 0 ]; then
    for f in "${FAILURES[@]}"; do echo "  FAILED: $f"; done
    exit "$FAIL"
fi
exit 0
