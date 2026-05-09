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
# build_agent.sh creates a nested workspace; source whichever install/
# setup.bash actually exposes micro_ros_agent.
for _setup in /opt/micro-ros-agent/install/setup.bash \
              /opt/micro-ros-agent/firmware/install/setup.bash \
              /opt/micro-ros-agent/firmware/agent_ws/install/setup.bash ; do
    [ -f "$_setup" ] && source "$_setup"
done
unset _setup

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
echo "=== Gazebo Harmonic basics ==="
# `gz sim` launching a world needs a display + GL even with
# --headless-rendering, which is unreliable in CI. Verify the binary is
# wired and the sim subcommand is invokable; real simulation testing is
# Phase B.6 territory (smoke-tests.yml with a virtual display + Mesa).
# `gz --version` was dropped in Gazebo Harmonic at the top level — use
# `gz sim --help` which prints usage cleanly.
check "gz sim --help exits 0"  gz sim --help

echo ""
echo "=== micro-ROS agent runnable ==="
# micro_ros_agent --help may exit non-zero on some builds while still
# printing the usage banner — assert on output, not exit code. Also
# sanity-check the binary file landed where build_agent.sh said it would.
MRA_BIN=/opt/micro-ros-agent/install/micro_ros_agent/lib/micro_ros_agent/micro_ros_agent
check "micro_ros_agent binary exists and is executable" test -x "$MRA_BIN"
check "ros2 lists micro_ros_agent as a package executable" \
    bash -c "ros2 pkg executables micro_ros_agent | grep -q micro_ros_agent"

MRA_OUT=$(timeout 5s ros2 run micro_ros_agent micro_ros_agent --help 2>&1 || true)
if echo "$MRA_OUT" | grep -qiE "(usage|--port|transport|micro)"; then
    green "  ✓ micro_ros_agent --help prints recognizable usage text"
    PASS=$((PASS + 1))
else
    red "  ✗ micro_ros_agent --help produced no recognizable output"
    echo "$MRA_OUT" | head -10 | sed 's/^/      /'
    FAILURES+=("micro_ros_agent --help")
    FAIL=$((FAIL + 1))
fi

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
