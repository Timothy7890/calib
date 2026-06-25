#!/usr/bin/env python3
"""Minimal rt/lowstate sniffer — checks if joint state is received over DDS.

Run:
    conda activate fastapi
    cd /home/robot/vision_arm_control/scripts/handeye
    python test_lowstate.py --iface enp86s0
"""

import argparse
import sys
import time

sys.path.insert(0, "/home/robot/vision_arm_control/unitree_sdk2_python")

from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", default="enp86s0", help="DDS network interface")
    parser.add_argument("--domain", type=int, default=0, help="DDS domain id")
    parser.add_argument("--seconds", type=float, default=3.0, help="listen duration")
    parser.add_argument("--peer", default=None,
                        help="Comma-separated robot IP(s) for UNICAST discovery, e.g. 192.168.123.164")
    args = parser.parse_args()

    if args.peer:
        sys.path.insert(0, "/home/robot/vision_arm_control/scripts/handeye")
        from backend.dds_unicast import enable_unicast_peers
        enable_unicast_peers([p.strip() for p in args.peer.split(",") if p.strip()])

    print(f"[test] ChannelFactoryInitialize(domain={args.domain}, iface={args.iface})")
    ChannelFactoryInitialize(args.domain, args.iface)

    count = {"n": 0}

    def cb(msg):
        count["n"] += 1
        if count["n"] == 1:
            print("[test] GOT lowstate! right_shoulder_pitch q =", msg.motor_state[22].q)

    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(cb, 10)

    time.sleep(args.seconds)
    print(f"[test] messages received in {args.seconds}s: {count['n']}")
    if count["n"] == 0:
        print("[test] RESULT: NO lowstate -> robot not publishing here / wrong domain.")
    else:
        print("[test] RESULT: lowstate OK -> DDS reachable on this machine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
