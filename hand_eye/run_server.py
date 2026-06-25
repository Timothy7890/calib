#!/usr/bin/env python3
"""Entry point for the hand-eye calibration capture web server.

Examples
--------
Local UI test, no robot (synthetic camera + zero joints):
    python run_server.py --camera-source mock --joint-source mock

On the robot, read joints in-process from rt/lowstate (read-only, never commands):
    python run_server.py --camera-source teleimager --camera-host 127.0.0.1 \
        --joint-source inproc --network-interface eth0 --board-size 11x8

If the camera and the Unitree SDK cannot share one venv, run a small joint
sidecar elsewhere and point the server at it:
    python run_server.py --joint-source http --joints-url http://127.0.0.1:18090/joints
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# Make the `backend` package importable regardless of the current directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="Hand-Eye Calibration Capture Web Server")
    parser.add_argument("--save-path", default="./handeye_data", help="Base directory for captures")
    parser.add_argument("--host", default="0.0.0.0", help="Server bind address")
    parser.add_argument("--port", type=int, default=8131, help="Server port")
    parser.add_argument("--board-size", default="11x8", help="Chessboard inner corners, e.g. 11x8")

    parser.add_argument("--camera-source", choices=["teleimager", "mock"], default="teleimager")
    parser.add_argument("--camera-host", default="127.0.0.1", help="Teleimager camera host")

    parser.add_argument("--joint-source", choices=["inproc", "http", "mock"], default="inproc")
    parser.add_argument("--network-interface", help="DDS network interface for inproc joints, e.g. eth0")
    parser.add_argument("--joints-url", help="JSON endpoint for http joint source")
    parser.add_argument("--dds-peer", default=None,
                        help="Comma-separated robot IP(s) for DDS UNICAST discovery, e.g. 192.168.123.164. "
                             "Use this when multicast is blocked (router/switch instead of direct cable).")

    parser.add_argument("--arm-control", action="store_true",
                        help="Enable right-arm jog control from the web UI (REAL MOTION). "
                             "Reads joints from the same executor; ignores --joint-source.")
    parser.add_argument("--urdf", default=str(Path(__file__).resolve().parents[2] / "g1_d.urdf"),
                        help="URDF path (for joint limits used by --arm-control)")
    parser.add_argument("--base-link", default="torso_link", help="URDF base link (for joint limits)")
    parser.add_argument("--tip-link", default="right_dex1_tool_link", help="URDF tip link (for joint limits)")
    parser.add_argument("--max-joint-speed", type=float, default=0.2,
                        help="Jog slew-rate limit in rad/s (default 0.2, conservative)")
    parser.add_argument("--hand-move-kd", type=float, default=2.0,
                        help="Damping kd for hand-guide (compliant) mode. Higher = more "
                             "resistance/slower sag, but harder to move (default 2.0)")

    parser.add_argument("--no-timestamp-dir", action="store_true",
                        help="Save directly into --save-path instead of a timestamped subfolder")
    args = parser.parse_args()

    from backend import app as app_module
    from backend.camera import HeadCamera
    from backend.detection import parse_board_size
    from backend.joints import make_joint_provider

    # Must run BEFORE any DDS/executor init so the patched config takes effect.
    if args.dds_peer:
        from backend.dds_unicast import enable_unicast_peers
        enable_unicast_peers([p.strip() for p in args.dds_peer.split(",") if p.strip()])

    session_dir = Path(args.save_path)
    if not args.no_timestamp_dir:
        session_dir = session_dir / datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"[handeye] camera_source = {args.camera_source} (host={args.camera_host})")
    camera = HeadCamera(source=args.camera_source, host=args.camera_host)

    if args.arm_control:
        from backend.arm import ArmController, ControllerJointProvider
        from urdf_robot_model import URDFRobotModel

        model = URDFRobotModel(args.urdf, args.base_link, args.tip_link)
        limits = {name: (float(lo), float(hi))
                  for name, (lo, hi) in zip(model.joint_names, model.joint_limits())}
        print("[handeye] *** ARM CONTROL ENABLED — the right arm will be actively held ***")
        print(f"[handeye] max_joint_speed = {args.max_joint_speed} rad/s, limits from {args.urdf}")
        controller = ArmController(
            network_interface=args.network_interface,
            limits=limits,
            max_speed_rad_s=args.max_joint_speed,
            hand_move_kd=args.hand_move_kd,
        )
        controller.start()
        app_module.arm_controller = controller
        joint_provider = ControllerJointProvider(controller)
        print("[handeye] SUPPORT THE ARM before stopping the server (hold stops on exit).")
    else:
        print(f"[handeye] joint_source  = {args.joint_source}")
        joint_provider = make_joint_provider(
            args.joint_source,
            network_interface=args.network_interface,
            http_url=args.joints_url,
        )

    app_module.camera = camera
    app_module.joint_provider = joint_provider
    app_module.save_path = session_dir
    app_module.board_size = parse_board_size(args.board_size)
    app_module.init_state()

    print(f"[handeye] save_path     = {session_dir}")
    print(f"[handeye] joint_names   = {joint_provider.joint_names}")
    print(f"[handeye] serving on http://{args.host}:{args.port}")

    import uvicorn

    uvicorn.run(app_module.app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
