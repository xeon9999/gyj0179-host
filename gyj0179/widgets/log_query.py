# -*- coding: utf-8 -*-
"""
GYJ-0179 日志查询窗口
按日期/级别/类别/从站/关键字筛选日志, 支持导出 CSV、删除日期日志。
"""
import csv
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime

from ..logs import LogManager
from ..labels import LabelManager


class LogQueryWindow(tk.Toplevel):
    """日志查询窗口, 支持多条件筛选与 CSV 导出。"""

    def __init__(self, parent, log_mgr, label_mgr=None):
        super().__init__(parent)
        self.title("日志查询")
        self.geometry("1050x680")
        self.minsize(900, 560)
        self.log_mgr = log_mgr
        self.label_mgr = label_mgr
        self._build_ui()
        self._refresh_dates()
        self._refresh_slaves()
        self._do_query()

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=8)

        left = ttk.LabelFrame(top, text="筛选条件", padding=8)
        left.pack(side="left", fill="both", expand=True)

        row1 = ttk.Frame(left)
        row1.pack(fill="x", pady=(0, 4))

        ttk.Label(row1, text="日期:").pack(side="left")
        self.date_var = tk.StringVar(value="全部")
        self.date_cb = ttk.Combobox(row1, textvariable=self.date_var,
                                    width=14, state="readonly")
        self.date_cb.pack(side="left", padx=(2, 12))

        ttk.Label(row1, text="级别:").pack(side="left")
        self.level_var = tk.StringVar(value="全部")
        ttk.Combobox(row1, textvariable=self.level_var,
                     values=("全部",) + LogManager.LEVELS,
                     width=10, state="readonly"
                     ).pack(side="left", padx=(2, 12))

        ttk.Label(row1, text="类别:").pack(side="left")
        self.cat_var = tk.StringVar(value="全部")
        ttk.Combobox(row1, textvariable=self.cat_var,
                     values=("全部",) + LogManager.CATEGORIES,
                     width=10, state="readonly"
                     ).pack(side="left", padx=(2, 12))

        ttk.Label(row1, text="最多条数:").pack(side="left")
        self.limit_var = tk.IntVar(value=2000)
        ttk.Spinbox(row1, from_=100, to=100000, increment=100,
                    width=8, textvariable=self.limit_var
                    ).pack(side="left", padx=(2, 0))

        row2 = ttk.Frame(left)
        row2.pack(fill="x", pady=(4, 0))

        ttk.Label(row2, text="关键字:").pack(side="left")
        self.kw_var = tk.StringVar(value="")
        ttk.Entry(row2, textvariable=self.kw_var, width=50
                  ).pack(side="left", padx=(2, 0))

        right = ttk.LabelFrame(top, text="从站 (Ctrl/Shift 多选)",
                               padding=8)
        right.pack(side="left", fill="both", padx=(8, 0))

        list_frame = ttk.Frame(right)
        list_frame.pack(fill="both", expand=True)

        self.slave_listbox = tk.Listbox(
            list_frame, selectmode="extended", height=5, width=20,
            exportselection=False, font=("Consolas", 10))
        sb = ttk.Scrollbar(list_frame, orient="vertical",
                           command=self.slave_listbox.yview)
        self.slave_listbox.configure(yscrollcommand=sb.set)
        self.slave_listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        btn_row = ttk.Frame(right)
        btn_row.pack(fill="x", pady=(4, 0))
        ttk.Button(btn_row, text="全选",
                   command=self._select_all_slaves,
                   width=6).pack(side="left", padx=1)
        ttk.Button(btn_row, text="全不选",
                   command=self._deselect_all_slaves,
                   width=6).pack(side="left", padx=1)
        ttk.Button(btn_row, text="反选",
                   command=self._invert_slaves,
                   width=6).pack(side="left", padx=1)
        ttk.Button(btn_row, text="🔄",
                   command=self._refresh_slaves,
                   width=3).pack(side="left", padx=1)

        self.slave_info_var = tk.StringVar(value="未选择=全部")
        ttk.Label(right, textvariable=self.slave_info_var,
                  foreground="gray").pack(anchor="w", pady=(2, 0))
        self.slave_listbox.bind("<<ListboxSelect>>",
                                self._on_slave_select_changed)

        ops = ttk.Frame(self)
        ops.pack(fill="x", padx=8)

        ttk.Button(ops, text="🔍 查询",
                   command=self._do_query).pack(side="left", padx=2)
        ttk.Button(ops, text="🔄 刷新日期/从站",
                   command=self._refresh_all
                   ).pack(side="left", padx=2)
        ttk.Button(ops, text="📥 导出 CSV",
                   command=self._export_csv).pack(side="left", padx=2)
        ttk.Button(ops, text="🗑 删除当前日期",
                   command=self._delete_current_date
                   ).pack(side="right", padx=2)

        res = ttk.LabelFrame(self, text="查询结果", padding=5)
        res.pack(fill="both", expand=True, padx=8, pady=(6, 8))

        self.info_var = tk.StringVar(value="")
        ttk.Label(res, textvariable=self.info_var,
                  foreground="blue").pack(anchor="w", pady=(0, 3))

        cols = ("ts", "level", "category", "sid", "msg")
        headers = ("时间", "级别", "类别", "从站", "消息")

        tv_frame = ttk.Frame(res)
        tv_frame.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(tv_frame, columns=cols,
                                 show="headings", height=15)
        for c, h in zip(cols, headers):
            self.tree.heading(c, text=h)
        self.tree.column("ts", width=170, anchor="w")
        self.tree.column("level", width=60, anchor="center")
        self.tree.column("category", width=70, anchor="center")
        self.tree.column("sid", width=100, anchor="w")
        self.tree.column("msg", width=550, anchor="w")

        self.tree.tag_configure("ERROR", foreground="#cc0000")
        self.tree.tag_configure("WARN", foreground="#cc6600")
        self.tree.tag_configure("INFO", foreground="#000000")
        self.tree.tag_configure("DEBUG", foreground="#888888")

        vsb = ttk.Scrollbar(tv_frame, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._on_double_click)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bottom, text="清空显示",
                   command=self._clear_display).pack(side="left")
        ttk.Button(bottom, text="关闭",
                   command=self.destroy).pack(side="right")

    def _select_all_slaves(self):
        self.slave_listbox.selection_set(0, "end")
        self._on_slave_select_changed(None)

    def _deselect_all_slaves(self):
        self.slave_listbox.selection_clear(0, "end")
        self._on_slave_select_changed(None)

    def _invert_slaves(self):
        sel = set(self.slave_listbox.curselection())
        self.slave_listbox.selection_clear(0, "end")
        for i in range(self.slave_listbox.size()):
            if i not in sel:
                self.slave_listbox.selection_set(i)
        self._on_slave_select_changed(None)

    def _get_selected_slaves(self):
        sel = self.slave_listbox.curselection()
        if not sel:
            return None
        sids = []
        for i in sel:
            txt = self.slave_listbox.get(i)
            try:
                sids.append(int(txt.split()[0]))
            except Exception:
                pass
        return sids

    def _on_slave_select_changed(self, event):
        sids = self._get_selected_slaves()
        if sids is None:
            self.slave_info_var.set("未选择 = 全部")
        elif len(sids) <= 5:
            self.slave_info_var.set(f"已选: {sids}")
        else:
            self.slave_info_var.set(f"已选: {len(sids)} 个")

    def _refresh_slaves(self):
        try:
            sids = self.log_mgr.list_slaves()
        except Exception:
            sids = []
        old_sel = set(self._get_selected_slaves() or [])
        self.slave_listbox.delete(0, "end")
        for sid in sids:
            if self.label_mgr:
                name = self.label_mgr.get_slave_name(sid)
                if name:
                    self.slave_listbox.insert("end", f"{sid} {name}")
                else:
                    self.slave_listbox.insert("end", str(sid))
            else:
                self.slave_listbox.insert("end", str(sid))
        for i, sid in enumerate(sids):
            if sid in old_sel:
                self.slave_listbox.selection_set(i)
        self._on_slave_select_changed(None)

    def _refresh_dates(self):
        dates = self.log_mgr.list_dates()
        values = ["全部"] + dates
        self.date_cb["values"] = values
        if self.date_var.get() not in values:
            self.date_var.set("全部")

    def _refresh_all(self):
        self._refresh_dates()
        self._refresh_slaves()
        self._do_query()

    def _do_query(self):
        date = None if self.date_var.get() == "全部" else self.date_var.get()
        level = None if self.level_var.get() == "全部" else self.level_var.get()
        cat = None if self.cat_var.get() == "全部" else self.cat_var.get()
        sids = self._get_selected_slaves()
        kw = self.kw_var.get().strip() or None
        limit = self.limit_var.get()

        for item in self.tree.get_children():
            self.tree.delete(item)

        t0 = time.time()
        rows = self.log_mgr.query(
            date=date, level=level, category=cat,
            slave_ids=sids, keyword=kw, limit=limit)
        elapsed = (time.time() - t0) * 1000

        for ts, lv, ct, sid_s, msg in rows:
            if sid_s != "-" and self.label_mgr:
                try:
                    sid_i = int(sid_s)
                    disp = self.label_mgr.display_slave(sid_i)
                except Exception:
                    disp = sid_s
            else:
                disp = sid_s
            self.tree.insert("", "end",
                             values=(ts, lv, ct, disp, msg),
                             tags=(lv,))

        sids_str = "全部" if sids is None else str(sids)
        self.info_var.set(
            f"共 {len(rows)} 条 (耗时 {elapsed:.0f}ms)  "
            f"[日期={date or '全部'} 级别={level or '全部'} "
            f"类别={cat or '全部'} 从站={sids_str} "
            f"关键字={kw or '无'}]")

    def _clear_display(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.info_var.set("")

    def _on_double_click(self, event):
        item = self.tree.focus()
        if not item:
            return
        vals = self.tree.item(item, "values")
        text = "\t".join(str(v) for v in vals)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.info_var.set(f"已复制: {text[:80]}...")

    def _export_csv(self):
        items = self.tree.get_children()
        if not items:
            messagebox.showinfo("提示", "没有可导出的数据")
            return
        fname = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
            initialfile=f"gyj0179_logs_{datetime.now():%Y%m%d_%H%M%S}.csv",
        )
        if not fname:
            return
        try:
            with open(fname, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["时间", "级别", "类别", "从站", "消息"])
                for item in items:
                    vals = self.tree.item(item, "values")
                    writer.writerow(vals)
            messagebox.showinfo("成功", f"已导出 {len(items)} 条到:\n{fname}")
        except Exception as e:
            messagebox.showerror("错误", f"导出失败: {e}")

    def _delete_current_date(self):
        date = self.date_var.get()
        if date == "全部":
            messagebox.showwarning("提示",
                "请先选择具体日期, 不能删除'全部'")
            return
        if not messagebox.askyesno("确认",
                f"确定删除 {date}.log ?\n\n此操作不可恢复!"):
            return
        try:
            f = self.log_mgr.log_dir / f"{date}.log"
            if f.exists():
                f.unlink()
                messagebox.showinfo("成功", f"已删除 {date}.log")
                self._refresh_all()
            else:
                messagebox.showwarning("提示", "文件不存在")
        except Exception as e:
            messagebox.showerror("错误", f"删除失败: {e}")
