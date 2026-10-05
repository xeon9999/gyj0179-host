# -*- coding: utf-8 -*-
"""
GYJ-0179 Modbus RTU 从站驱动 —— 常量与版本兼容
平台: STC15W408AS + SP485EE
协议: Modbus RTU, 9600 8N1
"""
import inspect

from pymodbus.client import ModbusSerialClient
from pymodbus import __version__ as PYMODBUS_VERSION

# 程序版本
VERSION = "1.0.0"

# 默认通信参数
DEFAULT_BAUDRATE = 9600
DEFAULT_SLAVE_ID = 1
DEFAULT_TIMEOUT = 1.0
DEFAULT_PARITY = "N"
DEFAULT_BYTESIZE = 8
DEFAULT_STOPBITS = 1

# 默认扫描范围
DEFAULT_ADDR_MIN = 1
DEFAULT_ADDR_MAX = 32

# 广播地址 (Modbus 标准)
BROADCAST_ADDR = 0x00

# 日志默认参数
DEFAULT_LOG_DIR = "logs"
DEFAULT_LOG_KEEP_DAYS = 30

# 标签文件
DEFAULT_LABELS_PATH = "labels.json"


def _detect_slave_kwarg():
    """
    自动识别 pymodbus 版本对应的从站地址参数名:
        < 3.3.0  →  unit
        3.3~3.7  →  slave
        >= 3.8   →  device_id
    """
    try:
        sig = inspect.signature(ModbusSerialClient.read_coils)
        params = list(sig.parameters.keys())
        for candidate in ("device_id", "slave", "unit"):
            if candidate in params:
                return candidate
    except Exception:
        pass
    return "device_id"


# 模块加载时探测一次, 全局复用
SLAVE_KW = _detect_slave_kwarg()
