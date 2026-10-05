# -*- coding: utf-8 -*-
"""
GYJ-0179 日志管理
按日期写入 tab 分隔的日志文件, 支持按条件查询、清理过期日志。
"""
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .constants import DEFAULT_LOG_DIR, DEFAULT_LOG_KEEP_DAYS


class LogManager:
    """线程安全地写入/查询按日期分文件的 tab 分隔日志。"""

    LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
    CATEGORIES = ("CONN", "SLAVE", "RELAY", "DI", "PARAM", "CMD", "SYS")

    def __init__(self, log_dir=DEFAULT_LOG_DIR, keep_days=DEFAULT_LOG_KEEP_DAYS):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        self.keep_days = keep_days
        self._lock = threading.Lock()
        self._cleanup_old_logs()

    def log(self, level, category, message, slave_id=None):
        level = level.upper()
        category = category.upper()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        sid = str(slave_id) if slave_id is not None else "-"
        message = str(message).replace("\t", " ").replace("\n", " ")
        line = f"{ts}\t{level}\t{category}\t{sid}\t{message}\n"
        with self._lock:
            try:
                fname = self.log_dir / f"{datetime.now():%Y-%m-%d}.log"
                with open(fname, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as e:
                print(f"[LogManager] 写日志失败: {e}")

    def list_dates(self):
        dates = []
        for f in self.log_dir.glob("*.log"):
            try:
                d = f.stem
                datetime.strptime(d, "%Y-%m-%d")
                dates.append(d)
            except Exception:
                pass
        dates.sort(reverse=True)
        return dates

    def list_slaves(self):
        sids = set()
        for f in self.log_dir.glob("*.log"):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    for line in fp:
                        parts = line.rstrip("\n").split("\t", 4)
                        if len(parts) < 5:
                            continue
                        sid_str = parts[3]
                        if sid_str == "-":
                            continue
                        try:
                            sids.add(int(sid_str))
                        except ValueError:
                            pass
            except Exception:
                pass
        return sorted(sids)

    def _cleanup_old_logs(self):
        if self.keep_days <= 0:
            return
        cutoff = datetime.now() - timedelta(days=self.keep_days)
        for f in self.log_dir.glob("*.log"):
            try:
                d = datetime.strptime(f.stem, "%Y-%m-%d")
                if d < cutoff:
                    f.unlink()
            except Exception:
                pass

    def query(self, date=None, level=None, category=None,
              slave_ids=None, keyword=None, limit=2000):
        if slave_ids is None:
            sid_set = None
        elif isinstance(slave_ids, (int, str)):
            sid_set = {str(slave_ids)}
        else:
            sid_set = {str(x) for x in slave_ids}
            if not sid_set:
                sid_set = None

        results = []
        if date:
            files = [self.log_dir / f"{date}.log"]
        else:
            files = sorted(self.log_dir.glob("*.log"), reverse=True)

        for f in files:
            if not f.exists():
                continue
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    for line in fp:
                        parts = line.rstrip("\n").split("\t", 4)
                        if len(parts) < 5:
                            continue
                        ts, lv, ct, sid, msg = parts
                        if level and lv != level.upper():
                            continue
                        if category and ct != category.upper():
                            continue
                        if sid_set is not None and sid not in sid_set:
                            continue
                        if keyword and keyword.lower() not in msg.lower():
                            continue
                        results.append((ts, lv, ct, sid, msg))
            except Exception as e:
                print(f"[LogManager] 读日志失败 {f}: {e}")

        results.reverse()
        return results[:limit]
