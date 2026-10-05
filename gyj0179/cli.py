# -*- coding: utf-8 -*-
"""
GYJ-0179 Modbus 从站命令行工具

用法示例:
    python -m gyj0179.cli COM3 status
    python -m gyj0179.cli COM3 relay 1 on
    python -m gyj0179.cli COM3 relay 1 off
    python -m gyj0179.cli COM3 di
    python -m gyj0179.cli COM3 param read 0
    python -m gyj0179.cli COM3 param write 0 1234 --save
    python -m gyj0179.cli COM3 broadcast all-off
"""

import argparse
import os
import sys

# 兼容两种运行方式:
#   - python -m gyj0179.cli        (作为包模块运行, 相对导入可用)
#   - python cli.py                (直接运行, 手动把父目录加入 sys.path)
if __package__ is None or __package__ == "":
    _PKG_DIR = os.path.dirname(os.path.abspath(__file__))
    _PARENT = os.path.dirname(_PKG_DIR)
    if _PARENT not in sys.path:
        sys.path.insert(0, _PARENT)
    from gyj0179.driver import GYJ0179
else:
    from .driver import GYJ0179


def build_parser():
    p = argparse.ArgumentParser(
        description="GYJ-0179 Modbus RTU 从站控制工具")
    p.add_argument("port", help="串口, 如 COM3 或 /dev/ttyUSB0")
    p.add_argument("--id", type=int, default=1, help="从站地址, 默认 1")
    p.add_argument("--baud", type=int, default=9600, help="波特率, 默认 9600")

    sub = p.add_subparsers(dest="cmd", required=True)

    # status
    sub.add_parser("status", help="打印设备状态")

    # relay
    p_relay = sub.add_parser("relay", help="继电器控制")
    p_relay.add_argument("relay", type=int, choices=[1, 2, 3])
    p_relay.add_argument("action",
                         choices=["on", "off", "toggle", "read"])

    # di
    sub.add_parser("di", help="读光耦输入")

    # param
    p_param = sub.add_parser("param", help="参数寄存器")
    p_param.add_argument("op", choices=["read", "write", "readall"])
    p_param.add_argument("index", type=int, nargs="?", default=0)
    p_param.add_argument("value", type=int, nargs="?", default=0)
    p_param.add_argument("--save", action="store_true",
                         help="写完自动触发 EEPROM 保存")

    # broadcast
    p_bc = sub.add_parser("broadcast", help="广播控制")
    p_bc.add_argument("action",
                      choices=["all-on", "all-off",
                               "relay1-on", "relay1-off",
                               "relay2-on", "relay2-off",
                               "relay3-on", "relay3-off"])

    return p


def main():
    args = build_parser().parse_args()

    with GYJ0179(args.port, slave_id=args.id, baudrate=args.baud) as dev:
        if not dev.connected:
            print(f"[ERROR] 打开串口失败: {args.port}")
            sys.exit(1)

        # -------- status --------
        if args.cmd == "status":
            dev.print_status()

        # -------- relay --------
        elif args.cmd == "relay":
            if args.action == "on":
                ok = dev.relay_on(args.relay)
                print(f"继电器 COM{args.relay} 打开: {'成功' if ok else '失败'}")
            elif args.action == "off":
                ok = dev.relay_off(args.relay)
                print(f"继电器 COM{args.relay} 关闭: {'成功' if ok else '失败'}")
            elif args.action == "toggle":
                ok = dev.relay_toggle(args.relay)
                print(f"继电器 COM{args.relay} 翻转: {'成功' if ok else '失败'}")
            elif args.action == "read":
                state = dev.relay_get(args.relay)
                if state is None:
                    print(f"继电器 COM{args.relay}: 读取失败")
                else:
                    print(f"继电器 COM{args.relay}: "
                          f"{'吸合' if state else '释放'}")

        # -------- di --------
        elif args.cmd == "di":
            bits = dev.read_di_bits()
            if bits is None:
                print("读取失败")
                sys.exit(1)
            for i, b in enumerate(bits):
                print(f"  IN{i+1}: {'有信号' if b else '无信号'}")
            val = dev.read_di_value()
            print(f"  打包值: 0x{val:02X}")

        # -------- param --------
        elif args.cmd == "param":
            if args.op == "read":
                v = dev.get_param(args.index)
                if v is None:
                    print("读取失败")
                else:
                    print(f"参数[{args.index}] = 0x{v:04X} ({v})")
            elif args.op == "write":
                ok = dev.set_param(args.index, args.value, save=args.save)
                print(f"写参数[{args.index}] = {args.value}: "
                      f"{'成功' if ok else '失败'}"
                      f"{' (已保存)' if ok and args.save else ''}")
            elif args.op == "readall":
                params = dev.get_all_params()
                if params is None:
                    print("读取失败")
                else:
                    for i, v in enumerate(params):
                        print(f"  参数[{i:2d}] = 0x{v:04X} ({v})")

        # -------- broadcast --------
        elif args.cmd == "broadcast":
            if args.action == "all-on":
                dev.broadcast_all_on()
                print("广播: 打开所有从站所有继电器")
            elif args.action == "all-off":
                dev.broadcast_all_off()
                print("广播: 关闭所有从站所有继电器")
            elif args.action.startswith("relay"):
                num = int(args.action[5])
                if args.action.endswith("on"):
                    dev.broadcast_relay_on(num)
                    print(f"广播: 打开所有从站 COM{num}")
                else:
                    dev.broadcast_relay_off(num)
                    print(f"广播: 关闭所有从站 COM{num}")


if __name__ == "__main__":
    main()
