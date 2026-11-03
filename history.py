# -*- coding: utf-8 -*-
"""电量历史 —— 采样、落盘、三档聚合

数据结构（``battery_history.json``，走 ``apppath.app_dir()``）：

    {
      "version": 1,
      "devices": {
        "<pid>_<unit_id>": {
          "name": "M350 Wireless Mouse (PID B021)",
          "kind": "mouse",
          "samples": [[ts, percent, voltage_mv?], ...],
          "baseline": {"ts": ..., "percent": 90},     # 上次换电池后的基线
          "battery_changes": [ts, ...]                # 换电池标记（画竖线用）
        }
      }
    }

``ts`` 是 Unix 秒（int 或 float），``percent`` 是 int 0..100，
``voltage_mv`` 可缺省（设备不上报电压时就没有）。

三条规则（都是需求里点名要的，别改）：

  1. **落样判据**：整数变化 ≥1% 就落一条；另每 15 分钟补一条心跳样，
     心跳只用来撑时间轴，不参与耗电计算。
  2. **换电池**：向上跳 > 20%，记一次 ``battery_changes`` 并把基线重置到
     新值。这样「用电量」永远不会算出负数。
  3. **脏帧**：单次跳变 > 15% 且下一拍不确认（没有回到附近的值）的样本
     丢弃。HID++ 偶尔会回一帧错位的电量，不滤掉会在曲线上戳出一个尖刺。

保留 32 天、单设备上限 20000 条；超了按时间抽稀（等间隔抽，保留首尾）。
写盘用「临时文件 + os.replace」；写失败降级为纯内存并在界面提示一句中文。
"""

import os
import sys
import json
import time

import apppath

# ---------------------------------------------------------------- 常量

HISTORY_NAME = "battery_history.json"
HISTORY_VERSION = 1

#: 保留天数。需求要求 32 天（30 日视图 + 2 天余量）。
KEEP_DAYS = 32

#: 单设备样本上限。20000 条 ≈ 32 天里每 2.3 分钟一条，足够细了。
MAX_SAMPLES = 20000

#: 心跳采样间隔（秒）。只影响时间轴密度，不影响耗电统计。
HEARTBEAT_SEC = 15 * 60

#: 落样阈值：整数变化 ≥ 1% 才落。**不要改成 5**（需求 Q2 明确禁止）。
MIN_DELTA_PERCENT = 1

#: 换电池判定：向上跳超过这个百分比。
BATTERY_CHANGE_DELTA = 20

#: 脏帧判定：单次跳变超过这个百分比、且下一拍不确认，就丢弃。
SPIKE_DELTA = 15

#: 判定「下一拍确认」的时间窗（秒）与容差（百分比）。
CONFIRM_WINDOW_SEC = 120
CONFIRM_TOLERANCE = 5

#: 三档聚合的桶宽（秒）与时间跨度（秒）。
RANGES = {
    "24h": {"span": 24 * 3600, "bucket": 5 * 60},
    "7d": {"span": 7 * 24 * 3600, "bucket": 3600},
    "30d": {"span": 30 * 24 * 3600, "bucket": 24 * 3600},
}


# ---------------------------------------------------------------- 设备键

def device_key(pid, unit_id="", index=0):
    """设备唯一键：``pid + unit_id``；取不到 unit_id 时退化为 ``pid + index``。

    为什么要这个键：换 USB 口时 device index 会变，但 unit_id 是设备自己
    的序列号级标识，不变。用 unit_id 当键，历史才不会因为换个口就断掉。
    """
    pid_s = ("%04X" % int(pid)) if pid else "0000"
    uid = (unit_id or "").strip().upper()
    if uid:
        return "%s_%s" % (pid_s, uid)
    return "%s_i%d" % (pid_s, int(index or 0))


# ---------------------------------------------------------------- 存储

class HistoryStore(object):
    """电量历史的内存副本 + 落盘。

    线程模型：采集线程调 ``add_sample``，HTTP/UI 线程调 ``aggregate``。
    两边都用 ``self._lock``，锁的粒度是「整次操作」，因为这些操作都很短
    （一次 append 或一次线性扫描），不需要更细的锁。
    """

    def __init__(self, path=None, clock=None):
        self.path = path or os.path.join(apppath.app_dir(), HISTORY_NAME)
        self._clock = clock or time.time
        self._lock = __import__("threading").Lock()
        self.devices = {}
        self.readonly = False
        self._last_error = ""
        self._load()

    # ---- 读写
    def _load(self):
        data = apppath._read_json(self.path)
        if not data or not isinstance(data, dict):
            self.devices = {}
            return
        devs = data.get("devices")
        if isinstance(devs, dict):
            cleaned = {}
            for key, val in devs.items():
                if not isinstance(val, dict):
                    continue
                samples = val.get("samples")
                if not isinstance(samples, list):
                    samples = []
                cleaned[key] = {
                    "name": val.get("name") or "",
                    "kind": val.get("kind") or "unknown",
                    "samples": [s for s in samples if _valid_sample(s)],
                    "baseline": val.get("baseline") if isinstance(
                        val.get("baseline"), dict) else None,
                    "battery_changes": [t for t in (val.get("battery_changes") or [])
                                        if isinstance(t, (int, float))],
                }
            self.devices = cleaned
        else:
            self.devices = {}

    def save(self):
        """原子写。失败时 ``readonly`` 置 True，调用方据此在界面提示。"""
        with self._lock:
            payload = {
                "version": HISTORY_VERSION,
                "devices": self.devices,
            }
        ok = apppath._write_json(self.path, payload)
        self.readonly = not ok
        if not ok:
            self._last_error = "历史文件不可写：%s" % self.path
        return ok

    @property
    def last_error(self):
        return self._last_error

    # ---- 采样
    def add_sample(self, key, percent, name="", kind="unknown",
                   voltage_mv=None, timestamp=None, curve_label=""):
        """落一条样本。返回 ``(action, info)``，动作是 added/skipped/...

        判定顺序（重要，别调换）：
          1. 换电池：向上跳 > 20% → 记标记 + 重置基线 + 落样
          2. 脏帧：与上一条差 > 15% 且下一拍不确认 → skip_spike
          3. 变化 ≥ 1% → 落样
          4. 距上一条 ≥ 15 分钟 → 落心跳样
          5. 其余 → skip

        为什么「换电池」必须排在「脏帧」前面：向上跳 >20% 是 >15% 的
        子集，脏帧先判的话换电池分支永远进不去（实测就是这么发现的 ——
        20% → 95% 一直返回 skipped_spike，battery_changes 永远为空，
        图表上也永远画不出换电池竖线）。
        """
        if percent is None:
            return "skipped", None
        pct = int(percent)
        if pct < 0 or pct > 100:
            return "skipped", None
        ts = float(timestamp if timestamp is not None else self._clock())

        with self._lock:
            dev = self.devices.get(key)
            if dev is None:
                dev = {"name": "", "kind": "unknown", "samples": [],
                       "baseline": None, "battery_changes": []}
                self.devices[key] = dev
            if name:
                dev["name"] = name
            if kind and kind != "unknown":
                dev["kind"] = kind
            samples = dev["samples"]

            last = samples[-1] if samples else None
            last_pct = last[1] if last else None
            last_ts = float(last[0]) if last else None

            # 1) 换电池：向上跳（必须先于脏帧判定，见 docstring）
            if last_pct is not None and (pct - last_pct) > BATTERY_CHANGE_DELTA:
                dev["battery_changes"].append(int(ts))
                dev["baseline"] = {"ts": int(ts), "percent": pct}
                samples.append(_sample(ts, pct, voltage_mv))
                self._prune(dev)
                return "battery_change", {"from": last_pct, "to": pct}

            # 2) 脏帧：跳得太狠，且留不出时间让下一拍确认
            if last_pct is not None and abs(pct - last_pct) > SPIKE_DELTA:
                if not _has_later_sample(samples, ts, pct, CONFIRM_WINDOW_SEC,
                                         CONFIRM_TOLERANCE):
                    return "skipped_spike", {"last": last_pct, "new": pct}

            # 3) 变化足够
            if last_pct is None or abs(pct - last_pct) >= MIN_DELTA_PERCENT:
                samples.append(_sample(ts, pct, voltage_mv))
                self._prune(dev)
                return "added", {"from": last_pct, "to": pct}

            # 4) 心跳
            if last_ts is not None and (ts - last_ts) >= HEARTBEAT_SEC:
                samples.append(_sample(ts, pct, voltage_mv))
                self._prune(dev)
                return "heartbeat", {"percent": pct}

            return "skipped", None

    def _prune(self, dev):
        """按时间抽稀到 MAX_SAMPLES 以内（保留首尾，等间隔取）。"""
        samples = dev["samples"]
        if len(samples) <= MAX_SAMPLES:
            return
        dev["samples"] = thin_samples(samples, MAX_SAMPLES)

    # ---- 读取
    def device_summary(self, key):
        with self._lock:
            dev = self.devices.get(key)
            if not dev:
                return None
            return {
                "key": key,
                "name": dev.get("name") or "",
                "kind": dev.get("kind") or "unknown",
                "count": len(dev.get("samples") or []),
                "battery_changes": len(dev.get("battery_changes") or []),
            }

    def aggregate(self, key, range_name="24h", now=None):
        """按档位聚合。返回 ``{points, changes, today_used, daily_avg, ...}``。"""
        spec = RANGES.get(range_name) or RANGES["24h"]
        now = float(now if now is not None else self._clock())
        start = now - spec["span"]
        bucket = spec["bucket"]

        with self._lock:
            dev = self.devices.get(key)
            if not dev:
                return empty_aggregate(range_name)
            samples = [s for s in dev["samples"] if float(s[0]) >= start]
            changes = [t for t in (dev.get("battery_changes") or [])
                       if float(t) >= start]

        if not samples:
            return empty_aggregate(range_name)

        # 按桶取「桶内最后一个值」，这样曲线跟真实走势一致（不是平均值，
        # 平均值会把缓慢下降抹平，看起来像一条水平线）
        buckets = {}
        for s in samples:
            idx = int((float(s[0]) - start) // bucket)
            buckets[idx] = s

        points = []
        for idx in sorted(buckets):
            s = buckets[idx]
            points.append({
                "t": int(float(s[0])),
                "pct": int(s[1]),
                "mv": s[2] if len(s) > 2 and s[2] is not None else None,
            })

        used = compute_used(samples)
        daily = compute_daily_average(samples, now)

        return {
            "range": range_name,
            "bucket_sec": bucket,
            "start": int(start),
            "end": int(now),
            "points": points,
            "changes": sorted(int(t) for t in changes),
            "today_used": used,
            "daily_avg": daily,
            "sample_count": len(samples),
            "first_pct": int(samples[0][1]),
            "last_pct": int(samples[-1][1]),
        }


def _valid_sample(s):
    """样本结构校验：[ts, pct] 或 [ts, pct, mv]。"""
    if not isinstance(s, (list, tuple)) or len(s) < 2:
        return False
    try:
        float(s[0])
        pct = int(s[1])
    except (TypeError, ValueError):
        return False
    return 0 <= pct <= 100


def _sample(ts, pct, voltage_mv=None):
    if voltage_mv is None:
        return [int(ts), int(pct)]
    return [int(ts), int(pct), int(voltage_mv)]


def _has_later_sample(samples, ts, pct, window, tolerance):
    """在这个时间点之后 window 秒内，有没有一条「与 pct 相差不超过 tolerance」
    的样本？有就说明这是个真实跳变（不是脏帧）。

    注意：调用的时候新样本还没落进列表，所以只能往后看历史里已有的。
    """
    for s in samples:
        t = float(s[0])
        if t <= ts:
            continue
        if t - ts > window:
            break
        if abs(int(s[1]) - pct) <= tolerance:
            return True
    return False


def thin_samples(samples, limit):
    """等间隔抽稀，保留首尾。样本已排序。"""
    n = len(samples)
    if n <= limit or limit < 3:
        return list(samples)
    step = (n - 1) / float(limit - 1)
    out = [samples[0]]
    for i in range(1, limit - 1):
        out.append(samples[int(round(i * step))])
    out.append(samples[-1])
    # 去重（四舍五入可能撞到同一个下标）
    dedup = []
    for s in out:
        if not dedup or dedup[-1] is not s:
            dedup.append(s)
    return dedup


def compute_used(samples):
    """用电量 = 期间向下累计的百分比变化（忽略向上跳变）。

    换电池（向上跳 > 20%）不产生负贡献，因为向上的一律不计。
    返回 int 百分比（向下取整，宁可少报不多报）。
    """
    if not samples or len(samples) < 2:
        return 0
    total = 0
    prev = int(samples[0][1])
    for s in samples[1:]:
        cur = int(s[1])
        if cur < prev:
            total += prev - cur
        prev = cur
    return int(total)


def compute_daily_average(samples, now=None):
    """日均用电量 = 总消耗 / 覆盖天数（至少按 1 天算，避免放大）。"""
    if not samples or len(samples) < 2:
        return 0
    now = float(now if now is not None else time.time())
    span = now - float(samples[0][0])
    days = max(1.0, span / 86400.0)
    return int(round(compute_used(samples) / days))


def empty_aggregate(range_name="24h"):
    return {
        "range": range_name,
        "bucket_sec": RANGES.get(range_name, RANGES["24h"])["bucket"],
        "start": 0,
        "end": 0,
        "points": [],
        "changes": [],
        "today_used": 0,
        "daily_avg": 0,
        "sample_count": 0,
        "first_pct": None,
        "last_pct": None,
    }


def purge_old(store, now=None, keep_days=KEEP_DAYS):
    """删掉超过 keep_days 的样本与换电池标记。返回删掉的条数。"""
    now = float(now if now is not None else time.time())
    cutoff = now - keep_days * 86400
    removed = 0
    with store._lock:
        for dev in store.devices.values():
            samples = dev.get("samples") or []
            kept = [s for s in samples if float(s[0]) >= cutoff]
            removed += len(samples) - len(kept)
            dev["samples"] = kept
            changes = dev.get("battery_changes") or []
            dev["battery_changes"] = [t for t in changes if float(t) >= cutoff]
    return removed


def track_snapshot(store, devices, now=None):
    """把一次「设备快照列表」喂进历史库。返回本次发生的事件列表。

    ``devices`` 是 devices.py 产出的列表，每项至少要有 key / name / kind /
    battery.percent。这个函数是采集线程调用的唯一入口 —— 别的地方不要
    直接写 store，否则换电池判定会被绕过。
    """
    events = []
    now = float(now if now is not None else time.time())
    for d in devices or []:
        bat = d.get("battery") or {}
        pct = bat.get("percent")
        if pct is None:
            continue
        key = d.get("key") or ""
        if not key:
            continue
        action, info = store.add_sample(
            key, pct,
            name=d.get("display_name") or d.get("name") or "",
            kind=d.get("kind") or "unknown",
            voltage_mv=bat.get("voltage_mv"),
            timestamp=now,
        )
        if action in ("added", "battery_change", "heartbeat"):
            events.append({"key": key, "action": action, "info": info})
    return events


def main():
    """命令行自检：造几条样本跑一遍聚合，确认没有负消耗。"""
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(prefix="hist-"), HISTORY_NAME)
    st = HistoryStore(path=tmp)
    base = 1793000000.0
    seq = [(0, 90), (60, 89), (120, 88), (180, 87)]
    for i, (dt, pct) in enumerate(seq):
        print("add", pct, "->", st.add_sample("B021_4080CFB3", pct,
                                              name="M350", kind="mouse",
                                              timestamp=base + dt))
    # 换电池
    print("swap ->", st.add_sample("B021_4080CFB3", 100, timestamp=base + 240))
    print("used =", st.aggregate("B021_4080CFB3", "24h", now=base + 300))
    st.save()
    print("saved to", tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
