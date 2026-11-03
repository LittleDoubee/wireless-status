# -*- coding: utf-8 -*-
"""设备抽象层 —— 为将来的耳机 / 手柄预留，本轮只实现 HID++ 鼠标

为什么单独抽一层：现在只有鼠标，但如果把「设备列表 → 功能区」直接写死在
鼠标上，将来加耳机就得改前端、改服务端、改历史统计三处。这个模块把
「有哪些设备、每个设备长什么样、能力如何」抽象成一个稳定的 dict 结构，
前端只认这个结构，不认鼠标。

接口（``DeviceProvider``）：

  * ``discover()``     —— 枚举设备，返回 ``[device, ...]``
  * ``snapshot()``     —— 返回 ``{"devices": [...], "error": ..., "hint": ...}``
  * ``capabilities()`` —— 这个 provider 支持哪些能力（dpi / battery / usage…）

设备 dict（前端契约，改这里要同步改 app.js）：

    {
      "key":          "B021_4080CFB3",       # pid + unit_id（换 USB 口不丢历史）
      "kind":         "mouse",               # mouse|headset|gamepad|unknown
      "icon":         "mouse",               # icon/<name>.svg
      "name":         "M350 Wireless Mouse",
      "brand":        "罗技",                # 品牌（VID 046D 推出来）
      "display_name": "罗技 M350 Wireless Mouse",   # 品牌 + 型号，不带 PID
      "pid":          "B021",
      "unit_id":      "4080CFB3",
      "index":        1,                     # 接收器上的 device index
      "connection":   {"kind": "usb_receiver", "label": "USB 接收器"},
      "status":       "online",              # online|asleep|offline
      "status_text":  "在线",
      "battery":      { ...battery.make_reading 的结构... },
      "dpi":          {supported, value, min, max, step, inferred, default},
      "level":        "good",                # 电量分档，配色用
    }

本轮 ``HidppProvider`` 直接复用 ``mouse_source.py`` 已经验证过的接收器枚举
与长短报文收发（report ID 0x10/0x11、device index 1–6），**不重写通信层**。
"""

import os
import sys

import battery
import mouse_source

# ---------------------------------------------------------------- 设备种类

KIND_MOUSE = "mouse"
KIND_HEADSET = "headset"
KIND_GAMEPAD = "gamepad"
KIND_UNKNOWN = "unknown"

#: 种类 → 图标文件名（不含扩展名）。图标缺失时前端走内联 SVG 兜底。
KIND_ICON = {
    KIND_MOUSE: "mouse",
    KIND_HEADSET: "headset",
    KIND_GAMEPAD: "gamepad",
    KIND_UNKNOWN: "mouse",
}

#: 品牌。目前只支持罗技（HID++ 走的就是它的私有协议），所以固定值；
#: 将来要支持别的厂商，这里改成按 VID 查表即可。
BRAND_LOGITECH = "罗技"

#: 需要从型号名里剥掉的缩写（大小写不敏感），避免出现「罗技 罗技 M350」
_BRAND_PREFIXES = ("logitech", "罗技", "logi")


def display_name_of(name, pid=""):
    """型号展示串：**品牌 + 型号**，不带 PID。

    需求明确要求列表页和详情页都不显示 PID 之类的技术串，所以这里
    统一产出 ``罗技 M350 Wireless Mouse``。

    三条规则：
      * 名字为空 → 退回 ``罗技 鼠标（PID xxxx）`` 之外的**无技术串**写法：
        ``罗技 未知型号``。PID 只留在日志和 /api 里排查用，不上界面。
      * 名字本身已经带品牌前缀（``Logitech M350``）→ 不重复加。
      * 其余情况 → ``罗技 `` + 原名。
    """
    raw = (name or "").strip()
    if not raw:
        return "%s 未知型号" % BRAND_LOGITECH
    low = raw.lower()
    for prefix in _BRAND_PREFIXES:
        if low.startswith(prefix):
            # 原型是「ロジクール」之类也当已带品牌，直接返回原名
            return raw
    return "%s %s" % (BRAND_LOGITECH, raw)


KIND_LABEL = {
    KIND_MOUSE: "鼠标",
    KIND_HEADSET: "耳机",
    KIND_GAMEPAD: "手柄",
    KIND_UNKNOWN: "设备",
}

#: 产品名里的关键词 → 种类。只做**明显的**判断，判不出来的算 mouse
#: （因为本轮只有鼠标，误判成耳机反而更糟）。
KIND_HINTS = (
    (KIND_HEADSET, ("headset", "headphone", "earbud", "zone ", "g pro x 2 light",
                    "耳机", "耳麦")),
    (KIND_GAMEPAD, ("gamepad", "controller", "手柄", "f310", "f710", "f920")),
    (KIND_MOUSE, ("mouse", "mice", "鼠标", "trackball", "ergo")),
)


def guess_kind(name, pids=None):
    """按产品名猜种类。判不出来返回 mouse（本轮只有鼠标）。"""
    text = (name or "").lower()
    for kind, words in KIND_HINTS:
        for w in words:
            if w in text:
                return kind
    return KIND_MOUSE


# ---------------------------------------------------------------- 接口

class DeviceProvider(object):
    """设备来源接口。本轮只有 HID++ 一个实现，方法签名先定下来。"""

    name = "base"

    def discover(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def capabilities(self):
        raise NotImplementedError


# ---------------------------------------------------------------- HID++

class HidppProvider(DeviceProvider):
    """罗技接收器（Nano / Unifying / Bolt）上的 HID++ 2.0 设备。

    这是**唯一**碰硬件的地方。``mouse_source.MouseSource`` 本来就已经做了
    后台线程 + 轮询 + 写队列，这里不重复造，只是把它吐出来的单设备快照
    翻译成「多设备列表」。
    """

    name = "hidpp"

    def __init__(self, source=None, curve="alkaline", levels=None):
        # 复用已验证的采集线程；interval 之类由 main.py 传进来
        self.source = source or mouse_source.MouseSource()
        self.curve = curve
        self.levels = levels or {}
        self._granularity = {}      # key -> battery.GranularityTracker
        self._medians = {}          # key -> battery.MedianFilter
        self._last = {}             # key -> 上一次的 device dict（掉线时保留显示）

    # ---- 对外
    def discover(self):
        """触发一次立刻采集，并把结果整理成设备列表。

        注意：「发现」和「读快照」在这里是同一件事 —— HID++ 的 device index
        只有在 ping 之后才知道有没有设备。所以 discover() 就是
        ``source.request_refresh()`` + 等一拍 + ``snapshot()``。
        """
        self.source.request_refresh()
        return self.snapshot().get("devices") or []

    def snapshot(self):
        raw = self.source.snapshot()
        devices = self._translate(raw)
        return {
            "devices": devices,
            "error": raw.get("error") or "",
            "hint": raw.get("hint") or "",
            "ok": bool(raw.get("ok")),
            "severity": raw.get("severity") or "info",
            "receiver": raw.get("receiver") or {},
            "updated": raw.get("updated") or 0,
            "log": raw.get("log") or [],
        }

    def capabilities(self):
        """这个 provider 全量支持的能力。单个设备再看它自己的字段。"""
        return {
            "dpi": True,
            "battery": True,
            "battery_voltage": True,
            "usage": True,
            "multi_device": True,
            "transports": ["usb_receiver", "usb", "unifying", "bolt", "bluetooth"],
        }

    # ---- 内部：把 MouseSource 的快照翻译成设备列表
    def _translate(self, raw):
        status = raw.get("status") or "offline"
        receiver = raw.get("receiver") or {}

        if status == "offline":
            # 离线时把上次见过的设备还回去，标成 offline，这样列表不会突然
            # 空一片 —— 用户更希望看到「设备在，但当前离线」。
            out = []
            for key, dev in self._last.items():
                d = dict(dev)
                d["status"] = "offline"
                d["status_text"] = "离线"
                d["battery"] = dict(d.get("battery") or battery.blank_reading())
                d["battery"]["supported"] = False
                d["battery"]["percent"] = None
                d["battery"]["source"] = battery.SOURCE_MISSING
                d["battery"]["source_label"] = battery.SOURCE_LABEL[battery.SOURCE_MISSING]
                d["level"] = "unknown"
                out.append(d)
            out.sort(key=lambda x: x.get("index") or 0)
            return out

        raws = raw.get("devices") or []
        out = []
        for item in raws:
            idx = item.get("index")
            if not idx:
                continue
            current = bool(item.get("current"))
            # 每台设备都可能带自己的 name/pid/unit_id/battery（采集线程已经把
            # 所有在线设备都 probe 过了，见 mouse_source._peer_for）；老格式
            # 只有 current 那台有字段、其余字段在顶层，所以两种都要认。
            if current or item.get("battery"):
                dev = self._build(raw, idx, receiver, status, item)
            else:
                key = "%s_i%d" % ((raw.get("receiver") or {}).get("pid") or "0000", idx)
                dev = self._last.get(key) or self._placeholder(idx, receiver, status)
            out.append(dev)
            self._last[dev["key"]] = dev
        out.sort(key=lambda x: x.get("index") or 0)
        return out

    def _build(self, raw, idx, receiver, status, item=None):
        item = item or {}
        # 优先用 per-item 字段；没有就退回快照顶层（单设备的老格式）
        name = item.get("name") or raw.get("name") or ""
        pid = item.get("pid") or raw.get("pid") or ""
        unit_id = item.get("unit_id") or raw.get("unit_id") or ""
        key = _key_of(pid, unit_id, idx)

        # 展示名 = 品牌 + 型号，**不带 PID**（需求 4）。
        # 名字读不到时也不能把 PID 摆到界面上，只降级成「罗技 未知型号」；
        # 真实的 PID / 接收器信息仍留在 dev["pid"] 与日志里供排查。
        display = display_name_of(name, pid)

        kind = guess_kind(name)

        # 电量：raw 里已经是解好的结构（mouse_source 用 battery 模块解的），
        # 这里补上 1% 粒度追踪与颜色分档。
        bat = dict(item.get("battery") or raw.get("battery") or battery.blank_reading())
        tracker = self._granularity.get(key)
        if tracker is None:
            tracker = battery.GranularityTracker()
            self._granularity[key] = tracker
        tracker.observe(bat.get("percent"))
        gran = tracker.granularity()
        if gran is not None:
            bat["granularity"] = gran
        bat["level"] = battery.level_of(bat.get("percent"), self.levels)

        dev = {
            "key": key,
            "kind": kind,
            "icon": KIND_ICON.get(kind, "mouse"),
            "kind_label": KIND_LABEL.get(kind, "设备"),
            "name": name,
            "brand": BRAND_LOGITECH,
            "display_name": display,
            "pid": pid,
            "unit_id": unit_id,
            "index": idx,
            "connection": item.get("connection") or raw.get("connection") or {},
            "status": status if status == "online" else status,
            "status_text": raw.get("status_text") or ("在线" if status == "online" else status),
            "battery": bat,
            "dpi": raw.get("dpi") or {},
            "level": bat.get("level") or "unknown",
        }
        return dev

    def _placeholder(self, idx, receiver, status):
        """非选中设备：字段拿不全，但列表里仍然要把这一行画出来。"""
        key = "%s_i%d" % (receiver.get("pid") or "0000", idx)
        return {
            "key": key,
            "kind": KIND_UNKNOWN,
            "icon": KIND_ICON[KIND_UNKNOWN],
            "kind_label": KIND_LABEL[KIND_UNKNOWN],
            "name": "",
            "display_name": display_name_of(""),
            "pid": "",
            "unit_id": "",
            "index": idx,
            "connection": {"kind": "usb_receiver", "label": "USB 接收器",
                           "device_index": idx},
            "status": "asleep" if status == "asleep" else "offline",
            "status_text": "休眠" if status == "asleep" else "离线",
            "battery": battery.blank_reading("未选中该设备，未读取电量"),
            "dpi": {},
            "level": "unknown",
        }


def _key_of(pid, unit_id, index):
    """设备键：pid + unit_id；没有 unit_id 就退化成 pid + index。"""
    return history_key(pid, unit_id, index)


def history_key(pid, unit_id, index):
    """把 history.device_key 引出来，避免两个模块各自拼一套格式。"""
    import history
    return history.device_key(_pid_int(pid), unit_id, index)


def _pid_int(pid):
    """``"B021"`` → 0xB021；已经 int 或空串都安全。"""
    if isinstance(pid, int):
        return pid
    text = (pid or "").strip()
    if not text:
        return 0
    try:
        return int(text, 16)
    except ValueError:
        return 0


def find_device(devices, key):
    for d in devices or []:
        if d.get("key") == key:
            return d
    return None


def main():
    """命令行自检：枚举一次并按列表格式打印。"""
    import json
    provider = HidppProvider()
    provider.source.start()
    import time
    for _ in range(30):
        snap = provider.snapshot()
        if snap.get("devices"):
            break
        time.sleep(0.3)
    print(json.dumps(provider.snapshot(), ensure_ascii=False, indent=2)[:2000])
    provider.source.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
