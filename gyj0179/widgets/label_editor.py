# -*- coding: utf-8 -*-
"""
GYJ-0179 标签编辑窗口
用于编辑从站、继电器、光耦、寄存器的命名标签。
"""
import tkinter as tk
from tkinter import ttk, messagebox

from ..labels import LabelManager


class LabelEditWindow(tk.Toplevel):
    """标签编辑窗口 - 增加实时状态提示"""

    def __init__(self, parent, label_mgr, slave_ids, on_saved=None):
        super().__init__(parent)
        self.title("编辑标签")
        self.geometry("680x640")
        self.minsize(600, 540)
        self.label_mgr = label_mgr
        self.slave_ids = list(slave_ids) if slave_ids else [1]
        self.on_saved = on_saved
        self.buf = {}
        self._current_sid = None
        self._dirty = False   # 是否有未保存的修改

        self._build_ui()
        self._load_slave(0)

        # 拦截窗口关闭, 提示未保存
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 键鼠操作自动标记 dirty
        self.bind_all("<KeyRelease>", self._mark_dirty, add="+")

    # ---------- UI ----------
    def _build_ui(self):
        # ---- 顶部状态栏 ----
        self.status_bar = tk.Frame(self, bg="#f0f0f0", bd=1, relief="sunken")
        self.status_bar.pack(fill="x", side="top")

        self.status_var = tk.StringVar(value="就绪")
        self.status_lbl = tk.Label(
            self.status_bar, textvariable=self.status_var,
            bg="#f0f0f0", fg="gray",
            font=("Microsoft YaHei", 9),
            anchor="w", padx=8, pady=4)
        self.status_lbl.pack(side="left", fill="x", expand=True)

        # ---- 从站选择 ----
        top = ttk.LabelFrame(self, text="从站", padding=8)
        top.pack(fill="x", padx=8, pady=8)

        ttk.Label(top, text="选择从站:").pack(side="left")
        self.slave_var = tk.StringVar()
        self.slave_cb = ttk.Combobox(
            top, textvariable=self.slave_var,
            values=[f"#{sid}" for sid in self.slave_ids],
            width=10, state="readonly")
        self.slave_cb.pack(side="left", padx=(4, 20))
        self.slave_cb.bind("<<ComboboxSelected>>", self._on_slave_change)

        ttk.Label(top, text="从站名称:").pack(side="left")
        self.name_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.name_var, width=20
                  ).pack(side="left", padx=(4, 4))
        ttk.Label(top, text="(如: 1号工位)",
                  foreground="gray").pack(side="left")

        # ---- 选项卡 ----
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.com_tab = ttk.Frame(nb, padding=8)
        self.di_tab = ttk.Frame(nb, padding=8)
        self.reg_tab = ttk.Frame(nb, padding=8)

        nb.add(self.com_tab, text="继电器 (COM)")
        nb.add(self.di_tab, text="光耦 (IN)")
        nb.add(self.reg_tab, text="保持寄存器")

        # COM
        self.com_vars = {}
        for i in (1, 2, 3):
            row = ttk.Frame(self.com_tab)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=f"COM{i}", width=8).pack(side="left")
            ttk.Label(row, text="→", width=2).pack(side="left")
            var = tk.StringVar()
            ttk.Entry(row, textvariable=var,
                      width=30).pack(side="left", padx=4)
            ttk.Label(row, text="(如: 主控继电器)",
                      foreground="gray").pack(side="left")
            self.com_vars[i] = var

        # DI
        self.di_vars = {}
        for i in range(1, 7):
            row = ttk.Frame(self.di_tab)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=f"IN{i}", width=8).pack(side="left")
            ttk.Label(row, text="→", width=2).pack(side="left")
            var = tk.StringVar()
            ttk.Entry(row, textvariable=var,
                      width=30).pack(side="left", padx=4)
            ttk.Label(row, text="(如: 启动按钮)",
                      foreground="gray").pack(side="left")
            self.di_vars[i] = var   # 键 1..6 (IN1~IN6)

        # REG
        reg_canvas = tk.Canvas(self.reg_tab, highlightthickness=0)
        reg_sb = ttk.Scrollbar(self.reg_tab, orient="vertical",
                               command=reg_canvas.yview)
        reg_frame = ttk.Frame(reg_canvas)
        reg_frame.bind("<Configure>",
                       lambda e: reg_canvas.configure(
                           scrollregion=reg_canvas.bbox("all")))
        reg_canvas.create_window((0, 0), window=reg_frame, anchor="nw")
        reg_canvas.configure(yscrollcommand=reg_sb.set)
        reg_canvas.pack(side="left", fill="both", expand=True)
        reg_sb.pack(side="right", fill="y")

        self.reg_vars = {}
        for i in range(16):
            row = ttk.Frame(reg_frame)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=f"reg[{i:2d}]",
                      width=10).pack(side="left")
            ttk.Label(row, text="→", width=2).pack(side="left")
            var = tk.StringVar()
            ttk.Entry(row, textvariable=var,
                      width=30).pack(side="left", padx=4)
            if i == 15:
                ttk.Label(row, text="(保存命令寄存器)",
                          foreground="gray").pack(side="left")
            self.reg_vars[i] = var

        # ---- 底部按钮 ----
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=8, pady=(0, 8))

        # 保存按钮 (加粗+强调)
        self.btn_save = tk.Button(
            bottom, text="💾 保存所有",
            font=("Microsoft YaHei", 10, "bold"),
            bg="#4CAF50", fg="white",
            activebackground="#45a049",
            activeforeground="white",
            relief="raised", bd=2,
            padx=15, pady=4,
            command=self._save)
        self.btn_save.pack(side="right", padx=4)

        ttk.Button(bottom, text="取消",
                   command=self._on_close).pack(side="right", padx=4)

        # 左侧: 清空按钮
        self.btn_clear = tk.Button(
            bottom, text="🗑 清空本从站标签",
            font=("Microsoft YaHei", 9),
            bg="#f44336", fg="white",
            activebackground="#d32f2f",
            activeforeground="white",
            relief="raised", bd=2,
            padx=10, pady=2,
            command=self._clear_current)
        self.btn_clear.pack(side="left", padx=4)

        ttk.Button(bottom, text="全选本从站",
                   command=self._select_all_current
                   ).pack(side="left", padx=4)

    # ---------- 状态提示 ----------
    def _set_status(self, text, color="gray", bg="#f0f0f0"):
        self.status_var.set(text)
        self.status_lbl.configure(fg=color, bg=bg)
        self.status_bar.configure(bg=bg)

    def _mark_dirty(self, event=None):
        """标记有未保存的修改"""
        if not self._dirty:
            self._dirty = True
            self._set_status("⚠ 有未保存的修改, 请点击 [💾 保存所有]",
                             color="#cc6600", bg="#fff8e0")

    def _mark_clean(self):
        self._dirty = False
        self._set_status("✓ 已保存", color="green", bg="#e8f5e9")

    # ---------- 保存/加载 ----------
    def _save_current_to_buf(self):
        if self._current_sid is None:
            return
        sid = self._current_sid
        self.buf[sid] = {
            "name": self.name_var.get().strip(),
            "com": {i: v.get().strip() for i, v in self.com_vars.items()},
            "di": {i: v.get().strip() for i, v in self.di_vars.items()},
            "reg": {i: v.get().strip() for i, v in self.reg_vars.items()},
        }

    def _load_slave(self, idx):
        if idx < 0 or idx >= len(self.slave_ids):
            return
        self.slave_cb.current(idx)
        sid = self.slave_ids[idx]
        self._current_sid = sid

        if sid in self.buf:
            d = self.buf[sid]
            self.name_var.set(d["name"])
            for i in (1, 2, 3):
                self.com_vars[i].set(d["com"].get(i, ""))
            for i in range(1, 7):
                self.di_vars[i].set(d["di"].get(i, ""))
            for i in range(16):
                self.reg_vars[i].set(d["reg"].get(i, ""))
        else:
            self.name_var.set(self.label_mgr.get_slave_name(sid))
            for i in (1, 2, 3):
                self.com_vars[i].set(self.label_mgr.get_com_name(sid, i))
            for i in range(1, 7):
                self.di_vars[i].set(self.label_mgr.get_di_name(sid, i))
            for i in range(16):
                self.reg_vars[i].set(self.label_mgr.get_reg_name(sid, i))

    def _on_slave_change(self, event):
        self._save_current_to_buf()
        idx = self.slave_cb.current()
        self._load_slave(idx)

    # ---------- 全选 (方便一键清空) ----------
    def _select_all_current(self):
        """把当前从站所有输入框聚焦, 便于用户手动清空"""
        self._set_status(
            f"提示: 当前从站 #{self._current_sid}, 点左侧 [🗑 清空本从站标签] 可一键清空",
            color="#1976d2", bg="#e3f2fd")

    # ---------- 清空 (无弹窗, 直接清空) ----------
    def _clear_current(self):
        """清空当前从站的所有标签 - 无弹窗版本"""
        if self._current_sid is None:
            return
        sid = self._current_sid

        self.name_var.set("")
        for v in self.com_vars.values():
            v.set("")
        for v in self.di_vars.values():
            v.set("")
        for v in self.reg_vars.values():
            v.set("")

        # 立即写入 buf
        self._save_current_to_buf()

        # 标记 dirty, 提示用户保存
        self._mark_dirty()
        self._set_status(
            f"⚠ 已清空从站 #{sid} 的标签, 请点击 [💾 保存所有] 生效",
            color="#cc0000", bg="#ffe0e0")

    # ---------- 保存 ----------
    def _save(self):
        # 保存当前界面到 buf
        self._save_current_to_buf()

        # 写入 label_mgr
        for sid, d in self.buf.items():
            self.label_mgr.set_slave_name(sid, d["name"])
            for i in (1, 2, 3):
                self.label_mgr.set_com_name(sid, i, d["com"].get(i, ""))
            for i in range(1, 7):
                self.label_mgr.set_di_name(sid, i, d["di"].get(i, ""))
            for i in range(16):
                self.label_mgr.set_reg_name(sid, i, d["reg"].get(i, ""))

        # 写文件
        if self.label_mgr.save():
            self._mark_clean()
            messagebox.showinfo("成功", "标签已保存", parent=self)
            if self.on_saved:
                self.on_saved()
            self.destroy()
        else:
            messagebox.showerror("错误", "保存失败, 检查文件权限",
                                 parent=self)

    # ---------- 关闭 ----------
    def _on_close(self):
        """关闭时如果有未保存修改, 询问"""
        if self._dirty:
            ans = messagebox.askyesnocancel(
                "未保存的修改",
                "有未保存的修改, 是否保存?\n\n"
                "是 = 保存并关闭\n"
                "否 = 放弃修改并关闭\n"
                "取消 = 返回编辑",
                parent=self)
            if ans is None:
                return  # 取消, 不关闭
            elif ans is True:
                self._save()
                return
            # False: 放弃修改, 直接关闭

        self.destroy()
