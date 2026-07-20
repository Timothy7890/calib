#!/usr/bin/env python3
"""3D-3D 手眼标定（眼在手外，联合估计指尖偏移）采集/解算服务入口。

示例
----
本地联调（无相机无机器人）:
    python run_server.py --camera-source mock --pose-source mock

H2 真机（DDS 只读 rt/lowstate + IK_replay FK，右臂）:
    python run_server.py --camera-source orbbec --camera-serial CP0BB53000FS \
        --pose-source h2 --network-interface eth0

手腕位姿手填 / sidecar:
    python run_server.py --camera-source orbbec                       # manual 手填
    python run_server.py --pose-source http --pose-url http://127.0.0.1:18091/pose
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="Hand-Eye 3D point-pair calibration server")
    parser.add_argument("--save-path", default="./handeye3d_data", help="采样数据目录")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8132)
    parser.add_argument("--no-timestamp-dir", action="store_true",
                        help="直接存到 --save-path，不建时间戳子目录")

    parser.add_argument("--camera-source", choices=["orbbec", "mock"], default="orbbec")
    parser.add_argument("--camera-serial", default=None,
                        help="Orbbec 相机序列号（不填用第一台；video_tools 里 video14 那台是 CP0BB53000FS）")

    parser.add_argument("--pose-source", choices=["manual", "http", "h2", "mock"],
                        default="manual", help="手腕位姿来源（默认 manual 手填）")
    parser.add_argument("--pose-url", help="http 模式的 JSON 端点，返回 {\"T\": 4x4} 或 {\"xyz\",\"rpy\"}")
    parser.add_argument("--network-interface", help="h2 模式的 DDS 网卡，如 eth0")
    parser.add_argument("--arm", choices=["right", "left"], default="right",
                        help="h2 模式用哪条手臂（默认 right）")
    parser.add_argument("--base-link", default=None,
                        help="h2 模式的基座 link（默认取 h2.yaml 的 torso_link）")

    parser.add_argument("--arm-control", action="store_true",
                        help="启用手臂点动/卸力控制（发布 rt/arm_sdk，真机会动！"
                             "确保没有其他程序在控制手臂）")
    parser.add_argument("--arm-max-speed", type=float, default=0.2,
                        help="点动最大关节速度 rad/s（默认 0.2）")
    args = parser.parse_args()

    from backend import app as app_module
    from backend.camera import make_camera
    from backend.robot import make_pose_provider

    session_dir = Path(args.save_path)
    if not args.no_timestamp_dir:
        session_dir = session_dir / datetime.now().strftime("%Y%m%d_%H%M%S")

    camera = make_camera(args.camera_source, serial=args.camera_serial)
    print(f"[handeye3d] camera = {args.camera_source} (serial={args.camera_serial or 'auto'})")
    camera.start()
    print(f"[handeye3d] camera info: {camera.info()}")

    arm_controller = None
    if args.arm_control:
        from backend.arm import H2ArmController

        print("[handeye3d] !!! 手臂控制已启用：将发布 rt/arm_sdk，真机会动。")
        print("[handeye3d] !!! 请确认没有其他程序（遥操作等）在控制手臂。")
        arm_controller = H2ArmController(
            arm=args.arm, network_interface=args.network_interface,
            max_speed_rad_s=args.arm_max_speed,
        )
        arm_controller.start()
        print(f"[handeye3d] arm_control = on ({args.arm}, "
              f"max_speed={args.arm_max_speed} rad/s)，已在当前姿态保持")

    pose_provider = make_pose_provider(
        args.pose_source, http_url=args.pose_url,
        network_interface=args.network_interface,
        arm=args.arm, base_link=args.base_link,
        q_reader=arm_controller.read_measured if arm_controller else None,
    )
    print(f"[handeye3d] pose_source = {pose_provider.source} (auto={pose_provider.available}, "
          f"base={pose_provider.base_link}, wrist={pose_provider.wrist_link})")

    app_module.camera = camera
    app_module.pose_provider = pose_provider
    app_module.arm_controller = arm_controller
    app_module.save_path = session_dir
    app_module.init_state()

    print(f"[handeye3d] save_path = {session_dir}")
    print(f"[handeye3d] serving on http://{args.host}:{args.port}")

    import uvicorn
    try:
        uvicorn.run(app_module.app, host=args.host, port=args.port)
    finally:
        if arm_controller is not None:
            print("[handeye3d] 手臂权重渐出、交还本体控制器（请扶住手臂）...")
            arm_controller.shutdown()
        camera.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
