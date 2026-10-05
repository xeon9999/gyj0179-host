# -*- coding: utf-8 -*-
"""
GYJ-0179 从站上电广播监听

固件开机后会在每一个支持的波特率上轮流广播一次:
    GYJ0179 ADDR=0xNN INDEX=i BAUD=nnnnn
所以即使从站的地址/波特率被改过、普通扫描找不到, 用本功能也能把它找回来。

用法: 打开本窗口 -> 点"开始监听" -> 给板子断电再上电 (或按复位)。
"""
import re
import time
import tkinter as tk
from tkinter import ttk

import serial
import serial.tools.list_ports as lp

# 与固件 Baud_Value() 一致的档位顺序
BAUDS = [9600, 19200, 38400, 57600, 115200, 28800, 14400]
PAT = re.compile(rb"GYJ0179\s+ADDR=0x([0-9A-Fa-f]{2})\s+INDEX=(\d+)\s+BAUD=(\d+)")
LISTEN_TOTAL_S = 20.0


class ListenReportWindow(tk.Toplevel):
    """监听从站上电广播, 自动识别地址与波特率"""

    def __init__(self, parent, on_found=None):
        super().__init__(parent)
        self.title("监听从站上电广播")
        self.geometry("560x360")
        self.resizable(False, True)
        self.transient(parent.winfo_toplevel())
        self.on_found = on_found
        self._listening = False
        self._ser = None
        self._found = {}          # (addr, idx, baud) -> heard_baud
        self._t_start = 0.0

        self.port_var = tk.StringVar()
        self.state_var = tk.StringVar(value="就绪: 选串口后点\"开始监听\", 再给板子断电上电")
        self.result_var = tk.StringVar(value="")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._refresh_ports)

    # ---------- 界面 ----------
    def _build_ui(self):
        top = ttk.LabelFrame(self, text="串口", padding=8)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="监听串口:").pack(side="left")
        self.port_cb = ttk.Combobox(top, textvariable=self.port_var,
                                    width=34, state="readonly")
        self.port_cb.pack(side="left", padx=(4, 6))
        ttk.Button(top, text="刷新", width=6,
                   command=self._refresh_ports).pack(side="left")

        box = ttk.LabelFrame(self, text="操作", padding=8)
        box.pack(fill="x", padx=10, pady=4)
        self.btn_start = ttk.Button(box, text="开始监听",
                                    command=self._start)
        self.btn_start.pack(side="left", padx=2)
        self.btn_stop = ttk.Button(box, text="停止", command=self._stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=2)
        ttk.Button(box, text="使用该参数扫描",
                   command=self._use_result).pack(side="left", padx=10)
        ttk.Button(box, text="关闭", command=self._on_close
                   ).pack(side="right", padx=2)

        info = ttk.LabelFrame(self, text="说明", padding=6)
        info.pack(fill="x", padx=10, pady=4)
        ttk.Label(
            info, justify="left", anchor="w", wraplength=520,
            foreground="#333333",
            text=("固件开机后会在每个支持的波特率上轮流广播一次本机地址与"
                  "波特率, 因此即使之前改错过地址/波特率也能找回来。\n"
                  "操作: 清空下面的日志 -> 点\"开始监听\" -> 给板子断电重新"
                  "上电 (或按复位) -> 等结果。\n"
                  "注意: 监听期间请先关闭其它占用该串口的程序; 本窗口会临时"
                  "占用串口。"),
        ).pack(side="left")

        res = ttk.LabelFrame(self, text="结果 / 日志", padding=6)
        res.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.text = tk.Text(res, height=8, wrap="word",
                            font=("Consolas", 9), state="disabled")
        self.text.pack(fill="both", expand=True)

    # ---------- 串口 ----------
    def _refresh_ports(self):
        vals = []
        for p in lp.comports():
            d = (p.description or "").upper()
            if "CH340" in d or "USB-SERIAL" in d or "USB" in d:
                vals.append(f"{p.device} ({p.description})")
        self.port_cb["values"] = vals
        if vals and self.port_var.get() not in vals:
            self.port_var.set(vals[0])

    def _log(self, msg):
        self.text.configure(state="normal")
        self.text.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.text.see("end")
        self.text.configure(state="disabled")

    # ---------- 监听 ----------
    def _start(self):
        if self._listening:
            return
        # ★ 每次开始前自动刷新串口列表: 重新插拔 USB 后 COM 号会变,
        #   若还按旧端口(如 COM6)打开会直接失败, 什么都收不到
        old_sel = self.port_var.get().strip()
        self._refresh_ports()
        if old_sel and self.port_var.get().strip() != old_sel:
            self._log(f"串口列表已刷新: {old_sel} → "
                      f"{self.port_var.get().strip()} (USB 重新插拔会自动换号)")
        sel = self.port_var.get().strip()
        if not sel:
            self._log("请先选择串口 (若列表为空, 检查 USB 连接后点刷新)")
            return
        port = sel.split()[0]
        try:
            self._ser = serial.Serial(port, timeout=0.15)
        except Exception as e:
            self._log(f"打开 {port} 失败: {e}")
            self._log("提示: 先关闭上位机的扫描连接/其它串口程序再试")
            return

        self._listening = True
        self._found = {}
        self._t_start = time.time()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._log(f"开始监听 {port} — 请现在给板子断电再上电 (或按复位)")
        self.after(10, self._round)

    def _round(self):
        """一轮: 依次切换波特率监听一小段"""
        if not self._listening:
            return
        if time.time() - self._t_start > LISTEN_TOTAL_S:
            self.state_var.set("监听时间到, 未收到上报")
            self._log("监听超时: 没有收到上报")
            self._log("检查: 板子是否刚断电重新上电 / 是否已烧写新版固件 / "
                      "串口是否选对 / A-B 接线")
            self._stop()
            return
        try:
            for baud in BAUDS:
                if not self._listening:
                    break
                try:
                    self._ser.baudrate = baud
                    self._ser.reset_input_buffer()
                except Exception:
                    continue
                self.state_var.set(f"正在监听 {baud} bps ...")
                self.update_idletasks()
                t0 = time.time()
                buf = b""
                while time.time() - t0 < 0.8 and self._listening:
                    chunk = self._ser.read(256)
                    if chunk:
                        buf += chunk
                    else:
                        self.update()
                        time.sleep(0.02)
                for m in PAT.finditer(buf):
                    addr = int(m.group(1), 16)
                    idx = int(m.group(2))
                    baud_val = int(m.group(3))
                    if (addr, idx, baud_val) not in self._found:
                        self._found[(addr, idx, baud_val)] = baud
                        self._log(f"★ 收到上报: 地址={addr} 波特率索引={idx} "
                                  f"({baud_val}bps)  在 {baud} 档监听到")
        except Exception as e:
            self._log(f"监听异常: {e}")
            self._stop()
            return

        if self._found:
            self._show_result()
            self._stop(keep_result=True)
        else:
            self.after(60, self._round)

    def _show_result(self):
        lines = []
        for (addr, idx, baud_val) in sorted(self._found):
            lines.append(f"地址 {addr} / {baud_val}bps (索引 {idx})")
        self.result_var.set("  |  ".join(lines))
        self._log("找到从站: " + "; ".join(lines))
        self.state_var.set("已找到从站, 可点\"使用该参数扫描\"直接扫描连接")

    def _stop(self, keep_result=False):
        self._listening = False
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        if not keep_result and "监听时间到" not in self.state_var.get():
            self.state_var.set("已停止")

    def _use_result(self):
        if not self._found:
            self._log("还没有监听到上报, 先点\"开始监听\"并给板子上电")
            return
        addr, idx, baud_val = sorted(self._found)[0]
        # 先释放串口, 再交给主界面扫描 (否则端口被本窗口占着, 扫描打不开)
        self._stop()
        ok = True
        if self.on_found:
            try:
                self.on_found(addr, baud_val)
            except Exception as e:
                ok = False
                self._log(f"应用结果失败: {e}")
        if ok:
            self._log(f"已把 地址={addr} / {baud_val}bps 交给主界面扫描")
        self.destroy()

    def _on_close(self):
        self._stop()
        self.destroy()
