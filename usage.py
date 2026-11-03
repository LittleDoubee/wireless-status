# -*- coding: utf-8 -*-
"""使用时长统计 —— 回答「今天用了多久」

口径（界面上也要写这一句）：**光标移动或按键（500ms 采样）**。

具体规则：

  * 独立线程每 500ms 采一次 ``GetCursorPos`` + ``GetAsyncKeyState``（左中右键）。
    位置变了或任一键按下，就算「这一拍是活跃的」。
  * 连续 **60 秒** 没有任何活跃，结束一个活跃段。也就是说最后 60 秒会
    在段结束后才补记（否则界面上的数字会跳）。
  * 跨 00:00 自动结转：一段活跃如果跨了午夜，会按午夜切成两段分别记账。
  * **休眠唤醒要校正**：休眠期间采样线程不跑，醒来后两次采样的时间差会
    很大。如果两次采样间隔 > ``SLEEP_GAP_SEC``，这段间隔里只按「最后一拍
    之后到入眠前」的合理部分计入，其余（也就是睡眠本身）全部丢弃 ——
    绝不能把 8 小时睡眠算成 8 小时使用。

为什么不直接用 ``GetLastInputInfo``：那个只给「最后一次输入距今多久」，
能算出空闲时长，但算不出「今天累计用了多久」（中间可能用了十次）。
两者配合也用不上，直接自己采样最干净。

落盘 ``usage_stats.json``（走 ``apppath.app_dir()``）：

    {"version": 1, "days": {"2026-02-14": {"active_seconds": 1234,
                                           "last_active": "2026-02-14T21:03:11"}}}

保留 30 天。
"""

import os
import sys
import json
import time
import datetime
import threading

import apppath

# ---------------------------------------------------------------- 常量

USAGE_NAME = "usage_stats.json"
USAGE_VERSION = 1

#: 采样间隔（毫秒）。500ms 是需求指定的；再快没意义（光标本就 125–1000Hz，
#: 但我们要的是「人还在不在」，不是精确计步）。
SAMPLE_MS = 500

#: 连续这么久没有活跃，就结束一个活跃段（秒）。
#: 需求：**停手 5 秒就该停表**。以前是 60 秒，用户看到的是「手停了数字
#: 还在爬，过一分钟突然不动」，与直觉不符。
IDLE_TIMEOUT_SEC = 5

#: 两次采样间隔超过这个值，认为中间发生过休眠/挂起，要校正（秒）。
SLEEP_GAP_SEC = 120

#: 保留天数。
KEEP_DAYS = 30

#: 落盘节流：活跃段结束或数值每涨这么多秒才写一次盘（减少 IO）。
SAVE_EVERY_SEC = 60


def _now_local():
    return datetime.datetime.now()


def day_key(dt=None):
    """``YYYY-MM-DD``（本地时区）。跨日结转全靠它。

    两种入参都收：``datetime`` 与 float 时间戳。``format_last_active`` 传的
    是时间戳，``_commit_segment`` 传的是 datetime —— 一开始只处理 datetime，
    时间戳传进来直接 ``AttributeError: 'float' object has no attribute
    'strftime'``（自测里就是这么炸出来的）。
    """
    if dt is None:
        dt = _now_local()
    elif isinstance(dt, (int, float)):
        dt = datetime.datetime.fromtimestamp(dt)
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 输入采样

def read_input_state():
    """读一次「光标位置 + 按键状态」。返回 ``((x, y), mask)``。

    非 Windows / 调用失败时返回 ``(None, 0)``，上层会把它当作「这一拍不活跃」，
    不会崩。
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        pt = wintypes.POINT()
        ok = user32.GetCursorPos(ctypes.byref(pt))
        pos = (int(pt.x), int(pt.y)) if ok else None

        mask = 0
        # VK_LBUTTON=0x01 / VK_RBUTTON=0x02 / VK_MBUTTON=0x04
        # GetAsyncKeyState 的最高位（0x8000）表示当前按下
        for vk, bit in ((0x01, 0x01), (0x02, 0x02), (0x04, 0x04)):
            try:
                if user32.GetAsyncKeyState(vk) & 0x8000:
                    mask |= bit
            except Exception:
                pass
        return pos, mask
    except Exception:
        return None, 0


# ---------------------------------------------------------------- 统计器

class UsageTracker(object):
    """使用时长统计。线程安全，采集线程写、UI/HTTP 线程读。

    内部只维护两样东西：
      * ``self.days``   —— {日期: {"active_seconds": int, "last_active": iso}}
      * ``self._segment_start`` —— 当前活跃段开始的时间戳（None 表示没在段里）
    """

    def __init__(self, path=None, clock=None, input_reader=None):
        self.path = path or os.path.join(apppath.app_dir(), USAGE_NAME)
        self._clock = clock or time.time
        self._reader = input_reader or read_input_state
        self._lock = threading.Lock()
        self.days = {}
        self.readonly = False
        self._last_error = ""

        self._last_pos = None
        self._last_mask = 0
        self._last_sample_ts = None
        self._segment_start = None      # 当前活跃段起点（None=空闲）
        self._last_active_ts = None     # 最近一次确认活跃的时刻
        self._segments = []             # [(start_ts, end_ts)] 已结束的段，待落账
        # 当前进行中的段已经记账到哪个时刻（只补差值，防止重复累加；
        # 段结束时必须归零，否则下一段会算出负增量）
        self._accounted_until = None
        self._unsaved = 0
        self._thread = None
        self._stop = threading.Event()
        self._load()

    # ---- 持久化
    def _load(self):
        data = apppath._read_json(self.path)
        if not data or not isinstance(data, dict):
            return
        days = data.get("days")
        if not isinstance(days, dict):
            return
        cleaned = {}
        for k, v in days.items():
            if not isinstance(v, dict):
                continue
            try:
                sec = float(v.get("active_seconds") or 0)
            except (TypeError, ValueError):
                sec = 0.0
            cleaned[k] = {
                "active_seconds": max(0.0, sec),
                "last_active": v.get("last_active") or "",
            }
        self.days = cleaned

    def save(self):
        # 落盘时才把浮点取整 —— 内部保留小数是为了让 500ms 采样能累积
        with self._lock:
            days = {}
            for k, v in self.days.items():
                days[k] = {
                    "active_seconds": int(round(float(v.get("active_seconds") or 0))),
                    "last_active": v.get("last_active") or "",
                }
            payload = {"version": USAGE_VERSION, "days": days}
        ok = apppath._write_json(self.path, payload)
        self.readonly = not ok
        if not ok:
            self._last_error = "使用时长文件不可写：%s" % self.path
        else:
            self._unsaved = 0
        return ok

    @property
    def last_error(self):
        return self._last_error

    # ---- 记账
    def _add_seconds(self, day, seconds, last_active_ts=None):
        """给某天加时长。

        **内部存浮点**：采样是 500ms 一拍，每次只加 0.5 秒，如果每次都
        int(round()) 就会被舍成 0，一整天下来时间全丢（实测：连续活跃
        30 秒，账上记 0 秒）。所以这里保留小数，只在落盘与显示时取整。
        """
        if seconds <= 0:
            return
        rec = self.days.setdefault(day, {"active_seconds": 0.0, "last_active": ""})
        rec["active_seconds"] = float(rec.get("active_seconds") or 0.0) + float(seconds)
        if last_active_ts is not None:
            rec["last_active"] = _iso_local(last_active_ts)

    def _commit_segment(self, start_ts, end_ts):
        """把一段活跃按午夜切开分别记账。"""
        if end_ts <= start_ts:
            return 0.0
        total = 0.0
        cur = start_ts
        while cur < end_ts:
            dt = datetime.datetime.fromtimestamp(cur)
            next_midnight = (dt.replace(hour=0, minute=0, second=0, microsecond=0)
                             + datetime.timedelta(days=1)).timestamp()
            chunk_end = min(end_ts, next_midnight)
            secs = chunk_end - cur
            if secs > 0:
                self._add_seconds(day_key(dt), secs, chunk_end)
                total += secs
            cur = chunk_end
        return total

    def tick(self, now=None):
        """采一次样并推进状态机。返回本次是否活跃（便于测试）。

        这个方法被线程循环调用，也可以被测试直接调 —— 所以时间从参数进。
        """
        now = float(now if now is not None else self._clock())
        pos, mask = self._reader()

        with self._lock:
            prev_ts = self._last_sample_ts
            prev_pos = self._last_pos
            prev_mask = self._last_mask
            self._last_sample_ts = now
            self._last_pos = pos
            self._last_mask = mask

            if prev_ts is None:
                # 第一拍只初始化，不知道该不该算活跃
                return False

            gap = now - prev_ts
            # 休眠校正：间隔异常大时，绝不把整段算成使用时间。
            # 只把「最后一拍到入眠前」那一点点计入（最多一个采样间隔 + 空闲阈值）。
            slept = gap > SLEEP_GAP_SEC

            moved = (pos is not None and prev_pos is not None and pos != prev_pos)
            pressed = bool(mask) or bool(prev_mask)
            active = (moved or pressed) and not slept

            if slept:
                # 截断当前活跃段：最多算到「最后一拍 + 空闲阈值」。
                # 睡眠本身（那 8 小时）绝不能计入。
                # 注意只补 _accounted_until 之后还没落账的那一截，
                # 否则 flush 已经记过的部分会被记第二遍。
                if self._segment_start is not None:
                    last = self._last_active_ts or self._segment_start
                    cut = min(prev_ts, last + IDLE_TIMEOUT_SEC)
                    pending_from = self._accounted_until or self._segment_start
                    if cut > pending_from:
                        self._segments.append((pending_from, cut))
                    self._segment_start = None
                    self._accounted_until = None
                self._last_active_ts = None
                return False

            if active:
                self._last_active_ts = now
                if self._segment_start is None:
                    # 新段从「上一拍」开始，避免第一拍被吃掉
                    self._segment_start = prev_ts
                    self._accounted_until = prev_ts
            else:
                # 空闲：若当前段已空闲超过阈值，就收段。
                # 收段末端算到「最后一次活跃 + 空闲阈值」，不是算到「最后
                # 一次活跃」—— 那样手一停数字立刻冻住，跟需求「停手 5 秒
                # 才停表」不符；算到 last+IDLE 则是先平稳走完这 5 秒再停。
                if self._segment_start is not None:
                    last = self._last_active_ts or self._segment_start
                    if now - last >= IDLE_TIMEOUT_SEC:
                        pending_from = self._accounted_until or self._segment_start
                        end = min(now, last + IDLE_TIMEOUT_SEC)
                        if end > pending_from:
                            self._segments.append((pending_from, end))
                        self._segment_start = None
                        self._accounted_until = None

            return active

    def flush_segments(self, now=None):
        """把攒着的段落账（并决定要不要写盘）。返回本次记了多少秒。

        记账分两块，关键在于**不重复累加**：
          * ``_segments`` 里是已经结束的段，整段记一次就丢；
          * 正在进行的段（``_segment_start``）每拍都要重算，所以用一个
            ``_accounted_until`` 记住「这段已经记到哪了」，只补差值。
        段结束时 ``_accounted_until`` 必须归零，否则下一段会拿上一段的
        进度去减，算出负增量 —— 时间就凭空丢掉了（自检第一次跑只记到
        1 秒就是这个 bug）。
        """
        now = float(now if now is not None else self._clock())
        with self._lock:
            segs, self._segments = self._segments, []
            total = 0
            for start, end in segs:
                total += self._commit_segment(start, end)

            if self._segment_start is not None:
                # 末端用 _live_end：手停后还能平滑走满空闲阈值，到点自停。
                live_end = self._live_end(now=now)
                already = self._accounted_until
                if already is None:
                    already = self._segment_start
                if live_end > already:
                    total += self._commit_segment(already, live_end)
                    self._accounted_until = live_end
            self._unsaved += total

        if self._unsaved >= SAVE_EVERY_SEC:
            self.save()
        return total

    def _close_segment(self):
        """关闭当前活跃段（调用方持锁）。段内的账已经由 flush 记过了。"""
        if self._segment_start is None:
            return
        # 与 today()/flush_segments() 同口径：末端算到「最后一次活跃 + 空闲阈值」
        end = self._live_end()
        if end > (self._accounted_until or self._segment_start):
            # 还有没记完的尾巴，补上
            self._commit_segment(self._accounted_until or self._segment_start, end)
        self._segment_start = None
        self._accounted_until = None

    # ---- 线程
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="usage-tracker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        # 收尾：先把「进行中的段」按已确认活跃的部分结账，再关闭段
        self.flush_segments()
        with self._lock:
            self._close_segment()
        self.save()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
                self.flush_segments()
            except Exception:
                # 统计线程绝不能让主程序崩
                pass
            self._stop.wait(SAMPLE_MS / 1000.0)

    # ---- 查询
    def _live_end(self, now=None):
        """当前活跃段的「可计入末端」（调用方持锁）。

        统一口径：**最后一次活跃 + 空闲阈值**，但不超过现在。
        这样「手停下之后数字还能平滑走满 5 秒」，到点就停，不会像以前那样
        先冻一分钟、再一次性跳一大截。

        为什么不是直接用 ``now``：那样手停了数字会一直涨，看起来像还在用。
        为什么要加满 IDLE：需求是「停手 5 秒后停表」，这 5 秒内继续走。
        """
        if self._segment_start is None:
            return 0.0
        last = self._last_active_ts or self._segment_start
        end = last + IDLE_TIMEOUT_SEC
        if now is not None:
            end = min(end, float(now))
        return max(self._segment_start, end)

    def today(self):
        """今日摘要：累计秒数、格式化文本、最后操作时间。

        坑（实测踩到过）：不能直接 ``rec + (last - segment_start)``，
        因为 flush_segments 已经把这段记到 ``_accounted_until`` 了，
        再整段加一次就会重复（30 秒的活跃显示成 58 秒）。只补
        ``_accounted_until`` 之后还没落账的那一截。
        """
        key = day_key()
        with self._lock:
            rec = dict(self.days.get(key) or {"active_seconds": 0, "last_active": ""})
            # 正在进行的段要实时加上「还没记账的那一截」。
            # 末端用 ``_live_end()``（最后一次活跃 + 空闲阈值），和
            # flush_segments / tick 收段用同一个口径 —— 三处口径不一致
            # 就是「数字先冻住、过几秒突然 +5」的原因。
            live = 0.0
            if self._segment_start is not None:
                done = self._accounted_until
                if done is None:
                    done = self._segment_start
                # 末端必须**封顶到此刻**（传 now），不能只算 last+IDLE：
                # 那样 today() 会比 flush_segments 实际能落账的多出最多 5 秒
                # （future time），等段一收、按 min(now, last+IDLE) 一算，
                # 显示值就往回掉 —— 这就是「秒数前后跳变」的直接原因。
                # 封顶后：手停 → 继续平滑走满 5 秒 → 到点冻住，且永不回退。
                live = max(0.0, self._live_end(now=self._clock()) - done)
            # 还没被 flush 记账的已结束段也要算进来：收段（tick）与记账
            # （flush）不是同一拍，中间那个瞬间若不补，数字会往回跳一下
            # —— 这就是用户看到的「秒数前后跳变」。
            pending = 0.0
            for start, end in self._segments:
                if end > start:
                    pending += (end - start)
            seconds = float(rec.get("active_seconds") or 0.0) + live + pending

        # 注意：``last_active`` 只留在内部记账（落盘还用它），**不再往外下发**。
        # 界面上「最后操作时间」这个字段已经按需求移除，前后端都不再出现。
        return {
            "day": key,
            "active_seconds": int(round(seconds)),
            "text": format_duration(seconds),
            "caliber": "口径：光标移动或按键（500ms 采样）",
        }

    def summary(self, days=KEEP_DAYS):
        cutoff = (_now_local() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        with self._lock:
            rows = [{"day": k, "active_seconds": int(v.get("active_seconds") or 0),
                     "last_active": v.get("last_active") or ""}
                    for k, v in sorted(self.days.items()) if k >= cutoff]
        return rows


# ---------------------------------------------------------------- 格式化

def _iso_local(ts):
    return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%dT%H:%M:%S")


def format_duration(seconds):
    """秒数 → 精确到秒的 ``HH:MM:SS``。

    需求：界面上「已运行」要精确到秒。超过 24 小时把天数提出来，写成
    ``N天 HH:MM:SS``（小时位仍然只保留 0–23，不然 25 小时会变成 25:00:00，
    和「天」的语义重复）。

    秒数下取整：采样是 500ms 一拍，向上取整会让它看起来跑得比真实快。
    """
    s = int(max(0, seconds or 0))
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    clock = "%02d:%02d:%02d" % (hours, mins, secs)
    if days > 0:
        return "%d天 %s" % (days, clock)
    return clock


def format_last_active(iso_text, now=None):
    """**已废弃**：界面上不再展示「最后操作时间」，这个函数只为旧调用点留着。

    新代码不要再调它；``today()`` 也不再下发 ``last_active``。
    """
    if not iso_text:
        return "—"
    try:
        dt = datetime.datetime.strptime(iso_text, "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return iso_text
    today = day_key(now)
    if dt.strftime("%Y-%m-%d") == today:
        return dt.strftime("%H:%M:%S")
    return dt.strftime("%m-%d %H:%M")


def purge_old(store, now=None, keep_days=KEEP_DAYS):
    """删掉超过 keep_days 的日期记录。"""
    cutoff = ((_now_local() if now is None else datetime.datetime.fromtimestamp(now))
              - datetime.timedelta(days=keep_days)).strftime("%Y-%m-%d")
    removed = 0
    with store._lock:
        for k in list(store.days.keys()):
            if k < cutoff:
                del store.days[k]
                removed += 1
    return removed


def main():
    """命令行自检：造一段假使用，打印今日摘要。"""
    tmp = os.path.join(__import__("tempfile").mkdtemp(prefix="usage-"), USAGE_NAME)
    seq = [((100, 100), 0), ((200, 200), 0), ((300, 300), 0)]
    it = iter(seq)
    st = UsageTracker(path=tmp, input_reader=lambda: next(it, (None, 0)))
    base = 1793000000.0
    for i in range(3):
        st.tick(now=base + i * 0.5)
    for i in range(3):
        st.tick(now=base + 10 + i * 0.5)
    st.flush_segments(now=base + 20)
    print(st.today())
    st.save()
    print("saved to", tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
