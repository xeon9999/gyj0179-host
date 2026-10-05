# -*- coding: utf-8 -*-
"""
GYJ-0179 Modbus RTU 从站 GUI 控制台 (单从站 · 功能与多从站同步版)
平台: STC15W408AS + SP485EE

功能 (与多从站监控台保持一致):
  - 扫描/连接单个从站, 记忆连接参数并支持下次快速重连
  - 从站详情: 继电器/光耦/保持寄存器, 支持面板设置(显隐)与标签命名
  - 宽屏下保持寄存器自动多列排列
  - 日志记录 (按日期落盘) 与日志查询
  - 串口热插拔监控: 端口移除自动停轮询, 恢复后自动重连
  - 轮询参数可调 (请求超时 / 离线判定 / 离线重试间隔)
  - 广播控制 (全部吸合/释放, 单路 COM1~3)
  - 底部状态栏: 连接/端口/波特率/从站/轮询/最后通信/错误计数

依赖: tkinter (Python 自带), pyserial, pymodbus
运行: python -m gyj0179.gui_single
"""
import os
import sys

# 兼容两种运行方式:
#   - python -m gyj0179.gui_single   (作为包模块运行, 相对导入可用)
#   - python gui_single.py           (直接运行, 手动把父目录加入 sys.path)
if __package__ is None or __package__ == "":
    _PKG_DIR = os.path.dirname(os.path.abspath(__file__))
    _PARENT = os.path.dirname(_PKG_DIR)
    if _PARENT not in sys.path:
        sys.path.insert(0, _PARENT)
    from gyj0179.constants import SLAVE_KW
    from gyj0179.driver import GYJ0179
    from gyj0179.labels import LabelManager
    from gyj0179.logs import LogManager
    from gyj0179.connection import ConnectionStore
    from gyj0179.widgets import (LabelEditWindow, ListenReportWindow,
                                 LogQueryWindow, SlaveConfigWindow,
                                 SlaveDetailView)
else:
    from .constants import SLAVE_KW
    from .driver import GYJ0179
    from .labels import LabelManager
    from .logs import LogManager
    from .connection import ConnectionStore
    from .widgets import (LabelEditWindow, ListenReportWindow,
                          LogQueryWindow, SlaveConfigWindow,
                          SlaveDetailView)

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

from pymodbus import __version__ as PYMODBUS_VERSION


# ==================== 独立探测 ====================

def _probe_one_slave(port, baudrate, slave_id, timeout=0.25,
                     cancelled=None):
    """独立打开串口探测指定从站是否在线 (用于扫描)

    cancelled: 可选回调, 返回 True 表示用户已请求停止扫描, 立即放弃本次探测。
    """
    if cancelled is not None and cancelled():
        return False
    from pymodbus.client import ModbusSerialClient
    client = None
    try:
        client = ModbusSerialClient(
            port=port, baudrate=baudrate,
            bytesize=8, parity='N', stopbits=1,
            timeout=timeout, retries=0)
        if not client.connect():
            return False
        rr = client.read_holding_registers(
            0, count=1, **{SLAVE_KW: slave_id})
        return not rr.isError()
    except Exception:
        return False
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


def _pick_first_serial_port():
    import serial.tools.list_ports
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "").upper()
        if "BLUETOOTH" in desc or "蓝牙" in desc:
            continue
        if "INTEL" in desc and "AMT" in desc:
            continue
        if "SOL" in desc:
            continue
        return p.device
    return None


# ==================== 主窗口 ====================

class GYJ0179GUI:
    def __init__(self, root):
        self.root = root
        self.root.title("GYJ-0179 Modbus 控制台 (单从站)")
        self.root.geometry("1150x860")
        self.root.minsize(1000, 720)

        self.dev = None
        self.dev_lock = threading.Lock()
        self.port = None
        self.baudrate = 9600
        self.slave_id = 1
        self.slave_data = {}
        self.polling = False
        self.poll_thread = None
        self.scan_thread = None
        self.log_queue = queue.Queue()
        self._ui_queue = queue.Queue()   # 后台线程 -> 主线程的 UI 更新队列

        self.log_mgr = LogManager(log_dir="logs", keep_days=30)
        self.label_mgr = LabelManager(path="labels.json")
        self.conn_store = ConnectionStore(path="connection.json")

        # 状态栏变量
        self.sb_conn_var = tk.StringVar(value="未连接")
        self.sb_port_var = tk.StringVar(value="--")
        self.sb_baud_var = tk.StringVar(value="--")
        self.sb_slaves_var = tk.StringVar(value="0 / 1")
        self.sb_last_comm_var = tk.StringVar(value="--")
        self.sb_poll_var = tk.StringVar(value="停止")
        self.sb_error_var = tk.StringVar(value="0")

        self._stat_total_polls = 0
        self._stat_errors = 0
        self._stat_last_ok_time = None
        self._stat_last_error_time = None
        self._closing = False            # 窗口已关闭, 后台线程不再回调 UI
        self._scan_stop = False         # 用户请求停止扫描
        self._cfg_switch_until = 0      # 配置切换冷却截止时间

        # 轮询参数的主线程缓存 (Tk 变量不可在后台线程访问)
        self._poll_timeout_ms = 200
        self._poll_fail_threshold = 2
        self._poll_retry_interval = 2

        # 串口热插拔检测状态
        self._port_state = "idle"        # idle/connected/removed/reconnecting
        self._port_monitor_running = False
        self._reconnect_attempted = False
        self._expected_port = None

        self._build_ui()
        self._start_log_pump()
        self._start_display_refresh()
        self._start_status_bar_refresh()
        self.root.after(50, self._pump_ui_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.log("SYS", f"程序启动, pymodbus {PYMODBUS_VERSION}, "
                        f"参数名 '{SLAVE_KW}'")
        self.log("SYS", f"日志目录: {self.log_mgr.log_dir.absolute()}")
        self.log("SYS", f"标签文件: {self.label_mgr.path.absolute()}")

        self._start_port_monitor()
        self.root.after(300, self._auto_reconnect)

    # ==================== UI 布局 ====================

    def _build_ui(self):
        self._build_scan_bar()
        self._build_detail_area()
        self._build_log_area()
        self._build_status_bar()

    def _build_scan_bar(self):
        bar = ttk.LabelFrame(self.root, text="扫描与控制", padding=8)
        bar.pack(fill="x", padx=5, pady=(5, 3))

        row1 = ttk.Frame(bar)
        row1.pack(fill="x")

        ttk.Label(row1, text="串口:").pack(side="left")
        self.port_var = tk.StringVar(value="自动")
        self.port_cb = ttk.Combobox(row1, textvariable=self.port_var,
                                    width=20, state="readonly")
        self.port_cb.pack(side="left", padx=(2, 10))
        self._refresh_ports()

        ttk.Label(row1, text="波特率:").pack(side="left")
        self.baud_var = tk.StringVar(value="自动")
        self.baud_cb = ttk.Combobox(
            row1, textvariable=self.baud_var,
            values=("自动", 9600, 19200, 38400, 57600, 115200),
            width=8, state="readonly")
        self.baud_cb.pack(side="left", padx=(2, 10))

        ttk.Label(row1, text="从站地址:").pack(side="left")
        self.slave_var = tk.IntVar(value=1)
        ttk.Spinbox(row1, from_=1, to=247, width=5,
                    textvariable=self.slave_var).pack(side="left")

        self.btn_scan = ttk.Button(row1, text="扫描并连接",
                                   command=self._on_scan)
        self.btn_scan.pack(side="left", padx=10)

        self.btn_stop_scan = ttk.Button(row1, text="■ 停止扫描",
                                        command=self._stop_scan,
                                        state="disabled")
        self.btn_stop_scan.pack(side="left", padx=2)

        self.btn_disconnect = ttk.Button(row1, text="断开",
                                         command=self._on_disconnect,
                                         state="disabled")
        self.btn_disconnect.pack(side="left", padx=2)

        self.btn_labels = ttk.Button(row1, text="✏ 编辑标签",
                                     command=self._open_label_window)
        self.btn_labels.pack(side="left", padx=2)

        self.btn_logs = ttk.Button(row1, text="📋 日志查询",
                                   command=self._open_log_window)
        self.btn_logs.pack(side="left", padx=2)

        self.btn_cfg = ttk.Button(row1, text="⚙ 通信配置",
                                  command=self._open_slave_config)
        self.btn_cfg.pack(side="left", padx=2)

        self.btn_listen = ttk.Button(row1, text="📡 监听上报",
                                     command=self._open_listen_report)
        self.btn_listen.pack(side="left", padx=2)

        ttk.Button(row1, text="🔄 刷新串口",
                   command=self._refresh_ports).pack(side="left", padx=2)

        self.status_var = tk.StringVar(value="未连接")
        ttk.Label(row1, textvariable=self.status_var,
                  foreground="red").pack(side="right")

        row2 = ttk.Frame(bar)
        row2.pack(fill="x", pady=(6, 0))

        ttk.Label(row2, text="广播:").pack(side="left")
        ttk.Button(row2, text="全部吸合",
                   command=lambda: self._broadcast("on")
                   ).pack(side="left", padx=2)
        ttk.Button(row2, text="全部释放",
                   command=lambda: self._broadcast("off")
                   ).pack(side="left", padx=2)

        ttk.Separator(row2, orient="vertical").pack(
            side="left", fill="y", padx=8)
        ttk.Label(row2, text="单路:").pack(side="left")
        for i in (1, 2, 3):
            ttk.Button(row2, text=f"开 COM{i}",
                       command=lambda i=i: self._broadcast("on", relay=i)
                       ).pack(side="left", padx=1)
            ttk.Button(row2, text=f"关 COM{i}",
                       command=lambda i=i: self._broadcast("off", relay=i)
                       ).pack(side="left", padx=1)

        row3 = ttk.Frame(bar)
        row3.pack(fill="x", pady=(6, 0))

        ttk.Label(row3, text="⚡ 轮询参数:",
                  font=("Microsoft YaHei", 9, "bold")
                  ).pack(side="left")

        ttk.Label(row3, text="  请求超时(毫秒):").pack(side="left")
        self.timeout_ms_var = tk.IntVar(value=200)
        ttk.Spinbox(row3, from_=50, to=2000, increment=50, width=5,
                    textvariable=self.timeout_ms_var
                    ).pack(side="left")
        ttk.Label(row3, text="(9600波特率下 200ms 足够)",
                  foreground="gray").pack(side="left", padx=(2, 15))

        ttk.Label(row3, text="离线判定:").pack(side="left")
        self.fail_threshold_var = tk.IntVar(value=2)
        ttk.Spinbox(row3, from_=1, to=20, width=3,
                    textvariable=self.fail_threshold_var
                    ).pack(side="left")
        ttk.Label(row3, text="次连续失败",
                  foreground="gray").pack(side="left", padx=(2, 15))

        ttk.Label(row3, text="离线重试间隔(秒):").pack(side="left")
        self.retry_interval_var = tk.IntVar(value=2)
        ttk.Spinbox(row3, from_=1, to=60, width=4,
                    textvariable=self.retry_interval_var
                    ).pack(side="left", padx=(2, 15))

        ttk.Button(row3, text="恢复默认值",
                   command=self._reset_poll_defaults
                   ).pack(side="left", padx=2)

        # 把轮询参数同步到主线程缓存, 供后台线程安全读取
        self.timeout_ms_var.trace_add("write", self._sync_poll_params)
        self.fail_threshold_var.trace_add("write", self._sync_poll_params)
        self.retry_interval_var.trace_add("write", self._sync_poll_params)
        self._sync_poll_params()

    def _build_detail_area(self):
        container = ttk.LabelFrame(self.root, text="从站详情",
                                   padding=5)
        container.pack(fill="both", expand=True, padx=5, pady=(0, 3))

        self.detail_view = SlaveDetailView(
            container,
            log_cb=self.log,
            relay_cb=self._do_relay,
            toggle_cb=self._do_toggle,
            write_param_cb=self._do_write_param,
            label_mgr=self.label_mgr,
        )
        self.detail_view.pack(fill="both", expand=True)

    def _build_log_area(self):
        frame = ttk.LabelFrame(self.root, text="日志 (实时)", padding=5)
        frame.pack(fill="x", padx=5, pady=(0, 3))

        self.log_text = scrolledtext.ScrolledText(
            frame, height=5, wrap="word",
            font=("Consolas", 9), state="disabled")
        self.log_text.pack(fill="both", expand=True)

        btn = ttk.Frame(frame)
        btn.pack(fill="x", pady=(2, 0))
        ttk.Button(btn, text="清空显示",
                   command=self._clear_log).pack(side="right")
        ttk.Button(btn, text="📋 打开日志查询",
                   command=self._open_log_window).pack(side="right",
                                                       padx=2)

    def _build_status_bar(self):
        bar = tk.Frame(self.root, bd=1, relief="sunken", bg="#f0f0f0")
        bar.pack(fill="x", side="bottom")

        left = tk.Frame(bar, bg="#f0f0f0")
        left.pack(side="left", padx=8, pady=3)

        self.sb_conn_lamp = tk.Label(left, text="  ", bg="gray",
                                     width=2, relief="sunken")
        self.sb_conn_lamp.pack(side="left", padx=(0, 4))

        self.sb_conn_lbl = tk.Label(
            left, textvariable=self.sb_conn_var,
            bg="#f0f0f0", fg="red",
            font=("Microsoft YaHei", 9, "bold"))
        self.sb_conn_lbl.pack(side="left", padx=(0, 12))

        tk.Label(left, text="端口:", bg="#f0f0f0", fg="gray"
                 ).pack(side="left")
        tk.Label(left, textvariable=self.sb_port_var,
                 bg="#f0f0f0", fg="black",
                 font=("Consolas", 9, "bold")
                 ).pack(side="left", padx=(2, 12))

        tk.Label(left, text="波特率:", bg="#f0f0f0", fg="gray"
                 ).pack(side="left")
        tk.Label(left, textvariable=self.sb_baud_var,
                 bg="#f0f0f0", fg="black",
                 font=("Consolas", 9, "bold")
                 ).pack(side="left", padx=(2, 12))

        tk.Label(left, text="从站:", bg="#f0f0f0", fg="gray"
                 ).pack(side="left")
        tk.Label(left, textvariable=self.sb_slaves_var,
                 bg="#f0f0f0", fg="black",
                 font=("Consolas", 9, "bold")
                 ).pack(side="left", padx=(2, 12))

        tk.Label(left, text="轮询:", bg="#f0f0f0", fg="gray"
                 ).pack(side="left")
        self.sb_poll_lbl = tk.Label(
            left, textvariable=self.sb_poll_var,
            bg="#f0f0f0", fg="gray",
            font=("Consolas", 9, "bold"))
        self.sb_poll_lbl.pack(side="left", padx=(2, 12))

        right = tk.Frame(bar, bg="#f0f0f0")
        right.pack(side="right", padx=8, pady=3)

        tk.Label(right, text="最后通信:", bg="#f0f0f0", fg="gray"
                 ).pack(side="left")
        tk.Label(right, textvariable=self.sb_last_comm_var,
                 bg="#f0f0f0", fg="black",
                 font=("Consolas", 9)
                 ).pack(side="left", padx=(2, 12))

        tk.Label(right, text="错误:", bg="#f0f0f0", fg="gray"
                 ).pack(side="left")
        tk.Label(right, textvariable=self.sb_error_var,
                 bg="#f0f0f0", fg="red",
                 font=("Consolas", 9, "bold")
                 ).pack(side="left", padx=(2, 0))

    # ==================== 状态栏刷新 ====================

    def _start_status_bar_refresh(self):
        self._update_status_bar()
        self.root.after(500, self._start_status_bar_refresh)

    def _update_status_bar(self):
        if self._port_state == "removed":
            self.sb_conn_var.set(f"端口异常: {self.port or '未知'} 已移除")
            self.sb_conn_lbl.configure(fg="red")
            self.sb_conn_lamp.configure(bg="red")
            self.sb_port_var.set(self.port or "--")
            self.sb_baud_var.set(str(self.baudrate))
            self.sb_poll_var.set("停止")
            self.sb_poll_lbl.configure(fg="gray")
        elif self._port_state == "reconnecting":
            self.sb_conn_var.set("正在重连...")
            self.sb_conn_lbl.configure(fg="orange")
            self.sb_conn_lamp.configure(bg="orange")
            self.sb_poll_var.set("重连中")
            self.sb_poll_lbl.configure(fg="orange")
        elif self.dev is None:
            self.sb_conn_var.set("未连接")
            self.sb_conn_lbl.configure(fg="red")
            self.sb_conn_lamp.configure(bg="gray")
            self.sb_port_var.set("--")
            self.sb_baud_var.set("--")
            self.sb_poll_var.set("停止")
            self.sb_poll_lbl.configure(fg="gray")
        else:
            try:
                sock_open = self.dev.client.is_socket_open()
            except Exception:
                sock_open = False

            if sock_open:
                self.sb_conn_var.set("已连接")
                self.sb_conn_lbl.configure(fg="green")
                self.sb_conn_lamp.configure(bg="lime")
            else:
                self.sb_conn_var.set("连接已断开")
                self.sb_conn_lbl.configure(fg="red")
                self.sb_conn_lamp.configure(bg="red")

            self.sb_port_var.set(self.port or "--")
            self.sb_baud_var.set(str(self.baudrate))

            if self.polling:
                self.sb_poll_var.set("运行中")
                self.sb_poll_lbl.configure(fg="green")
            else:
                self.sb_poll_var.set("停止")
                self.sb_poll_lbl.configure(fg="gray")

        # 从站在线状态 (单从站: 1/1)
        if self.dev is None:
            self.sb_slaves_var.set("0 / 1")
        else:
            online = self.slave_data.get(self.slave_id, {}).get(
                'online', False)
            mark = "✓" if online else "✗"
            self.sb_slaves_var.set(f"{1 if online else 0} / 1  {mark}")

        # 最后通信时间
        abnormal = (self._port_state in ("removed", "reconnecting")
                    or (self.dev is not None
                        and not self.slave_data.get(
                            self.slave_id, {}).get('online', True)))
        if abnormal and self._stat_last_error_time is not None:
            self.sb_last_comm_var.set(
                "异常 " + time.strftime(
                    "%H:%M:%S",
                    time.localtime(self._stat_last_error_time)))
        elif self._stat_last_ok_time is None:
            self.sb_last_comm_var.set("--")
        else:
            ago = time.time() - self._stat_last_ok_time
            if ago < 1:
                self.sb_last_comm_var.set("刚刚")
            elif ago < 60:
                self.sb_last_comm_var.set(f"{ago:.1f}s 前")
            elif ago < 3600:
                self.sb_last_comm_var.set(f"{ago/60:.1f}min 前")
            else:
                self.sb_last_comm_var.set(f"{ago/3600:.1f}h 前")

        self.sb_error_var.set(str(self._stat_errors))

    # ==================== 串口热插拔检测 ====================

    def _get_available_ports(self):
        import serial.tools.list_ports
        try:
            return [p.device for p in serial.tools.list_ports.comports()]
        except Exception:
            return []

    def _start_port_monitor(self):
        if self._port_monitor_running:
            return
        self._port_monitor_running = True
        self._monitor_ports()

    def _monitor_ports(self):
        """定时检查物理串口, 处理移除/恢复 (Tk 主线程, 不阻塞 UI)"""
        if not self._port_monitor_running:
            return
        try:
            available = self._get_available_ports()

            if self._port_state == "connected" and self.dev is not None:
                if self.port not in available:
                    self._handle_port_removed()
                elif (self._expected_port
                        and self._expected_port in available
                        and self.port != self._expected_port):
                    self.log("CONN",
                             f"原端口 {self._expected_port} 已恢复, "
                             f"从 {self.port} 切回...")
                    self._port_state = "reconnecting"
                    self._handle_port_reconnect(use_saved=True)

            elif self._port_state in ("removed", "reconnecting"):
                if (self._expected_port
                        and self._expected_port in available):
                    self._handle_port_reconnect(use_saved=True)
                elif available:
                    self._handle_port_reconnect(use_saved=False)

        except Exception as e:
            self.log("CONN", f"端口监控异常: {e}", level="ERROR")

        self.root.after(500, self._monitor_ports)

    def _handle_port_removed(self):
        self._port_state = "removed"
        removed_port = self.port
        self._expected_port = self.port

        self.polling = False
        if self.poll_thread:
            self.poll_thread.join(timeout=1)
            self.poll_thread = None
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None

        self._stat_last_error_time = time.time()

        self.sb_conn_var.set(f"端口异常: {removed_port} 已移除")
        self.sb_conn_lbl.configure(fg="red")
        self.sb_conn_lamp.configure(bg="red")
        self.sb_poll_var.set("停止")
        self.sb_poll_lbl.configure(fg="gray")

        self.log("CONN", f"检测到 {removed_port} 已被移除, 等待重连...",
                 level="WARN")
        self.status_var.set(f"{removed_port} 已移除, 等待重连...")

    def _handle_port_reconnect(self, use_saved):
        """端口恢复或新端口插入: 自动重连期望端口, 否则重新扫描"""
        if self._reconnect_attempted:
            return
        self._reconnect_attempted = True
        self._port_state = "reconnecting"
        self.status_var.set("检测到串口, 正在重连...")

        self.polling = False
        if self.poll_thread:
            self.poll_thread.join(timeout=1)
            self.poll_thread = None
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None

        if use_saved and self._expected_port:
            target = self._expected_port
            self.log("CONN", f"端口 {target} 已恢复, 自动重连...")
            self.port_var.set(target)
            self.baud_var.set(str(self.baudrate))
        else:
            self.log("CONN", "检测到新串口, 自动扫描...")
            self.port_var.set("自动")

        self._on_scan()

        def _reset_flag():
            self._reconnect_attempted = False
        self.root.after(3000, _reset_flag)

    def _mark_connected(self):
        self._port_state = "connected"

    def _mark_idle(self):
        self._port_state = "idle"
        self._expected_port = None

    def _reset_status_stats(self):
        self._stat_total_polls = 0
        self._stat_errors = 0
        self._stat_last_ok_time = None
        self._stat_last_error_time = None

    def _reset_poll_defaults(self):
        self.timeout_ms_var.set(200)
        self.fail_threshold_var.set(2)
        self.retry_interval_var.set(2)
        self.log("SYS", "轮询参数已恢复默认 (超时200ms / 离线2次 / 重试2s)")

    def _sync_poll_params(self, *_args):
        """把 Tk 变量的轮询参数缓存到普通属性 (后台线程读取用)"""
        def _int(var, default, low, high):
            try:
                v = int(var.get())
            except Exception:
                return default
            return max(low, min(high, v))

        # 与 _build_scan_bar 的初始化顺序无关: 直接赋值不需要属性预先存在
        self._poll_timeout_ms = _int(self.timeout_ms_var, 200, 50, 2000)
        self._poll_fail_threshold = _int(self.fail_threshold_var, 2, 1, 20)
        self._poll_retry_interval = _int(self.retry_interval_var, 2, 1, 60)

    # ==================== 日志 ====================

    def log(self, category, message, slave_id=None, level="INFO"):
        """线程安全日志: 写入界面队列 + 落盘"""
        ts = time.strftime("%H:%M:%S")
        prefix = f"[{ts}]"
        if slave_id is not None:
            disp = self.label_mgr.display_slave(slave_id)
            prefix += f" [{disp}]"
        prefix += f" [{category}]"
        self.log_queue.put(f"{prefix} {message}")
        self.log_mgr.log(level, category, message, slave_id)

    def _start_log_pump(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._start_log_pump)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _open_log_window(self):
        LogQueryWindow(self.root, self.log_mgr, self.label_mgr)

    def _open_label_window(self):
        slave_ids = [self.slave_id] if self.dev is not None else [1]

        def on_saved():
            self.detail_view.refresh_labels()
            self.log("SYS", "标签已更新")

        LabelEditWindow(self.root, self.label_mgr, slave_ids,
                        on_saved=on_saved)

    # ---------- 从站通信配置 (地址 / 波特率, 掉电保存) ----------

    def _open_slave_config(self):
        if self.dev is None:
            messagebox.showwarning("提示", "请先扫描连接从站")
            return

        def _read(sid):
            """读从站当前地址/波特率 → (addr, baud_idx)"""
            if self.dev is None:
                return None
            # 刚下发过切换命令: 从站正在换参数, 这期间不要读, 免得白等一轮
            if time.time() < getattr(self, "_cfg_switch_until", 0):
                return None
            try:
                with self.dev_lock:
                    cur = self.dev.slave_id
                    if cur != sid:
                        self.dev.slave_id = sid
                    res = self.dev.get_slave_config()
                    # 恢复成"当前应有"的地址:
                    # 若期间已按新地址切换过, 不能再打回旧地址
                    self.dev.slave_id = getattr(self, "_cfg_addr_now", cur)
                return res
            except Exception as e:
                self.log("SYS", f"读取从站配置异常: {e}", level="ERROR")
                return None

        def _write(sid, addr, baud_idx, cmd):
            """下发新配置; 0xA500/0xA501 时从站会切换参数"""
            if self.dev is None:
                return False
            is_apply = cmd in (GYJ0179.CFG_CMD_APPLY,
                               GYJ0179.CFG_CMD_FACTORY)
            detail = []
            try:
                with self.dev_lock:
                    saved = self.dev.slave_id
                    self.dev.slave_id = sid
                    ok = self.dev.set_slave_config(addr, baud_idx, cmd,
                                                   detail=detail)
                    # 切换类命令: 设备应答后即改用新地址, 这里必须跟着更新,
                    # 否则后续读取/轮询会拿旧地址去访问 (无应答)
                    self.dev.slave_id = addr if (ok and is_apply) else saved
                    if ok and is_apply:
                        self._cfg_addr_now = addr
            except Exception as e:
                self.log("SYS", f"写入从站配置异常: {e}", level="ERROR")
                return False

            if not ok:
                # 命令字(奇数条)无应答、但地址/波特率已写入: 设备仍会切换,
                # 让窗口继续走"切换并重连", 不要误报写入失败卡住。
                # 完全无应答(第 1 条就失败)才算真失败。
                if is_apply and len(detail) >= 2 and detail[0] and detail[1]:
                    self._cfg_switch_until = time.time() + 3.0
                    self.slave_var.set(addr)
                    self.log("CONN",
                             f"配置已部分写入 (命令字无应答), 仍尝试按新参数"
                             f"重连: 地址={addr} "
                             f"波特率={GYJ0179.baud_of_index(baud_idx)}",
                             level="WARN")
                    return "partial"
                return False

            self.log("CONN", f"下发通信配置: 地址={addr} "
                             f"波特率={GYJ0179.baud_of_index(baud_idx)} "
                             f"命令=0x{cmd:04X}")

            # 注意: 这里不能再用"旧地址回读"来校验!
            # 从站收到 0xA500 后会先回应答帧、随即换用新地址/波特率,
            # 再用旧地址去读必然无应答, 会被误判成"写入失败"。
            # 写入是否生效, 由随后的自动扫描(按新参数连接)来验证。
            if is_apply:
                # 短暂等一下让从站把应答帧发完并切换;
                # 完整的切换等待由窗口的 _on_apply 统一负责 (避免重复等待)
                self._cfg_switch_until = time.time() + 3.0
                time.sleep(0.5)
                # 界面先切到新参数; self.slave_id 等扫描成功后由
                # _on_connected 设定 (老固件没真切换的话扫描会失败)
                self.baudrate = GYJ0179.baud_of_index(baud_idx) or \
                    self.baudrate
                self.slave_var.set(addr)
                self.baud_var.set(str(self.baudrate))
                self.log("CONN", "从站已切换, 开始自动重连", level="WARN")
            return True

        def on_applied(addr, baud_idx):
            self.status_var.set(
                f"从站配置已改为 #{addr} @ "
                f"{GYJ0179.baud_of_index(baud_idx)}bps, 正在重新扫描连接...")
            # 稍等片刻让从站切换稳定, 再自动扫描并把地址框切到新地址
            self.slave_var.set(addr)
            self.root.after(800, self._auto_scan_after_config)

        SlaveConfigWindow(
            self.root, self.slave_id, self.baudrate,
            _read, _write, on_applied=on_applied, app=self)

    def _auto_scan_after_config(self):
        """配置生效后自动重新扫描并连接"""
        if self._closing:
            return
        self.log("CONN", f"配置已更新, 自动扫描 {self.port_var.get()} "
                         f"从站 {self.slave_var.get()}...")
        self._on_scan()

    # ---------- 监听从站上电广播 (改了地址/波特率后找回来) ----------

    def _open_listen_report(self):
        # 监听要独占串口, 先停轮询并断开, 避免端口被占用
        was_connected = self.dev is not None
        if was_connected:
            self._on_disconnect()

        def on_found(addr, baud_val):
            self.slave_var.set(addr)
            self.baud_var.set("自动")
            self.log("CONN", f"监听到从站: 地址={addr} 波特率={baud_val}, "
                             f"开始扫描连接")
            self.status_var.set(f"监听到从站 #{addr} @ {baud_val}bps, 正在连接...")
            self.root.after(300, self._on_scan)

        ListenReportWindow(self.root, on_found=on_found)

    def _start_display_refresh(self):
        """主线程定期把最新数据刷到详情视图"""
        if self.dev is not None:
            data = self.slave_data.get(self.slave_id)
            if data is not None:
                self.detail_view.update_data(data)
        self.root.after(200, self._start_display_refresh)

    # ==================== 串口扫描/连接 ====================


    # ---------- 扫描中止 ----------

    def _stop_scan(self):
        """用户点"停止扫描": 置中断标志, 扫描线程会在最近一次探测后退出"""
        self._scan_stop = True
        try:
            self.status_var.set("正在停止扫描...")
        except Exception:
            pass
        self.log("SYS", "用户请求停止扫描", level="WARN")

    def _scan_stopped(self):
        """扫描被中断后收尾 (在工作线程里调用)"""
        self._scan_stop = False
        self._ui_call(self._after_scan_stop)

    def _after_scan_stop(self):
        """主线程: 恢复按钮并提示"""
        try:
            self.btn_scan.configure(state="normal")
            self.btn_stop_scan.configure(state="disabled")
            self.status_var.set("已停止扫描")
        except Exception:
            pass
        self.log("SYS", "扫描已停止")

    def _refresh_ports(self):
        import serial.tools.list_ports
        ports = ["自动"]
        for p in serial.tools.list_ports.comports():
            desc = p.description or ""
            upper = desc.upper()
            if "BLUETOOTH" in upper or "蓝牙" in desc:
                continue
            if "INTEL" in upper and "AMT" in upper:
                continue
            if "SOL" in upper:
                continue
            ports.append(f"{p.device} ({desc})")
        self.port_cb["values"] = ports
        if self.port_var.get() not in ports:
            self.port_var.set("自动")

    def _port_exists(self, port):
        import serial.tools.list_ports
        try:
            for p in serial.tools.list_ports.comports():
                if p.device == port:
                    return True
        except Exception:
            pass
        return False

    def _auto_reconnect(self):
        """启动时自动用上次成功连接的参数快速重连; 端口失效则自动扫描"""
        if not self.conn_store.has_saved():
            self.log("SYS", "无上次连接记录, 等待手动扫描")
            return

        saved_port = self.conn_store.port
        saved_baud = self.conn_store.baudrate
        saved_sid = self.conn_store.slave_id
        ts = self.conn_store.timestamp or "未知时间"
        self.log("SYS", f"检测到上次连接记录: {saved_port} @ {saved_baud} "
                        f"(从站 {saved_sid}, {ts})")

        self.port_var.set(saved_port)
        self.baud_var.set(str(saved_baud))
        self.slave_var.set(saved_sid)

        if not self._port_exists(saved_port):
            self.log("SYS", f"端口 {saved_port} 已失效, 切换到自动扫描")
            self.port_var.set("自动")

        self._on_scan()

    def _on_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            self.log("SYS", "扫描已在进行中")
            return

        port_str = self.port_var.get()
        try:
            slave_id = int(self.slave_var.get())
        except Exception:
            messagebox.showerror("错误", "从站地址必须是 1~247 的整数")
            return
        if not 1 <= slave_id <= 247:
            messagebox.showerror("错误", "从站地址必须在 1~247 之间")
            return

        baud_sel = str(self.baud_var.get())
        if baud_sel == "自动":
            baudrates = [9600, 19200, 38400, 57600, 115200, 28800, 14400]
        else:
            baudrates = [int(baud_sel)]

        self._reset_status_stats()
        self.log("CONN", f"开始扫描: 从站 {slave_id}, "
                         f"波特率 {baudrates}, 串口={port_str}")

        self._scan_stop = False
        self.btn_scan.configure(state="disabled")
        self.btn_stop_scan.configure(state="normal")
        self.status_var.set("扫描中...")

        self.scan_thread = threading.Thread(
            target=self._scan_worker,
            args=(port_str, baudrates, slave_id),
            daemon=True)
        self.scan_thread.start()

    def _scan_worker(self, port_str, baudrates, slave_id):
        def _cancelled():
            return bool(self._scan_stop) or self._closing

        try:
            if _cancelled():
                self._scan_stopped()
                return
            if port_str == "自动":
                self.log("CONN", "自动识别串口...")
                port = _pick_first_serial_port()
                if port is None:
                    self.log("CONN", "未找到任何可用串口", level="ERROR")
                    self._ui_call(self._scan_failed, "未找到串口")
                    return
            else:
                port = port_str.split()[0]

            self.log("CONN", f"使用串口: {port}")
            self.port = port

            matched_baud = None
            for baudrate in baudrates:
                self.log("CONN", f"尝试波特率 {baudrate} 探测从站 "
                                 f"#{slave_id}...")
                if _probe_one_slave(port, baudrate, slave_id,
                                    timeout=0.2, cancelled=_cancelled):
                    matched_baud = baudrate
                    break
                if _cancelled():
                    self._scan_stopped()
                    return
                self.log("CONN", f"波特率 {baudrate} 未响应, 尝试下一个",
                         level="DEBUG")

            if matched_baud is None:
                # 找不到时自动加大搜索: 全地址 × 常用波特率
                # (从站地址/波特率可能被改过, 这样才不会"再也连不上")
                self.log("CONN", "指定地址未响应, 自动搜索 1~247 全地址...",
                         level="WARN")
                self._ui_call(self.status_var.set,
                              "未找到, 正在自动搜索全地址...")
                all_bauds = []
                for b in list(baudrates) + [9600, 19200, 38400,
                                            57600, 115200]:
                    if b not in all_bauds:
                        all_bauds.append(b)
                found_id = None
                for baudrate in all_bauds:
                    for sid in range(1, 248):
                        if _probe_one_slave(port, baudrate, sid,
                                            timeout=0.1,
                                            cancelled=_cancelled):
                            found_id = sid
                            matched_baud = baudrate
                            break
                        if _cancelled():
                            self._scan_stopped()
                            return
                    if found_id is not None:
                        break
                if found_id is None:
                    self.log("CONN", "全地址搜索仍未找到从站", level="ERROR")
                    self._ui_call(
                        self._scan_failed,
                        "全地址(1~247) x 常用波特率 都未找到从站\n\n"
                        "排查: 板子是否上电 / A-B 接线 / 串口是否选对\n"
                        "或先用 STC-ISP 重新烧写固件 (恢复 地址2/115200)")
                    return
                slave_id = found_id
                self.log("CONN", f"自动搜索到从站 #{slave_id} @ "
                                 f"{matched_baud}", slave_id)
                self._ui_call(self.slave_var.set, slave_id)

            self.baudrate = matched_baud
            self.slave_id = slave_id
            self.log("CONN", f"从站 #{slave_id} 在线, "
                             f"波特率确定为 {matched_baud}")
            self._ui_call(self.baud_var.set, str(matched_baud))

            try:
                timeout_s = self._poll_timeout_ms / 1000.0
                base_dev = GYJ0179(
                    port, slave_id=slave_id,
                    baudrate=matched_baud,
                    auto_scan=False, timeout=timeout_s)
                if not base_dev.connect():
                    self.log("CONN", f"打开 {port} 失败", level="ERROR")
                    self._ui_call(self._scan_failed, "打开串口失败")
                    return

                try:
                    base_dev.client.retries = 0
                    self.log("CONN", f"client 配置: timeout={timeout_s}s, "
                                     f"retries=0")
                except Exception:
                    pass

                self.dev = base_dev
                self.log("CONN", f"连接已建立 ({port})")
            except Exception as e:
                self.log("CONN", f"打开串口异常: {e}", level="ERROR")
                self._ui_call(self._scan_failed, str(e))
                return

            now = time.time()
            self.slave_data[slave_id] = {
                'online': True,
                'last_seen': now,
                'relays': [False, False, False],
                'di': [False] * 6,
                'di_value': 0,
                'params': [0] * 16,
            }
            self._stat_last_ok_time = now

            self._ui_call(self._on_connected, slave_id)

            self.polling = True
            self.poll_thread = threading.Thread(
                target=self._poll_loop, daemon=True)
            self.poll_thread.start()

        except Exception as e:
            if self._closing:
                return      # 窗口已关闭, 不再回调 UI
            self.log("CONN", f"扫描异常: {e}", level="ERROR")
            self._ui_call(self._scan_failed, str(e))

    def _ui_call(self, fn, *args):
        """从后台线程安全地把 UI 更新交给主线程执行。

        注意: 后台线程直接调用 root.after() 并不可靠 —— 有时抛
        "main thread is not in main loop", 有时排进队列却永不执行。
        这里改为写入线程安全队列, 由主线程定时消费 (_pump_ui_queue)。
        """
        if self._closing:
            return
        self._ui_queue.put((fn, args))

    def _pump_ui_queue(self):
        """主线程消费后台线程投递的 UI 更新"""
        while True:
            try:
                fn, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception as e:
                try:
                    self.log("SYS", f"UI 回调异常: {e}", level="ERROR")
                except Exception:
                    pass
        self.root.after(50, self._pump_ui_queue)

    def _scan_failed(self, reason):
        self.log("CONN", f"扫描失败: {reason}", level="ERROR")
        self.status_var.set("扫描失败")
        self.btn_scan.configure(state="normal")
        self.btn_stop_scan.configure(state="disabled")
        self.btn_disconnect.configure(state="disabled")

    def _on_connected(self, slave_id):
        """扫描成功后初始化详情视图/按钮/连接记忆"""
        self.slave_id = slave_id
        self.detail_view.show_slave(slave_id,
                                    self.slave_data.get(slave_id))
        self.status_var.set(
            f"已连接 {self.port} #{slave_id} ({self.label_mgr.display_slave(slave_id)})")
        self.btn_disconnect.configure(state="normal")
        self.btn_scan.configure(state="disabled")
        self.btn_stop_scan.configure(state="disabled")

        if self._port_state != "reconnecting":
            self._expected_port = self.port
        self._mark_connected()

        try:
            self.conn_store.save_single(self.port, self.baudrate, slave_id)
            self.log("CONN", "连接参数已保存, 下次可快速重连")
        except Exception as e:
            self.log("CONN", f"保存连接参数异常: {e}", level="WARN")

        self.log("CONN", f"开始轮询从站 #{slave_id}")

    # ==================== 轮询 ====================

    def _poll_loop(self):
        """后台轮询线程: 读继电器/光耦/寄存器, 处理离线与断线重连"""
        fail_count = 0
        last_online = True
        skip_until = 0.0

        while self.polling and self.dev:
            # 串口掉线自动重连
            try:
                if not self.dev.client.is_socket_open():
                    self.log("CONN", "串口连接已断开, 尝试重连...",
                             level="WARN")
                    try:
                        self.dev.client.close()
                    except Exception:
                        pass
                    time.sleep(0.15)
                    if not self.dev.client.connect():
                        time.sleep(0.5)
                        continue
                    self.log("CONN", "串口重连成功")
                    fail_count = 0
                    skip_until = 0.0
            except Exception:
                pass

            try:
                threshold = self._poll_fail_threshold
                if threshold < 1:
                    threshold = 1
            except Exception:
                threshold = 2

            try:
                retry_interval = self._poll_retry_interval
                if retry_interval < 1:
                    retry_interval = 1
            except Exception:
                retry_interval = 2

            now = time.time()
            sid = self.slave_id

            # 离线期间按重试间隔跳过轮询
            if (not last_online) and (now < skip_until):
                time.sleep(0.1)
                continue

            coils = bits = params = None
            try:
                with self.dev_lock:
                    coils = self.dev.read_coils(0, 3)
                with self.dev_lock:
                    bits = self.dev.read_di_bits()
                with self.dev_lock:
                    params = self.dev.get_all_params()
            except Exception:
                coils = bits = params = None

            if coils is None or bits is None or params is None:
                self._stat_errors += 1
                self._stat_last_error_time = time.time()
                fail_count += 1
                if fail_count >= threshold and last_online:
                    last_online = False
                    skip_until = time.time() + retry_interval
                    if sid in self.slave_data:
                        self.slave_data[sid]['online'] = False
                    self.log("SLAVE",
                             f"离线 (连续 {fail_count} 次无响应)",
                             sid, level="WARN")
                time.sleep(0.1)
                continue

            now_ok = time.time()
            self._stat_last_ok_time = now_ok
            self._stat_last_error_time = None
            self._stat_total_polls += 1

            self.slave_data[sid] = {
                'online': True,
                'last_seen': now_ok,
                'relays': list(coils[:3]),
                'di': list(bits[:6]),
                'di_value': sum(b << i for i, b in enumerate(bits[:6])),
                'params': list(params),
            }
            fail_count = 0

            if not last_online:
                last_online = True
                skip_until = 0.0
                self.log("SLAVE", "恢复在线", sid)

            time.sleep(0.2)

    # ==================== 设备操作回调 ====================

    def _do_relay(self, sid, relay, on):
        if not self.dev:
            return False
        try:
            with self.dev_lock:
                saved = self.dev.slave_id
                self.dev.slave_id = sid
                ok = (self.dev.relay_on(relay) if on
                      else self.dev.relay_off(relay))
                self.dev.slave_id = saved
            if ok:
                self._stat_last_ok_time = time.time()
            else:
                self._stat_errors += 1
            return bool(ok)
        except Exception as e:
            self.log("RELAY", f"操作异常: {e}", sid, level="ERROR")
            self._stat_errors += 1
            return False

    def _do_toggle(self, sid, relay):
        if not self.dev:
            return False
        try:
            with self.dev_lock:
                saved = self.dev.slave_id
                self.dev.slave_id = sid
                ok = self.dev.relay_toggle(relay)
                self.dev.slave_id = saved
            if ok:
                self._stat_last_ok_time = time.time()
            else:
                self._stat_errors += 1
            return bool(ok)
        except Exception as e:
            self.log("RELAY", f"翻转异常: {e}", sid, level="ERROR")
            self._stat_errors += 1
            return False

    def _do_write_param(self, sid, idx, val, save):
        if not self.dev:
            return False
        try:
            with self.dev_lock:
                saved = self.dev.slave_id
                self.dev.slave_id = sid

                if idx == -1:
                    ok = self.dev.set_all_params([0] * 15, save=False)
                elif idx == 0x0F:
                    ok = self.dev.save_params()
                else:
                    ok = self.dev.set_param(idx, val, save=save)

                self.dev.slave_id = saved
            if ok:
                self._stat_last_ok_time = time.time()
            else:
                self._stat_errors += 1
            return bool(ok)
        except Exception as e:
            self.log("PARAM", f"写参数异常: {e}", sid, level="ERROR")
            self._stat_errors += 1
            return False

    def _broadcast(self, action, relay=None):
        if not self.dev:
            messagebox.showwarning("提示", "请先扫描连接")
            return
        try:
            with self.dev_lock:
                saved = self.dev.slave_id
                self.dev.slave_id = self.dev.BROADCAST_ADDR

                if relay is None:
                    if action == "on":
                        self.dev.broadcast_all_on()
                        self.log("CMD", "广播: 全部吸合")
                    else:
                        self.dev.broadcast_all_off()
                        self.log("CMD", "广播: 全部释放")
                else:
                    if action == "on":
                        self.dev.broadcast_relay_on(relay)
                        self.log("CMD", f"广播: 开 COM{relay}")
                    else:
                        self.dev.broadcast_relay_off(relay)
                        self.log("CMD", f"广播: 关 COM{relay}")

                self.dev.slave_id = saved
            self._stat_last_ok_time = time.time()
        except Exception as e:
            self.log("CMD", f"广播异常: {e}", level="ERROR")
            self._stat_errors += 1

    # ==================== 断开 / 关闭 ====================

    def _on_disconnect(self):
        self.polling = False
        self._mark_idle()

        if self.poll_thread:
            self.poll_thread.join(timeout=2)
            self.poll_thread = None

        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None

        self.slave_data = {}
        self.detail_view.clear()
        self.detail_view.sid = None

        self.status_var.set("未连接")
        self.btn_disconnect.configure(state="disabled")
        self.btn_scan.configure(state="normal")

        self.sb_conn_var.set("未连接")
        self.sb_conn_lbl.configure(fg="red")
        self.sb_conn_lamp.configure(bg="gray")
        self.sb_port_var.set("--")
        self.sb_baud_var.set("--")
        self.sb_slaves_var.set("0 / 1")
        self.sb_poll_var.set("停止")
        self.sb_poll_lbl.configure(fg="gray")

        self.log("CONN", "已断开")

    def _on_close(self):
        self._closing = True
        self._port_monitor_running = False
        self.polling = False
        if self.poll_thread:
            self.poll_thread.join(timeout=2)
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
        self.log("SYS", "程序退出")
        self.root.destroy()


# ==================== 入口 ====================

def main():
    root = tk.Tk()
    # 中文字体
    try:
        root.option_add("*Font", ("Microsoft YaHei", 9))
    except Exception:
        pass
    GYJ0179GUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
