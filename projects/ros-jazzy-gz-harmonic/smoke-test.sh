#!/usr/bin/env bash
# Phase B sub-step 2 smoke test for the ros-jazzy-gz-harmonic project
# image. Runs in CI via `podman run --rm IMAGE bash /usr/local/bin/smoke-
# test.sh`. End users can invoke the same script after pulling the image
# to verify it.
#
# Exits non-zero with a count of failed checks; otherwise exits 0.
#
# Note: -u (nounset) is intentionally NOT set. ROS's setup.bash references
# AMENT_TRACE_SETUP_FILES without a default and trips nounset; that's an
# upstream pattern we work around rather than fight.
set -o pipefail

source /opt/ros/jazzy/setup.bash
source /opt/micro-ros-agent/install/setup.bash

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

echo "=== ROS packages ==="
check "ros-jazzy-desktop core (rclcpp present)"  bash -c 'ros2 pkg list | grep -q "^rclcpp$"'
check "nav2_bringup present"                      bash -c 'ros2 pkg list | grep -q "^nav2_bringup$"'
check "ros_gz_bridge present"                     bash -c 'ros2 pkg list | grep -q "^ros_gz_bridge$"'
check "turtlebot3 present"                        bash -c 'ros2 pkg list | grep -q "^turtlebot3$"'
check "demo_nodes_cpp present"                    bash -c 'ros2 pkg list | grep -q "^demo_nodes_cpp$"'

echo ""
echo "=== Hardware tools on PATH ==="
for bin in gz arduino-cli platformio esptool dfu-util stm32flash picotool picocom screen v4l2-ctl colcon rosdep; do
    check "$bin on PATH" command -v "$bin"
done

echo ""
echo "=== Defaults ==="
check "RMW pinned to cyclonedds" bash -c '[ "${RMW_IMPLEMENTATION:-}" = "rmw_cyclonedds_cpp" ]'
check "ROS_DOMAIN_ID set"        bash -c '[ -n "${ROS_DOMAIN_ID:-}" ]'

echo ""
echo "=== ros2 talker actually publishes ==="
TALKER_OUT=$(timeout 3s ros2 run demo_nodes_cpp talker 2>&1 || true)
if echo "$TALKER_OUT" | grep -q "Publishing:"; then
    green "  ✓ ros2 talker emits 'Publishing:'"
    PASS=$((PASS + 1))
else
    red "  ✗ ros2 talker did not emit 'Publishing:' inside 3s"
    echo "$TALKER_OUT" | head -10 | sed 's/^/      /'
    FAILURES+=("ros2 talker")
    FAIL=$((FAIL + 1))
fi

echo ""
echo "=== gz sim --headless-rendering doesn't crash ==="
GZ_OUT=$(timeout 5s gz sim --headless-rendering -s shapes.sdf 2>&1 || true)
if echo "$GZ_OUT" | grep -qiE "(simulation|loading world|world loaded|loaded|fps:|started)"; then
    green "  ✓ gz sim got past initialization"
    PASS=$((PASS + 1))
else
    red "  ✗ gz sim didn't show signs of life within 5s"
    echo "$GZ_OUT" | head -20 | sed 's/^/      /'
    FAILURES+=("gz sim")
    FAIL=$((FAIL + 1))
fi

echo ""
echo "=== micro-ROS agent runnable ==="
check "micro_ros_agent --help" bash -c "ros2 run micro_ros_agent micro_ros_agent --help"

echo ""
echo "=================================="
echo "RESULT: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
    echo "Failed checks:"
    for f in "${FAILURES[@]}"; do
        echo "  - $f"
    done
    exit "$FAIL"
fi
echo "All smoke checks passed."
exit 0
