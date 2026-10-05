# -*- coding: utf-8 -*-
"""
GYJ-0179 从站通信配置窗口

功能: 上位机在线修改从站的 Modbus 从站地址与串口波特率, 并保存到单片机
      EEPROM (掉电不丢)。当前从站 GUI (单站/多站) 共用本窗口。

单片机侧的寄存器约定 (保持寄存器):
    reg[13] = 从站地址  (1~247, 0x00FF 表示恢复默认)
    reg[14] = 波特率索引 0~4
              0=9600  1=19200  2=38400  3=57600  4=115200
    reg[15] = 命令寄存器
              0x5AA5 → 只保存参数到 EEPROM
              0xA500 → 保存并立即应用新地址/波特率 (不断电)
              0xA501 → 恢复出厂设置 (地址/波特率回默认)

写序列: 先把地址与波特率写入 RAM, 再写命令触发保存并生效。
设备发完应答帧后切换, 所以本窗口必须在生效后等待约 1.5s 再让上位机重连。
"""
import time
import tkinter as tk
from tkinter import ttk, messagebox

# ---------------- 寄存器与命令定义 (与 chengxu.c 保持一致) ----------------
REG_ADDR_INDEX = 13
REG_BAUD_INDEX = 14
REG_CMD_INDEX = 15

CFG_CMD_SAVE = 0x5AA5
CFG_CMD_APPLY = 0xA500
CFG_CMD_FACTORY = 0xA501

DEFAULT_SLAVE_ADDR = 2
DEFAULT_BAUD_INDEX = 4

# (索引, 波特率数值, 界面文本) —— 必须与固件 Baud_Value() 完全一致
BAUD_TABLE = (
    (0, 9600, "9600"),
    (1, 19200, "19200"),
    (2, 38400, "38400"),
    (3, 57600, "57600"),
    (4, 115200, "115200"),
    (5, 28800, "28800"),
    (6, 14400, "14400"),
)
BAUD_TEXTS = [t for _, _, t in BAUD_TABLE]

# 配置生效等待时间 (秒): 设备需发完应答帧再切换波特率
APPLY_WAIT_S = 1.5


def baud_text_of_index(idx):
    for i, _v, t in BAUD_TABLE:
        if i == idx:
            return t
    return f"未知({idx})"


def baud_index_of_text(text):
    for i, _v, t in BAUD_TABLE:
        if t == str(text).strip():
            return i
    return None


def _set_status(app, text):
    """安全地更新主界面状态栏 (app 可能不是 GUI 实例)"""
    try:
        app.status_var.set(text)
    except Exception:
        pass


class SlaveConfigWindow(tk.Toplevel):
    """修改从站地址 / 波特率 (保存在从站 EEPROM)

    read_cb(sid) -> (addr, baud_index) 或 None
    write_cb(sid, addr, baud_index, cmd) -> bool
    """

    def __init__(self, parent, slave_id, baudrate, read_cb, write_cb,
                 on_applied=None, title_suffix="", app=None):
        super().__init__(parent)
        # app: 主界面实例 (用于状态栏提示); 未提供时退回 parent
        self.app = app if app is not None else parent
        self.title(f"从站通信配置{title_suffix}")
        # 先给个初始宽度; 高度在内容建好后按需要自适应, 不留空白
        self.geometry("470x300")
        self.resizable(False, True)
        self.transient(parent.winfo_toplevel())
        self.grab_set()

        self.slave_id = slave_id
        self.baudrate = baudrate
        self.read_cb = read_cb
        self.write_cb = write_cb
        self.on_applied = on_applied
        self._dirty = False
        self._loading = False

        self.addr_var = tk.StringVar(value=str(slave_id))
        self.baud_var = tk.StringVar(value=baud_text_of_index(
            baud_index_of_text(baudrate) if baud_index_of_text(baudrate)
            is not None else DEFAULT_BAUD_INDEX))
        self.cur_var = tk.StringVar(value="读取中...")
        self.tip_var = tk.StringVar(value="")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(120, self.refresh)

    # ---------- 界面 ----------
    def _build_ui(self):
        WIN_W = 470
        WRAP = WIN_W - 60          # 文字自动换行宽度 (留出左右边距)

        top = ttk.LabelFrame(self, text="当前从站", padding=8)
        top.pack(fill="x", padx=10, pady=(10, 4))

        ttk.Label(top, textvariable=self.cur_var, justify="left",
                  wraplength=WRAP, font=("Consolas", 9)
                  ).pack(side="left", fill="x")

        box = ttk.LabelFrame(self, text="修改为", padding=8)
        box.pack(fill="x", padx=10, pady=4)

        r1 = ttk.Frame(box)
        r1.pack(fill="x", pady=3)
        ttk.Label(r1, text="从站地址:", width=10).pack(side="left")
        self.addr_spin = ttk.Spinbox(r1, from_=1, to=247, width=6,
                                     textvariable=self.addr_var,
                                     command=self._mark_dirty)
        self.addr_spin.pack(side="left")
        ttk.Label(r1, text="(1~247)", foreground="gray"
                  ).pack(side="left", padx=(6, 0))

        r2 = ttk.Frame(box)
        r2.pack(fill="x", pady=3)
        ttk.Label(r2, text="波特率:", width=10).pack(side="left")
        self.baud_cb = ttk.Combobox(r2, textvariable=self.baud_var,
                                    values=BAUD_TEXTS, width=9,
                                    state="readonly")
        self.baud_cb.pack(side="left")
        self.baud_cb.bind("<<ComboboxSelected>>", lambda e: self._mark_dirty())
        ttk.Label(r2, text="bps", foreground="gray"
                  ).pack(side="left", padx=(6, 0))

        info = ttk.LabelFrame(self, text="说明", padding=6)
        info.pack(fill="x", padx=10, pady=4)
        ttk.Label(
            info, justify="left", anchor="w", foreground="#333333",
            wraplength=WRAP,
            text=("修改后立即写入从站 EEPROM, 掉电不丢。生效后从站换用新的"
                  "地址/波特率, 上位机需重新扫描连接。\n"
                  "地址或波特率改错会连不上, 可点\"恢复出厂\"回到 "
                  f"地址 {DEFAULT_SLAVE_ADDR} / "
                  f"{baud_text_of_index(DEFAULT_BAUD_INDEX)}bps。\n"
                  "提示: 本功能需新版单片机固件支持。")
        ).pack(side="left", fill="x")

        self.tip_lbl = ttk.Label(self, textvariable=self.tip_var,
                                 foreground="#0a6", justify="left",
                                 anchor="w", wraplength=WRAP)
        self.tip_lbl.pack(fill="x", padx=12, pady=(0, 2))

        # 按钮区固定窗口底部 (先 pack 到 bottom), 内容再多也不会把它挤出可视区
        # 用 grid 分两组左对齐: 最长的"应用并保存到从站"不会被右侧按钮挤压
        bar = ttk.Frame(self)
        bar.pack(side="bottom", fill="x", padx=10, pady=(4, 10))
        self.btn_apply = ttk.Button(bar, text="应用并保存到从站",
                                    command=self._on_apply, state="disabled")
        self.btn_apply.grid(row=0, column=1, padx=(0, 6))
        ttk.Button(bar, text="关闭", command=self.destroy
                   ).grid(row=0, column=2, padx=2)
        self.btn_refresh = ttk.Button(bar, text="重新读取",
                                      command=self.refresh)
        self.btn_refresh.grid(row=0, column=3, padx=(16, 2))
        ttk.Button(bar, text="恢复出厂", command=self._on_factory
                   ).grid(row=0, column=4, padx=2)
        self._bar = bar

        # 按内容自适应高度: 不留大片空白, 内容变化时也自动贴合
        self.after_idle(self._fit_height)

    WIN_W = 470

    def _fit_height(self, *_a):
        """高度按内容自适应, 宽度取"设计宽度"与"按钮行所需宽度"的较大值"""
        try:
            if not self.winfo_exists():
                return          # 窗口已关闭, 不再调整 (避免 TclError)
            self.update_idletasks()
            h = self.winfo_reqheight()
            need = getattr(self, "_bar", None)
            need_w = (need.winfo_reqwidth() + 30) if need is not None else 0
            w = max(self.WIN_W, need_w)
            self.geometry(f"{w}x{h}")
            self.update_idletasks()
        except Exception:
            pass

    def _mark_dirty(self, *_a):
        if self._loading:
            return
        self._dirty = True
        self.btn_apply.configure(state="normal")

    # ---------- 读 ----------
    def refresh(self):
        """从从站读回当前地址与波特率"""
        self.tip_var.set("")
        try:
            res = self.read_cb(self.slave_id)
        except Exception as e:
            res = None
            self.tip_var.set(f"读取异常: {e}")
        if not res:
            self.cur_var.set(f"从站 #{self.slave_id} 读取失败 (离线或地址不符)")
            self.tip_var.set("请先扫描连接再改配置")
            self.btn_apply.configure(state="disabled")
            self._dirty = False
            self.after_idle(self._fit_height)
            return

        addr, baud_idx = int(res[0]), int(res[1])

        # 合法性校验: 旧固件没有 reg[13]/reg[14], 会返回 0, 不能当成真实配置
        addr_bad = not 1 <= addr <= 247
        baud_bad = not 0 <= baud_idx < len(BAUD_TABLE)
        if addr_bad or baud_bad:
            self.cur_var.set(
                f"从站 #{self.slave_id} 读到的配置无效 "
                f"(reg[{REG_ADDR_INDEX}]={addr}, reg[{REG_BAUD_INDEX}]={baud_idx})")
            self.tip_var.set(
                "该从站固件不支持在线改地址/波特率, 请重写 chengxu.c; "
                "也可直接填入目标值后点“应用并保存到从站”")
            # 地址框给个可用的初值, 方便老固件用户直接设置
            self._loading = True
            if addr_bad:
                self.addr_var.set(str(self.slave_id
                                      if 1 <= self.slave_id <= 247
                                      else DEFAULT_SLAVE_ADDR))
            if baud_bad:
                idx = baud_index_of_text(self.baudrate)
                self.baud_var.set(baud_text_of_index(
                    idx if idx is not None else DEFAULT_BAUD_INDEX))
            self._loading = False
            self._dirty = False
            # 允许用户主动写入 (老固件写了也不生效, 但不会误读成 0)
            self.btn_apply.configure(state="normal")
            self.after_idle(self._fit_height)
            return

        self.cur_var.set(
            f"从站 #{self.slave_id}   当前地址 {addr}   "
            f"当前波特率 {baud_text_of_index(baud_idx)}bps "
            f"(reg[{REG_ADDR_INDEX}]={addr}, reg[{REG_BAUD_INDEX}]={baud_idx})")

        self._loading = True
        self.addr_var.set(str(addr))
        self.baud_var.set(baud_text_of_index(baud_idx))
        self._loading = False
        self._dirty = False
        self.btn_apply.configure(state="disabled")
        self.after_idle(self._fit_height)

    # ---------- 写 ----------
    def _validate(self):
        try:
            addr = int(str(self.addr_var.get()).strip())
        except Exception:
            messagebox.showerror("错误", "从站地址必须是 1~247 的整数", parent=self)
            return None
        if not 1 <= addr <= 247:
            messagebox.showerror("错误", "从站地址必须在 1~247 之间", parent=self)
            return None
        idx = baud_index_of_text(self.baud_var.get())
        if idx is None:
            messagebox.showerror("错误", "请选择波特率", parent=self)
            return None
        return addr, idx

    def _on_apply(self):
        v = self._validate()
        if v is None:
            return
        addr, idx = v
        if addr == self.slave_id and baud_text_of_index(idx) == str(self.baudrate):
            self.tip_var.set("与当前配置相同, 无需修改")
            return

        if not messagebox.askyesno(
                "确认", f"把从站 #{self.slave_id} 改为\n\n"
                        f"    地址 {addr}\n"
                        f"    波特率 {baud_text_of_index(idx)}bps\n\n"
                        f"并保存到 EEPROM (掉电不丢)?\n"
                        f"生效后本软件需重新扫描连接。", parent=self):
            return

        self.tip_var.set("正在写入...")
        self.update_idletasks()
        ok = False
        try:
            # True=完全成功; "partial"=地址/波特率已写仅命令字无应答; False=失败
            ok = self.write_cb(self.slave_id, addr, idx, CFG_CMD_APPLY)
        except Exception as e:
            self.tip_var.set(f"写入异常: {e}")
        if ok is not True and ok != "partial":
            if not self.tip_var.get().startswith("写入异常"):
                self.tip_var.set("写入失败: 从站无应答 (检查连接/地址)")
            if self._dirty:
                self.btn_apply.configure(state="normal")
            return

        self.btn_apply.configure(state="disabled")
        self._dirty = False
        if ok == "partial":
            # 地址/波特率已写入、仅命令字无应答: 设备仍会切换,
            # 继续走"等待切换 → 关窗 → 重新扫描", 不要卡在这里
            self.tip_var.set("配置已下发 (命令应答异常), 尝试按新参数重连...")
        else:
            self.tip_var.set("已下发, 等待从站切换...")
        self.update_idletasks()

        # 等从站发完应答并切到新参数 (设备侧需约 1.5s)
        t0 = time.time()
        while time.time() - t0 < APPLY_WAIT_S:
            self.update()
            time.sleep(0.03)

        self.tip_var.set("已保存并生效")
        baud_txt = baud_text_of_index(idx)
        _set_status(self.app,
                    f"从站配置已改为 #{addr} @ {baud_txt}bps, "
                    f"正在重新扫描连接...")
        # 自动关闭本窗口, 由主界面自动扫描并重新建立连接
        self.destroy()
        if self.on_applied:
            try:
                self.on_applied(addr, idx)
            except Exception:
                pass

    def _on_factory(self):
        if not messagebox.askyesno(
                "确认", f"把从站 #{self.slave_id} 恢复为出厂通信配置?\n\n"
                        f"    地址 {DEFAULT_SLAVE_ADDR}, "
                        f"{baud_text_of_index(DEFAULT_BAUD_INDEX)}bps\n\n"
                        f"此操作会同时保存到 EEPROM。", parent=self):
            return
        self.tip_var.set("正在下发恢复命令...")
        self.update_idletasks()
        _root = self.winfo_toplevel()
        try:
            _root._cfg_partial = False
        except Exception:
            pass
        ok = False
        try:
            ok = self.write_cb(self.slave_id, DEFAULT_SLAVE_ADDR,
                               DEFAULT_BAUD_INDEX, CFG_CMD_FACTORY)
        except Exception as e:
            self.tip_var.set(f"写入异常: {e}")
        if ok is not True and ok != "partial":
            if not self.tip_var.get().startswith("写入异常"):
                self.tip_var.set("恢复失败: 从站无应答")
            return
        if ok == "partial":
            self.tip_var.set("已下发 (命令应答异常), 尝试按默认参数重连...")
        else:
            self.tip_var.set("已下发, 等待从站切换...")
        self.update_idletasks()
        t0 = time.time()
        while time.time() - t0 < APPLY_WAIT_S:
            self.update()
            time.sleep(0.03)
        _set_status(self.app,
                    f"从站已恢复出厂: 地址 {DEFAULT_SLAVE_ADDR} / "
                    f"{baud_text_of_index(DEFAULT_BAUD_INDEX)}bps, "
                    f"正在重新扫描连接...")
        self.destroy()
        if self.on_applied:
            try:
                self.on_applied(DEFAULT_SLAVE_ADDR, DEFAULT_BAUD_INDEX)
            except Exception:
                pass
