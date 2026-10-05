# -*- coding: utf-8 -*-
"""
GYJ-0179 从站详情视图
显示单个从站的继电器、光耦、寄存器状态, 并提供控制按钮。
通过回调函数与主界面交互, 不直接持有设备对象。
"""
import json
import time
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

from ..labels import LabelManager


def _display_width(text):
    """计算字符串显示宽度: 中文/全角算 2, 其他算 1"""
    return sum(2 if ord(ch) > 127 else 1 for ch in text)


def _truncate_label(text, max_chars=8):
    """按显示宽度截断文本, 超出加省略号"""

    if _display_width(text) <= max_chars:
        return text
    result = []
    width = 0
    for ch in text:
        w = 2 if ord(ch) > 127 else 1
        if width + w > max_chars - 1:  # 预留 1 个字符给省略号
            result.append("…")
            break
        result.append(ch)
        width += w
    return "".join(result)


def _truncate_name(text, max_chars=8):
    """截断名称标签: 保留前缀开头 + 默认标签后缀 (如 '333… (COM1)').
    保证能看到 COM1/IN1/reg[0] 等默认标签, 同时保留部分自定义名称。
    """
    if _display_width(text) <= max_chars:
        return text
    # 查找默认标签后缀
    import re
    m = re.search(r"\s*\((COM\d+|IN\d+|reg\[\d+\])\)\s*$", text)
    if m:
        tail = m.group(0).strip()   # "(COM1)"
        head = text[:m.start()].strip()
    else:
        tail = ""
        head = text
    tail_w = _display_width(tail)
    # 后缀本身就超宽: 截断后缀
    if tail_w >= max_chars:
        return _truncate_label(tail, max_chars=max_chars)
    # 前缀空间: 总宽 - 默认标签宽 - 省略号(2) - 分隔空格(1)
    head_max = max_chars - tail_w - 3
    if head_max >= 1:
        head_trunc = _truncate_label(head, max_chars=head_max)
        return f"{head_trunc} {tail}"
    else:
        # 空间不足: 保留前缀首字符 + 省略号 + 默认标签
        first = head[:1] if head else ""
        return f"{first}… {tail}"


class ToolTip:
    """tooltip: 鼠标悬停延迟显示完整文本, 移开延迟隐藏(防闪烁)."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self._tip = None
        self._show_job = None
        self._hide_job = None
        widget.bind("<Enter>", self._schedule_show, add="+")
        widget.bind("<Leave>", self._schedule_hide, add="+")
        widget.bind("<ButtonPress>", self._schedule_hide, add="+")

    def _schedule_show(self, event):
        self._cancel_hide()
        if self._show_job is None:
            self._show_job = self.widget.after(300, self._show)

    def _schedule_hide(self, event):
        if self._show_job:
            self.widget.after_cancel(self._show_job)
            self._show_job = None
        if self._tip:
            self._cancel_hide()
            self._hide_job = self.widget.after(200, self._hide)

    def _cancel_hide(self):
        if self._hide_job:
            self.widget.after_cancel(self._hide_job)
            self._hide_job = None

    def _show(self):
        self._show_job = None
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + 25
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(self._tip, text=self.text, justify="left",
                         bg="#ffffe0", relief="solid", borderwidth=1,
                         font=("Microsoft YaHei", 9))
        label.pack()

    def _hide(self):
        self._hide_job = None
        if self._tip:
            self._tip.destroy()
            self._tip = None


class SlaveDetailView(ttk.Frame):
    """单个从站的状态详情视图。"""

    PANEL_STATE_PATH = "panel_state.json"

    def __init__(self, parent, log_cb,
                 relay_cb, toggle_cb, write_param_cb,
                 label_mgr=None):
        super().__init__(parent)
        self.log_cb = log_cb
        self.relay_cb = relay_cb
        self.toggle_cb = toggle_cb
        self.write_param_cb = write_param_cb
        self.label_mgr = label_mgr
        self.sid = None
        # 面板显隐状态: relay / di / param, 每个含 show(面板整体) 和 items(单项)
        self.panel_state = self._default_panel_state()
        self._load_panel_state()
        self._build_ui()
        self._apply_panel_visibility()

    # ---------- 面板状态持久化 ----------

    @staticmethod
    def _default_panel_state():
        """默认面板状态: 全部显示"""
        return {
            "relay": {"show": True,
                      "items": {str(i): True for i in (1, 2, 3)}},
            "di": {"show": True,
                    "items": {str(i): True for i in range(6)}},
            "param": {"show": True,
                      "items": {str(i): True for i in range(16)}},
        }

    def _load_panel_state(self):
        """从文件加载面板显隐状态 (兼容旧格式)"""
        try:
            path = Path(self.PANEL_STATE_PATH)
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    for name in self.panel_state:
                        if name not in d:
                            continue
                        val = d[name]
                        if isinstance(val, bool):
                            # 旧格式: 只有整体显隐
                            self.panel_state[name]["show"] = val
                        elif isinstance(val, dict):
                            if "show" in val:
                                self.panel_state[name]["show"] = bool(val["show"])
                            if isinstance(val.get("items"), dict):
                                for k in self.panel_state[name]["items"]:
                                    if k in val["items"]:
                                        self.panel_state[name]["items"][k] = bool(val["items"][k])
        except Exception:
            pass

    def _save_panel_state(self):
        """保存面板显隐状态"""
        try:
            with open(self.PANEL_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.panel_state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 面板/条目显隐控制 ----------

    def _set_panel_visible(self, name, visible):
        """显示/隐藏指定面板整体"""
        frame = getattr(self, f"{name}_frame", None)
        if frame is None:
            return
        if visible:
            frame.pack(side=frame._pack_side, fill=frame._pack_fill,
                       expand=frame._pack_expand, pady=frame._pack_pady)
        else:
            frame.pack_forget()

    def _effective_item_visible(self, name, idx):
        """条目是否应显示: 优先取设置窗口里尚未应用的勾选状态"""
        item_vars = getattr(self, "_item_vars", None) or {}
        var = item_vars.get(name, {}).get(str(idx))
        if var is not None:
            try:
                return bool(var.get())
            except tk.TclError:
                pass
        return bool(self.panel_state.get(name, {}).get("items", {}).get(str(idx), True))

    def _set_item_visible(self, name, idx, visible):
        """显示/隐藏面板内的单个条目"""
        if name == "relay":
            rows = getattr(self, "relay_rows", {})
            row = rows.get(idx)
        elif name == "di":
            cells = getattr(self, "di_cells", {})
            row = cells.get(idx)
        elif name == "param":
            rows = getattr(self, "param_rows", [])
            row = rows[idx] if 0 <= idx < len(rows) else None
        else:
            row = None
        if row is None:
            return
        if visible:
            if name == "param":
                # 寄存器用 grid 排布 (宽屏可多列), 具体位置由 _apply_param_cols 排
                row.grid(row=row._grid_row, column=row._grid_col,
                         sticky="w", padx=(0, 14), pady=2)
            elif name == "di":
                # 光耦用 grid 布局, 具体位置由 _rebuild_order 统一排
                row.grid(row=row._grid_row, column=row._grid_col,
                         sticky="w", padx=5, pady=3)
            else:
                row.pack(fill="x", pady=2)
        else:
            if name in ("di", "param"):
                # 用 grid_remove 保留行列信息, 恢复时先原样回来再重排
                row.grid_remove()
            else:
                row.pack_forget()

    def _on_param_canvas_config(self, event):
        """寄存器区宽度变化: 重新计算可用列数 (空闲时执行, 避免抖动)"""
        if getattr(self, "_param_layout_pending", False):
            return
        self._param_layout_pending = True
        self.after_idle(self._layout_param_later)

    def _layout_param_later(self):
        self._param_layout_pending = False
        self._apply_param_cols()

    def _apply_param_cols(self):
        """按可用宽度把寄存器条目排成 1~3 列: 宽屏并排, 窄屏单列滚动"""
        canvas = getattr(self, "param_canvas", None)
        rows = getattr(self, "param_rows", [])
        if canvas is None or not rows:
            return
        avail = canvas.winfo_width()
        if avail <= 1:
            return
        # 单列所需宽度 = 最宽的一个寄存器条目 + 列间留白
        unit = max(r.winfo_reqwidth() for r in rows) + 12
        cols = max(1, min(3, int(avail // unit)))
        if cols != self._param_cols:
            self._param_cols = cols
            # 按横向阅读顺序铺开: 0 1 / 2 3 / … 而不是竖着填满一列
            for i, r in enumerate(rows):
                r._grid_row = i // cols
                r._grid_col = i % cols
            # 各列等宽拉伸: 单列时条目铺满整行, 多列时列宽一致 (消除右侧空闲)
            frame = rows[0].master
            for c in range(cols):
                frame.columnconfigure(c, weight=1, uniform="param_col")
            for c in range(cols, 3):
                frame.columnconfigure(c, weight=0, uniform="")
        # 按当前显隐重排 (宽度可能变了但列数没变, 也要对齐 grid)
        for i, r in enumerate(rows):
            if self._effective_item_visible("param", i):
                r.grid(row=r._grid_row, column=r._grid_col,
                       sticky="w", padx=(0, 10), pady=2)
            else:
                r.grid_remove()

    def _rebuild_order(self, name):
        """按 1 2 3 (继电器) / 0 1 2 … (寄存器) 的默认顺序重排条目。

        显隐一律以逻辑状态 (勾选框 / 已保存状态) 为准, 不用 winfo_ismapped:
        条目滚出可视区时 ismapped 为 0, 据此判断会把它们当成隐藏项丢掉,
        导致勾选后条目跑到最后甚至消失。
        """
        if name == "relay":
            keys = [1, 2, 3]
            rows = getattr(self, "relay_rows", {})
            pady = 2
        elif name == "param":
            # 寄存器用 grid 多列排布, 由 _apply_param_cols 统一处理
            self._apply_param_cols()
            return
        elif name == "di":
            # 光耦: 按顺序重新 grid 到连续位置, 实现左对齐
            visible = [self.di_cells[i] for i in range(6)
                       if self._effective_item_visible("di", i)]
            for cell in self.di_cells.values():
                cell.grid_forget()
            for k, cell in enumerate(visible):
                cell.grid(row=k // 2, column=k % 2,
                          sticky="w", padx=5, pady=3)
            return
        else:
            return

        # 先全部解除, 再按顺序重新 pack, 保证最后勾选的不会跑到最前/最后
        for idx in keys:
            row = rows.get(idx)
            if row is not None:
                row.pack_forget()
        for idx in keys:
            row = rows.get(idx)
            if row is not None and self._effective_item_visible(name, idx):
                row.pack(fill="x", pady=pady)

    def _apply_panel_visibility(self):
        """按当前面板状态应用显隐 (整体 + 单项)"""
        for name, state in self.panel_state.items():
            self._set_panel_visible(name, state["show"])
            if state["show"]:
                for idx, visible in state["items"].items():
                    self._set_item_visible(name, int(idx), visible)

    def _build_ui(self):
        status_bar = ttk.Frame(self)
        status_bar.pack(fill="x", pady=(0, 3))

        self.title_var = tk.StringVar(value="无从站")
        ttk.Label(status_bar, textvariable=self.title_var,
                  font=("Microsoft YaHei", 11, "bold")
                  ).pack(side="left")

        self.state_var = tk.StringVar(value="")
        self.state_lbl = ttk.Label(status_bar, textvariable=self.state_var,
                                   font=("Microsoft YaHei", 10, "bold"))
        self.state_lbl.pack(side="left", padx=15)

        # 面板设置按钮
        self.btn_panel_settings = ttk.Button(
            status_bar, text="⚙ 面板设置",
            command=self._open_panel_settings)
        self.btn_panel_settings.pack(side="right")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=False, padx=(0, 5))

        self.relay_frame = self._build_relay_panel(left)
        self.di_frame = self._build_input_panel(left)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        self.param_frame = self._build_param_panel(right)

    # ---------- 面板设置窗口 ----------

    def _open_panel_settings(self):
        """弹出面板设置窗口: 控制每个面板及每个条目的显隐"""
        win = tk.Toplevel(self)
        win.title("面板设置")
        win.transient(self.winfo_toplevel())
        win.grab_set()
        # 先挪到屏幕外, 量完真实所需尺寸再定位显示 (避免尺寸跳变)
        win.geometry("+6000+6000")
        win.resizable(False, False)

        # ---- 底部固定按钮区 (先 pack, 固定在窗口底部) ----
        bottom_bar = ttk.Frame(win)
        bottom_bar.pack(side="bottom", fill="x", padx=12, pady=(4, 10))

        ttk.Button(bottom_bar, text="全部显示", width=8,
                   command=lambda: self._set_all_panels(True)
                   ).pack(side="left", padx=2)
        ttk.Button(bottom_bar, text="全部隐藏", width=8,
                   command=lambda: self._set_all_panels(False)
                   ).pack(side="left", padx=2)

        # 取消 / 关闭: 丢弃本次勾选, 恢复成已保存的显隐状态
        def _cancel():
            win.destroy()
            self._item_vars = None     # 作废未应用的勾选状态
            self._panel_vars = None
            self._apply_panel_visibility()
            self._rebuild_order("param")

        ttk.Button(bottom_bar, text="应用并保存",
                   command=lambda: self._apply_panel_settings(win)
                   ).pack(side="right", padx=2)
        ttk.Button(bottom_bar, text="取消", command=_cancel
                   ).pack(side="right", padx=2)
        win.protocol("WM_DELETE_WINDOW", _cancel)

        # ---- 中部可滚动区域 (占据剩余空间) ----
        # 滚动条先 pack 到最右, 再放画布, 保证滚动条贴窗口右缘
        canvas = tk.Canvas(win, highlightthickness=0, width=300)
        sb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        inner_win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _fit_inner(event):
            """手动拉宽窗口时让条目区跟随变宽 (不窄于内容需求, 避免压缩条目)"""
            canvas.itemconfigure(
                inner_win, width=max(event.width, inner.winfo_reqwidth()))

        canvas.bind("<Configure>", _fit_inner)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 8), pady=8)
        canvas.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)

        # 当前从站 (用于自定义标签)
        sid = self.sid
        lm = self.label_mgr

        def _label(fn, *args, **kwargs):
            """优先用自定义标签, 否则用默认标签 (kwargs 透传, 如 special=True)"""
            if sid is not None and lm is not None:
                try:
                    return fn(sid, *args, **kwargs)
                except Exception:
                    pass
            return None

        # 面板设置里的寄存器条目名: reg[13]/reg[14] 用固定名称, 便于识别
        _reg_items = []
        for i in range(16):
            if i in (13, 14):
                lab = _label(lm.display_reg, i, special=True)
            else:
                lab = _label(lm.display_reg, i)
            _reg_items.append((i, lab or f"reg[{i}]"))

        # 面板名 -> (中文名, [(索引, 显示标签), ...], 每行列数)
        # 统一 3 列: 各面板列宽一致, 复选框之间留出间隔, 不会挤在一起
        panels = [
            ("relay", "继电器 (COM)",
             [(i, _label(lm.display_com, i) or f"COM{i}") for i in (1, 2, 3)],
             3),
            ("di", "光耦 (IN)",
             [(i, _label(lm.display_di, i + 1) or f"IN{i+1}")
              for i in range(6)],
             3),
            ("param", "保持寄存器", _reg_items, 3),
        ]

        self._panel_vars = {}
        self._item_vars = {}

        for name, label, items, cols in panels:
            lf = ttk.LabelFrame(inner, text=label, padding=6)
            lf.pack(fill="x", pady=3)

            # 各列等宽拉伸, 复选框之间拉开距离, 不再挤成一团
            for c in range(cols):
                lf.columnconfigure(c, weight=1, uniform=f"{name}_col")

            # 面板整体显示开关
            show_var = tk.BooleanVar(value=self.panel_state[name]["show"])
            self._panel_vars[name] = show_var
            ttk.Checkbutton(
                lf, text="显示此面板", variable=show_var,
                command=lambda n=name: self._on_panel_check(n)
            ).grid(row=0, column=0, columnspan=cols, sticky="w",
                   padx=4, pady=(0, 4))

            # 每个条目的多选框 (带自定义标签, 过长截断 + tooltip 显示完整)
            item_vars = {}
            for k, (idx, item_label) in enumerate(items):
                var = tk.BooleanVar(
                    value=self.panel_state[name]["items"].get(str(idx), True))
                item_vars[str(idx)] = var
                # 半角宽度约 26 个字符, 常见"名称 (reg[15])"能完整显示
                display = _truncate_label(item_label, max_chars=26)
                cb = ttk.Checkbutton(
                    lf, text=display, variable=var,
                    command=lambda n=name, i=str(idx): self._on_item_check(n, i))
                cb.grid(row=k // cols + 1, column=k % cols,
                        sticky="w", padx=(4, 14), pady=3)
                # 若被截断, 鼠标悬停显示完整标签
                if display != item_label:
                    ToolTip(cb, item_label)
            self._item_vars[name] = item_vars

        # ---- 按内容实际所需宽度定尺寸 (并限制在屏幕内), 再移到屏幕中央 ----
        win.update_idletasks()
        sb_w = sb.winfo_reqwidth()
        need_w = inner.winfo_reqwidth() + sb_w + 8 + 8 + 4
        need_h = inner.winfo_reqheight() + bottom_bar.winfo_reqheight() + 30
        max_w = int(win.winfo_screenwidth() * 0.92)
        max_h = int(win.winfo_screenheight() * 0.9)
        win_w = max(460, min(need_w, max_w))
        win_h = max(380, min(need_h, max_h))
        canvas.configure(width=max(300, win_w - sb_w - 20),
                         height=max(200, win_h - bottom_bar.winfo_reqheight() - 24))
        win.update_idletasks()
        # 只用画布宽度修正一次, 避免"窗口变宽→内容变宽"反复增长
        win_w = min(max(win_w, canvas.winfo_reqwidth() + sb_w + 20), max_w)
        x = max(0, (win.winfo_screenwidth() - win_w) // 2)
        y = max(0, (win.winfo_screenheight() - win_h) // 3)
        win.geometry(f"{win_w}x{win_h}+{x}+{y}")
        win.resizable(True, True)
        win.minsize(420, 360)

    def _on_panel_check(self, name):
        """面板整体勾选变化: 实时预览显隐 (不保存)"""
        visible = self._panel_vars[name].get()
        self._set_panel_visible(name, visible)

    def _on_item_check(self, name, idx):
        """条目勾选变化: 实时预览显隐 (不保存), 按 1 2 3 的默认顺序重排"""
        visible = self._item_vars[name][idx].get()
        self._set_item_visible(name, int(idx), visible)
        # 只显示/隐藏还不够, 重新按默认顺序 pack, 否则新勾选的条目会排在最后
        self._rebuild_order(name)

    def _set_all_panels(self, value):
        """全部显示/隐藏所有面板及其条目"""
        for name, panel_var in self._panel_vars.items():
            panel_var.set(value)
            self._set_panel_visible(name, value)
            for idx, item_var in self._item_vars[name].items():
                item_var.set(value)
                self._set_item_visible(name, int(idx), value)
            # 全部显示后按原始顺序重排, 避免顺序错乱
            if value:
                self._rebuild_order(name)

    def _apply_panel_settings(self, win):
        """应用面板设置并保存"""
        for name in self.panel_state:
            show = self._panel_vars[name].get()
            self.panel_state[name]["show"] = show
            self._set_panel_visible(name, show)
            for idx, item_var in self._item_vars[name].items():
                self.panel_state[name]["items"][idx] = item_var.get()
                self._set_item_visible(name, int(idx), item_var.get())
            # 应用后按原始顺序重排, 保持 1 2 3 / 0 1 2 … 的默认顺序
            self._rebuild_order(name)
        self._save_panel_state()
        win.destroy()

    def _build_relay_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="继电器 (COM)", padding=8)
        frame.pack(fill="x", pady=(0, 5))
        frame._pack_side = "top"
        frame._pack_fill = "x"
        frame._pack_expand = False
        frame._pack_pady = (0, 5)

        self.relay_inds = {}
        self.relay_name_vars = {}
        self.relay_rows = {}
        self.relay_name_labels = {}
        for i in (1, 2, 3):
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=2)
            row._pack_side = "top"
            row._pack_fill = "x"
            row._pack_pady = 2
            self.relay_rows[i] = row
            name_var = tk.StringVar(value=f"COM{i}")
            name_lbl = ttk.Label(row, textvariable=name_var,
                                 width=22, anchor="w")
            name_lbl.pack(side="left")
            self.relay_name_vars[i] = name_var
            self.relay_name_labels[i] = name_lbl

            ind = tk.Label(row, text="  ", bg="gray",
                           width=3, relief="sunken")
            ind.pack(side="left", padx=(2, 8))
            self.relay_inds[i] = ind

            ttk.Button(row, text="吸合", width=5,
                       command=lambda i=i: self._relay(i, True)
                       ).pack(side="left", padx=1)
            ttk.Button(row, text="释放", width=5,
                       command=lambda i=i: self._relay(i, False)
                       ).pack(side="left", padx=1)
            ttk.Button(row, text="翻转", width=5,
                       command=lambda i=i: self._toggle(i)
                       ).pack(side="left", padx=1)
        return frame

    def _build_input_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="光耦 (IN)", padding=8)
        frame.pack(fill="x")
        frame._pack_side = "top"
        frame._pack_fill = "x"
        frame._pack_expand = False
        frame._pack_pady = (0, 0)

        grid = ttk.Frame(frame)
        grid.pack(fill="x")

        self.di_inds = {}
        self.di_name_vars = {}
        self.di_name_labels = {}
        self.di_cells = {}
        for i in range(6):
            col = i % 2
            row = i // 2
            cell = ttk.Frame(grid)
            cell.grid(row=row, column=col, sticky="w", padx=5, pady=3)
            cell._grid_row = row
            cell._grid_col = col
            self.di_cells[i] = cell

            name_var = tk.StringVar(value=f"IN{i+1}")
            name_lbl = ttk.Label(cell, textvariable=name_var,
                                 width=22, anchor="w")
            name_lbl.pack(side="left")
            self.di_name_vars[i] = name_var
            self.di_name_labels[i] = name_lbl
            self.di_name_vars[i] = name_var

            ind = tk.Label(cell, text="  ", bg="gray",
                           width=3, relief="sunken")
            ind.pack(side="left", padx=(2, 4))
            lbl = ttk.Label(cell, text="无信号", width=7)
            lbl.pack(side="left")
            self.di_inds[i] = (ind, lbl)

        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="打包值:").pack(side="left")
        self.di_value_var = tk.StringVar(value="0x00")
        ttk.Label(row, textvariable=self.di_value_var,
                  font=("Consolas", 11, "bold")
                  ).pack(side="left", padx=5)
        return frame

    def _build_param_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="保持寄存器", padding=8)
        frame.pack(fill="both", expand=True)
        frame._pack_side = "left"
        frame._pack_fill = "both"
        frame._pack_expand = True
        frame._pack_pady = (0, 0)

        header = ttk.Frame(frame)
        header.pack(fill="x")
        ttk.Label(header, text="名称 (地址)", width=16, anchor="w"
                  ).pack(side="left")
        ttk.Label(header, text="HEX", width=8, anchor="w").pack(side="left")
        ttk.Label(header, text="DEC", width=8, anchor="w").pack(side="left")
        ttk.Label(header, text="操作", anchor="w").pack(side="left")

        canvas = tk.Canvas(frame, height=160, highlightthickness=0)
        sb = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)

        scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)

        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.param_canvas = canvas

        canvas.bind_all(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(
                int(-1 * (e.delta / 120)), "units"))

        self.param_hex_vars = []
        self.param_dec_vars = []
        self.param_entries = []
        self.param_name_vars = []
        self.param_rows = []
        self.param_name_labels = []
        # 宽屏自适应列数: 窗口变宽时寄存器自动并排成多列, 充分利用空白区域
        self._param_cols = 1
        self._param_layout_pending = False
        canvas.bind("<Configure>", self._on_param_canvas_config)

        for i in range(16):
            row = ttk.Frame(scroll_frame)
            row._grid_row = i          # 供多列排布使用
            row._grid_col = 0
            row.grid(row=i, column=0, sticky="w", padx=(0, 14), pady=2)
            self.param_rows.append(row)

            name_var = tk.StringVar(value=f"reg[{i}]")
            name_lbl = ttk.Label(row, textvariable=name_var,
                                 width=12, anchor="w",
                                 font=("Consolas", 9))
            name_lbl.pack(side="left")
            self.param_name_vars.append(name_var)
            self.param_name_labels.append(name_lbl)

            hex_var = tk.StringVar(value="0x0000")
            ttk.Label(row, textvariable=hex_var, width=8, anchor="w",
                      font=("Consolas", 9)).pack(side="left")

            dec_var = tk.StringVar(value="0")
            ttk.Label(row, textvariable=dec_var, width=8, anchor="w"
                      ).pack(side="left")

            entry = ttk.Entry(row, width=8)
            entry.pack(side="left", padx=2)

            ttk.Button(row, text="写", width=4,
                       command=lambda i=i: self._write_param(i, save=False)
                       ).pack(side="left", padx=1)

            if i < 15:
                ttk.Button(row, text="写存", width=5,
                           command=lambda i=i: self._write_param(i, save=True)
                           ).pack(side="left", padx=1)

            self.param_hex_vars.append(hex_var)
            self.param_dec_vars.append(dec_var)
            self.param_entries.append(entry)

        bottom = ttk.Frame(frame)
        bottom.pack(fill="x", pady=(6, 0))
        ttk.Button(bottom, text="保存到 EEPROM",
                   command=self._save_eeprom
                   ).pack(side="left", padx=2)
        ttk.Button(bottom, text="全部清零",
                   command=self._clear_all
                   ).pack(side="left", padx=2)
        return frame

    def _set_name_label(self, var, label, display_text, tip_text=None):
        """设置名称标签: 过长截断显示, tooltip 显示实际标签名称.
        - display_text: 用于截断显示的文本 (如 '名称 (COM1)')
        - tip_text: tooltip 显示的完整实际名称 (默认用 display_text)
        """
        if tip_text is None:
            tip_text = display_text
        display = _truncate_name(display_text, max_chars=20)
        var.set(display)
        # 仅当被截断时绑定 tooltip, 且避免重复绑定
        if display != tip_text and label is not None:
            if not getattr(self, "_name_tip_bound", None):
                self._name_tip_bound = set()
            if id(label) not in self._name_tip_bound:
                ToolTip(label, tip_text)
                self._name_tip_bound.add(id(label))

    def _apply_labels(self, sid):
        if self.label_mgr is None:
            return
        for i in (1, 2, 3):
            # 显示用: 带默认标签截断; tooltip 用: 完整名称 + 默认标签
            display = self.label_mgr.display_com(sid, i)
            self._set_name_label(self.relay_name_vars[i],
                                 self.relay_name_labels.get(i), display)
        for i in range(6):
            display = self.label_mgr.display_di(sid, i + 1)
            self._set_name_label(self.di_name_vars[i],
                                 self.di_name_labels.get(i), display)
        for i in range(16):
            # special=True: reg[13]/reg[14] 未自定义命名时显示"从站地址/波特率索引"
            display = self.label_mgr.display_reg(sid, i, special=True)
            self._set_name_label(self.param_name_vars[i],
                                 self.param_name_labels[i], display)

    def show_slave(self, sid, data):
        self.sid = sid
        if self.label_mgr:
            self.title_var.set(self.label_mgr.display_slave(sid))
        else:
            self.title_var.set(f"从站 #{sid}")
        self._apply_labels(sid)
        for e in self.param_entries:
            e.delete(0, "end")
        self.update_data(data)

    def refresh_labels(self):
        if self.sid is not None:
            if self.label_mgr:
                self.title_var.set(self.label_mgr.display_slave(self.sid))
            self._apply_labels(self.sid)

    def clear(self):
        self.sid = None
        self.title_var.set("无从站")
        self.state_var.set("")
        self.state_lbl.configure(foreground="black")
        for i in (1, 2, 3):
            self.relay_inds[i].configure(bg="gray")
            self.relay_name_vars[i].set(f"COM{i}")
        for i, (ind, lbl) in enumerate(self.di_inds.values()):
            ind.configure(bg="gray")
            lbl.configure(text="无信号", foreground="black")
            self.di_name_vars[i].set(f"IN{i+1}")
        for i in range(16):
            self.param_name_vars[i].set(f"reg[{i}]")
            self.param_hex_vars[i].set("0x0000")
            self.param_dec_vars[i].set("0")
        for e in self.param_entries:
            e.delete(0, "end")
        self.di_value_var.set("0x00")

    def update_data(self, data):
        if data is None:
            return
        online = data.get('online', False)
        if online:
            last = data.get('last_seen', 0)
            ago = time.time() - last if last > 0 else 0
            self.state_var.set(f"● 在线  (最后响应 {ago:.1f}s 前)")
            self.state_lbl.configure(foreground="green")
        else:
            self.state_var.set("● 离线")
            self.state_lbl.configure(foreground="red")

        if not online:
            for i in (1, 2, 3):
                self.relay_inds[i].configure(bg="lightgray")
            for ind, lbl in self.di_inds.values():
                ind.configure(bg="lightgray")
                lbl.configure(text="--", foreground="gray")
            self.di_value_var.set("--")
            for i in range(16):
                self.param_hex_vars[i].set("--")
                self.param_dec_vars[i].set("--")
            return

        for i, c in enumerate(data.get('relays', [False]*3), 1):
            self.relay_inds[i].configure(bg="lime" if c else "gray")

        for i, b in enumerate(data.get('di', [False]*6)):
            ind, lbl = self.di_inds[i]
            ind.configure(bg="lime" if b else "gray")
            lbl.configure(text="有信号" if b else "无信号",
                          foreground="green" if b else "black")

        self.di_value_var.set(f"0x{data.get('di_value', 0):02X}")

        params = data.get('params', [0]*16)
        for i, val in enumerate(params):
            if i < len(self.param_hex_vars):
                self.param_hex_vars[i].set(f"0x{val:04X}")
                self.param_dec_vars[i].set(str(val))

    def _relay(self, idx, on):
        if self.sid is None:
            return
        ok = self.relay_cb(self.sid, idx, on)
        self.log_cb("RELAY", f"COM{idx} "
                    f"{'吸合' if on else '释放'}: "
                    f"{'成功' if ok else '失败'}", self.sid)

    def _toggle(self, idx):
        if self.sid is None:
            return
        ok = self.toggle_cb(self.sid, idx)
        self.log_cb("RELAY", f"COM{idx} 翻转: "
                    f"{'成功' if ok else '失败'}", self.sid)

    def _write_param(self, idx, save=False):
        if self.sid is None:
            messagebox.showwarning("提示", "没有选中的从站")
            return
        s = self.param_entries[idx].get().strip()
        if not s:
            messagebox.showwarning("提示", "请输入要写入的值")
            return
        try:
            if s.lower().startswith("0x"):
                val = int(s, 16)
            else:
                val = int(s, 10)
            if not 0 <= val <= 0xFFFF:
                raise ValueError("值必须在 0~65535")
        except Exception as e:
            messagebox.showerror("错误", f"输入不合法: {e}")
            return

        ok = self.write_param_cb(self.sid, idx, val, save)
        self.log_cb("PARAM",
                    f"写参数[{idx}] = {val}"
                    f"{' (已保存)' if ok and save else ''}: "
                    f"{'成功' if ok else '失败'}", self.sid)
        if ok:
            self.param_entries[idx].delete(0, "end")

    def _save_eeprom(self):
        if self.sid is None:
            return
        if not messagebox.askyesno("确认",
                "将所有参数 (0~14) 保存到 EEPROM?\n\n"
                "注意: EEPROM 寿命约 10 万次, 不要频繁保存。"):
            return
        ok = self.write_param_cb(self.sid, 0x0F, 0x5AA5, False)
        self.log_cb("PARAM", "EEPROM 保存: "
                    f"{'成功' if ok else '失败'}", self.sid)

    def _clear_all(self):
        if self.sid is None:
            return
        if not messagebox.askyesno("确认", "将所有参数清零?"):
            return
        ok = self.write_param_cb(self.sid, -1, 0, False)
        self.log_cb("PARAM", "全部清零: "
                    f"{'成功' if ok else '失败'}", self.sid)
