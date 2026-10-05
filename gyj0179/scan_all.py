# -*- coding: utf-8 -*-
"""
GYJ-0179 一键扫描脚本
扫描所有串口 × 地址范围 × 波特率, 打印完整状态报告,
并保存成功连接的参数到 connection.json, 供 GUI 下次快速重连。

运行: python -m gyj0179.scan_all
"""
import os
import sys

# 兼容直接运行 (python scan_all.py)
if __package__ is None or __package__ == "":
    _PKG_DIR = os.path.dirname(os.path.abspath(__file__))
    _PARENT = os.path.dirname(_PKG_DIR)
    if _PARENT not in sys.path:
        sys.path.insert(0, _PARENT)
    from gyj0179.driver import GYJ0179
    from gyj0179.connection import ConnectionStore
else:
    from .driver import GYJ0179
    from .connection import ConnectionStore

# 默认扫描参数
BAUDRATES = [9600, 19200, 38400, 57600, 115200]
ADDR_MIN = 1
ADDR_MAX = 16


def main():
    print("=" * 60)
    print("  GYJ-0179 一键扫描 (自动波特率)")
    print("=" * 60)

    results = GYJ0179.auto_detect_all(
        baudrate=BAUDRATES,
        addr_min=ADDR_MIN,
        addr_max=ADDR_MAX,
        report=True,
    )

    print()
    print(f"===== 共找到 {len(results)} 个 GYJ-0179 从站 =====")
    for port, sid, baud in results:
        print(f"  {port}  从站地址 = {sid}  @ {baud}")

    # 保存成功连接参数, 供 GUI 下次快速重连
    if results:
        store = ConnectionStore(path="connection.json")
        ports = sorted({r[0] for r in results})
        # 用第一个找到的端口/波特率作为快速重连基准
        first_port, first_sid, first_baud = results[0]
        all_sids = sorted(r[1] for r in results if r[0] == first_port)
        store.save_multi(
            first_port, first_baud, all_sids,
            addr_min=ADDR_MIN, addr_max=ADDR_MAX)
        print(f"\n连接参数已保存: {first_port} @ {first_baud} "
              f"(从站 {all_sids}), 下次 GUI 可快速重连")
    else:
        print("\n未找到任何从站, 未保存连接参数")

    return 0


if __name__ == "__main__":
    sys.exit(main())
