#!/usr/bin/env python3
"""Calculate radial distance from camera optical center to a 3D point."""

import math
import sys


def radial_distance(x: float, y: float, z: float) -> float:
    return math.sqrt(x * x + y * y + z * z)


def main():
    if len(sys.argv) == 4:
        x, y, z = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
        d = radial_distance(x, y, z)
        print(f"X={x:.2f}, Y={y:.2f}, Z={z:.2f}")
        print(f"径向距离 = {d:.2f} mm")
        return

    print("径向距离计算工具")
    print("输入 x y z（单位 mm），计算光心到该点的直线距离")
    print("输入 q 退出\n")

    while True:
        try:
            line = input("x y z > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.lower() == 'q':
            break
        parts = line.split()
        if len(parts) != 3:
            print("  请输入三个数值，空格分隔")
            continue
        try:
            x, y, z = float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            print("  无效数值")
            continue
        d = radial_distance(x, y, z)
        print(f"  径向距离 = {d:.2f} mm ({d/10:.2f} cm)")


if __name__ == "__main__":
    main()
