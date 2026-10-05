# -*- coding: utf-8 -*-
"""独立监听从站上电广播 (不依赖 GUI)

用法:
  1) 先关闭上位机 (它会占用串口)
  2) 运行:  python 监听上报.py
  3) 看到 "开始监听" 后, 立刻给板子断电再上电 (或按复位)
  可选参数: 串口号 监听秒数    例如: python 监听上报.py COM11 30
           不传串口号则自动逐个试所有 USB/CH340 串口
"""
import re
import sys
import time

import serial
import serial.tools.list_ports as lp

BAUDS = [9600, 19200, 38400, 57600, 115200, 28800, 14400]
PAT = re.compile(rb"GYJ0179[^\r\n]{0,64}")
PRINTABLE = re.compile(rb"[ -~]{4,}")     # 任何可打印串, 用于发现"收到了但格式不同"


def list_ports():
    out = []
    for p in lp.comports():
        d = (p.description or "").upper()
        if "BLUETOOTH" in d or "蓝牙" in d or "INTEL" in d:
            continue
        out.append((p.device, p.description or ""))
    return out


def listen_port(port, total, label):
    """在指定串口上按 7 档波特率轮流监听 total 秒"""
    print(f"\n--- 监听 {label} ---")
    try:
        ser = serial.Serial(port, timeout=0.1)
    except Exception as e:
        print(f"  [跳过] 打不开 {port}: {e}")
        return [], False
    per = total / len(BAUDS)
    hits = []
    got_any = False
    try:
        for baud in BAUDS:
            try:
                ser.baudrate = baud
                ser.reset_input_buffer()
            except Exception:
                continue
            t0 = time.time()
            buf = b""
            while time.time() - t0 < per:
                chunk = ser.read(512)
                if chunk:
                    got_any = True
                    buf += chunk
                    ts = time.strftime("%H:%M:%S")
                    for m in PRINTABLE.finditer(chunk):
                        print(f"  [{ts}] {baud:>6}bps 原始数据: {m.group(0)!r}")
                else:
                    time.sleep(0.02)
            for m in PAT.finditer(buf):
                hits.append((baud, m.group(0)))
    finally:
        ser.close()
    return hits, got_any


def main():
    ports = list_ports()
    print("=== 可用串口 ===")
    for dev, desc in ports:
        print(f"  {dev:8s} {desc}")
    if not ports:
        print("\n没有可用串口, 检查 USB 连接")
        return 1

    if len(sys.argv) > 1:
        targets = [(sys.argv[1], sys.argv[1])]
    else:
        targets = [(d, f"{d} {s}") for d, s in ports]

    total = float(sys.argv[2]) if len(sys.argv) > 2 else 28.0
    print(f"\n每个串口监听 {total:.0f} 秒, 将依次尝试: "
          f"{[t[0] for t in targets]}")
    print(">>> 现在请给板子断电, 再重新上电 (或按复位) <<<")

    all_hits = []
    any_data = False
    for port, label in targets:
        hits, got = listen_port(port, total, label)
        all_hits += hits
        any_data = any_data or got
        if hits:
            break

    print("\n=== 结果 ===")
    if all_hits:
        for baud, raw in all_hits:
            txt = raw.decode("ascii", "replace")
            m = re.search(r"ADDR=0x([0-9A-Fa-f]{2}).*?BAUD=(\d+)", txt)
            if m:
                print(f"  ★ 收到上报: {txt}")
                print(f"     → 地址={int(m.group(1), 16)}  "
                      f"波特率={m.group(2)}")
        print("\n结论: 通信正常。请在上位机把地址范围设 1~247、"
              "波特率选\"自动\"后扫描连接")
        return 0

    if any_data:
        print("  收到了数据, 但内容无法识别 (上面已打印原始字节)")
        print("  可能: 波特率不在支持列表 / 线路干扰 / 板子在发别的调试信息")
        return 3

    print("  完全没有收到任何字节 (串口线路层面没有数据)")
    print("\n板上 T 灯如果在上电时闪烁, 说明单片机确实在发送, 那么问题在:")
    print("  ① A/B 两根线接反或松动  ← 最常见! 交换 A、B 再试")
    print("  ② 板子 TX(485 输出) 到适配器的线没接好 / 适配器坏")
    print("  ③ 该串口不是接板子的那个 (换一个 USB 口试)")
    print("  ④ 板子与适配器没有共地 (GND 未连接)")
    print("  自检办法: 用 USB 转 485 模块把 A-B 短接到自己, 自发自收测试")
    return 2


if __name__ == "__main__":
    sys.exit(main())
