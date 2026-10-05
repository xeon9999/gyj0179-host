# -*- coding: utf-8 -*-
"""
GYJ-0179 从站标签管理
用于给从站、继电器、光耦、寄存器命名, 方便人机识别。
数据保存在 JSON 文件中。
"""
import json
import threading
from pathlib import Path

from .constants import DEFAULT_LABELS_PATH


# 通信配置寄存器的固定名称 (未自定义命名时使用)
# 与单片机 chengxu.c 的约定一致
SPECIAL_REG_NAMES = {
    13: "从站地址",
    14: "波特率索引",
}


class LabelManager:
    """从站标签管理, 线程安全地读写 JSON 标签文件。"""

    def __init__(self, path=DEFAULT_LABELS_PATH):
        self.path = Path(path)
        self.data = {"slaves": {}}
        self._lock = threading.Lock()
        self.load()

    def load(self):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict) and "slaves" in d:
                    self.data = d
            except Exception as e:
                print(f"[LabelManager] 加载失败: {e}")

    def save(self):
        with self._lock:
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                return True
            except Exception as e:
                print(f"[LabelManager] 保存失败: {e}")
                return False

    def _ensure_slave(self, sid):
        key = str(sid)
        if key not in self.data["slaves"]:
            self.data["slaves"][key] = {
                "name": "", "com": {}, "di": {}, "reg": {}
            }
        s = self.data["slaves"][key]
        for k in ("com", "di", "reg"):
            if k not in s:
                s[k] = {}
        return s

    # ---- 从站 ----
    def get_slave_name(self, sid):
        return self.data["slaves"].get(str(sid), {}).get("name", "")

    def set_slave_name(self, sid, name):
        self._ensure_slave(sid)["name"] = name.strip() if name else ""

    def display_slave(self, sid):
        name = self.get_slave_name(sid)
        return f"{name} (#{sid})" if name else f"从站 #{sid}"

    # ---- 继电器 ----
    def get_com_name(self, sid, idx):
        return self.data["slaves"].get(str(sid), {}).get("com", {}).get(str(idx), "")

    def set_com_name(self, sid, idx, name):
        self._ensure_slave(sid)["com"][str(idx)] = name.strip() if name else ""

    def display_com(self, sid, idx):
        name = self.get_com_name(sid, idx)
        return f"{name} (COM{idx})" if name else f"COM{idx}"

    # ---- 光耦 ----
    def get_di_name(self, sid, idx):
        return self.data["slaves"].get(str(sid), {}).get("di", {}).get(str(idx), "")

    def set_di_name(self, sid, idx, name):
        self._ensure_slave(sid)["di"][str(idx)] = name.strip() if name else ""

    def display_di(self, sid, idx):
        name = self.get_di_name(sid, idx)
        return f"{name} (IN{idx})" if name else f"IN{idx}"

    # ---- 寄存器 ----
    def get_reg_name(self, sid, idx):
        return self.data["slaves"].get(str(sid), {}).get("reg", {}).get(str(idx), "")

    def set_reg_name(self, sid, idx, name):
        self._ensure_slave(sid)["reg"][str(idx)] = name.strip() if name else ""

    def display_reg(self, sid, idx, special=False):
        """special=True 时, 通信配置寄存器用固定名称显示 (与固件约定一致):
            reg[13] → 从站地址    reg[14] → 波特率索引
        这两个寄存器是通信配置, 名称固定, 不允许被自定义标签覆盖。
        """
        if special:
            fixed = SPECIAL_REG_NAMES.get(idx)
            if fixed:
                return f"{fixed} (reg[{idx}])"
        name = self.get_reg_name(sid, idx)
        return f"{name} (reg[{idx}])" if name else f"reg[{idx}]"
