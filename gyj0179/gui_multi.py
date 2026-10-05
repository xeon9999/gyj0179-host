# -*- coding: utf-8 -*-
"""
GYJ-0179 多从站监控台（标签命名版）
平台: STC15W408AS + SP485EE

功能:
  - 扫描多个从站并共享一条串口连接轮询
  - 分页浏览每个从站的继电器/光耦/寄存器详情
  - 标签命名、日志记录与查询
  - 广播控制、自动翻页、离线重连
"""
import os
import sys

# 兼容两种运行方式:
#   - python -m gyj0179.gui_multi   (作为包模块运行, 相对导入可用)
#   - python gui_multi.py           (直接运行, 手动把父目录加入 sys.path)
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

from pymodbus.client import ModbusSerialClient
from pymodbus import __version__ as PYMODBUS_VERSION


# ==================== 独立探测 ====================

def _probe_one_slave(port, baudrate, slave_id, timeout=0.25,
                     cancelled=None):
    """探测指定从站是否在线; cancelled 返回 True 时立即放弃 (停止扫描)"""
    if cancelled is not None and cancelled():
        return False
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

class MultiSlaveGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("GYJ-0179 多从站监控台 (标签命名版)")
        self.root.geometry("1150x860")
        self.root.minsize(1000, 760)

        self.slaves = []
        self.slave_data = {}
        self.slave_buttons = {}
        self.current_page = 0
        self.dev = None
        self.dev_lock = threading.Lock()
        self.port = None
        self.baudrate = 9600
        self.polling = False
        self.poll_thread = None
        self.scan_thread = None
        self.log_queue = queue.Queue()
        self._ui_queue = queue.Queue()   # 后台线程 -> 主线程的 UI 更新队列
        self._auto_job = None

        self.log_mgr = LogManager(log_dir="logs", keep_days=30)
        self.label_mgr = LabelManager(path="labels.json")
        self.conn_store = ConnectionStore(path="connection.json")

        self.sb_conn_var = tk.StringVar(value="未连接")
        self.sb_port_var = tk.StringVar(value="--")
        self.sb_baud_var = tk.StringVar(value="--")
        self.sb_slaves_var = tk.StringVar(value="0 / 0")
        self.sb_last_comm_var = tk.StringVar(value="--")
        self.sb_poll_var = tk.StringVar(value="停止")
        self.sb_error_var = tk.StringVar(value="0")

        self._stat_total_polls = 0
        self._stat_errors = 0
        self._stat_last_ok_time = None
        self._stat_last_error_time = None   # 最近一次通讯异常的时间
        self._closing = False               # 窗口已关闭, 后台线程不再回调 UI
        self._scan_stop = False         # 用户请求停止扫描
        self._cfg_switch_until = 0      # 配置切换冷却截止时间

        # 轮询参数的主线程缓存 (Tk 变量不可在后台线程访问, 否则线程会卡住)
        self._poll_timeout_ms = 200
        self._poll_fail_threshold = 2
        self._poll_retry_interval = 2

        # 串口热插拔检测状态
        self._port_state = "idle"       # idle/connected/removed/reconnecting
        self._port_monitor_running = False
        self._reconnect_attempted = False
        self._expected_port = None       # 期望连接的端口 (原端口), 用于优先重连

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

        # 启动串口热插拔监控
        self._start_port_monitor()

        # 启动后自动尝试快速重连上次成功连接的参数
        self.root.after(300, self._auto_reconnect)

    def _build_ui(self):
        self._build_top_bar()
        self._build_page_bar()
        self._build_detail_area()
        self._build_log_area()
        self._build_status_bar()

    def _build_top_bar(self):
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

        ttk.Label(row1, text="地址范围:").pack(side="left")
        self.addr_min_var = tk.IntVar(value=1)
        self.addr_max_var = tk.IntVar(value=9)
        ttk.Spinbox(row1, from_=1, to=247, width=4,
                    textvariable=self.addr_min_var).pack(side="left")
        ttk.Label(row1, text="~").pack(side="left")
        ttk.Spinbox(row1, from_=1, to=247, width=4,
                    textvariable=self.addr_max_var).pack(side="left")

        self.btn_scan = ttk.Button(row1, text="扫描并连接",
                                   command=self._on_scan)
        self.btn_scan.pack(side="left", padx=10)

        self.btn_stop_scan = ttk.Button(row1, text="■ 停止扫描",
                                        command=self._stop_scan,
                                        state="disabled")
        self.btn_stop_scan.pack(side="left", padx=2)

        self.btn_disconnect = ttk.Button(row1, text="断开全部",
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

        # 把轮询参数同步到主线程缓存, 供轮询线程安全读取
        self.timeout_ms_var.trace_add("write", self._sync_poll_params)
        self.fail_threshold_var.trace_add("write", self._sync_poll_params)
        self.retry_interval_var.trace_add("write", self._sync_poll_params)
        self._sync_poll_params()

    def _build_page_bar(self):
        bar = ttk.LabelFrame(self.root, text="分页", padding=8)
        bar.pack(fill="x", padx=5, pady=(0, 3))

        ttk.Button(bar, text="◀ 上一页",
                   command=self._prev_page).pack(side="left", padx=2)

        self.page_var = tk.StringVar(value="-- / --")
        ttk.Label(bar, textvariable=self.page_var,
                  font=("Consolas", 12, "bold"), width=22
                  ).pack(side="left", padx=8)

        ttk.Button(bar, text="下一页 ▶",
                   command=self._next_page).pack(side="left", padx=2)

        ttk.Separator(bar, orient="vertical").pack(
            side="left", fill="y", padx=10)

        self.auto_switch_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="自动切换",
                        variable=self.auto_switch_var,
                        command=self._on_auto_switch_changed
                        ).pack(side="left", padx=2)

        ttk.Label(bar, text="间隔(秒):").pack(side="left", padx=(10, 2))
        self.interval_var = tk.StringVar(value="5")
        ttk.Spinbox(bar, from_=1, to=60, width=4,
                    textvariable=self.interval_var
                    ).pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(
            side="left", fill="y", padx=10)
        ttk.Label(bar, text="在线从站:").pack(side="left")
        self.slaves_bar = ttk.Frame(bar)
        self.slaves_bar.pack(side="left", padx=5)

    def _build_detail_area(self):
        container = ttk.LabelFrame(self.root, text="从站详情",
                                   padding=5)
        container.pack(fill="both", expand=True,
                       padx=5, pady=(0, 3))

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

    def _start_status_bar_refresh(self):
        self._update_status_bar()
        self.root.after(500, self._start_status_bar_refresh)

    def _update_status_bar(self):
        # 端口移除/重连状态优先显示
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

        total = len(self.slaves)
        online = sum(1 for sid in self.slaves
                     if self.slave_data.get(sid, {}).get('online', False))
        if total == 0:
            self.sb_slaves_var.set("0 / 0")
        else:
            if online == total:
                self.sb_slaves_var.set(f"{online} / {total}  ✓")
            elif online == 0:
                self.sb_slaves_var.set(f"{online} / {total}  ✗")
            else:
                self.sb_slaves_var.set(f"{online} / {total}")

        # 判断是否处于通讯异常状态 (端口移除/重连中/所有从站离线)
        abnormal = (self._port_state in ("removed", "reconnecting")
                    or (self.slaves and not any(
                        d.get('online') for d in self.slave_data.values())))
        if abnormal and self._stat_last_error_time is not None:
            # 通讯异常: 显示异常发生时刻 (时分秒)
            self.sb_last_comm_var.set(
                "异常 " + time.strftime("%H:%M:%S",
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
        """获取当前所有可用串口设备名列表"""
        import serial.tools.list_ports
        try:
            return [p.device for p in serial.tools.list_ports.comports()]
        except Exception:
            return []

    def _start_port_monitor(self):
        """启动串口热插拔定时监控 (Tk 主线程, 不阻塞 UI)"""
        if self._port_monitor_running:
            return
        self._port_monitor_running = True
        self._monitor_ports()

    def _monitor_ports(self):
        """定时检查物理串口, 处理移除/恢复/新端口"""
        if not self._port_monitor_running:
            return

        try:
            available = self._get_available_ports()

            # 状态 1: 已连接, 检查端口移除 / 期望端口恢复
            if self._port_state == "connected" and self.dev is not None:
                if self.port not in available:
                    # 当前端口被移除
                    self._handle_port_removed()
                elif (self._expected_port
                        and self._expected_port in available
                        and self.port != self._expected_port):
                    # 期望端口已恢复, 但当前连在别的端口上 → 切回期望端口
                    self.log("CONN",
                             f"原端口 {self._expected_port} 已恢复, "
                             f"从 {self.port} 切回...")
                    self._port_state = "reconnecting"
                    self._handle_port_reconnect(use_saved=True)

            # 状态 2/3: 移除/重连中, 检查期望端口恢复或新端口
            elif self._port_state in ("removed", "reconnecting"):
                if (self._expected_port
                        and self._expected_port in available):
                    # 期望端口恢复, 优先重连
                    self._handle_port_reconnect(use_saved=True)
                elif available:
                    # 期望端口未恢复但有可用端口, 自动扫描
                    self._handle_port_reconnect(use_saved=False)

        except Exception as e:
            self.log("CONN", f"端口监控异常: {e}", level="ERROR")

        self.root.after(500, self._monitor_ports)

    def _handle_port_removed(self):
        """端口被移除: 停止轮询, 关闭设备, 更新状态并提示"""
        self._port_state = "removed"
        removed_port = self.port
        # 记录期望端口 (原端口), 供恢复时优先重连
        self._expected_port = self.port

        # 停止轮询
        self.polling = False
        if self.poll_thread:
            self.poll_thread.join(timeout=1)
            self.poll_thread = None

        # 关闭设备
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None

        # 记录异常时间
        self._stat_last_error_time = time.time()

        # 状态栏更新
        self.sb_conn_var.set(f"端口异常: {removed_port} 已移除")
        self.sb_conn_lbl.configure(fg="red")
        self.sb_conn_lamp.configure(bg="red")
        self.sb_poll_var.set("停止")
        self.sb_poll_lbl.configure(fg="gray")

        self.log("CONN", f"检测到 {removed_port} 已被移除, 等待重连...",
                 level="WARN")
        self.status_var.set(f"{removed_port} 已移除, 等待重连...")

    def _handle_port_reconnect(self, use_saved):
        """端口恢复或新端口插入: 自动重连/扫描。
        优先重连期望端口, 否则扫描其他可用端口。
        """
        # 防止重复触发 (同一时刻只发起一次重连)
        if self._reconnect_attempted:
            return
        self._reconnect_attempted = True
        self._port_state = "reconnecting"
        self.status_var.set("检测到串口, 正在重连...")

        # 切换端口前, 先停止轮询并关闭现有设备
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
            # 期望端口恢复, 优先重连它
            target = self._expected_port
            self.log("CONN", f"端口 {target} 已恢复, 自动重连...")
            self.port_var.set(target)
            self.baud_var.set(str(self.baudrate))
        else:
            self.log("CONN", "检测到新串口, 自动扫描...")
            self.port_var.set("自动")

        # 触发扫描 (后台线程)
        self._on_scan()

        # 延迟重置重连标志, 允许后续再次触发
        def _reset_flag():
            self._reconnect_attempted = False
        self.root.after(3000, _reset_flag)

    def _mark_connected(self):
        """扫描成功后调用, 更新端口状态"""
        self._port_state = "connected"

    def _mark_idle(self):
        """断开/未连接时调用"""
        self._port_state = "idle"

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

    def _reset_status_stats(self):
        self._stat_total_polls = 0
        self._stat_errors = 0
        self._stat_last_ok_time = None

    def _reset_poll_defaults(self):
        """恢复轮询参数为默认值"""
        self.timeout_ms_var.set(200)
        self.fail_threshold_var.set(2)
        self.retry_interval_var.set(2)
        self.log("SYS", "轮询参数已恢复默认 (超时200ms / 离线2次 / 重试2s)")

    def _sync_poll_params(self, *_args):
        """把 Tk 变量的轮询参数缓存到普通属性 (轮询线程读取用)

        Tk 变量只能在主线程访问: 后台线程里调用 IntVar.get() 会阻塞甚至报错,
        导致轮询线程卡死。轮询线程一律读这里的缓存值。
        """
        def _int(var, default, low, high):
            try:
                v = int(var.get())
            except Exception:
                return default
            return max(low, min(high, v))

        self._poll_timeout_ms = _int(self.timeout_ms_var, 200, 50, 2000)
        self._poll_fail_threshold = _int(self.fail_threshold_var, 2, 1, 20)
        self._poll_retry_interval = _int(self.retry_interval_var, 2, 1, 60)

    def log(self, category, message, slave_id=None, level="INFO"):
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
        slave_ids = self.slaves if self.slaves else list(range(1, 5))

        def on_saved():
            self._refresh_slave_buttons()
            self.detail_view.refresh_labels()
            self._update_page_indicator()
            self.log("SYS", "标签已更新")

        LabelEditWindow(self.root, self.label_mgr, slave_ids,
                        on_saved=on_saved)

    # ---------- 从站通信配置 (地址 / 波特率, 掉电保存) ----------

    def _current_slave_id(self):
        if self.slaves and 0 <= self.current_page < len(self.slaves):
            return self.slaves[self.current_page]
        return None

    def _open_slave_config(self):
        sid = self._current_slave_id()
        if self.dev is None or sid is None:
            messagebox.showwarning("提示", "请先扫描连接从站")
            return
        disp = self.label_mgr.display_slave(sid)

        def _read(_sid):
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
                    # 恢复成"当前应有"的地址: 若该从站刚被改成新地址,
                    # 不能再打回旧地址
                    self.dev.slave_id = getattr(self, "_cfg_addr_now", cur)
                return res
            except Exception as e:
                self.log("SYS", f"读取从站配置异常: {e}", level="ERROR")
                return None

        def _write(_sid, addr, baud_idx, cmd):
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
                # 命令字无应答、但地址/波特率已写入: 设备仍会切换, 继续重连;
                # 完全无应答(第 1 条就失败)才算真失败
                if is_apply and len(detail) >= 2 and detail[0] and detail[1]:
                    self._cfg_switch_until = time.time() + 3.0
                    self.log("CONN",
                             f"配置已部分写入 (命令字无应答), 仍尝试按新参数"
                             f"重连: 地址={addr} "
                             f"波特率={GYJ0179.baud_of_index(baud_idx)}", sid,
                             level="WARN")
                    return "partial"
                return False

            self.log("CONN", f"下发通信配置: {disp} → 地址={addr} "
                             f"波特率={GYJ0179.baud_of_index(baud_idx)} "
                             f"命令=0x{cmd:04X}", sid)

            # 注意: 这里不能再用"旧地址回读"来校验!
            # 从站收到 0xA500 后会先回应答帧、随即换用新地址/波特率,
            # 再用旧地址去读必然无应答, 会被误判成"写入失败"。
            # 写入是否生效, 由随后的自动扫描(按新参数连接)来验证。
            if is_apply:
                # 短暂等一下让从站把应答帧发完并切换;
                # 完整的切换等待由窗口的 _on_apply 统一负责 (避免重复等待)
                self._cfg_switch_until = time.time() + 3.0
                time.sleep(0.5)
                self.baudrate = GYJ0179.baud_of_index(baud_idx) or \
                    self.baudrate
                self.baud_var.set(str(self.baudrate))
                self.slave_data = {}
                self.log("CONN", "从站已切换, 开始自动重连", sid,
                         level="WARN")
            return True

        def on_applied(addr, baud_idx):
            self.status_var.set(
                f"{disp} 通信配置已改为 #{addr} @ "
                f"{GYJ0179.baud_of_index(baud_idx)}bps, 正在重新扫描连接...")
            # 新地址可能落在扫描范围外, 自动把范围包进来
            try:
                if addr < self.addr_min_var.get():
                    self.addr_min_var.set(max(1, addr))
                if addr > self.addr_max_var.get():
                    self.addr_max_var.set(min(247, addr))
            except Exception:
                pass
            self.root.after(800, self._auto_scan_after_config)

        SlaveConfigWindow(
            self.root, sid, self.baudrate,
            _read, _write, on_applied=on_applied,
            title_suffix=f" - {disp}", app=self)

    # ---------- 监听从站上电广播 (改了地址/波特率后找回来) ----------

    def _open_listen_report(self):
        # 监听要独占串口, 先断开避免端口被占用
        if self.dev is not None:
            self._on_disconnect()

        def on_found(addr, baud_val):
            # 新地址可能不在当前扫描范围内, 自动扩大范围
            try:
                if addr < self.addr_min_var.get():
                    self.addr_min_var.set(max(1, addr))
                if addr > self.addr_max_var.get():
                    self.addr_max_var.set(min(247, addr))
            except Exception:
                pass
            self.baud_var.set("自动")
            self.log("CONN", f"监听到从站: 地址={addr} 波特率={baud_val}, "
                             f"开始扫描连接", addr)
            self.status_var.set(f"监听到从站 #{addr} @ {baud_val}bps, 正在连接...")
            self.root.after(300, self._on_scan)

        ListenReportWindow(self.root, on_found=on_found)

    def _auto_scan_after_config(self):
        """配置生效后自动重新扫描并连接"""
        if self._closing:
            return
        self.log("CONN", "配置已更新, 自动扫描并重连...")
        self._on_scan()

    def _start_display_refresh(self):
        if (self.slaves and
                0 <= self.current_page < len(self.slaves)):
            sid = self.slaves[self.current_page]
            data = self.slave_data.get(sid)
            if data is not None:
                self.detail_view.update_data(data)
        self.root.after(200, self._start_display_refresh)


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
        """检查指定端口是否仍在当前可用串口列表中"""
        import serial.tools.list_ports
        try:
            for p in serial.tools.list_ports.comports():
                if p.device == port:
                    return True
        except Exception:
            pass
        return False

    def _auto_reconnect(self):
        """启动时自动尝试用上次成功连接的参数快速重连; 端口失效则自动扫描"""
        if not self.conn_store.has_saved():
            self.log("SYS", "无上次连接记录, 等待手动扫描")
            return

        saved_port = self.conn_store.port
        saved_baud = self.conn_store.baudrate
        saved_min = self.conn_store.addr_min
        saved_max = self.conn_store.addr_max
        ts = self.conn_store.timestamp or "未知时间"
        self.log("SYS", f"检测到上次连接记录: {saved_port} @ {saved_baud} "
                        f"(从站 {saved_min}~{saved_max}, {ts})")

        # 预填界面参数
        self.port_var.set(saved_port)
        self.baud_var.set(str(saved_baud))
        self.addr_min_var.set(saved_min)
        self.addr_max_var.set(saved_max)

        # 端口失效则回退自动扫描
        if not self._port_exists(saved_port):
            self.log("SYS", f"端口 {saved_port} 已失效, 切换到自动扫描")
            self.port_var.set("自动")

        self._on_scan()

    def _on_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            self.log("SYS", "扫描已在进行中")
            return

        port_str = self.port_var.get()
        addr_min = self.addr_min_var.get()
        addr_max = self.addr_max_var.get()

        if addr_min > addr_max:
            messagebox.showerror("错误", "起始地址 > 结束地址")
            return

        # 波特率: 选"自动"则遍历所有波特率, 否则只用选定的
        baud_sel = str(self.baud_var.get())
        if baud_sel == "自动":
            baudrates = [9600, 19200, 38400, 57600, 115200, 28800, 14400]
        else:
            baudrates = [int(baud_sel)]

        self._reset_status_stats()
        self.log("CONN", f"开始扫描: 地址 {addr_min}~{addr_max}, "
                 f"波特率 {baudrates}, 串口={port_str}")

        self._scan_stop = False
        self.btn_scan.configure(state="disabled")
        self.btn_stop_scan.configure(state="normal")
        self.status_var.set("扫描中...")

        self.scan_thread = threading.Thread(
            target=self._scan_worker,
            args=(port_str, baudrates, addr_min, addr_max),
            daemon=True)
        self.scan_thread.start()

    def _scan_worker(self, port_str, baudrates, addr_min, addr_max):
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
                    self.log("CONN", "未找到任何可用串口",
                             level="ERROR")
                    self._ui_call(self._scan_failed, "未找到串口")
                    return
            else:
                port = port_str.split()[0]

            self.log("CONN", f"使用串口: {port}")
            self.port = port

            # 外层遍历波特率, 内层遍历地址, 自动切换波特率直到找到设备
            found = []
            matched_baud = None
            for baudrate in baudrates:
                self.log("CONN",
                         f"尝试波特率 {baudrate}, 扫描地址 "
                         f"{addr_min}~{addr_max}...")
                baud_found = []
                for sid in range(addr_min, addr_max + 1):
                    ok = _probe_one_slave(port, baudrate, sid,
                                          timeout=0.2,
                                          cancelled=_cancelled)
                    if _cancelled():
                        self._scan_stopped()
                        return
                    if ok:
                        baud_found.append(sid)
                        self.log("SLAVE", f"发现从站 #{sid} 在线",
                                 sid)
                    else:
                        self.log("SLAVE", f"地址 #{sid} 无响应",
                                 sid, level="DEBUG")
                if baud_found:
                    found = baud_found
                    matched_baud = baudrate
                    break
                self.log("CONN",
                         f"波特率 {baudrate} 未找到设备, 切换到下一个")

            # 固定波特率没找到时, 自动回退再试一遍常用波特率
            # (设备可能被改成过别的波特率, 这样就不会"再也连不上")
            if not found:
                extra = [b for b in (9600, 19200, 38400, 57600, 115200)
                         if b not in baudrates]
                if extra:
                    self.log("CONN",
                             f"指定波特率未找到, 自动尝试其它波特率 {extra}...",
                             level="WARN")
                    for baudrate in extra:
                        self.log("CONN",
                                 f"尝试波特率 {baudrate}, 扫描地址 "
                                 f"{addr_min}~{addr_max}...")
                        baud_found = []
                        for sid in range(addr_min, addr_max + 1):
                            if _probe_one_slave(port, baudrate, sid,
                                                timeout=0.2,
                                                cancelled=_cancelled):
                                baud_found.append(sid)
                                self.log("SLAVE",
                                         f"发现从站 #{sid} 在线", sid)
                            if _cancelled():
                                self._scan_stopped()
                                return
                        if baud_found:
                            found = baud_found
                            matched_baud = baudrate
                            break
                        self.log("CONN",
                                 f"波特率 {baudrate} 未找到设备")

            if not found:
                self.log("CONN", "未找到任何从站", level="ERROR")
                self._ui_call(self._scan_failed, "未找到从站")
                return

            self.baudrate = matched_baud
            self.log("CONN", f"波特率确定为 {matched_baud}, "
                     f"共找到 {len(found)} 个从站: {found}")
            # 同步界面下拉框显示实际匹配的波特率
            self._ui_call(self.baud_var.set, str(matched_baud))

            try:
                timeout_s = self._poll_timeout_ms / 1000.0
                base_dev = GYJ0179(
                    port, slave_id=found[0],
                    baudrate=matched_baud,
                    auto_scan=False, timeout=timeout_s)
                if not base_dev.connect():
                    self.log("CONN", f"打开 {port} 失败",
                             level="ERROR")
                    self._ui_call(self._scan_failed, "打开串口失败")
                    return

                try:
                    base_dev.client.retries = 0
                    self.log("CONN",
                             f"client 配置: timeout={timeout_s}s, "
                             f"retries=0")
                except Exception:
                    pass

                self.dev = base_dev
                self.log("CONN", f"共享连接已建立 ({port})")
            except Exception as e:
                self.log("CONN", f"打开串口异常: {e}",
                         level="ERROR")
                self._ui_call(self._scan_failed, str(e))
                return

            now = time.time()
            for sid in found:
                self.slave_data[sid] = {
                    'online': True,
                    'last_seen': now,
                    'relays': [False, False, False],
                    'di': [False] * 6,
                    'di_value': 0,
                    'params': [0] * 16,
                }
            self._stat_last_ok_time = now

            self._ui_call(self._init_pages, found)

            self.polling = True
            self.poll_thread = threading.Thread(
                target=self._poll_loop, daemon=True)
            self.poll_thread.start()

        except Exception as e:
            if self._closing:
                return      # 窗口已关闭, 不再回调 UI
            self.log("CONN", f"扫描异常: {e}", level="ERROR")
            self._ui_call(self._scan_failed, str(e))

    def _scan_failed(self, reason):
        self.log("CONN", f"扫描失败: {reason}", level="ERROR")
        self.status_var.set("扫描失败")
        self.btn_scan.configure(state="normal")
        self.btn_stop_scan.configure(state="disabled")

    def _init_pages(self, slave_ids):
        self.slaves = list(slave_ids)
        self.current_page = 0
        self.slave_buttons = {}

        for w in self.slaves_bar.winfo_children():
            w.destroy()

        for i, sid in enumerate(self.slaves):
            disp = self.label_mgr.display_slave(sid)
            btn = tk.Button(
                self.slaves_bar,
                text=disp,
                width=10,
                font=("Microsoft YaHei", 8, "bold"),
                bg="lightgreen",
                fg="black",
                relief="raised",
                command=lambda i=i: self._goto_page(i))
            btn.pack(side="left", padx=1)
            self.slave_buttons[sid] = btn

        self._goto_page(0)
        self.status_var.set(f"已连接 {len(self.slaves)} 个从站")
        self.btn_disconnect.configure(state="normal")
        self.btn_scan.configure(state="disabled")
        self.btn_stop_scan.configure(state="disabled")

        # 标记端口为已连接 (串口监控用)
        # 仅在非重连场景更新期望端口; 重连场景保持原期望端口 (优先切回原端口)
        if self._port_state != "reconnecting":
            self._expected_port = self.port
        self._mark_connected()

        # 保存本次成功连接的参数, 供下次快速重连
        try:
            self.conn_store.save_multi(
                self.port, self.baudrate, self.slaves,
                addr_min=self.addr_min_var.get(),
                addr_max=self.addr_max_var.get())
            self.log("CONN", "连接参数已保存, 下次可快速重连")
        except Exception as e:
            self.log("CONN", f"保存连接参数异常: {e}", level="WARN")

        self.log("CONN",
                 f"分页已初始化, 开始轮询 {len(self.slaves)} 个从站")

    def _refresh_slave_buttons(self):
        for sid, btn in self.slave_buttons.items():
            try:
                btn.configure(text=self.label_mgr.display_slave(sid))
            except Exception:
                pass

    def _update_page_indicator(self):
        total = len(self.slaves)
        if total == 0:
            self.page_var.set("-- / --")
        else:
            sid = self.slaves[self.current_page]
            data = self.slave_data.get(sid, {})
            online = data.get('online', False)
            mark = "●" if online else "○"
            disp = self.label_mgr.display_slave(sid)
            self.page_var.set(f"{mark} {disp} ({self.current_page+1}/{total})")

    def _goto_page(self, idx):
        if not self.slaves:
            return
        if idx < 0:
            idx = len(self.slaves) - 1
        if idx >= len(self.slaves):
            idx = 0
        self.current_page = idx
        sid = self.slaves[idx]
        data = self.slave_data.get(sid)
        self.detail_view.show_slave(sid, data)
        self._update_page_indicator()
        self._highlight_current_button()

    def _highlight_current_button(self):
        if not self.slaves:
            return
        current_sid = self.slaves[self.current_page]
        for sid, btn in self.slave_buttons.items():
            if sid == current_sid:
                btn.configure(relief="sunken", bd=3)
            else:
                btn.configure(relief="raised", bd=1)

    def _prev_page(self):
        if not self.slaves:
            return
        self._goto_page(self.current_page - 1)

    def _next_page(self):
        if not self.slaves:
            return
        self._goto_page(self.current_page + 1)

    def _on_auto_switch_changed(self):
        if self.auto_switch_var.get():
            self.log("SYS", "自动切换已开启")
            self._schedule_auto_switch()
        else:
            self.log("SYS", "自动切换已关闭")
            if self._auto_job:
                self.root.after_cancel(self._auto_job)
                self._auto_job = None

    def _schedule_auto_switch(self):
        if not self.auto_switch_var.get():
            return
        try:
            interval_s = float(self.interval_var.get())
            if interval_s < 0.5:
                interval_s = 0.5
        except Exception:
            interval_s = 5.0
        self._auto_job = self.root.after(
            int(interval_s * 1000), self._auto_switch_tick)

    def _auto_switch_tick(self):
        if not self.auto_switch_var.get():
            return
        if len(self.slaves) > 1:
            self._next_page()
        self._schedule_auto_switch()

    def _poll_loop(self):
        fail_count = {sid: 0 for sid in self.slaves}
        last_online = {sid: True for sid in self.slaves}
        skip_until = {sid: 0.0 for sid in self.slaves}

        while self.polling and self.dev:
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
                    fail_count = {sid: 0 for sid in self.slaves}
                    skip_until = {sid: 0.0 for sid in self.slaves}
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

            for sid in list(self.slaves):
                if not self.polling:
                    break

                # 分页初始化 (_init_pages) 在主线程完成后才会填充状态字典;
                # 轮询线程可能先启动, 这里补齐缺失的从站以避开竞态
                if sid not in last_online:
                    last_online[sid] = True
                    fail_count[sid] = 0
                    skip_until[sid] = 0.0

                if (not last_online[sid]) and (now < skip_until[sid]):
                    continue

                coils = None
                try:
                    with self.dev_lock:
                        self.dev.slave_id = sid
                        coils = self.dev.read_coils(0, 3)
                except Exception:
                    coils = None

                if coils is None:
                    self._stat_errors += 1
                    self._stat_last_error_time = time.time()
                    fail_count[sid] = fail_count.get(sid, 0) + 1
                    if (fail_count[sid] >= threshold
                            and last_online[sid]):
                        last_online[sid] = False
                        skip_until[sid] = time.time() + retry_interval
                        if sid in self.slave_data:
                            self.slave_data[sid]['online'] = False
                        self.log("SLAVE",
                                 f"离线 (连续 {fail_count[sid]} 次无响应)",
                                 sid, level="WARN")
                        self._ui_call(self._update_button_color, sid, False)
                    continue

                bits = None
                try:
                    with self.dev_lock:
                        bits = self.dev.read_di_bits()
                except Exception:
                    bits = None

                if bits is None:
                    self._stat_errors += 1
                    self._stat_last_error_time = time.time()
                    fail_count[sid] = fail_count.get(sid, 0) + 1
                    if (fail_count[sid] >= threshold
                            and last_online[sid]):
                        last_online[sid] = False
                        skip_until[sid] = time.time() + retry_interval
                        if sid in self.slave_data:
                            self.slave_data[sid]['online'] = False
                        self.log("SLAVE", "离线 (光耦无响应)",
                                 sid, level="WARN")
                        self._ui_call(self._update_button_color, sid, False)
                    continue

                params = None
                try:
                    with self.dev_lock:
                        params = self.dev.get_all_params()
                except Exception:
                    params = None

                if params is None:
                    self._stat_errors += 1
                    self._stat_last_error_time = time.time()
                    fail_count[sid] = fail_count.get(sid, 0) + 1
                    if (fail_count[sid] >= threshold
                            and last_online[sid]):
                        last_online[sid] = False
                        skip_until[sid] = time.time() + retry_interval
                        if sid in self.slave_data:
                            self.slave_data[sid]['online'] = False
                        self.log("SLAVE", "离线 (参数无响应)",
                                 sid, level="WARN")
                        self._ui_call(self._update_button_color, sid, False)
                    continue

                now_ok = time.time()
                self._stat_last_ok_time = now_ok
                self._stat_last_error_time = None   # 通讯恢复, 清除异常时间
                self._stat_total_polls += 1

                self.slave_data[sid] = {
                    'online': True,
                    'last_seen': now_ok,
                    'relays': list(coils[:3]),
                    'di': list(bits[:6]),
                    'di_value': sum(b << i
                                    for i, b in enumerate(bits[:6])),
                    'params': list(params),
                }
                fail_count[sid] = 0

                if not last_online[sid]:
                    last_online[sid] = True
                    skip_until[sid] = 0.0
                    self.log("SLAVE", "恢复在线", sid)
                    self._ui_call(self._update_button_color, sid, True)

                time.sleep(0.03)

            time.sleep(0.1)

    def _update_button_color(self, sid, online):
        btn = self.slave_buttons.get(sid)
        if btn is None:
            return
        if online:
            btn.configure(bg="lightgreen", fg="black")
        else:
            btn.configure(bg="salmon", fg="white")

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
            return ok
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
            return ok
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
            return ok
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

    def _on_disconnect(self):
        self.polling = False
        self._mark_idle()

        if self.auto_switch_var.get():
            self.auto_switch_var.set(False)
        if self._auto_job:
            self.root.after_cancel(self._auto_job)
            self._auto_job = None

        if self.poll_thread:
            self.poll_thread.join(timeout=2)
            self.poll_thread = None

        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None

        self.slaves = []
        self.slave_data = {}
        self.slave_buttons = {}
        self.current_page = 0
        self.detail_view.clear()
        self._update_page_indicator()

        for w in self.slaves_bar.winfo_children():
            w.destroy()

        self.status_var.set("未连接")
        self.btn_disconnect.configure(state="disabled")
        self.btn_scan.configure(state="normal")

        self.sb_conn_var.set("未连接")
        self.sb_conn_lbl.configure(fg="red")
        self.sb_conn_lamp.configure(bg="gray")
        self.sb_port_var.set("--")
        self.sb_baud_var.set("--")
        self.sb_slaves_var.set("0 / 0")
        self.sb_poll_var.set("停止")
        self.sb_poll_lbl.configure(fg="gray")

        self.log("CONN", "已断开所有从站")

    def _on_close(self):
        self._closing = True
        self.polling = False
        if self._auto_job:
            self.root.after_cancel(self._auto_job)
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
    try:
        root.option_add("*Font", ("Microsoft YaHei", 9))
    except Exception:
        pass
    app = MultiSlaveGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
