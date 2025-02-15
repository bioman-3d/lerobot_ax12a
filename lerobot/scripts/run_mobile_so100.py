import base64
import json
import threading
import time
from pathlib import Path

import cv2
import torch
import zmq

from lerobot.common.robot_devices.cameras.utils import make_cameras_from_configs
from lerobot.common.robot_devices.motors.configs import FeetechMotorsBusConfig
from lerobot.common.robot_devices.motors.feetech import FeetechMotorsBus, TorqueMode
from lerobot.common.robot_devices.robots.configs import MobileSO100RobotConfig
from lerobot.common.robot_devices.robots.mobileso100 import MobileSO100

# Initialize
config = MobileSO100RobotConfig()
cameras = make_cameras_from_configs(config.cameras)
for _, cam in cameras.items():
    cam.connect()

motor_config = FeetechMotorsBusConfig(
    port="/dev/ttyACM0",
    motors={
        # Arm joints
        "shoulder_pan": [1, "sts3215"],
        "shoulder_lift": [2, "sts3215"],
        "elbow_flex": [3, "sts3215"],
        "wrist_flex": [4, "sts3215"],
        "wrist_roll": [5, "sts3215"],
        "gripper": [6, "sts3215"],
        # Wheel motors (not calibrated)
        "wheel_1": (7, "sts3215"),
        "wheel_2": (8, "sts3215"),
        "wheel_3": (9, "sts3215"),
    },
)
motors_bus = FeetechMotorsBus(motor_config)
motors_bus.connect()

calibration_dir: str = ".cache/calibration/so100"
robot = MobileSO100(motors_bus)

# ZeroMQ Setup
context = zmq.Context()
cmd_socket = context.socket(zmq.PULL)
cmd_socket.setsockopt(zmq.CONFLATE, 1)
cmd_socket.bind("tcp://*:5555")  # TODO(pepijn): Add from config

video_socket = context.socket(zmq.PUSH)
video_socket.setsockopt(zmq.CONFLATE, 1)
video_socket.bind("tcp://*:5556")

print("Robot server started, waiting for commands and streaming video...")

arm_motor_ids = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]


# Arm calibration
def calibrate_follower_arm(motors_bus, calib_dir_str):
    calib_dir = Path(calib_dir_str)
    calib_dir.mkdir(parents=True, exist_ok=True)
    calib_file = calib_dir / "main_follower.json"

    try:
        from lerobot.common.robot_devices.robots.feetech_calibration import run_arm_manual_calibration
    except ImportError:
        print("[WARNING] Calibration function not available. Skipping calibration.")
        return

    if calib_file.exists():
        with open(calib_file) as f:
            calibration = json.load(f)
        print(f"[INFO] Loaded calibration for follower arm from {calib_file}")
    else:
        print("[INFO] Calibration file not found. Running manual calibration...")
        calibration = run_arm_manual_calibration(motors_bus, "so100", "follower_arm", "follower")
        print(f"[INFO] Calibration complete. Saving to {calib_file}")
        with open(calib_file, "w") as f:
            json.dump(calibration, f)
    try:
        motors_bus.set_calibration(calibration)
        print("[INFO] Applied calibration for follower arm.")
    except Exception as e:
        print(f"[WARNING] Could not apply calibration: {e}")


calibrate_follower_arm(motors_bus, calibration_dir)

# Disable torque for the arm motors
for motor in arm_motor_ids:
    motors_bus.write("Torque_Enable", TorqueMode.DISABLED.value, motor)

# Shared data for camera frames (global), plus a lock
latest_images_dict = {}
images_lock = threading.Lock()


# Camera capture loop in a separate thread
def camera_capture_loop(stop_event):
    """
    Continuously capture frames from each camera, encode them, and update latest_images_dict
    as fast as possible (no sleep).
    """
    while not stop_event.is_set():
        local_dict = {}
        for cam_name, cam in cameras.items():
            frame = cam.async_read()
            ret, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            if ret:
                local_dict[cam_name] = base64.b64encode(buffer).decode("utf-8")
            else:
                local_dict[cam_name] = ""
        with images_lock:
            latest_images_dict.update(local_dict)
        time.sleep(0.02)


stop_event = threading.Event()
cam_thread = threading.Thread(target=camera_capture_loop, args=(stop_event,), daemon=True)
cam_thread.start()

start_time = time.perf_counter()
iteration_count = 0
total_elapsed_time = 0.0
print_interval = 100
last_cmd_time = time.time()

try:
    while True:
        loop_start_time = time.perf_counter()
        now = time.time()

        while True:
            try:
                msg = cmd_socket.recv_string(zmq.NOBLOCK)
            except zmq.Again:
                break
            try:
                data = json.loads(msg)
                # Arm position commands
                if "arm_positions" in data:
                    arm_positions = data["arm_positions"]
                    if not isinstance(arm_positions, list):
                        print(f"[ERROR] Invalid arm_positions: {arm_positions}")
                    elif len(arm_positions) < len(arm_motor_ids):
                        print(
                            f"[WARNING] Received {len(arm_positions)} arm_positions, expected {len(arm_motor_ids)}"
                        )
                    else:
                        for motor, pos in zip(arm_motor_ids, arm_positions, strict=False):
                            motors_bus.write("Goal_Position", pos, motor)
                # Wheel commands
                if "raw_velocity" in data:
                    raw_command = data["raw_velocity"]
                    command_speeds = [int(raw_command.get(f"wheel_{i}", 0)) for i in [1, 2, 3]]
                    robot.set_velocity(command_speeds)
                    last_cmd_time = now
            except Exception as e:
                print(f"Error parsing message: {e}")

        # Watchdog: stop if no command for 0.5s
        if now - last_cmd_time > 0.5:
            robot.stop()
            last_cmd_time = now

        current_velocity = robot.read_velocity()
        follower_arm_state = []
        for motor in arm_motor_ids:
            try:
                pos = motors_bus.read("Present_Position", motor)
                follower_arm_state.append(torch.from_numpy(pos))
            except Exception as e:
                print(f"Error reading motor {motor}: {e}")

        if follower_arm_state:
            follower_arm_state = torch.cat(follower_arm_state)
            arm_state_list = follower_arm_state.tolist()
        else:
            arm_state_list = []

        with images_lock:
            images_dict_copy = dict(latest_images_dict)

        # Build an observation and send
        observation = {
            "images": images_dict_copy,
            "present_speed": {
                "1": int(current_velocity.get("1", 0)),
                "2": int(current_velocity.get("2", 0)),
                "3": int(current_velocity.get("3", 0)),
            },
            "follower_arm_state": arm_state_list,
        }
        video_socket.send_string(json.dumps(observation))

        loop_end_time = time.perf_counter()
        iteration_time = loop_end_time - loop_start_time
        total_elapsed_time += iteration_time
        iteration_count += 1

        if iteration_count % print_interval == 0:
            avg_it_time = total_elapsed_time / iteration_count
            freq_hz = 1.0 / avg_it_time
            print(f"Average loop frequency: {freq_hz:.2f} Hz over {iteration_count} iterations")

        # Important to sleep otherwise overload cpu
        time.sleep(0.02)

except KeyboardInterrupt:
    print("Shutting down robot server.")

finally:
    stop_event.set()
    cam_thread.join()
    robot.stop()
    motors_bus.disconnect()
    cmd_socket.close()
    video_socket.close()
    context.term()
