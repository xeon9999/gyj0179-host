# -*- coding: utf-8 -*-
"""
GYJ-0179 连接记忆与快速重连
保存上次成功连接的参数 (端口/波特率/从站), 供下次启动时快速连接。
数据保存在 connection.json (工作目录)。
"""
import json
import time
from pathlib import Path

from .constants import (
    DEFAULT_BAUDRATE,
    DEFAULT_SLAVE_ID,
    DEFAULT_ADDR_MIN,
    DEFAULT_ADDR_MAX,
)

DEFAULT_CONN_PATH = "connection.json"


class ConnectionStore:
    """读写上次成功连接参数, 支持多从站 (slave_ids) 与单从站 (slave_id)。"""

    def __init__(self, path=DEFAULT_CONN_PATH):
        self.path = Path(path)
        self.data = {
            "port": None,
            "baudrate": DEFAULT_BAUDRATE,
            "slave_id": DEFAULT_SLAVE_ID,
            "slave_ids": None,     # 多从站: [1, 3, 5]
            "addr_min": DEFAULT_ADDR_MIN,
            "addr_max": DEFAULT_ADDR_MAX,
            "timestamp": None,
        }
        self.load()

    def load(self):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    for k in self.data:
                        if k in d:
                            self.data[k] = d[k]
            except Exception as e:
                print(f"[ConnectionStore] 加载失败: {e}")

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"[ConnectionStore] 保存失败: {e}")
            return False

    # ---- 通用读取 ----
    def has_saved(self):
        """是否有上次成功连接的记录"""
        return bool(self.data.get("port"))

    @property
    def port(self):
        return self.data.get("port")

    @property
    def baudrate(self):
        return self.data.get("baudrate", DEFAULT_BAUDRATE)

    @property
    def slave_id(self):
        return self.data.get("slave_id", DEFAULT_SLAVE_ID)

    @property
    def slave_ids(self):
        return self.data.get("slave_ids")

    @property
    def addr_min(self):
        return self.data.get("addr_min", DEFAULT_ADDR_MIN)

    @property
    def addr_max(self):
        return self.data.get("addr_max", DEFAULT_ADDR_MAX)

    @property
    def timestamp(self):
        return self.data.get("timestamp")

    # ---- 保存单从站连接 ----
    def save_single(self, port, baudrate, slave_id):
        self.data.update({
            "port": port,
            "baudrate": int(baudrate),
            "slave_id": int(slave_id),
            "slave_ids": None,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.save()

    # ---- 保存多从站连接 ----
    def save_multi(self, port, baudrate, slave_ids,
                   addr_min=None, addr_max=None):
        self.data.update({
            "port": port,
            "baudrate": int(baudrate),
            "slave_id": slave_ids[0] if slave_ids else DEFAULT_SLAVE_ID,
            "slave_ids": sorted(int(s) for s in slave_ids),
            "addr_min": int(addr_min) if addr_min is not None
                        else self.data.get("addr_min", DEFAULT_ADDR_MIN),
            "addr_max": int(addr_max) if addr_max is not None
                        else self.data.get("addr_max", DEFAULT_ADDR_MAX),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.save()
