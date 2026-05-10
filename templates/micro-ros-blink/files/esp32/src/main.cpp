// {{project_name}} — ESP32 micro-ROS IMU publisher skeleton.
// Generated from the omnilab template `micro-ros-blink`.
#include <Arduino.h>
#include <micro_ros_arduino.h>
#include <rcl/rcl.h>
#include <rclc/rclc.h>
#include <rclc/executor.h>
#include <sensor_msgs/msg/imu.h>

rcl_publisher_t publisher;
sensor_msgs__msg__Imu msg;
rclc_executor_t executor;
rclc_support_t support;
rcl_allocator_t allocator;
rcl_node_t node;
rcl_timer_t timer;

static void timer_callback(rcl_timer_t *, int64_t) {
  rcl_publish(&publisher, &msg, nullptr);
}

void setup() {
  set_microros_transports();
  delay(2000);

  allocator = rcl_get_default_allocator();
  rclc_support_init(&support, 0, nullptr, &allocator);
  rclc_node_init_default(&node, "{{project_name}}_imu", "", &support);
  rclc_publisher_init_default(
      &publisher, &node,
      ROSIDL_GET_MSG_TYPE_SUPPORT(sensor_msgs, msg, Imu),
      "/imu/data_raw");
  rclc_timer_init_default(&timer, &support, RCL_MS_TO_NS(100), timer_callback);
  rclc_executor_init(&executor, &support.context, 1, &allocator);
  rclc_executor_add_timer(&executor, &timer);
}

void loop() {
  rclc_executor_spin_some(&executor, RCL_MS_TO_NS(100));
}
