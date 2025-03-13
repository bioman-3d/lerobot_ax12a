#!/usr/bin/env python

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import time
import threading
import numpy as np
import time
# import torch
import base64
import cv2
import torch

from lerobot.common.cameras.utils import make_cameras_from_configs
from lerobot.common.constants import OBS_IMAGES, OBS_STATE
from lerobot.common.errors import DeviceAlreadyConnectedError, DeviceNotConnectedError
from ..robot import Robot
from ..utils import ensure_safe_goal_position
from .configuration_daemon_lekiwi import DaemonLeKiwiRobotConfig
import zmq

# TODO(Steven): This doesn't need to inherit from Robot
# But we do it for now to offer a familiar API
# TODO(Steven): This doesn't need to take care of the
# mapping from teleop to motor commands, but given that
# we already have a middle-man (this class) we add it here
class DaemonLeKiwiRobot(Robot):

    config_class = DaemonLeKiwiRobotConfig
    name = "daemonlekiwi"

    def __init__(self, config: DaemonLeKiwiRobotConfig):
        super().__init__(config)
        self.config = config
        self.id = config.id
        self.robot_type = config.type

        self.max_relative_target = config.max_relative_target

        self.remote_ip = config.remote_ip
        self.port_zmq_cmd = config.port_zmq_cmd
        self.port_zmq_observations = config.port_zmq_observations

        self.teleop_keys = config.teleop_keys

        self.zmq_context = None
        self.zmq_cmd_socket = None
        self.zmq_observation_socket = None

        self.last_frames = {}
        self.last_present_speed = {}
        self.last_remote_arm_state = torch.zeros(6, dtype=torch.float32)

        # Define three speed levels and a current index
        self.speed_levels = [
            {"xy": 0.1, "theta": 30},  # slow
            {"xy": 0.2, "theta": 60},  # medium
            {"xy": 0.3, "theta": 90},  # fast
        ]
        self.speed_index = 0  # Start at slow

        # Keyboard state for base teleoperation.
        # self.running = True
        # self.pressed_keys = {
        #     "forward": False,
        #     "backward": False,
        #     "left": False,
        #     "right": False,
        #     "rotate_left": False,
        #     "rotate_right": False,
        # }

        self.is_connected = False
        self.logs = {}

    @property
    def state_feature(self) -> dict:
        # TODO(Steven): Get this from the data fetched?
        # return {
        #     "dtype": "float32",
        #     "shape": (len(self.actuators),),
        #     "names": {"motors": list(self.actuators.motors)},
        # }
        pass

    @property
    def action_feature(self) -> dict:
        return self.state_feature

    @property
    def camera_features(self) -> dict[str, dict]:
        # TODO(Steven): Fetch this info or set it static?
        # cam_ft = {}
        # for cam_key, cam in self.cameras.items():
        #     cam_ft[cam_key] = {
        #         "shape": (cam.height, cam.width, cam.channels),
        #         "names": ["height", "width", "channels"],
        #         "info": None,
        #     }
        # return cam_ft
        pass
    
    def connect(self) -> None:
        if self.is_connected:
            raise DeviceAlreadyConnectedError(
                "LeKiwi Daemon is already connected. Do not run `robot.connect()` twice."
            )

        self.zmq_context = zmq.Context()
        self.zmq_cmd_socket = self.zmq_context.socket(zmq.PUSH)
        zmq_cmd_locator = f"tcp://{self.remote_ip}:{self.port_zmq_cmd}"
        self.zmq_cmd_socket.connect(zmq_cmd_locator)
        self.zmq_cmd_socket.setsockopt(zmq.CONFLATE, 1)

        self.zmq_observation_socket = self.zmq_context.socket(zmq.PULL)
        zmq_observations_locator = f"tcp://{self.remote_ip}:{self.port_zmq_observations}"
        self.zmq_observation_socket.connect(zmq_observations_locator)
        self.zmq_observation_socket.setsockopt(zmq.CONFLATE,1)

        self.is_connected = True

    def calibrate(self) -> None:
        # TODO(Steven): Nothing to calibrate
        pass
    
    # Consider moving these static functions out of the class
    # Copied from robot_lekiwi MobileManipulator class
    @staticmethod
    def degps_to_raw(degps: float) -> int:
        steps_per_deg = 4096.0 / 360.0
        speed_in_steps = abs(degps) * steps_per_deg
        speed_int = int(round(speed_in_steps))
        if speed_int > 0x7FFF:
            speed_int = 0x7FFF
        if degps < 0:
            return speed_int | 0x8000
        else:
            return speed_int & 0x7FFF
    
    # Copied from robot_lekiwi MobileManipulator class
    @staticmethod
    def raw_to_degps(raw_speed: int) -> float:
        steps_per_deg = 4096.0 / 360.0
        magnitude = raw_speed & 0x7FFF
        degps = magnitude / steps_per_deg
        if raw_speed & 0x8000:
            degps = -degps
        return degps
    
    # Copied from robot_lekiwi MobileManipulator class
    def body_to_wheel_raw(
        self,
        x_cmd: float,
        y_cmd: float,
        theta_cmd: float,
        wheel_radius: float = 0.05,
        base_radius: float = 0.125,
        max_raw: int = 3000,
    ) -> dict:
        """
        Convert desired body-frame velocities into wheel raw commands.

        Parameters:
          x_cmd      : Linear velocity in x (m/s).
          y_cmd      : Linear velocity in y (m/s).
          theta_cmd  : Rotational velocity (deg/s).
          wheel_radius: Radius of each wheel (meters).
          base_radius : Distance from the center of rotation to each wheel (meters).
          max_raw    : Maximum allowed raw command (ticks) per wheel.

        Returns:
          A dictionary with wheel raw commands:
             {"left_wheel": value, "back_wheel": value, "right_wheel": value}.

        Notes:
          - Internally, the method converts theta_cmd to rad/s for the kinematics.
          - The raw command is computed from the wheels angular speed in deg/s
            using degps_to_raw(). If any command exceeds max_raw, all commands
            are scaled down proportionally.
        """
        # Convert rotational velocity from deg/s to rad/s.
        theta_rad = theta_cmd * (np.pi / 180.0)
        # Create the body velocity vector [x, y, theta_rad].
        velocity_vector = np.array([x_cmd, y_cmd, theta_rad])

        # Define the wheel mounting angles with a -90° offset.
        angles = np.radians(np.array([240, 120, 0]) - 90)
        # Build the kinematic matrix: each row maps body velocities to a wheel’s linear speed.
        # The third column (base_radius) accounts for the effect of rotation.
        m = np.array([[np.cos(a), np.sin(a), base_radius] for a in angles])

        # Compute each wheel’s linear speed (m/s) and then its angular speed (rad/s).
        wheel_linear_speeds = m.dot(velocity_vector)
        wheel_angular_speeds = wheel_linear_speeds / wheel_radius

        # Convert wheel angular speeds from rad/s to deg/s.
        wheel_degps = wheel_angular_speeds * (180.0 / np.pi)

        # Scaling
        steps_per_deg = 4096.0 / 360.0
        raw_floats = [abs(degps) * steps_per_deg for degps in wheel_degps]
        max_raw_computed = max(raw_floats)
        if max_raw_computed > max_raw:
            scale = max_raw / max_raw_computed
            wheel_degps = wheel_degps * scale

        # Convert each wheel’s angular speed (deg/s) to a raw integer.
        wheel_raw = [DaemonLeKiwiRobot.degps_to_raw(deg) for deg in wheel_degps]

        return {"left_wheel": wheel_raw[0], "back_wheel": wheel_raw[1], "right_wheel": wheel_raw[2]}
    
    # Copied from robot_lekiwi MobileManipulator class
    def wheel_raw_to_body(
        self, wheel_raw: dict, wheel_radius: float = 0.05, base_radius: float = 0.125
    ) -> tuple:
        """
        Convert wheel raw command feedback back into body-frame velocities.

        Parameters:
          wheel_raw   : Dictionary with raw wheel commands (keys: "left_wheel", "back_wheel", "right_wheel").
          wheel_radius: Radius of each wheel (meters).
          base_radius : Distance from the robot center to each wheel (meters).

        Returns:
          A tuple (x_cmd, y_cmd, theta_cmd) where:
             x_cmd      : Linear velocity in x (m/s).
             y_cmd      : Linear velocity in y (m/s).
             theta_cmd  : Rotational velocity in deg/s.
        """
        # Extract the raw values in order.
        raw_list = [
            int(wheel_raw.get("left_wheel", 0)),
            int(wheel_raw.get("back_wheel", 0)),
            int(wheel_raw.get("right_wheel", 0)),
        ]

        # Convert each raw command back to an angular speed in deg/s.
        wheel_degps = np.array([DaemonLeKiwiRobot.raw_to_degps(r) for r in raw_list])
        # Convert from deg/s to rad/s.
        wheel_radps = wheel_degps * (np.pi / 180.0)
        # Compute each wheel’s linear speed (m/s) from its angular speed.
        wheel_linear_speeds = wheel_radps * wheel_radius

        # Define the wheel mounting angles with a -90° offset.
        angles = np.radians(np.array([240, 120, 0]) - 90)
        m = np.array([[np.cos(a), np.sin(a), base_radius] for a in angles])

        # Solve the inverse kinematics: body_velocity = M⁻¹ · wheel_linear_speeds.
        m_inv = np.linalg.inv(m)
        velocity_vector = m_inv.dot(wheel_linear_speeds)
        x_cmd, y_cmd, theta_rad = velocity_vector
        theta_cmd = theta_rad * (180.0 / np.pi)
        return (x_cmd, y_cmd, theta_cmd)
    
    def get_data(self):
        """Polls the video socket for up to 15 ms. If data arrives, decode only
        the *latest* message, returning frames, speed, and arm state. If
        nothing arrives for any field, use the last known values."""

        frames = {}
        present_speed = {}
        remote_arm_state_tensor = torch.zeros(6, dtype=torch.float32)

        # Poll up to 15 ms
        poller = zmq.Poller()
        poller.register(self.zmq_observation_socket, zmq.POLLIN)
        socks = dict(poller.poll(15))
        if self.zmq_observation_socket not in socks or socks[self.zmq_observation_socket] != zmq.POLLIN:
            # No new data arrived → reuse ALL old data
            return (self.last_frames, self.last_present_speed, self.last_remote_arm_state)

        # Drain all messages, keep only the last
        last_msg = None
        while True:
            try:
                obs_string = self.zmq_observation_socket.recv_string(zmq.NOBLOCK)
                last_msg = obs_string
            except zmq.Again:
                break

        if not last_msg:
            # No new message → also reuse old
            return (self.last_frames, self.last_present_speed, self.last_remote_arm_state)

        # Decode only the final message
        try:
            observation = json.loads(last_msg)

            #TODO(Steven): Check this
            images_dict = observation.get("images", {})
            new_speed = observation.get("present_speed", {})
            new_arm_state = observation.get("follower_arm_state", None)

            # Convert images
            for cam_name, image_b64 in images_dict.items():
                if image_b64:
                    jpg_data = base64.b64decode(image_b64)
                    np_arr = np.frombuffer(jpg_data, dtype=np.uint8)
                    frame_candidate = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                    if frame_candidate is not None:
                        frames[cam_name] = frame_candidate

            # If remote_arm_state is None and frames is None there is no message then use the previous message
            if new_arm_state is not None and frames is not None:
                self.last_frames = frames

                remote_arm_state_tensor = torch.tensor(new_arm_state, dtype=torch.float32)
                self.last_remote_arm_state = remote_arm_state_tensor

                present_speed = new_speed
                self.last_present_speed = new_speed
            else:
                frames = self.last_frames

                remote_arm_state_tensor = self.last_remote_arm_state

                present_speed = self.last_present_speed

        except Exception as e:
            print(f"[DEBUG] Error decoding video message: {e}")
            # If decode fails, fall back to old data
            return (self.last_frames, self.last_present_speed, self.last_remote_arm_state)
        return frames, present_speed, remote_arm_state_tensor
    
    # TODO(Steven): The returned space is different from the get_observation of LeKiwiRobot
    # This returns body-frames velocities instead of wheel pos/speeds
    def get_observation(self) -> dict[str, np.ndarray]:
        """
        Capture observations from the remote robot: current follower arm positions,
        present wheel speeds (converted to body-frame velocities: x, y, theta),
        and a camera frame.
        """
        if not self.is_connected:
            raise DeviceNotConnectedError(
                "DaemonLeKiwiRobot is not connected. You need to run `robot.connect()`."
            )

        obs_dict = {}

        # TODO(Steven): Check this
        frames, present_speed, remote_arm_state_tensor = self.get_data()
        body_state = self.wheel_raw_to_body(present_speed)
        body_state_mm = (body_state[0] * 1000.0, body_state[1] * 1000.0, body_state[2])  # Convert x,y to mm/s
        wheel_state_tensor = torch.tensor(body_state_mm, dtype=torch.float32)
        combined_state_tensor = torch.cat((remote_arm_state_tensor, wheel_state_tensor), dim=0)
        
        obs_dict = {OBS_STATE: combined_state_tensor}

        # Loop over each configured camera
        for cam_name, cam in self.cameras.items():
            frame = frames.get(cam_name, None)
            if frame is None:
                # Create a black image using the camera's configured width, height, and channels
                frame = np.zeros((cam.height, cam.width, cam.channels), dtype=np.uint8)
            obs_dict[f"{OBS_IMAGES}.{cam_name}"] = torch.from_numpy(frame)

        return obs_dict

    def from_teleop_action_to_motor_action(self, action):
        # # Speed control
        #     elif key.char == self.teleop_keys["speed_up"]:
        #         self.speed_index = min(self.speed_index + 1, 2)
        #         print(f"Speed index increased to {self.speed_index}")
        #     elif key.char == self.teleop_keys["speed_down"]:
        #         self.speed_index = max(self.speed_index - 1, 0)
        #         print(f"Speed index decreased to {self.speed_index}")
        pass

    # TODO(Steven)
    def send_action(self, action: np.ndarray) -> np.ndarray:
        # Copied from S100 robot
        """Command lekiwi to move to a target joint configuration.

        The relative action magnitude may be clipped depending on the configuration parameter
        `max_relative_target`. In this case, the action sent differs from original action.
        Thus, this function always returns the action actually sent.

        Args:
            action (np.ndarray): array containing the goal positions for the motors.

        Raises:
            RobotDeviceNotConnectedError: if robot is not connected.

        Returns:
            np.ndarray: the action sent to the motors, potentially clipped.
        """
        if not self.is_connected:
            raise DeviceNotConnectedError(
                "ManipulatorRobot is not connected. You need to run `robot.connect()`."
            )

        goal_pos = action

        # Cap goal position when too far away from present position.
        # /!\ Slower fps expected due to reading from the follower.
        if self.config.max_relative_target is not None:
            present_pos = self.actuators.read("Present_Position")
            goal_pos = ensure_safe_goal_position(goal_pos, present_pos, self.config.max_relative_target)

        # Send goal position to the actuators
        # TODO(Steven): Base motors should set a vel instead
        self.actuators.write("Goal_Position", goal_pos.astype(np.int32))

        return goal_pos
    
    def print_logs(self):
        # TODO(Steven): Refactor logger
        pass

    def disconnect(self):
        if not self.is_connected:
            raise DeviceNotConnectedError(
                "LeKiwi is not connected. You need to run `robot.connect()` before disconnecting."
            )
        # TODO(Steven): Consider sending a stop to the remote mobile robot
        self.zmq_observation_socket.close()
        self.zmq_cmd_socket.close()
        self.zmq_context.term()
        self.is_connected = False
    
    def __del__(self):
        if getattr(self, "is_connected", False):
            self.disconnect()
