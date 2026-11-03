# -*- coding: utf-8 -*-
"""鼠标信息采集：Logitech HID++ 2.0（走 Nano / Unifying / Bolt 接收器）。

这个模块只干一件事：把「一只罗技鼠标现在是什么状态」查出来，并把
「把 DPI 改成多少」写进去。UI 只读这里吐出来的 dict，不碰任何 HID 细节。

---------------------------------------------------------------- 实测结论
在本机 M350 Wireless Mouse + Nano 接收器（VID 0x046D / PID 0xC52F）上
逐条验过，下面这些坑都是真踩过的，改代码前先看：

1. 走哪个 HID 集合。一个接收器会枚举出 4 个接口，只有
   usage_page == 0xFF00 且 usage == 2 那个能收 20 字节长报文
   （usage == 1 是 7 字节短报文）。打错了 write 会直接失败或永远没回包。

2. 特性 ID 是 MSB 在前。请求 getFeature(featureId) 的参数是
   [featureId >> 8, featureId & 0xFF]。写成 LSB 在前不会报错，只会
   一直拿到错误帧，非常难查。

3. 必须按设备索引过滤回包。接收器上可能挂着别的设备，也会收到上一轮
   迟到的响应。判据：r[1] == devIdx and r[2] == featIdx and 高四位 == func。
   少任何一个条件都会串帧（实测过：读到另一只设备的电量）。

4. 错误响应用 byte4 == 0xFF 标记，byte5 是错误码。正常响应 byte4 是
   第一个数据字节（sensorIdx / entityCount 之类，永远是 0..15），所以
   这个判据是安全的。

5. 写 DPI 时设备不校验也不夹取。实测写 1 / 21000 / 32000 / 65535 全部
   被原样回显（回包"成功"），但实际灵敏度是设备自己夹的。所以上下限
   只能从 0x2201 getSensorDpiList 解出来，不能靠「写极值试出来」。

6. getSensorDpiList 的连字符编码：16 位值里 bit15..13 == 0b111 表示
   「这是一个步进值」，低 13 位才是步进；否则就是离散档位或上下限。
   本机回 [400, 0xE064, 4000, 0] → 400..4000 步进 100。

7. 电量用 0x1000（batteryStatus，老式单请求）。func0 回
   [电量百分比, 下一个报告阈值, 充电状态]，本机 5A 32 00 = 90% /
   阈值 50% / 正在放电。0x1004（unifiedBattery）本机不存在；0x1001
   （batteryVoltage）也不存在。不要按型号猜，按枚举结果来。

8. 设备信息 0x0003 一次就能拿全。func0 回
   [entityCount, unitId×4, reserved, transport, pid0×2, pid1×2, pid2×2, extModel, caps]，
   其中 transport 的 bit3..0 依次是 USB / eQuad / BTLE / 蓝牙，三个 PID
   槽位按「蓝牙、BTLE、eQuad、USB」的使能顺序填。本机 transport=0x06
   （eQuad + BTLE），pid0=0xB021、pid1=0x4080。

9. 设备名 0x0005 要分片读。func0 回名称长度，func1(offset) 回
   16 字节一片的 UTF-8。一次读完会截断。

10. 只有被配对的设备索引会应答（本机是 1）。1..6 全 ping 一遍，
    没人应答就是「接收器上没设备 / 设备在深度休眠」。
"""

from __future__ import print_function

import sys
import time
import json
import threading

# 电量解释（百分比/电压/估算标记）统一走 battery 模块，这里只负责取帧。
# 单独起别名是为了让下面的调用点一眼能看出「这不是本地逻辑」。
import battery as _battery

try:
    import hid
except Exception:                                     # pragma: no cover
    hid = None


# ---------------------------------------------------------------- 常量

VID_LOGITECH = 0x046D

REPORT_ID_SHORT = 0x10
REPORT_ID_LONG = 0x11
USAGE_PAGE_HIDPP = 0xFF00
USAGE_LONG = 2

FEAT_ROOT = 0x0000
FEAT_FEATURESET = 0x0001
FEAT_DEVICE_INFO = 0x0003
FEAT_DEVICE_NAME = 0x0005
FEAT_BATTERY_STATUS = 0x1000           # 老式：func0 一把梭
FEAT_BATTERY_UNIFIED = 0x1004          # 新式：分 capabilities/status 两步
FEAT_BATTERY_VOLTAGE = 0x1001
FEAT_ADJ_DPI = 0x2201
FEAT_EXT_ADJ_DPI = 0x2202

DEVICE_INDEXES = (1, 2, 3, 4, 5, 6)

# 一次请求的等待窗口。实测 0.9s + 2 次重试足够稳；再长就拖慢 5 秒轮询。
TIMEOUT_S = 0.9
RETRIES = 2

# ping 单独用更短的窗口。在线设备实测 0.02~0.08 秒就回，给它 0.9 秒纯属浪费：
# 6 个索引全不应答时，一次扫描会从 0.9 秒拖到 5.4 秒，首屏就一直是「检测中」。
PING_TIMEOUT = 0.15

# 同步写命令（DPI）等结果的上限。轮询线程在两次 poll 之间处理命令，
# 正常情况下 0.1~0.5 秒就有结果；留给它 4 秒是因为设备刚唤醒时第一包
# 可能超时一次，写完还要回读确认。
SYNC_WAIT_S = 4.0

# HID++ 错误码 → 中文（只列实际可能遇到的，其余按码值显示）
ERROR_TEXT = {
    0x00: "设备返回了空错误码（通常是特性 ID 大小端写反了）",
    0x01: "未知错误",
    0x02: "参数无效",
    0x03: "参数超出范围",
    0x04: "请求失败",
    0x05: "设备不支持这个特性",
    0x06: "设备不支持这个功能（function）",
    0x07: "设备忙，请稍后重试",
    0x08: "设备忙，请稍后重试",
    0x09: "不支持的操作",
    0x0A: "参数无效",
    0x0B: "参数超出范围",
}

# transport 位 → 连接方式文案。0x0003 的 byte6。
TRANSPORT_BITS = (
    (0x08, "usb", "USB 线"),
    (0x04, "usb_receiver", "USB 接收器"),
    (0x02, "bolt", "Bolt 接收器"),
    (0x01, "bluetooth", "蓝牙"),
)

# 接收器 PID → 种类文案。用来把「USB 接收器」细分成 Nano / Unifying / Bolt。
RECEIVER_KINDS = {
    0xC52B: ("unifying", "Unifying 接收器"),
    0xC532: ("unifying", "Unifying 接收器"),
    0xC534: ("unifying", "Unifying 接收器"),
    0xC539: ("unifying", "Unifying 接收器"),
    0xC548: ("bolt", "Bolt 接收器"),
    0xC547: ("bolt", "Bolt 接收器"),
    0xC52F: ("usb_receiver", "Nano 接收器"),
}

BATTERY_STATUS_TEXT = {
    0: "正在放电",
    1: "正在充电",
    2: "即将充满",
    3: "已充满",
    4: "慢速充电",
    5: "电池异常",
    6: "温度异常",
    7: "其他充电错误",
}

# 设备不接受上报范围时用的保守默认（需求里写的「常规范围」）与推断标记
FALLBACK_DPI_MIN = 200
FALLBACK_DPI_MAX = 12000
FALLBACK_DPI_STEP = 50


# ---------------------------------------------------------------- 小工具

def _log_line(msg):
    """统一日志格式。轮询线程和 UI 线程都会调，只做追加。"""
    try:
        print("[%s] mouse: %s" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def idle_seconds():
    """距最后一次键鼠交互的秒数（GetLastInputInfo）。

    口径说明（界面上也要写）：这是整个系统的空闲时长，不是"这只鼠标
    多久没动"。设备侧拿不到逐设备空闲时间，所以只能给这个。返回值 None
    表示调用失败（非 Windows 或 API 被拦）。
    """
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = ctypes.windll.kernel32.GetTickCount()
        delta = (int(now) - int(info.dwTime)) & 0xFFFFFFFF
        return delta / 1000.0
    except Exception:
        return None


def _be16(hi, lo):
    return (int(hi) << 8) | int(lo)


# ---------------------------------------------------------------- 帧收发

class HidppError(Exception):
    """HID++ 层错误（设备回了错误帧，或压根没回包）。"""

    def __init__(self, code, text=None, raw=None):
        self.code = code
        self.raw = raw
        super(HidppError, self).__init__(text or ERROR_TEXT.get(code, "错误码 0x%02X" % code))


class ReceiverLink(object):
    """一个接收器接口的收发封装。

    生命周期：open() → 若干 request() → close()。设备休眠时 write 可能
    静默失败，所以连续失败要重建链路（由 MouseSource 负责）。
    """

    def __init__(self, info):
        self.info = info
        self.path = info.get("path")
        self.product_id = int(info.get("product_id") or 0)
        self.product_string = (info.get("product_string") or "").strip()
        self._dev = None
        self._lock = threading.Lock()

    # ---- 打开 / 关闭
    def open(self):
        if hid is None:
            raise RuntimeError("未安装 hidapi（python -m pip install hidapi）")
        dev = hid.device()
        dev.open_path(self.path)
        try:
            dev.set_nonblocking(True)
        except Exception:
            pass
        self._dev = dev
        return self

    def close(self):
        dev, self._dev = self._dev, None
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass

    @property
    def is_open(self):
        return self._dev is not None

    # ---- 收一发
    def _drain(self):
        """发之前把缓冲里的迟到帧丢掉，否则会读到上一轮的响应。"""
        try:
            for _ in range(64):
                if not self._dev.read(32, 1):
                    break
        except Exception:
            pass

    def _write(self, frame):
        data = bytes(bytearray(frame))
        try:
            n = self._dev.write(data)
        except Exception as exc:
            raise HidppError(-1, "写 HID 失败：%s" % exc)
        if n is not None and n < 0:
            raise HidppError(-1, "写 HID 被系统拒绝（接口可能被 Logi Options+ / G HUB 独占）")
        return True

    def request(self, dev_index, feat_index, func, params=(), timeout=TIMEOUT_S,
                retries=RETRIES, func_byte=None):
        """发一条长报文并等匹配的回包。

        返回完整响应帧（list[int]，20 字节）。设备回错误帧时抛 HidppError。

        ``func_byte`` 用来覆盖 byte3。常规请求 byte3 是 ``(func << 4) | swid``，
        但 0x0000 的 ping 是特例：byte3 直接就是 **0x1F**（相当于
        "featureIndex=0x00 且 function=0x1F"，不是四位移位），响应也原样回
        0x1F。传了 func_byte 就跳过 func 过滤，否则 ping 永远收不到回包
        —— 实测症状是「接收器打开成功、1..6 全部无应答」。
        """
        if self._dev is None:
            raise HidppError(-2, "接收器链路未打开")
        payload = [REPORT_ID_LONG, dev_index & 0xFF, feat_index & 0xFF,
                   ((int(func) << 4) & 0xF0) if func_byte is None else (int(func_byte) & 0xFF)]
        payload += [int(x) & 0xFF for x in params]
        frame = (payload + [0] * 20)[:20]
        want = payload[3]
        with self._lock:
            last_exc = None
            for _attempt in range(retries + 1):
                self._drain()
                self._write(frame)
                t0 = time.time()
                while time.time() - t0 < timeout:
                    try:
                        raw = self._dev.read(32, 100)
                    except Exception as exc:
                        last_exc = HidppError(-1, "读 HID 失败：%s" % exc)
                        break
                    if not raw:
                        continue
                    r = list(raw)
                    # 三个条件缺一不可（见模块 docstring 第 3 条）
                    if len(r) < 4:
                        continue
                    if r[1] != (dev_index & 0xFF):
                        continue
                    if r[2] != (feat_index & 0xFF):
                        continue
                    if (r[3] & 0xF0) != (want & 0xF0):
                        continue
                    if len(r) >= 6 and r[4] == 0xFF:
                        raise HidppError(r[5], raw=r)
                    return r
                if last_exc is not None:
                    # 读失败一般是句柄废了，重试没意义，直接把链路判死
                    raise last_exc
            raise HidppError(-3, "设备没有响应（超时 %.1fs，可能是休眠或未配对）" % timeout)

    # ---- 高层封装
    def get_feature_index(self, dev_index, feature_id):
        """0x0000 getFeature：把 16 位特性 ID 换成 8 位索引。0 表示不支持。"""
        r = self.request(dev_index, FEAT_ROOT, 0x0,
                         [(feature_id >> 8) & 0xFF, feature_id & 0xFF])
        return int(r[4]) if len(r) > 4 else 0

    def read_short_string(self, dev_index, feat_index, length, chunk=16):
        """0x0005 那种「先给长度再分片读」的字符串。"""
        out = bytearray()
        offset = 0
        while offset < length:
            r = self.request(dev_index, feat_index, 0x1, [offset, 0, 0, 0])
            piece = bytearray()
            for b in r[4:]:
                if b == 0:
                    break
                piece.append(b)
            if not piece:
                break
            out.extend(piece)
            offset += len(piece)
            if len(piece) < min(chunk, length - (offset - len(piece))):
                break
        return bytes(out).decode("utf-8", "replace").strip("\x00 ").strip()


# ---------------------------------------------------------------- 设备对象

class MouseDevice(object):
    """接收器上的一只鼠标。所有 feature 索引在这里缓存，避免每轮重查。"""

    def __init__(self, link, index):
        self.link = link
        self.index = index
        self.name = ""
        self.unit_id = ""
        self.pid = 0
        self.pids = []
        self.ext_model = 0
        self.transport_bits = 0
        self.feat = {}
        self.dpi_min = 0
        self.dpi_max = 0
        self.dpi_step = 0
        self.dpi_inferred = False
        self.battery_supported = False
        self.battery_feature = 0
        # 电压换算成百分比时的曲线（"alkaline" / "nimh"），由 MouseSource 注入
        self.battery_curve = None
        # 中值滤波：电压读数有噪声，换算前必须先滤
        self._median = None
        self._probing_done = False

    # ---- 型号
    def _read_name(self):
        fi = self.feat.get("name")
        if not fi:
            return ""
        try:
            r = self.link.request(self.index, fi, 0x0)
            length = int(r[4]) if len(r) > 4 else 0
            if not length or length > 64:
                return ""
            return self.link.read_short_string(self.index, fi, length)
        except HidppError as exc:
            _log_line("读设备名失败：%s" % exc)
            return ""

    def _read_info(self):
        fi = self.feat.get("info")
        if not fi:
            return
        try:
            r = self.link.request(self.index, fi, 0x0)
        except HidppError as exc:
            _log_line("读设备信息失败：%s" % exc)
            return
        p = r[4:]
        if len(p) < 15:
            return
        self.unit_id = "%02X%02X%02X%02X" % (p[1], p[2], p[3], p[4])
        self.transport_bits = p[6]
        pids = []
        for k in range(3):
            v = _be16(p[7 + 2 * k], p[8 + 2 * k])
            if v:
                pids.append(v)
        self.pids = pids
        self.pid = pids[0] if pids else 0
        self.ext_model = p[13]

    # ---- DPI 能力
    def _read_dpi_caps(self):
        """解 getSensorDpiList。失败就把 inferred 置 True，用保守默认。"""
        self.dpi_inferred = True
        self.dpi_min, self.dpi_max, self.dpi_step = (
            FALLBACK_DPI_MIN, FALLBACK_DPI_MAX, FALLBACK_DPI_STEP)
        # 出厂默认 DPI（getSensorDpi 的 defaultDpi），界面「恢复默认」用；
        # v0 固件不报，保持 None
        self.dpi_default = None
        fi = self.feat.get("dpi")
        if not fi:
            _log_line("设备没有 0x2201/0x2202 特性，DPI 不可调（按推断值显示）")
            return
        try:
            for sensor in range(2):
                r = self.link.request(self.index, fi, 0x1, [sensor, 0, 0, 0])
                values = []
                # 帧结构：[0x11, devIdx, featIdx, funcByte, sensorIdx, 列表...]
                # 所以 16 位列表从 r[5] 开始，从 r[4] 开始会整体错位一个字节
                # （实测会解出 1–40960 步进 15345 这种离谱值）。
                for k in range(5, min(len(r) - 1, 19), 2):
                    v = _be16(r[k], r[k + 1])
                    if v == 0:
                        break
                    values.append(v)
                if not values:
                    continue
                lo, hi, step = decode_dpi_list(values)
                if lo and hi and hi > lo:
                    self.dpi_min, self.dpi_max, self.dpi_step = lo, hi, step
                    self.dpi_inferred = False
                    _log_line("DPI 能力（传感器 %d）：%d–%d 步进 %d，原始 %s"
                              % (sensor, lo, hi, step,
                                 " ".join("%04X" % v for v in values)))
                    return
        except HidppError as exc:
            _log_line("读 DPI 能力失败：%s（按推断值 %d–%d 显示）"
                      % (exc, FALLBACK_DPI_MIN, FALLBACK_DPI_MAX))

    # ---- 电量能力
    def _probe_battery(self):
        """按**实际枚举到的**特性决定电量怎么读，不按型号猜。

        优先级（越高越可信）：
          0x1004 unifiedBattery —— 新版百分比
          0x1000 batteryStatus  —— 老版百分比（本机 M350 就是它，回 90%）
          0x1001 batteryVoltage —— 只有电压，必须换算成估算值

        注意语义：需求文本写的是「0x1001=百分比 / 0x1000=电压」，但罗技官方
        规范和本机实测都是反过来的（0x1000 回 0x5A=90 的百分比，0x1001 才是
        毫伏）。所以这里不硬编码语义，统一交给 ``battery.interpret_reading()``
        做「官方规范优先 + 数值合理性兜底」的解释 —— 两个都自洽时按规范来，
        只有一个自洽时用它，都不自洽时显示「不支持」。详见 battery.py。
        """
        self.battery_feature = 0
        self.battery_supported = False
        for key, fid in (("battery_unified", FEAT_BATTERY_UNIFIED),
                         ("battery", FEAT_BATTERY_STATUS),
                         ("battery_voltage", FEAT_BATTERY_VOLTAGE)):
            if self.feat.get(key):
                self.battery_feature = fid
                self.battery_supported = True
                _log_line("电量来源：0x%04X（%s）" % (fid, key))
                return
        # 三个都没有 → 界面上显示「不支持」，绝不显示 0%
        _log_line("设备没有可读的电量特性（0x1000 / 0x1001 / 0x1004 均不存在）")

    # ---- 探测（只在第一次 ping 成功后跑一次）
    def probe(self):
        if self._probing_done:
            return
        for key, fid in (("info", FEAT_DEVICE_INFO), ("name", FEAT_DEVICE_NAME),
                         ("battery", FEAT_BATTERY_STATUS),
                         ("battery_unified", FEAT_BATTERY_UNIFIED),
                         ("battery_voltage", FEAT_BATTERY_VOLTAGE),
                         ("dpi", FEAT_ADJ_DPI), ("dpi_ext", FEAT_EXT_ADJ_DPI)):
            try:
                fi = self.link.get_feature_index(self.index, fid)
            except HidppError:
                fi = 0
            if fi:
                self.feat[key] = fi
        # 只有 0x2202 没有 0x2201 时才退到扩展版（需求第 2 条）
        if not self.feat.get("dpi") and self.feat.get("dpi_ext"):
            self.feat["dpi"] = self.feat["dpi_ext"]
            _log_line("设备只支持扩展 DPI 特性 0x2202，已切到该特性")
        self._read_info()
        self.name = self._read_name()
        self._read_dpi_caps()
        self._probe_battery()
        # 先读一次当前 DPI：既填充界面初值，也顺手拿到出厂默认值
        self.read_dpi()
        self._probing_done = True
        _log_line("设备就绪：%s | PID %s | unit %s | transport 0x%02X | 特性 %s"
                  % (self.name or "(名称未知)", self.pid_hex or "-", self.unit_id or "-",
                     self.transport_bits, ",".join(sorted(self.feat))))

    @property
    def pid_hex(self):
        return ("%04X" % self.pid) if self.pid else ""

    def read_dpi(self):
        """读当前 DPI，顺手把出厂默认值存到 self.dpi_default。

        getSensorDpi(func2) 回 [sensorIdx, dpiMSB, dpiLSB, defaultMSB, defaultLSB]，
        默认值在 r[7..8]。版本 0 的固件不报默认值（回 0），此时保持 None，
        界面上的「恢复默认」会退回范围中点而不是写 0。
        """
        fi = self.feat.get("dpi")
        if not fi:
            return None
        try:
            r = self.link.request(self.index, fi, 0x2, [0, 0, 0, 0])
        except HidppError as exc:
            _log_line("读 DPI 失败：%s" % exc)
            return None
        if len(r) < 7:
            return None
        if len(r) >= 9:
            dflt = _be16(r[7], r[8])
            if dflt:
                self.dpi_default = dflt
        return _be16(r[5], r[6])

    def write_dpi(self, value):
        """写 DPI：先夹到设备上报的范围（含步进对齐），再写、再回读确认。

        返回 (ok, actual, message)。
        """
        fi = self.feat.get("dpi")
        if not fi:
            return False, None, "设备不支持 DPI 调节（没有 0x2201 特性）"
        target = clamp_dpi(value, self.dpi_min, self.dpi_max, self.dpi_step)
        try:
            self.link.request(self.index, fi, 0x3,
                              [0x00, (target >> 8) & 0xFF, target & 0xFF, 0])
        except HidppError as exc:
            msg = str(exc)
            if exc.code == 0x05:
                msg = "设备不支持 DPI 写入"
            elif exc.code in (0x07, 0x08):
                msg = "设备忙，稍后重试"
            elif exc.code == -3:
                msg = "写入超时：可能是接口被 Logi Options+ / G HUB 独占，或设备已休眠"
            return False, None, msg
        actual = self.read_dpi()
        if actual is None:
            return False, target, "写入已发出，但回读失败（无法确认是否生效）"
        if abs(actual - target) > max(1, self.dpi_step // 2):
            return False, actual, ("设备把 DPI 改成了 %d（请求 %d）——"
                                   "实际上下限与你设的不同，已按设备回读值显示"
                                   % (actual, target))
        return True, actual, "已写入并回读确认：%d DPI" % actual

    def read_battery(self):
        """读一次电量，返回**标准读数 dict**（见 battery.make_reading）。

        改动的理由：原来返回 ``(percent, text, ok, note)`` 四元组，信息量不够 ——
        界面需要区分「设备上报」和「电压估算」，还要拿到原始毫伏做 tooltip。
        现在统一成 battery 模块的结构，``MouseSource`` 再把它塞进快照。

        三种情况（对应需求 Q1）：
          * 设备上报百分比 → source="reported"，界面标「设备上报」
          * 只有电压      → source="estimated"，界面标「估算」+ 附 mV
          * 都没有        → source="missing"，界面标「不支持」（绝不显示 0%）
        """
        fid = self.battery_feature
        if not fid:
            return _battery.blank_reading("设备不上报电量")

        curve = getattr(self, "battery_curve", None) or _battery.DEFAULT_CURVE
        median = self._median_filter()

        try:
            if fid == FEAT_BATTERY_UNIFIED:
                r = self.link.request(self.index, self.feat["battery_unified"], 0x1)
                payload = r[4:]
                if len(payload) < 2:
                    return _battery.blank_reading("电量响应帧过短")
                return _battery.interpret_reading(
                    FEAT_BATTERY_UNIFIED, payload, curve=curve, median_filter=median)

            if fid == FEAT_BATTERY_VOLTAGE:
                r = self.link.request(self.index, self.feat["battery_voltage"], 0x0)
                payload = r[4:]
                if len(payload) < 2:
                    return _battery.blank_reading("电压响应帧过短")
                return _battery.interpret_reading(
                    FEAT_BATTERY_VOLTAGE, payload, curve=curve, median_filter=median)

            r = self.link.request(self.index, self.feat["battery"], 0x0)
            payload = r[4:]
            if len(payload) < 3:
                return _battery.blank_reading("电量响应帧过短")
            status = payload[2]
            text = BATTERY_STATUS_TEXT.get(status, "状态 0x%02X" % status)
            reading = _battery.interpret_reading(
                FEAT_BATTERY_STATUS, payload, curve=curve, median_filter=median,
                status_text=text)
            # batteryStatus 特有：放电中读到 0 表示「真的空了」，其它状态下 0
            # 表示「未知」。这两种要区分开，不能都显示 0%。
            if reading.get("percent") == 0 and status != 0:
                return _battery.blank_reading("设备未上电或电量为未知")
            return reading
        except HidppError as exc:
            return _battery.blank_reading("读电量失败：%s" % exc)

    def _median_filter(self):
        """每台设备一个中值滤波器（电压换算是有噪声的，必须滤波）。"""
        if self._median is None:
            self._median = _battery.MedianFilter()
        return self._median


# ---------------------------------------------------------------- 解码

def decode_dpi_list(values):
    """把 getSensorDpiList 的 16 位值序列解成 (min, max, step)。

    规则见模块 docstring 第 6 条：
      * [400, 0xE064, 1300] → 连字符在中间 → 400..1300 步进 100
      * [400, 800, 1600]    → 离散档位 → min/max + 相邻最小差
      * [1000]              → 只有一档 → 上下限相同
    """
    # 0x0000 是「列表结束」标记，不是 DPI 值。调用方可能已经在校验时截断，
    # 也可能把整条列表连结束符一起传进来 —— 这里统一先剔掉，否则 plain[-1]
    # 会取到 0，解出 max=0 这种明显错的边界。
    values = [v for v in (values or []) if v]
    if not values:
        # 一条有效的 DPI 值都没有 → 交回推断范围，让界面标注「推断值」，
        # 而不是返回 0（返回 0 会让滑块上下限都变成 0，整个控件废掉）。
        return FALLBACK_DPI_MIN, FALLBACK_DPI_MAX, FALLBACK_DPI_STEP
    plain = []
    hyphen = None
    for v in values:
        if (v >> 13) & 0x07 == 0x07:
            hyphen = v & 0x1FFF
        else:
            plain.append(v)
    if not plain:
        # 只有连字符、没有端点：信息不全，无法确定上下限，退回推断值。
        # （真机上不会出现；出现说明解析错位，宁可显示推断值也别显示 0）
        return FALLBACK_DPI_MIN, FALLBACK_DPI_MAX, (hyphen or FALLBACK_DPI_STEP)
    if hyphen is not None and len(plain) >= 2:
        lo, hi = plain[0], plain[-1]
        return lo, hi, (hyphen or FALLBACK_DPI_STEP)
    if len(plain) == 1:
        return plain[0], plain[0], (hyphen or FALLBACK_DPI_STEP)
    lo, hi = plain[0], plain[-1]
    diffs = [b - a for a, b in zip(plain, plain[1:]) if b > a]
    step = min(diffs) if diffs else FALLBACK_DPI_STEP
    return lo, hi, max(1, step)


def clamp_dpi(value, lo, hi, step):
    """把任意输入夹到 [lo, hi] 并对齐到 step 的整数倍。

    step <= 0 时只夹不断档。返回 int。
    """
    try:
        v = int(round(float(value)))
    except Exception:
        v = lo
    lo = int(lo or FALLBACK_DPI_MIN)
    hi = int(hi or FALLBACK_DPI_MAX)
    if hi < lo:
        lo, hi = hi, lo
    v = max(lo, min(hi, v))
    step = int(step or 0)
    if step > 0:
        v = lo + int(round((v - lo) / float(step))) * step
        if v > hi:
            v -= step
        if v < lo:
            v = lo
    return int(v)


def receiver_kind(product_id, product_string=""):
    """接收器 PID → (kind, label)，用于界面上写清是哪种接收器。"""
    if product_id in RECEIVER_KINDS:
        return RECEIVER_KINDS[product_id]
    text = (product_string or "").lower()
    if "bolt" in text:
        return "bolt", "Bolt 接收器"
    if "unifying" in text:
        return "unifying", "Unifying 接收器"
    return "usb_receiver", "USB 接收器"


# ---------------------------------------------------------------- 主采集器

class MouseSource(object):
    """后台采集线程 + 供 UI 读取的快照 + 写命令入口。

    线程模型：一条线程独占 HID 句柄，UI 只在 snapshot() 里读 dict，
    要写 DPI 就 set_dpi(v)（内部排队，不抢句柄）。这样不需要复杂的锁，
    也不会出现两条线程同时 read 同一个句柄导致丢帧。
    """

    def __init__(self, interval=5.0, interval_hidden=30.0, device_index=0,
                 log_limit=200):
        self.interval = max(1.0, float(interval or 5.0))
        self.interval_hidden = max(self.interval, float(interval_hidden or 30.0))
        self.device_index = int(device_index or 0)
        self.log_limit = int(log_limit or 200)

        self._lock = threading.Lock()
        self._state = self._blank_state()
        self._log = []
        self._apply_lock = threading.Lock()
        self._pending = None              # (target_index, dpi) 待写的命令
        self._pending_result = None
        #: set_dpi() 同步等的信号：轮询线程写完 DPI 就 set 一次
        self._dpi_event = threading.Event()
        self._dpi_result = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._active = True               # 窗口可见时 True
        self._thread = None
        self._link = None
        self._device = None
        #: index -> MouseDevice，所有在线设备的缓存（需求：有连接的设备都显示）。
        #: probe 过的设备留着复用 —— 每台 probe 要走十几次 HID++ 请求（实测
        #: 0.7 秒），缓存之后每轮只读一次电量（约 50 ms）。
        self._peers = {}

    # ---- 状态骨架
    def _blank_state(self):
        return {
            "ok": False,
            "severity": "info",
            "error": "尚未采集",
            "hint": "",
            "updated": 0.0,
            "devices": [],
            "active_index": 0,
            "name": "",
            "display_name": "",
            "pid": "",
            "unit_id": "",
            "receiver": {"pid": "", "label": "", "kind": ""},
            "connection": {"kind": "", "label": "", "device_index": 0},
            "status": "offline",
            "status_text": "离线",
            "battery": _battery.blank_reading(),
            "dpi": {"supported": False, "value": None, "min": FALLBACK_DPI_MIN,
                    "max": FALLBACK_DPI_MAX, "step": FALLBACK_DPI_STEP,
                    "inferred": True, "default": None},
            "idle": {"seconds": None, "text": "—"},
            "poll_interval": self.interval,
            "hidapi": hid is not None,
        }

    # ---- 日志
    def log(self, msg):
        line = "%s  %s" % (time.strftime("%H:%M:%S"), msg)
        self._log.append(line)
        if len(self._log) > self.log_limit:
            del self._log[:len(self._log) - self.log_limit]
        _log_line(msg)

    def logs(self):
        return list(self._log)

    # ---- 快照
    def snapshot(self):
        with self._lock:
            snap = json.loads(json.dumps(self._state))   # 深拷贝，防止外部改
        snap["log"] = self.logs()[-40:]
        snap["take_result"] = self._take_pending_result()
        return snap

    def _take_pending_result(self):
        with self._apply_lock:
            res, self._pending_result = self._pending_result, None
        return res

    # ---- 控制
    def set_active(self, active):
        """窗口隐藏时降频（需求第 1 条）。"""
        active = bool(active)
        if active != self._active:
            self._active = active
            self._wake.set()

    def set_interval(self, seconds):
        self.interval = max(1.0, float(seconds or 5.0))
        with self._lock:
            self._state["poll_interval"] = self.interval
        self._wake.set()

    def set_device_index(self, index):
        if int(index or 0) != self.device_index:
            self.device_index = int(index or 0)
            self._drop_device()
            self._wake.set()

    def request_refresh(self):
        self._wake.set()

    def set_dpi(self, value):
        """写 DPI 并**等结果**，返回 {ok, requested, actual, message}。

        为什么不只排队：排队的话调用方立刻拿到 None，前端拿不到任何失败
        原因，用户点了「应用」没有任何反馈（需求明确要求失败时说清原因：
        设备不支持 / 权限不足 / 通信超时）。所以这里等到轮询线程真正写完
        并回读确认，最多等 SYNC_WAIT_S 秒。

        超时不代表没写成功——只是没等到确认，返回里如实说明。
        """
        value = int(value)
        with self._apply_lock:
            self._pending = (self.device_index, value)
            self._dpi_result = None
            self._dpi_event.clear()
        self._wake.set()

        if not self._dpi_event.wait(SYNC_WAIT_S):
            with self._apply_lock:
                # 请求还在队列里就别撤了，让它继续写；只是这次没等到结果
                still_pending = self._pending is not None
            self.log("写入 DPI %d 等结果超时（%.1fs）" % (value, SYNC_WAIT_S))
            return {
                "ok": False,
                "requested": value,
                "actual": None,
                "message": ("等待设备确认超时（%.0f 秒）——命令已发出，"
                            "但没收到回读。多半是设备休眠或接口被 "
                            "Logi Options+ / G HUB 占用，动一下鼠标再试"
                            % SYNC_WAIT_S),
                "timeout": bool(still_pending),
            }

        with self._apply_lock:
            res = self._dpi_result
        if not res:
            return {"ok": False, "requested": value, "actual": None,
                    "message": "写 DPI 没有返回结果（内部异常，详见日志）"}
        return res

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mouse-source", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        th = self._thread
        if th and th.is_alive():
            th.join(timeout=3.0)
        self._close_link()

    # ---- 链路管理
    def _close_link(self):
        if self._link is not None:
            try:
                self._link.close()
            except Exception:
                pass
        self._link = None
        # 链路一关，之前 probe 过的设备全失效（句柄都换了），一起清掉
        self._peers = {}

    def _drop_device(self):
        # 只丢「当前选中那台」的探测缓存：链路还在的时候，其它设备缓存仍然有效，
        # 换选中设备没必要让它们再花 0.7 秒重 probe 一遍。
        self._device = None

    def _peer_for(self, index):
        """拿到某台设备的 MouseDevice（probe 结果缓存复用）。

        需求「有连接的设备都显示」要求每台都能报电量，而电量必须在
        ``probe()`` 之后才读得到（要先查 0x1000/0x1001/0x1004 特性索引）。
        probe 一台实测约 0.7 秒（十几次 HID++ 往返），不能每轮都做 ——
        所以按 index 缓存，掉线或换链路时整表清空重来。
        """
        dev = self._peers.get(index)
        if dev is not None:
            return dev
        dev = MouseDevice(self._link, index)
        dev.battery_curve = self.battery_curve
        dev.probe()
        self._peers[index] = dev
        return dev

    def _find_receiver(self):
        """枚举罗技接收器，找 usage_page=0xFF00 / usage=2 的长报文接口。"""
        if hid is None:
            raise RuntimeError("未安装 hidapi（python -m pip install hidapi）")
        try:
            entries = hid.enumerate(VID_LOGITECH, 0)
        except Exception as exc:
            raise RuntimeError("枚举 HID 设备失败：%s" % exc)
        longs = [e for e in entries
                 if e.get("usage_page") == USAGE_PAGE_HIDPP and e.get("usage") == USAGE_LONG]
        if not longs:
            # 有罗技设备但都不是接收器长报文接口 → 大概率是插线/蓝牙直连
            others = [e for e in entries if e.get("usage_page") == USAGE_PAGE_HIDPP]
            if others:
                raise RuntimeError("找到罗技 HID++ 接口，但没有长报文集合"
                                   "（可能只有短报文接口，或驱动异常）")
            raise RuntimeError("未检测到罗技接收器（VID 046D 的 HID++ 接口不存在）")
        # 优先选 PID 已知是接收器的那一个，避免把键盘接收器当鼠标
        longs.sort(key=lambda e: 0 if int(e.get("product_id") or 0) in RECEIVER_KINDS else 1)
        return longs[0]

    def _ensure_link(self):
        if self._link is not None and self._link.is_open:
            return self._link
        info = self._find_receiver()
        link = ReceiverLink(info).open()
        self._link = link
        kind, label = receiver_kind(int(info.get("product_id") or 0),
                                    info.get("product_string") or "")
        self.log("接收器已打开：PID %04X（%s）%s"
                 % (int(info.get("product_id") or 0), label,
                    (info.get("product_string") or "").strip()))
        with self._lock:
            self._state["receiver"] = {
                "pid": "%04X" % int(info.get("product_id") or 0),
                "label": label,
                "kind": kind,
            }
        return link

    # ---- 设备发现
    def _ping(self, index, timeout=PING_TIMEOUT):
        """0x0000 ping：发 [0x00, 0x00, 0xAA]，回显说明设备在线。

        注意 func_byte=0x1F：ping 的 byte3 直接就是 0x1F，不是 (func << 4)。
        见 request() 的说明，写错了会「接收器正常、所有索引都无应答」。

        timeout 默认只用 0.15 秒 —— 在线设备实测 0.02~0.08 秒就回，等 0.35 秒
        纯属浪费：6 个索引全不应答时，一次扫描就从 2.1 秒变成 0.9 秒。
        """
        try:
            r = self._link.request(index, FEAT_ROOT, 0x1F, [0x00, 0x00, 0xAA],
                                   timeout=timeout, retries=0, func_byte=0x1F)
            return len(r) > 6 and r[6] == 0xAA
        except HidppError:
            return False

    def _discover(self):
        """在 1..6 上 ping，返回所有应答的索引。

        刚 open 完接收器时，第一次 ping 常常全灭 —— 无线链路还没热身，
        首帧被吞掉（实测刚打开句柄的第一轮 7 个索引全超时，等 1 秒后
        第二轮就全好了）。所以全失败时立刻再扫一轮，别让用户对着
        「未检测到设备」等 5 秒的下一个轮询周期。
        """
        alive = [i for i in DEVICE_INDEXES if self._ping(i)]
        if not alive:
            time.sleep(0.5)          # 给无线链路一点热身时间
            alive = [i for i in DEVICE_INDEXES if self._ping(i)]
        if not alive:
            raise HidppError(-4, "接收器上没有任何设备应答（鼠标可能已关机、深度休眠，"
                                 "或配对信息丢失）")
        self.log("接收器上应答的设备索引：%s" % ",".join(str(i) for i in alive))
        return alive

    def _open_device(self, index):
        dev = MouseDevice(self._link, index)
        dev.probe()
        self._device = dev
        with self._lock:
            self._state["active_index"] = index
        return dev

    # ---- 一次采集
    def _poll_once(self):
        if hid is None:
            self._fail("未安装 hidapi",
                       "在命令行执行 python -m pip install hidapi 后重启本程序", "error")
            return

        # 1) 接收器链路
        try:
            self._ensure_link()
        except Exception as exc:
            self._close_link()
            self._drop_device()
            self._fail("未检测到罗技接收器", "%s；请确认接收器已插好" % exc, "error")
            return

        # 2) 设备应答（多设备时列出全部）
        try:
            alive = self._discover()
        except HidppError as exc:
            # 这里**不要**关闭链路：能走到 _discover 说明接收器句柄是好的，
            # 纯粹是鼠标不应答（休眠 / 刚上电）。关了再重开的代价是下一轮要
            # 多做一次 open + 枚举，实测首屏要多等 2 秒多。
            # 只有 follow-up 也失败、句柄真卡住时才由 _ensure_link 重建。
            self._drop_device()
            prev = self._state_prev_status()
            if prev == "online":
                self._mark_asleep(str(exc))
            else:
                self._fail("未检测到设备", str(exc), "warn")
            return

        # 3) 选设备：优先用户指定的索引，其次第一个
        target = self.device_index if self.device_index in alive else alive[0]
        if self._device is not None and self._device.index != target:
            self._drop_device()

        try:
            if self._device is None:
                self._open_device(target)
            dev = self._device
            dpi_value = dev.read_dpi()
            # read_battery 现在返回标准读数 dict（含 source / voltage_mv /
            # granularity），不再是不带来源信息的四元组
            bat = dev.read_battery()
            battery_raw = self._read_battery_extra(dev)
        except HidppError as exc:
            self._close_link()
            self._drop_device()
            self._fail("读取失败", str(exc), "error")
            return

        idle = idle_seconds()
        now = time.time()

        # 4) 其余在线设备也各读一次电量（需求：有连接的设备都显示）。
        #    只在窗口可见时才做 —— 收起时降到 30 秒一轮，而多台设备每台
        #    一轮要多花一次 HID++ 往返，没必要为看不见的贴片付这个代价。
        #    取不到的那台不写死 0%，交给 devices.py 降级成「未读取」。
        peers = {}
        if self._active:
            for i in alive:
                if i == target:
                    continue
                try:
                    peer = self._peer_for(i)
                    peers[i] = {
                        "name": peer.name,
                        "pid": peer.pid_hex,
                        "unit_id": peer.unit_id,
                        "battery": peer.read_battery(),
                        "dpi": {},
                        "connection": {
                            "kind": self._connection_kind(peer),
                            "label": self._connection_label(peer),
                            "device_index": i,
                        },
                    }
                except Exception:
                    # 单台失败不能连累其它设备：这台这轮先不报，下一轮再试
                    self._peers.pop(i, None)

        devices = []
        for i in alive:
            item = {"index": i,
                    "name": dev.name if i == target else "",
                    "pid": dev.pid_hex if i == target else "",
                    "current": i == target}
            peer = peers.get(i)
            if peer:
                item["name"] = peer["name"]
                item["pid"] = peer["pid"]
                item["unit_id"] = peer["unit_id"]
                item["battery"] = peer["battery"]
                item["connection"] = peer["connection"]
            devices.append(item)

        with self._lock:
            st = self._state
            st["ok"] = True
            st["severity"] = "info" if dpi_value is not None else "warn"
            st["error"] = ""
            st["hint"] = ""
            st["updated"] = now
            st["devices"] = devices
            st["active_index"] = target
            st["name"] = dev.name
            # 需求：显示「产品名 (PID xxxx)」，名字读不到就降级成
            # 「接收器 PID xxxx / 设备索引 N」，不留空
            if dev.name and dev.pid_hex:
                st["display_name"] = "%s (PID %s)" % (dev.name, dev.pid_hex)
                st["hint"] = ""
            elif dev.name:
                st["display_name"] = dev.name
                st["hint"] = ""
            else:
                st["display_name"] = "接收器 PID %s / 设备索引 %d" % (
                    st["receiver"].get("pid") or "-", target)
                st["hint"] = "设备名称读取失败，已降级显示；原始报文见日志"
            st["pid"] = dev.pid_hex
            st["unit_id"] = dev.unit_id
            st["status"] = "online"
            st["status_text"] = "在线"
            st["connection"] = {
                "kind": self._connection_kind(dev),
                "label": self._connection_label(dev),
                "device_index": target,
            }
            st["battery"] = {
                # 标准读数结构（battery.make_reading）：percent / source /
                # source_label / voltage_mv / granularity / status_text / note
                "supported": bool(bat.get("supported")),
                "percent": bat.get("percent"),
                "source": bat.get("source"),
                "source_label": bat.get("source_label"),
                "voltage_mv": bat.get("voltage_mv"),
                "granularity": bat.get("granularity"),
                "text": bat.get("status_text") or "",
                "note": bat.get("note") or battery_raw,
                # 原始值保留给界面：即使显示 0%，也要能看出设备报的是什么
                "raw_percent": bat.get("percent"),
                "low": (bat.get("percent") is not None and bat.get("percent") < 5),
            }
            st["dpi"] = {
                "supported": bool(dev.feat.get("dpi")),
                "value": dpi_value,
                "min": dev.dpi_min,
                "max": dev.dpi_max,
                "step": dev.dpi_step,
                "inferred": bool(dev.dpi_inferred),
                "default": dev.dpi_default,
            }
            st["idle"] = {
                "seconds": idle,
                "text": format_idle(idle),
            }
            st["poll_interval"] = self.interval if self._active else self.interval_hidden

        self._flush_pending(dev)

    def _read_battery_extra(self, dev):
        """给界面补一句电量来源说明，方便排查（需求：读不到怎么排查）。"""
        if dev.battery_feature == FEAT_BATTERY_STATUS:
            return "来源 0x1000 batteryStatus"
        if dev.battery_feature == FEAT_BATTERY_UNIFIED:
            return "来源 0x1004 unifiedBattery"
        if dev.battery_feature == FEAT_BATTERY_VOLTAGE:
            return "来源 0x1001 batteryVoltage（电压换算，非设备百分比）"
        return ""

    def _connection_kind(self, dev):
        # 能从接收器通信，连接方式必然是接收器；再用 DeviceInfo 的 transport 位细分
        bits = dev.transport_bits
        if bits & 0x04:
            return "usb_receiver"
        if bits & 0x02:
            return "bolt"
        if bits & 0x08:
            return "usb"
        if bits & 0x01:
            return "bluetooth"
        return "usb_receiver"

    def _connection_label(self, dev):
        kind = self._connection_kind(dev)
        for _bit, key, label in TRANSPORT_BITS:
            if key == kind:
                return label
        return "USB 接收器"

    def _flush_pending(self, dev):
        with self._apply_lock:
            job, self._pending = self._pending, None
        if not job:
            return
        _index, value = job
        ok, actual, msg = dev.write_dpi(value)
        self.log("写入 DPI %d → %s" % (value, msg))
        with self._apply_lock:
            # 同步等结果的调用方（set_dpi）靠这两个字段拿回执；
            # _pending_result 保留给老路径 / 轮询快照兜底。
            self._pending_result = {
                "ok": bool(ok),
                "requested": int(value),
                "actual": actual,
                "message": msg,
                "at": time.time(),
            }
            self._dpi_result = self._pending_result
            self._dpi_event.set()      # 唤醒 set_dpi 的等待
        if actual is not None:
            with self._lock:
                self._state["dpi"]["value"] = actual

    def _state_prev_status(self):
        with self._lock:
            return self._state.get("status")

    def _mark_asleep(self, why):
        with self._lock:
            st = self._state
            st["status"] = "asleep"
            st["status_text"] = "休眠"
            st["severity"] = "warn"
            st["error"] = "设备无响应（休眠中）"
            st["hint"] = "动一下鼠标让它唤醒，下一轮会自动恢复；%s" % why
            st["updated"] = time.time()

    def _fail(self, title, hint, severity="error"):
        with self._lock:
            st = self._state
            st["ok"] = False
            st["severity"] = severity
            st["error"] = title
            st["hint"] = hint
            st["updated"] = time.time()
            st["status"] = "offline"
            st["status_text"] = "离线"
            st["devices"] = []
            st["connection"] = {"kind": "", "label": "", "device_index": 0}
            st["battery"] = _battery.blank_reading()
            st["dpi"] = {"supported": False, "value": None, "min": FALLBACK_DPI_MIN,
                         "max": FALLBACK_DPI_MAX, "step": FALLBACK_DPI_STEP,
                         "inferred": True, "default": None}
            idle = idle_seconds()
            st["idle"] = {"seconds": idle, "text": format_idle(idle)}
        self.log("%s：%s" % (title, hint))

    # ---- 线程主循环
    def _run(self):
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:                    # 绝不让线程死掉
                self._fail("采集异常", repr(exc), "error")
            # 这一轮没能走到 _flush_pending（设备离线/休眠/读失败），
            # 排队的写命令要立刻给调用方一个明确答复，而不是让它空等
            # SYNC_WAIT_S 秒再报「超时」——那会把「没设备」误报成「超时」。
            self._reject_stale_pending()
            delay = self.interval if self._active else self.interval_hidden
            self._wake.wait(delay)
            self._wake.clear()

    def _reject_stale_pending(self):
        with self._apply_lock:
            if self._pending is None or self._dpi_event.is_set():
                return
            _index, value = self._pending
            self._pending = None
            self._dpi_result = {
                "ok": False,
                "requested": int(value),
                "actual": None,
                "message": (self._state.get("error") or "设备当前不可写")
                           + "。DPI 未写入，设备恢复后再试一次",
                "at": time.time(),
            }
            self._pending_result = self._dpi_result
            self._dpi_event.set()


def format_idle(seconds):
    """空闲时长的人类可读写法。"""
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return "%d 秒" % s
    if s < 3600:
        return "%d 分 %d 秒" % (s // 60, s % 60)
    if s < 86400:
        return "%d 小时 %d 分" % (s // 3600, (s % 3600) // 60)
    return "%d 天 %d 小时" % (s // 86400, (s % 86400) // 3600)


def quick_probe():
    """给 selftest / 命令行排查用：不通线程，直接采一次。"""
    src = MouseSource()
    src._poll_once()
    snap = src.snapshot()
    src.stop()
    return snap


def main():
    """命令行自检：打印一次采集结果，然后退出。"""
    snap = quick_probe()
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    return 0 if snap.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
