# -*- coding: utf-8 -*-
"""
GYJ-0179 Modbus RTU 从站驱动
平台: STC15W408AS + SP485EE
协议: Modbus RTU, 9600 8N1

自动兼容 pymodbus 各版本:
    < 3.3.0  →  unit
    3.3 ~ 3.7  →  slave
    >= 3.8     →  device_id

用法 1: 手动指定串口
    from gyj0179.driver import GYJ0179
    with GYJ0179("COM6", slave_id=1) as dev:
        dev.relay_on(1)

用法 2: 自动扫描 + 完整状态报告
    from gyj0179.driver import GYJ0179
    with GYJ0179() as dev:
        pass    # 构造时已经打印完整状态

    GYJ0179.scan_and_report()
    GYJ0179.auto_detect(slave_ids=range(1, 17), report=True)
"""

import time

from pymodbus.client import ModbusSerialClient

from .constants import SLAVE_KW


class GYJ0179:
    """GYJ-0179 工控板 Modbus RTU 驱动"""

    # ---- 保持寄存器 ----
    REG_PARAM_START = 0x0000
    REG_PARAM_COUNT = 15
    REG_SAVE_CMD    = 0x000F
    SAVE_MAGIC      = 0x5AA5

    # ---- 输入寄存器 ----
    REG_DI_PACK     = 0x0000

    # ---- 线圈 ----
    COIL_COM1 = 0
    COIL_COM2 = 1
    COIL_COM3 = 2

    # ---- 离散输入 ----
    DI_IN1 = 0
    DI_IN2 = 1
    DI_IN3 = 2
    DI_IN4 = 3
    DI_IN5 = 4
    DI_IN6 = 5

    BROADCAST_ADDR = 0x00

    # ==================== 自动扫描 ====================

    @staticmethod
    def _report_connected(port, sid, baudrate):
        """命中后建连并打印完整状态报告"""
        try:
            with GYJ0179(port, slave_id=sid, baudrate=baudrate,
                         auto_scan=False) as dev:
                if dev.connected:
                    dev.full_report()
        except Exception as e:
            print(f"  [警告] 完整报告读取失败: {e}")

    @staticmethod
    def auto_detect(baudrate=9600, slave_ids=(1,), timeout=0.3,
                    verbose=True, report=False):
        """
        自动扫描所有串口, 找到 GYJ-0179 板子。

        :param baudrate:  波特率
        :param slave_ids: 尝试的从站地址列表
        :param timeout:   单次探测超时秒数
        :param verbose:   是否打印扫描过程
        :param report:    命中后是否立即打印完整状态报告
        :return:          (port, slave_id) 或 (None, None)
        """
        candidates = _list_candidate_ports()
        if not candidates:
            if verbose:
                print("[auto_detect] 没有找到任何可用串口")
            return None, None

        if verbose:
            print(f"[auto_detect] 开始扫描 {len(candidates)} 个串口 "
                  f"(波特率 {baudrate}, 从站地址 {list(slave_ids)})...")

        for p in candidates:
            if verbose:
                print(f"  测试 {p.device:8s} ({p.description}) ... ",
                      end="", flush=True)

            ok, sid = _probe_port(p.device, baudrate, slave_ids, timeout)

            if ok:
                if verbose:
                    print(f"✓ 命中! 从站地址 = {sid}")
                if report:
                    GYJ0179._report_connected(p.device, sid, baudrate)
                return p.device, sid
            else:
                if verbose:
                    print("✗ 无响应")

        if verbose:
            print("[auto_detect] 未找到 GYJ-0179 板子")
            print("             排查: 电源 / A-B 接线 / 从站地址 / 波特率")
        return None, None

    @staticmethod
    def auto_detect_all(baudrate=9600, addr_min=1, addr_max=32,
                        timeout=0.2, verbose=True, report=False):
        """
        全扫描: 遍历所有串口 × 所有从站地址。
        用于多从站组网时一次找出所有在线设备。

        :param baudrate: 单个波特率(int) 或多个波特率(list/tuple)。
                         传列表时自动切换波特率, 依次尝试直到找到设备。
        :param addr_min: 起始从站地址
        :param addr_max: 结束从站地址
        :param timeout:  单次探测超时秒数
        :param verbose:  是否打印扫描过程
        :param report:   命中后是否打印完整状态报告
        :return: [(port, slave_id, baudrate), ...]
        """
        # 归一化波特率为列表
        if isinstance(baudrate, (list, tuple)):
            baudrates = list(baudrate)
        else:
            baudrates = [baudrate]

        results = []          # [(port, sid, baudrate), ...]
        candidates = _list_candidate_ports()
        slave_ids = range(addr_min, addr_max + 1)

        if verbose:
            print(f"[auto_detect_all] 扫描 {len(candidates)} 个串口 "
                  f"× 地址 {addr_min}~{addr_max} "
                  f"× 波特率 {baudrates}...")

        for p in candidates:
            if verbose:
                print(f"  扫描 {p.device} ({p.description})")
            for baudrate in baudrates:
                if verbose:
                    print(f"    波特率 {baudrate}:")
                for sid in slave_ids:
                    ok, found_sid = _probe_port(p.device, baudrate,
                                                (sid,), timeout)
                    if ok:
                        results.append((p.device, found_sid, baudrate))
                        if verbose:
                            print(f"      找到从站: 地址 {found_sid} "
                                  f"@ {baudrate}")
                if any(r[0] == p.device for r in results):
                    break  # 该串口已找到设备, 无需继续切换波特率

        if report and results:
            # 对每个找到的从站打印完整报告 (用各自匹配的波特率)
            for port, sid, baud in results:
                GYJ0179._report_connected(port, sid, baud)

        if verbose and not results:
            print("[auto_detect_all] 未找到任何从站")

        return results

    @staticmethod
    def scan_and_report(baudrate=9600, slave_ids=(1,), timeout=0.3):
        """
        一步到位: 扫描串口 + 匹配从站 + 打印完整状态报告。
        等价于 auto_detect(..., report=True) 但返回的是实例。

        :return: 已连接的 GYJ0179 实例, 未找到返回 None
        """
        port, sid = GYJ0179.auto_detect(
            baudrate=baudrate, slave_ids=slave_ids,
            timeout=timeout, verbose=True, report=False)
        if port is None:
            return None

        dev = GYJ0179(port, slave_id=sid, baudrate=baudrate,
                      auto_scan=False)
        if not dev.connect():
            print(f"[ERROR] 打开 {port} 失败")
            return None

        dev.full_report()
        return dev

    # ==================== 初始化 ====================

    def __init__(self, port=None, slave_id=1, baudrate=9600,
                 timeout=1.0, auto_scan=True, auto_report=True):
        """
        初始化驱动

        :param port:        串口号; None 时自动扫描
        :param slave_id:    从站地址
        :param baudrate:    波特率
        :param timeout:     超时秒数
        :param auto_scan:   port=None 时是否自动扫描
        :param auto_report: 自动扫描命中后是否打印完整状态
        """
        self._auto_report = False
        if port is None:
            if not auto_scan:
                raise ValueError("port 为空时必须开启 auto_scan")
            print("[GYJ0179] 未指定 port, 自动扫描串口...")
            detected_port, detected_id = GYJ0179.auto_detect(
                baudrate=baudrate,
                slave_ids=(slave_id,),
                report=False)
            if detected_port is None:
                raise RuntimeError("未找到 GYJ-0179 板子")
            port = detected_port
            if detected_id is not None:
                slave_id = detected_id
            print(f"[GYJ0179] 自动使用 {port}, 从站地址 {slave_id}")
            self._auto_report = auto_report

        self.port = port
        self.slave_id = slave_id
        self.client = ModbusSerialClient(
            port=port, baudrate=baudrate,
            bytesize=8, parity='N', stopbits=1, timeout=timeout,
        )
        self.connected = False

    # ==================== 连接管理 ====================

    def connect(self):
        self.connected = self.client.connect()
        if self.connected and self._auto_report:
            self.full_report()
        return self.connected

    def close(self):
        if self.connected:
            self.client.close()
            self.connected = False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()

    # ==================== 完整状态报告 ====================

    def full_report(self):
        """
        读取并打印所有资源:
          - 3 路继电器 (COM)
          - 6 路光耦 (IN)
          - 16 个保持寄存器
          - 16 个输入寄存器
          - 16 个线圈
          - 16 路离散输入
        """
        print()
        print("=" * 60)
        print(f"  GYJ-0179 设备状态报告")
        print(f"  端口: {self.port}    从站地址: {self.slave_id}")
        print("=" * 60)

        # ---- 继电器 (线圈 0~2) ----
        print()
        print("【继电器】(线圈 0x01 / 0x05)")
        coils = self.read_coils(self.COIL_COM1, 3)
        if coils is None:
            print("  读取失败")
        else:
            for i, c in enumerate(coils, 1):
                print(f"  COM{i} (P3.{8-i}): "
                      f"{'吸合' if c else '释放'}")

        # ---- 光耦 (离散输入 0~5) ----
        print()
        print("【光耦输入】(离散输入 0x02)")
        bits = self.read_di_bits()
        if bits is None:
            print("  读取失败")
        else:
            labels = ["IN1(P1.2)", "IN2(P1.3)", "IN3(P1.4)",
                      "IN4(P1.5)", "IN5(P1.6)", "IN6(P1.7)"]
            for label, b in zip(labels, bits):
                print(f"  {label}: {'有信号' if b else '无信号'}")

        # ---- 光耦打包值 ----
        print()
        print("【光耦打包寄存器】(输入寄存器 0x00)")
        v = self.read_di_value()
        if v is None:
            print("  读取失败")
        else:
            bin_str = format(v, '06b')
            print(f"  值: 0x{v:02X}  二进制: 0b{bin_str}")
            print(f"  分解: IN6~IN1 = "
                  f"{' '.join(bin_str)}")

        # ---- 保持寄存器 ----
        print()
        print("【保持寄存器】(0x03 / 0x06 / 0x10)")
        params = self.get_all_params()
        if params is None:
            print("  读取失败")
        else:
            for i, val in enumerate(params):
                print(f"  参数[{i:2d}]: 0x{val:04X}  ({val})")
            # 读一下保存命令寄存器
            cmd = self.read_holding_registers(self.REG_SAVE_CMD, 1)
            if cmd is not None:
                print(f"  参数[15]: 0x{cmd[0]:04X}  "
                      f"(保存命令, 写 0x5AA5 触发)")

        # ---- 输入寄存器 ----
        print()
        print("【输入寄存器】(0x04)")
        input_regs = self.read_input_registers(0, 4)
        if input_regs is None:
            print("  读取失败")
        else:
            for i, val in enumerate(input_regs):
                print(f"  IR[{i:2d}]: 0x{val:04X}  ({val})")

        print()
        print("=" * 60)
        print()

    # ==================== 读操作 ====================

    def _read(self, func_name, address, count, attr, truncate=False):
        """通用 Modbus 读操作, 统一错误处理"""
        try:
            rr = getattr(self.client, func_name)(
                address, count=count, **{SLAVE_KW: self.slave_id})
            if rr.isError():
                return None
            data = getattr(rr, attr)
            return data[:count] if truncate else data
        except Exception as e:
            print(f"[ERROR] {func_name}: {e}")
            return None

    def read_holding_registers(self, address, count):
        return self._read("read_holding_registers", address, count,
                          "registers")

    def read_input_registers(self, address, count):
        return self._read("read_input_registers", address, count,
                          "registers")

    def read_coils(self, address, count):
        return self._read("read_coils", address, count, "bits",
                          truncate=True)

    def read_discrete_inputs(self, address, count):
        return self._read("read_discrete_inputs", address, count,
                          "bits", truncate=True)

    # ==================== 写操作 ====================

    def _write(self, func_name, address, *args):
        """通用 Modbus 写操作, 统一错误处理"""
        try:
            rr = getattr(self.client, func_name)(
                address, *args, **{SLAVE_KW: self.slave_id})
            return not rr.isError()
        except Exception as e:
            print(f"[ERROR] {func_name}: {e}")
            return False

    def write_holding_register(self, address, value):
        return self._write("write_register", address, value)

    def write_holding_registers(self, address, values):
        return self._write("write_registers", address, values)

    def write_coil(self, address, value):
        return self._write("write_coil", address, bool(value))

    def write_coils(self, address, values):
        return self._write("write_coils", address, values)

    # ==================== 继电器 API ====================

    _COILS = {1: 0, 2: 1, 3: 2}   # relay 编号 -> 线圈地址

    @classmethod
    def _coil_of(cls, relay):
        """校验继电器编号并返回对应线圈地址"""
        try:
            return cls._COILS[relay]
        except KeyError:
            raise ValueError("relay must be 1, 2 or 3")

    def relay_on(self, relay):
        return self.write_coil(self._coil_of(relay), True)

    def relay_off(self, relay):
        return self.write_coil(self._coil_of(relay), False)

    def relay_toggle(self, relay):
        coil = self._coil_of(relay)
        bits = self.read_coils(coil, 1)
        if bits is None:
            return False
        return self.write_coil(coil, not bits[0])

    def relay_get(self, relay):
        coil = self._coil_of(relay)
        bits = self.read_coils(coil, 1)
        return None if bits is None else bits[0]

    def relay_all_on(self):
        return self.write_coils(self.COIL_COM1, [True, True, True])

    def relay_all_off(self):
        return self.write_coils(self.COIL_COM1, [False, False, False])

    # ==================== 光耦 API ====================

    def read_di_bits(self):
        bits = self.read_discrete_inputs(self.DI_IN1, 6)
        return None if bits is None else tuple(bits)

    def read_di_value(self):
        regs = self.read_input_registers(self.REG_DI_PACK, 1)
        return None if regs is None else (regs[0] & 0x3F)

    # ==================== 参数 API ====================

    def save_params(self):
        ok = self.write_holding_register(self.REG_SAVE_CMD, self.SAVE_MAGIC)
        if ok:
            time.sleep(0.1)
        return ok

    def get_param(self, index):
        if not 0 <= index < self.REG_PARAM_COUNT:
            raise ValueError(f"index must be 0 ~ {self.REG_PARAM_COUNT - 1}")
        regs = self.read_holding_registers(self.REG_PARAM_START + index, 1)
        return None if regs is None else regs[0]

    def set_param(self, index, value, save=False):
        if not 0 <= index < self.REG_PARAM_COUNT:
            raise ValueError(f"index must be 0 ~ {self.REG_PARAM_COUNT - 1}")
        if not 0 <= value <= 0xFFFF:
            raise ValueError("value must be 0 ~ 65535")
        ok = self.write_holding_register(self.REG_PARAM_START + index, value)
        if ok and save:
            ok = self.save_params()
        return ok

    def get_all_params(self):
        return self.read_holding_registers(self.REG_PARAM_START,
                                           self.REG_PARAM_COUNT)

    def set_all_params(self, values, save=False):
        if len(values) > self.REG_PARAM_COUNT:
            raise ValueError(
                f"values length must ≤ {self.REG_PARAM_COUNT}")
        ok = self.write_holding_registers(self.REG_PARAM_START, list(values))
        if ok and save:
            ok = self.save_params()
        return ok

    # ==================== 从站通信配置 API ====================
    # 与单片机 chengxu.c 的约定:
    #   reg[13] 从站地址 (1~247)
    #   reg[14] 波特率索引 0~4 → 9600/19200/38400/57600/115200
    #   reg[15] 命令: 0x5AA5 保存 / 0xA500 保存并立即生效 / 0xA501 恢复出厂

    REG_ADDR_IDX = 13
    REG_BAUD_IDX = 14
    REG_CFG_CMD_IDX = 15

    CFG_CMD_SAVE = 0x5AA5
    CFG_CMD_APPLY = 0xA500
    CFG_CMD_FACTORY = 0xA501

    # 波特率表必须与单片机固件 chengxu.c 的 Baud_Value() 完全一致 (索引 0~6)
    BAUD_TABLE = (9600, 19200, 38400, 57600, 115200, 28800, 14400)
    DEFAULT_SLAVE_ADDR = 2
    DEFAULT_BAUD_IDX = 4

    @classmethod
    def baud_of_index(cls, idx):
        try:
            return cls.BAUD_TABLE[int(idx)]
        except Exception:
            return None

    @classmethod
    def index_of_baud(cls, baud):
        try:
            return list(cls.BAUD_TABLE).index(int(baud))
        except Exception:
            return None

    def get_slave_config(self):
        """读取从站当前通信配置 → (从站地址, 波特率索引) 或 None"""
        regs = self.read_holding_registers(self.REG_ADDR_IDX, 3)
        if regs is None or len(regs) < 2:
            return None
        return int(regs[0]) & 0xFFFF, int(regs[1]) & 0xFFFF

    def set_slave_config(self, addr, baud_idx, cmd=CFG_CMD_APPLY,
                         restart_wait=0.0, detail=None):
        """把从站地址/波特率写入从站并保存到 EEPROM。

        用功能码 0x06 逐条写 (实测本板 0x10 写多寄存器应答不稳定);
        顺序: 地址 → 波特率 → 命令(最后写, 避免半途生效)。
        detail: 可选 list, 会依次填入每条写入的成功布尔值, 便于调用方区分
                "完全无应答" 与 "仅命令字无应答"。
        cmd=0xA500 时从站会在应答后切换参数, 调用方需等约 1.5s 再重连。
        """
        if not 1 <= int(addr) <= 247:
            raise ValueError("从站地址必须在 1~247 之间")
        if self.baud_of_index(baud_idx) is None:
            raise ValueError("波特率索引必须是 0~4")

        ok = self.write_holding_register(self.REG_ADDR_IDX, int(addr))
        if detail is not None:
            detail.append(bool(ok))
        if ok:
            ok = self.write_holding_register(self.REG_BAUD_IDX, int(baud_idx))
            if detail is not None:
                detail.append(bool(ok))
        if ok:
            ok = self.write_holding_register(self.REG_CFG_CMD_IDX,
                                             int(cmd) & 0xFFFF)
            if detail is not None:
                detail.append(bool(ok))
        if ok and restart_wait > 0:
            time.sleep(restart_wait)
        return bool(ok)

    # ==================== 广播 API ====================

    def _broadcast_coil(self, coil, value):
        """向所有从站广播写单个线圈"""
        try:
            self.client.write_coil(
                coil, value, **{SLAVE_KW: self.BROADCAST_ADDR})
        except Exception:
            pass
        return True

    def _broadcast_coils(self, values):
        """向所有从站广播写多个线圈"""
        try:
            self.client.write_coils(
                self.COIL_COM1, values,
                **{SLAVE_KW: self.BROADCAST_ADDR})
        except Exception:
            pass
        return True

    def broadcast_relay_on(self, relay):
        return self._broadcast_coil(self._coil_of(relay), True)

    def broadcast_relay_off(self, relay):
        return self._broadcast_coil(self._coil_of(relay), False)

    def broadcast_all_on(self):
        return self._broadcast_coils([True, True, True])

    def broadcast_all_off(self):
        return self._broadcast_coils([False, False, False])

    # ==================== 兼容旧接口 ====================

    def print_status(self):
        """保留旧接口, 等同 full_report()"""
        self.full_report()


# ==================== 模块级辅助函数 ====================

def _list_candidate_ports():
    """列出所有可能连接的串口, 排除蓝牙 / Intel AMT / SOL 虚拟口"""
    import serial.tools.list_ports
    ports = list(serial.tools.list_ports.comports())
    candidates = []
    for p in ports:
        desc = (p.description or "").upper()
        if "BLUETOOTH" in desc or "蓝牙" in desc:
            continue
        if "INTEL" in desc and ("AMT" in desc or "SOL" in desc):
            continue
        if "SOL" in desc:
            continue
        candidates.append(p)
    return candidates


def _probe_port(port, baudrate, slave_ids, timeout):
    """尝试打开端口, 依次探测多个从站地址, 返回 (成功?, 从站地址)"""
    try:
        client = ModbusSerialClient(
            port=port, baudrate=baudrate,
            bytesize=8, parity='N', stopbits=1, timeout=timeout,
        )
        if not client.connect():
            return False, None
        try:
            for sid in slave_ids:
                try:
                    rr = client.read_holding_registers(
                        0, count=1, **{SLAVE_KW: sid})
                    if not rr.isError():
                        return True, sid
                except Exception:
                    pass
        finally:
            client.close()
    except Exception:
        pass
    return False, None
