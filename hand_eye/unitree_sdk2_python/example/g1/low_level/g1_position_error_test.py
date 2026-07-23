import argparse
import math
import sys
import time

import numpy as np

from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.core.channel import ChannelPublisher
from unitree_sdk2py.core.channel import ChannelSubscriber
from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
from unitree_sdk2py.utils.crc import CRC
from unitree_sdk2py.utils.thread import RecurrentThread


G1_NUM_MOTOR = 29
TARGET_JOINTS = list(range(22, 29))
DEFAULT_TARGET_Q = [-0.069597, -0.270574, -0.014017, 0.179214, 0.097234, 0.653286, 0.25104]

KP = [
    60, 60, 60, 100, 40, 40,
    60, 60, 60, 100, 40, 40,
    60, 40, 40,
    40, 40, 40, 40, 40, 40, 40,
    40, 40, 40, 40, 40, 40, 40,
]

KD = [
    1, 1, 1, 2, 1, 1,
    1, 1, 1, 2, 1, 1,
    1, 1, 1,
    1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1,
]


class Mode:
    PR = 0


class PositionErrorTester:
    def __init__(self, args):
        self.args = args
        self.control_dt = 0.002
        self.low_cmd = unitree_hg_msg_dds__LowCmd_()
        self.low_state = None
        self.mode_machine = 0
        self.have_state = False
        self.crc = CRC()
        self.start_q = None
        self.target_q = np.array(args.q, dtype=float)
        self.hold_samples = []
        self.done = False
        self.start_time = None
        self.traj_duration = args.move_duration

    def init(self):
        self.msc = MotionSwitcherClient()
        self.msc.SetTimeout(5.0)
        self.msc.Init()

        if not self.args.keep_motion_mode:
            status, result = self.msc.CheckMode()
            while result["name"]:
                print("Releasing motion mode:", result["name"])
                self.msc.ReleaseMode()
                status, result = self.msc.CheckMode()
                time.sleep(1)

        self.lowcmd_publisher = ChannelPublisher("rt/lowcmd", LowCmd_)
        self.lowcmd_publisher.Init()

        self.lowstate_subscriber = ChannelSubscriber("rt/lowstate", LowState_)
        self.lowstate_subscriber.Init(self.low_state_handler, 10)

    def low_state_handler(self, msg: LowState_):
        self.low_state = msg
        if not self.have_state:
            self.mode_machine = msg.mode_machine
            self.have_state = True

    def start(self):
        print("Waiting for first low_state...")
        while not self.have_state:
            time.sleep(0.1)

        self.start_q = np.array([self.low_state.motor_state[i].q for i in range(G1_NUM_MOTOR)], dtype=float)
        delta = self.target_q - self.start_q[TARGET_JOINTS]
        if self.args.max_joint_speed > 0:
            speed_limited_duration = np.max(np.abs(delta)) * math.pi / (2.0 * self.args.max_joint_speed)
            self.traj_duration = max(self.args.move_duration, float(speed_limited_duration))

        print("Target joints:", TARGET_JOINTS)
        print("Start q:", " ".join(["%.6f" % v for v in self.start_q[TARGET_JOINTS]]))
        print("Target q:", " ".join(["%.6f" % v for v in self.target_q]))
        print("Move duration: %.3f s" % self.traj_duration)
        print("Hold seconds: %.3f s" % self.args.hold_seconds)

        self.start_time = time.time()
        self.thread = RecurrentThread(interval=self.control_dt, target=self.write_low_cmd, name="position_error_test")
        self.thread.Start()

    def desired_target_q(self, elapsed):
        if self.traj_duration <= 0.0:
            return self.target_q, np.zeros(len(TARGET_JOINTS))

        ratio = np.clip(elapsed / self.traj_duration, 0.0, 1.0)
        blend = 0.5 - 0.5 * math.cos(math.pi * ratio)
        blend_dot = 0.5 * math.pi * math.sin(math.pi * ratio) / self.traj_duration
        delta = self.target_q - self.start_q[TARGET_JOINTS]
        return self.start_q[TARGET_JOINTS] + delta * blend, delta * blend_dot

    def write_low_cmd(self):
        now = time.time()
        elapsed = now - self.start_time
        desired_q, desired_dq = self.desired_target_q(elapsed)

        self.low_cmd.mode_pr = Mode.PR
        self.low_cmd.mode_machine = self.mode_machine

        for i in range(G1_NUM_MOTOR):
            self.low_cmd.motor_cmd[i].mode = 1
            self.low_cmd.motor_cmd[i].tau = 0.0
            self.low_cmd.motor_cmd[i].q = float(self.start_q[i])
            self.low_cmd.motor_cmd[i].dq = 0.0
            self.low_cmd.motor_cmd[i].kp = KP[i]
            self.low_cmd.motor_cmd[i].kd = KD[i]

        for local_index, joint in enumerate(TARGET_JOINTS):
            self.low_cmd.motor_cmd[joint].q = float(desired_q[local_index])
            self.low_cmd.motor_cmd[joint].dq = float(desired_dq[local_index])

        self.low_cmd.crc = self.crc.Crc(self.low_cmd)
        self.lowcmd_publisher.Write(self.low_cmd)

        if elapsed >= self.traj_duration:
            actual_q = np.array([self.low_state.motor_state[j].q for j in TARGET_JOINTS], dtype=float)
            self.hold_samples.append(actual_q)

        if elapsed >= self.traj_duration + self.args.hold_seconds:
            self.done = True

    def print_result(self):
        if not self.hold_samples:
            print("No hold samples were collected.")
            return

        samples = np.array(self.hold_samples)
        errors = samples - self.target_q
        abs_errors = np.abs(errors)
        mean_abs = np.mean(abs_errors, axis=0)
        max_abs = np.max(abs_errors, axis=0)
        final_q = samples[-1]
        final_error = final_q - self.target_q

        print("")
        print("Position error result, rad")
        print("joint  target_q   final_q    final_err  mean_abs   max_abs")
        for idx, joint in enumerate(TARGET_JOINTS):
            print(
                "%5d %9.6f %9.6f %+10.6f %9.6f %9.6f"
                % (joint, self.target_q[idx], final_q[idx], final_error[idx], mean_abs[idx], max_abs[idx])
            )


def parse_args():
    parser = argparse.ArgumentParser(description="G1 right arm low-level position error test for joints 22-28.")
    parser.add_argument("interface", nargs="?", help="Network interface connected to the robot, e.g. eth0.")
    parser.add_argument("--q", nargs=7, type=float, default=DEFAULT_TARGET_Q, metavar="Q")
    parser.add_argument("--move-duration", type=float, default=3.0)
    parser.add_argument("--hold-seconds", type=float, default=2.0)
    parser.add_argument("--max-joint-speed", type=float, default=0.4)
    parser.add_argument("--keep-motion-mode", action="store_true", help="Do not release existing motion mode.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print("WARNING: Low-level control can move the robot directly. Clear the area and keep emergency stop ready.")
    print("This script controls joints 22-28 and holds other G1 joints at their initial positions.")
    input("Press Enter to continue...")

    if args.interface:
        ChannelFactoryInitialize(0, args.interface)
    else:
        ChannelFactoryInitialize(0)

    tester = PositionErrorTester(args)
    tester.init()
    tester.start()

    try:
        while not tester.done:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("Interrupted.")
        sys.exit(1)

    tester.print_result()
