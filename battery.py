# -*- coding: utf-8 -*-
"""电量换算 —— 回答「AA 电池的电量准不准」

================================================================ 结论先说
只有一种情况下电量的数字是真的：**设备自己上报了百分比**。其余全是估算。

  1. 设备上报百分比（0x1000 batteryStatus 的 dischargeLevel，或 0x1004
     unifiedBattery 的 level）—— **原样采用**，界面标「设备上报」。
     这是设备的固件根据它自己的电量模型给的，我们不加工。

  2. 设备只上报**电压**（0x1001 batteryVoltage，单位毫伏）—— 必须换算，
     界面标「估算」并在 tooltip 里附当前 mV。误差来源：
       * 我们用的是**通用 AA 放电曲线**，不是这只鼠标厂家的数据；
       * 鼠标移动的瞬间负载电流上升，电压会被拉低几十到一百多毫伏，
         所以「不动的时候」和「正在甩鼠标」读到的百分比能差好几个点；
       * 一次性电池刚装上时电压偏高（1500mV+），前 10% 电量几乎看不出来。
     ⇒ 结论：**估算误差约 ±5%，移动瞬间偏低**。所以这里做了 5 点中值滤波，
       并且界面上的「估算」角标是必须的，不能省。

  3. 两者都不支持 —— 显示「不支持」，**绝不显示 0%**（0% 会让用户以为要换电池）。

---------------------------------------------------------------- 关于特性 ID
需求里写的是「0x1001 = Battery Level Status（百分比）、0x1000 = Battery
Voltage（毫伏）」，但罗技 HID++ 官方规范是反过来的：

  0x1000 batteryStatus   → byte0 百分比、byte1 下一个档位、byte2 充电状态
  0x1001 batteryVoltage  → byte0..1 毫伏、byte2 状态位
  0x1004 unifiedBattery  → 新版，capabilities + status 两步

本机实测 M350 只支持 0x1000，回 `5A 32 00` = 90% / 阈值 50 / 放电中，
这只能是百分比（当电压的话 0x5A32 = 23090mV，AA 电池不可能）。

所以这里**不按 ID 硬编码语义**，而是两手都做：
  * 先按官方规范试解（0x1000→百分比、0x1001→毫伏）；
  * 再用数值合理性兜底（0..100 才有资格当百分比；900..3300 才有资格当毫伏）；
  * 两者都能自洽时以官方规范为准；只有一个自洽就用那一个。
这样不管固件/型号怎么偏，都不会把 90% 读成 90mV 或者在界面上显示一个
离谱的数字。详见 ``interpret_reading()``。

---------------------------------------------------------------- 1% 计数单位
内部一律 int 0..100，**不做任何 //5*5、round(x/10)*10 的取整**。
设备的计数粒度另存 ``granularity`` 字段：
  * 连续两次读数的最小间隔 ≥ 5%，就认为设备粒度是 5%，界面标「设备粒度 5%」；
  * 粒度是 1% 就不标。
1% 是**我们这边的计数单位**，不代表设备一定能报这么细 —— 这句话 README
和界面说明里都要写。
"""

import os
import sys

# ---------------------------------------------------------------- 配置

#: 碱性 AA 放电曲线：[(毫伏, 剩余百分比), ...]，毫伏**降序**。
#: 这是**经验曲线**，不是厂家数据。数值来源是常见的碱性 AA 放电资料
#: （轻载 20-50mA 档），并按鼠标这种脉冲负载做了保守下调。
#: 需要实测校准：拿一只新碱性电池的鼠标，每掉 10% 记一次电压，反过来修这张表。
CURVE_ALKALINE = [
    (1500, 100),
    (1400, 85),
    (1300, 65),
    (1200, 45),
    (1100, 20),
    (1000, 5),
    (900, 0),
]

#: 镍氢（NiMH）AA 放电曲线。平台电压比碱性低约 100–200mV，且末端掉得更陡。
#: 同样是经验值，需要实测校准。
CURVE_NIMH = [
    (1400, 100),
    (1300, 88),
    (1250, 72),
    (1200, 55),
    (1150, 35),
    (1100, 18),
    (1050, 6),
    (1000, 0),
]

CURVES = {
    "alkaline": CURVE_ALKALINE,
    "nimh": CURVE_NIMH,
}

DEFAULT_CURVE = "alkaline"

#: 电压读数的合理区间（毫伏）。低于/高于这个范围的一律当异常帧丢弃，
#: 而不是硬换算出一个百分比。
VOLTAGE_MIN_MV = 800
VOLTAGE_MAX_MV = 3500

#: 中值滤波窗口。取 5 是因为鼠标一秒钟能采好几次，5 点正好压掉
#: 「甩鼠标时的那一下压降」，又不至于让电量响应太迟钝。
MEDIAN_WINDOW = 5

#: 设备粒度判定：连续两次有效读数的最小差值 ≥ 这个值，就当设备粒度是它。
#: 目前只判 5%（最常见的固件档位），其余情况按 1% 处理。
GRANULARITY_CANDIDATES = (10, 5)


# ---------------------------------------------------------------- 曲线换算

def voltage_to_percent(mv, curve=DEFAULT_CURVE):
    """毫伏 → 百分比（int 0..100）。分段线性插值。

    * 毫伏超出范围时**夹到端点**，不会返回负数或 >100。
    * 曲线表按毫伏降序，函数内部不假设顺序（会自己排一次，代价可忽略）。
    * ``curve`` 不认识时退回 alkaline，不让程序因为配置写错而崩。
    """
    try:
        mv = float(mv)
    except (TypeError, ValueError):
        return None
    if mv != mv:                       # NaN
        return None

    table = CURVES.get(curve) or CURVE_ALKALINE
    pts = sorted(table, key=lambda p: -p[0])   # 毫伏降序
    if not pts:
        return None

    # 高于最高点 → 100；低于最低点 → 0
    if mv >= pts[0][0]:
        return 100
    if mv <= pts[-1][0]:
        return 0

    for i in range(len(pts) - 1):
        hi_mv, hi_pct = pts[i]
        lo_mv, lo_pct = pts[i + 1]
        if lo_mv <= mv <= hi_mv:
            span = float(hi_mv - lo_mv)
            if span <= 0:
                return int(hi_pct)
            ratio = (mv - lo_mv) / span
            pct = lo_pct + ratio * (hi_pct - lo_pct)
            return clamp_percent(pct)
    return 0


def clamp_percent(value):
    """任何来源的百分比都要经过这里：夹到 0..100 并取整。

    **不取整到 5 或 10** —— 90 和 89 必须能区分开（见需求 Q2）。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    if v < 0:
        v = 0
    if v > 100:
        v = 100
    return int(round(v))


# ---------------------------------------------------------------- 中值滤波

class MedianFilter(object):
    """定长滑动窗口中值滤波。

    为什么用中值而不是均值：鼠标甩动的瞬间电压会掉一大截（负载压降），
    均值会被这些尖峰拉低；中值直接忽略尖峰。
    """

    def __init__(self, window=MEDIAN_WINDOW):
        self.window = max(1, int(window))
        self._buf = []

    def push(self, value):
        """塞一个新值，返回滤波后的值（窗口没填满时返回当前中值）。"""
        try:
            value = float(value)
        except (TypeError, ValueError):
            return self.current()
        if value != value:
            return self.current()
        self._buf.append(value)
        if len(self._buf) > self.window:
            del self._buf[:len(self._buf) - self.window]
        return self.current()

    def current(self):
        if not self._buf:
            return None
        ordered = sorted(self._buf)
        n = len(ordered)
        mid = n // 2
        if n % 2:
            return int(round(ordered[mid]))
        return int(round((ordered[mid - 1] + ordered[mid]) / 2.0))

    def reset(self):
        self._buf = []

    @property
    def filled(self):
        return len(self._buf) >= self.window


# ---------------------------------------------------------------- 读数解释

SOURCE_REPORTED = "reported"     # 设备上报的百分比，原样采用
SOURCE_ESTIMATED = "estimated"   # 由电压换算，是估算
SOURCE_MISSING = "missing"       # 读不到

SOURCE_LABEL = {
    SOURCE_REPORTED: "设备上报",
    SOURCE_ESTIMATED: "估算",
    SOURCE_MISSING: "不支持",
}


def blank_reading(note=""):
    """读不到电量时的标准结构。percent 必须是 None，**不能是 0**。

    ``raw_percent`` 与正常读数一样给出：前端会用 ``percent !== null``
    判断是否支持电量，用 ``raw_percent`` 显示设备原始值。结构保持一致，
    前端就不必写两套分支（缺字段会让 ``undefined`` 漏到界面上）。
    """
    return {
        "supported": False,
        "percent": None,
        "raw_percent": None,
        "source": SOURCE_MISSING,
        "source_label": SOURCE_LABEL[SOURCE_MISSING],
        "voltage_mv": None,
        "granularity": None,
        "status_text": "",
        "note": note or "设备未上报电量",
    }


def make_reading(percent, source, voltage_mv=None, status_text="",
                 granularity=None, note=""):
    """组装一条标准读数。percent 允许 None（表示这次没读到，但能力是支持的）。

    ``raw_percent`` 与 ``percent`` 同值：设备报 5% 粒度时我们**不伪造**
    更细的抖动，直接把原始值原样存进 raw_percent，前端要显示「设备粒度
    5%」时读它。``clamp_percent`` 已经是 int，这里不再二次取整。
    """
    pct = clamp_percent(percent) if percent is not None else None
    return {
        "supported": pct is not None,
        "percent": pct,
        "raw_percent": pct,
        "source": source if pct is not None else SOURCE_MISSING,
        "source_label": SOURCE_LABEL.get(source, SOURCE_LABEL[SOURCE_MISSING])
                        if pct is not None else SOURCE_LABEL[SOURCE_MISSING],
        "voltage_mv": int(voltage_mv) if voltage_mv is not None else None,
        "granularity": granularity,
        "status_text": status_text or "",
        "note": note or "",
    }


def interpret_reading(feature_id, payload, curve=DEFAULT_CURVE,
                      median_filter=None, status_text=""):
    """把一条电量响应帧解释成标准读数。

    ``feature_id`` 是枚举到的特性 ID（0x1000 / 0x1001 / 0x1004）。
    ``payload`` 是 ``r[4:]`` —— 已经去掉报头的那段。

    判定顺序（官方规范优先，数值合理性兜底）：
      0x1000 → 优先当百分比（byte0），不合算就当毫伏
      0x1001 → 优先当毫伏（byte0..1），不合算就当百分比
      0x1004 → 按 unifiedBattery 解（byte0 百分比）

    「合算」的判据见 is_plausible_percent / is_plausible_voltage。
    这样即使型号/固件把两个特性的语义搞反，也不会显示离谱数字。
    """
    if not payload:
        return blank_reading("电量响应帧为空")

    pct_candidate = payload[0]
    mv_candidate = ((payload[0] << 8) | payload[1]) if len(payload) >= 2 else None

    if feature_id == 0x1004:
        # unifiedBattery：byte0 是百分比（v0 布局），解不出就不显示
        if is_plausible_percent(pct_candidate):
            return make_reading(pct_candidate, SOURCE_REPORTED,
                                status_text=status_text)
        return blank_reading("unifiedBattery 回报的数值不在 0–100（原始 %02X）"
                             % pct_candidate)

    # 0x1000 优先百分比，0x1001 优先毫伏
    prefer_percent = (feature_id == 0x1000)

    if prefer_percent:
        if is_plausible_percent(pct_candidate):
            return make_reading(pct_candidate, SOURCE_REPORTED,
                                status_text=status_text)
        if mv_candidate is not None and is_plausible_voltage(mv_candidate):
            return _from_voltage(mv_candidate, curve, median_filter, status_text)
    else:
        if mv_candidate is not None and is_plausible_voltage(mv_candidate):
            return _from_voltage(mv_candidate, curve, median_filter, status_text)
        if is_plausible_percent(pct_candidate):
            return make_reading(pct_candidate, SOURCE_REPORTED,
                                status_text=status_text)

    return blank_reading("电量帧解不出合理值（特性 0x%04X，原始 %s）"
                         % (feature_id, " ".join("%02X" % b for b in payload[:4])))


def _from_voltage(mv, curve, median_filter, status_text):
    filtered = median_filter.push(mv) if median_filter is not None else mv
    pct = voltage_to_percent(filtered, curve)
    if pct is None:
        return blank_reading("电压换算失败（%s mV）" % mv)
    note = "由电压换算（滤波后 %d mV，原始 %d mV）" % (filtered, mv)
    reading = make_reading(pct, SOURCE_ESTIMATED, voltage_mv=mv,
                           status_text=status_text, note=note)
    # 估算值的 tooltip 一定要能拿到原始电压，所以这里保留两个值
    reading["filtered_mv"] = filtered
    return reading


def is_plausible_percent(v):
    """0..100 才算合法百分比。**注意 0 也算合法**（真的没电了）。"""
    try:
        return 0 <= int(v) <= 100
    except (TypeError, ValueError):
        return False


def is_plausible_voltage(mv):
    """毫伏的合理区间。AA 电池满电 1.6V、耗尽 0.9V，留点余量。"""
    try:
        mv = int(mv)
    except (TypeError, ValueError):
        return False
    return VOLTAGE_MIN_MV <= mv <= VOLTAGE_MAX_MV


# ---------------------------------------------------------------- 粒度判定

class GranularityTracker(object):
    """推断设备本身报电量的粒度（1% 还是 5%）。

    做法：记录历次**不同的**读数，求相邻差值的最大公约数近似值。
    只要出现过 1 的差值，粒度就是 1；全是 5 的倍数、且至少出现过两次
    变化，粒度就是 5。样本不够时返回 None（界面不显示粒度标注）。
    """

    def __init__(self):
        self._values = []

    def observe(self, percent):
        if percent is None:
            return
        p = clamp_percent(percent)
        if not self._values or self._values[-1] != p:
            self._values.append(p)
            if len(self._values) > 64:
                del self._values[:len(self._values) - 64]

    def granularity(self):
        """返回 1 / 5 / 10，或 None（样本不足）。"""
        if len(self._values) < 3:
            return None
        diffs = [abs(b - a) for a, b in zip(self._values, self._values[1:])]
        diffs = [d for d in diffs if d > 0]
        if not diffs:
            return None
        if any(d % 5 for d in diffs):
            return 1
        # 差值全是 5 的倍数：再确认是不是 5（而不是 10）
        if any(d == 5 for d in diffs):
            return 5
        if all(d % 10 == 0 for d in diffs):
            return 10
        return 5 if all(d % 5 == 0 for d in diffs) else 1


# ---------------------------------------------------------------- 颜色分档

def level_of(percent, levels=None):
    """百分比 → 分档名（good / low / critical / empty）。levels 来自配置。"""
    cfg = levels or {}
    good = int(cfg.get("good", 50))
    low = int(cfg.get("low", 20))
    critical = int(cfg.get("critical", 5))
    if percent is None:
        return "unknown"
    p = clamp_percent(percent)
    if p >= good:
        return "good"
    if p >= low:
        return "low"
    if p >= critical:
        return "critical"
    return "empty"


def main():
    """命令行自检：打几张曲线的关键点，确认换算没有跳变。"""
    print("碱性 AA 曲线：")
    for mv in (1600, 1500, 1450, 1400, 1300, 1200, 1100, 1000, 950, 900, 800):
        print("  %4d mV -> %3d%%" % (mv, voltage_to_percent(mv, "alkaline")))
    print("镍氢 AA 曲线：")
    for mv in (1400, 1300, 1250, 1200, 1150, 1100, 1050, 1000, 900):
        print("  %4d mV -> %3d%%" % (mv, voltage_to_percent(mv, "nimh")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
