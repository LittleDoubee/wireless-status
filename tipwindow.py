# -*- coding: utf-8 -*-
"""贴片悬停提示窗 —— 独立的 Win32 顶层小气泡

为什么不能继续用 HTML tooltip（``web/pill.css`` 里那个 ``.chart-tip``）：

  贴片窗是一个 30 逻辑像素高的小窗口，而且被 ``SetWindowRgn`` 裁成了圆角
  矩形。**WebView2 画不出窗口之外的东西** —— 那个 ``.chart-tip`` 只要往上
  弹出窗口边界就被裁掉，屏幕上什么都不显示。实测 hover 完全没有任何反馈，
  这就是需求 4「实现了但用不了」的根因。

所以提示改成一个**独立的顶层窗口**：它不属于贴片窗，不受裁剪影响；
``WS_EX_NOACTIVATE`` 保证鼠标划过去不会把焦点从用户正在打字的窗口抢走。

================================================================ 必须保留的坑
1. 窗口必须在**自己线程**里创建，并在同一个线程里跑消息泵，否则
   ``WM_PAINT`` 收不到、窗口永远是一块白。``PumpMessages`` 就是这个用法
   （托盘 tray.py 同样是这个模式）。

2. 跨线程不能直接改窗口状态 —— 只能 ``PostMessage`` 自定义消息，让窗口
   线程自己去 ``SetWindowPos``。直接调用会在 UI 线程忙时互相等。

3. 字体每次绘制都重建、画完立刻删：缓存下来的 HFONT 在 DPI 变化后不会
   自动更新。一次绘制约 1ms，没必要为它做缓存池。

4. ``win32gui`` **没有** ``CreateFont``。要么 ``win32ui.CreateFont``（返回
   的 PyCFont 没有 DeleteObject 方法，只能靠 GC），要么 ``ctypes`` 调
   ``gdi32.CreateFontW`` 拿裸句柄。这里走 ctypes：句柄能显式删，也不会
   把 pythoncom/win32ui 的 COM 状态带进这条线程。
"""

import ctypes
import threading
import traceback

import win32api
import win32con
import win32gui

#: 自定义消息：显示 / 隐藏。``WM_APP`` 之后是应用私有区间。
WM_TIP_SHOW = win32con.WM_APP + 1
WM_TIP_HIDE = win32con.WM_APP + 2

CLASS_NAME = "WirelessStatusTipWindow"

# ---------------------------------------------------------------- 外观常量
#
# 尺寸单位全部是**逻辑像素**，绘制前乘 dpi_scale —— 150% 缩放下写死物理值
# 会得到一个只有半个字高的气泡。

PAD_X = 10            # 左右内边距
PAD_Y = 8             # 上下内边距
RADIUS = 8            # 圆角半径
LINE_GAP = 3          # 两行之间的间距
MAX_WIDTH = 460       # 单行最长宽度，超了末尾省略（不换行：提示窗不该又高又窄）
MIN_WIDTH = 90        # 极短文案（比如只有「无设备」）时的最小宽度

#: 两套配色，跟任务栏深浅走 —— 浅色任务栏上一块黑气泡很扎眼，反之亦然。
#: 取值对齐 Win11 系统 tooltip 的观感。文字一律高对比，不用灰度小字。
THEME = {
    "light": {
        "bg": (0xF9, 0xF9, 0xF9),
        "border": (0xD2, 0xD2, 0xD2),
        "title": (0x1A, 0x1A, 0x1A),
        "sub": (0x5A, 0x5A, 0x5A),
    },
    "dark": {
        "bg": (0x2B, 0x2B, 0x2B),
        "border": (0x46, 0x46, 0x46),
        "title": (0xF2, 0xF2, 0xF2),
        "sub": (0xB4, 0xB4, 0xB4),
    },
}

# gdi32 的 CreateFontW 必须显式声明原型：HFONT 是指针，ctypes 默认按 c_int
# 收，64 位下会被截断（本项目 dpi_scale 里踩过同样的坑）。另外**不声明
# argtypes 时 ctypes 会按调用现场猜参数个数**，多传一个就 TypeError。
_gdi = ctypes.windll.gdi32
_gdi.CreateFontW.restype = ctypes.c_void_p
_gdi.CreateFontW.argtypes = [
    ctypes.c_int,        # nHeight
    ctypes.c_int,        # nWidth
    ctypes.c_int,        # nEscapement
    ctypes.c_int,        # nOrientation
    ctypes.c_int,        # fnWeight
    ctypes.c_uint,       # fdwItalic
    ctypes.c_uint,       # fdwUnderline
    ctypes.c_uint,       # fdwStrikeOut
    ctypes.c_uint,       # iCharSet
    ctypes.c_uint,       # iOutPrecision
    ctypes.c_uint,       # iClipPrecision
    ctypes.c_uint,       # iQuality
    ctypes.c_uint,       # iPitchAndFamily
    ctypes.c_wchar_p,    # pszFaceName
]


def _dpi_scale():
    """当前系统缩放倍率。失败按 1.0（宁可小一点，不要错位）。"""
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
        if dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def _rgb(color):
    """``(r, g, b)`` → GDI 的 COLORREF（0x00BBGGRR）。"""
    r, g, b = color[:3]
    return (r & 0xFF) | ((g & 0xFF) << 8) | ((b & 0xFF) << 16)


def make_font(height_px, weight, face="Microsoft YaHei UI"):
    """建一个 HFONT（裸句柄，调用方用完必须 ``win32gui.DeleteObject``）。

    CreateFontW 的 14 个参数依次是：高、宽、字符角度、基线角度、字重、
    斜体、下划线、删除线、字符集、输出精度、裁剪精度、质量、字距、字面名。
    """
    return _gdi.CreateFontW(
        -abs(int(height_px)), 0, 0, 0, int(weight),
        0, 0, 0, win32con.DEFAULT_CHARSET,
        win32con.OUT_DEFAULT_PRECIS, win32con.CLIP_DEFAULT_PRECIS,
        win32con.CLEARTYPE_QUALITY, 0, face)


def text_size(hdc, text):
    """量一段文本在**当前选中字体**下的像素尺寸。空串返回 (0, 0)。"""
    if not text:
        return 0, 0
    return win32gui.GetTextExtentPoint32(hdc, text)


class TipWindow(object):
    """一个常驻的悬停提示窗（创建一次，反复 show/hide）。

    ``start()`` 之后就可以从任意线程调 ``show(...)`` / ``hide()``。
    平时是 ``SW_HIDE``，不占屏幕也不吃鼠标事件。
    """

    def __init__(self):
        self.hwnd = None
        self._thread = None
        self._ready = threading.Event()
        self._fail = None
        self._lock = threading.Lock()
        #: 要显示的内容 —— 窗口线程绘制时读，整体替换，用锁保护。
        self._title = ""
        self._sub = ""
        self._dark = False
        #: 锚点矩形（物理像素，贴片自己的 ``(x, y, w, h)``）。
        self._anchor = None

    # ------------------------------------------------------------ 生命周期
    def start(self, timeout=3.0):
        """起窗口线程并等它就绪。失败不抛 —— 提示坏了不该影响主程序。"""
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(target=self._run, name="pill-tip",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout=timeout)
        return self._fail is None

    @property
    def error(self):
        return self._fail

    def stop(self):
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)

    # ------------------------------------------------------------ 对外接口
    def show(self, title, sub="", anchor=None, dark=False):
        """显示提示。

        ``anchor`` 是贴片的物理像素矩形 ``(x, y, w, h)``；气泡摆在它**正上
        方**（贴片在任务栏里，下方没有空间）。为 None 时沿用上次位置。
        """
        with self._lock:
            self._title = title or ""
            self._sub = sub or ""
            self._dark = bool(dark)
            if anchor:
                self._anchor = tuple(anchor)
        self._post(WM_TIP_SHOW)

    def hide(self):
        self._post(WM_TIP_HIDE)

    @property
    def visible(self):
        """窗口当前是否可见（给后端去重，避免每 0.2s 重复 Post 消息）。"""
        if not self.hwnd:
            return False
        try:
            return bool(win32gui.IsWindowVisible(self.hwnd))
        except Exception:
            return False

    def _post(self, msg):
        if not self.hwnd:
            return False
        try:
            win32gui.PostMessage(self.hwnd, msg, 0, 0)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------ 线程体
    def _run(self):
        try:
            self._create()
        except Exception as exc:
            self._fail = "%s: %s" % (type(exc).__name__, exc)
            traceback.print_exc()
            self._ready.set()
            return
        self._ready.set()
        try:
            win32gui.PumpMessages()
        except Exception:
            traceback.print_exc()

    def _create(self):
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = CLASS_NAME
        wc.lpfnWndProc = self._wnd_proc
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            # 类已注册（上一条线程没清干净）—— 直接用
            pass
        # 尺寸先给个占位，真正大小在 _render 里按文字量出来再 SetWindowPos。
        #
        # style **必须显式给 WS_POPUP**：传 0 时 pywin32 会退回到"有标题栏的
        # 默认窗口"，实测弹出来是一条带 caption 的白色小窗（标题就是类名外的
        # 那串 "tip"），完全不是气泡。WS_POPUP + 无边框 = 真正的一块浮层。
        self.hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE |
            win32con.WS_EX_TOPMOST,
            CLASS_NAME, "tip",
            win32con.WS_POPUP,
            0, 0, 10, 10, 0, 0, wc.hInstance, None)

    # ------------------------------------------------------------ 消息处理
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TIP_SHOW:
            try:
                self._render()
            except Exception:
                traceback.print_exc()
            return 0
        if msg == WM_TIP_HIDE:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            except Exception:
                pass
            return 0
        if msg == win32con.WM_PAINT:
            try:
                self._paint(hwnd)
            except Exception:
                traceback.print_exc()
            return 0
        if msg == win32con.WM_ERASEBKGND:
            # 自己整块重绘，别让系统先刷一遍白底（会闪）
            return 1
        if msg == win32con.WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    # ------------------------------------------------------------ 排版
    def _font_specs(self, scale):
        """返回 ``[(句柄, 用途), ...]``，调用方负责删。

        第一行 12px 半粗（设备名）、第二行 11px 常规（型号/电量），
        与系统 tooltip 的主次层级一致。
        """
        return [
            ("title", make_font(round(12 * scale), win32con.FW_SEMIBOLD)),
            ("sub", make_font(round(11 * scale), win32con.FW_NORMAL)),
        ]

    def _measure(self, hwnd, scale):
        """量出气泡需要的宽高（物理像素）。"""
        with self._lock:
            title, sub = self._title, self._sub
        hdc = win32gui.GetDC(hwnd)
        try:
            fonts = dict(self._font_specs(scale))
            try:
                win32gui.SelectObject(hdc, fonts["title"])
                tw, th = text_size(hdc, title)
                win32gui.SelectObject(hdc, fonts["sub"])
                sw, sh = text_size(hdc, sub)
            finally:
                for h in fonts.values():
                    win32gui.DeleteObject(h)
        finally:
            win32gui.ReleaseDC(hwnd, hdc)

        pad_x = int(round(PAD_X * scale))
        pad_y = int(round(PAD_Y * scale))
        gap = int(round(LINE_GAP * scale)) if sub else 0
        maxw = int(round(MAX_WIDTH * scale))
        minw = int(round(MIN_WIDTH * scale))
        w = min(max(tw, sw), maxw) + pad_x * 2
        w = max(w, minw)
        h = th + (gap + sh if sub else 0) + pad_y * 2
        return w, h

    def _position(self, w, h, scale):
        """把气泡摆到贴片正上方，并夹进虚拟桌面。"""
        with self._lock:
            anchor = self._anchor
        gap = int(round(6 * scale))
        if anchor:
            ax, ay, aw, _ah = anchor
            x = int(ax + aw // 2 - w // 2)
            y = int(ay - h - gap)
        else:
            # 没有锚点：贴着任务栏上沿水平居中，至少不会跑到屏幕外
            try:
                bar = win32gui.GetWindowRect(
                    win32gui.FindWindow("Shell_TrayWnd", None))
                x = (bar[0] + bar[2]) // 2 - w // 2
                y = bar[1] - h - gap
            except Exception:
                x, y = 100, 100
        try:
            vx = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
            vy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
            vw = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
            vh = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
            x = max(vx + 4, min(x, vx + vw - w - 4))
            y = max(vy + 4, min(y, vy + vh - h - 4))
        except Exception:
            pass
        return int(x), int(y)

    # ------------------------------------------------------------ 绘制
    def _render(self):
        hwnd = self.hwnd
        if not hwnd:
            return
        scale = _dpi_scale()
        w, h = self._measure(hwnd, scale)
        x, y = self._position(w, h, scale)
        # 先几何、再裁形状：SetWindowRgn 的区域是窗口坐标，窗口尺寸变了
        # 区域不会跟着缩放（贴片那边踩过一模一样的坑）。
        win32gui.SetWindowPos(
            hwnd, win32con.HWND_TOPMOST, x, y, w, h,
            win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)
        self._clip(hwnd, w, h, scale)
        win32gui.InvalidateRect(hwnd, None, True)
        win32gui.UpdateWindow(hwnd)

    def _clip(self, hwnd, w, h, scale):
        """裁成圆角矩形。

        不用分层窗口做透明：GDI 窗口虽然支持 ``SetLayeredWindowAttributes``，
        但半透明底色压在深浅两种任务栏上都会发脏，纯色 + 圆角更干净。
        """
        try:
            r = max(1, int(round(RADIUS * scale)))
            rgn = win32gui.CreateRoundRectRgn(0, 0, w + 1, h + 1, r * 2, r * 2)
            # 句柄交给系统后不能再 DeleteObject（系统接管所有权）
            win32gui.SetWindowRgn(hwnd, rgn, True)
        except Exception:
            pass

    def _paint(self, hwnd):
        hdc, _paint = win32gui.BeginPaint(hwnd)
        try:
            rect = win32gui.GetClientRect(hwnd)
            w, h = max(1, rect[2]), max(1, rect[3])
            scale = _dpi_scale()
            with self._lock:
                dark, title, sub = self._dark, self._title, self._sub
            theme = THEME["dark" if dark else "light"]

            mem = win32gui.CreateCompatibleDC(hdc)
            bmp = win32gui.CreateCompatibleBitmap(hdc, w, h)
            old_bmp = win32gui.SelectObject(mem, bmp)
            try:
                brush = win32gui.CreateSolidBrush(_rgb(theme["bg"]))
                win32gui.FillRect(mem, (0, 0, w, h), brush)
                win32gui.DeleteObject(brush)

                # 1px 边框：靠最外圈描一条，圆角处跟着 region 一起被裁，
                # 不会冒到圆角外面。
                r = max(1, int(round(RADIUS * scale)))
                pen = win32gui.CreatePen(win32con.PS_SOLID, 1,
                                         _rgb(theme["border"]))
                old_pen = win32gui.SelectObject(mem, pen)
                hollow = win32gui.GetStockObject(win32con.HOLLOW_BRUSH)
                old_brush = win32gui.SelectObject(mem, hollow)
                win32gui.RoundRect(mem, 0, 0, w, h, r * 2, r * 2)
                win32gui.SelectObject(mem, old_brush)
                win32gui.SelectObject(mem, old_pen)
                win32gui.DeleteObject(pen)

                win32gui.SetBkMode(mem, win32con.TRANSPARENT)
                pad_x = int(round(PAD_X * scale))
                pad_y = int(round(PAD_Y * scale))
                gap = int(round(LINE_GAP * scale))
                fonts = dict(self._font_specs(scale))
                try:
                    y = pad_y
                    box = (pad_x, y, w - pad_x, h - pad_y)
                    if title:
                        win32gui.SelectObject(mem, fonts["title"])
                        win32gui.SetTextColor(mem, _rgb(theme["title"]))
                        win32gui.DrawText(
                            mem, title, -1, box,
                            win32con.DT_LEFT | win32con.DT_SINGLELINE |
                            win32con.DT_TOP | win32con.DT_NOPREFIX |
                            win32con.DT_END_ELLIPSIS)
                        y += text_size(mem, title)[1] + gap
                    if sub:
                        win32gui.SelectObject(mem, fonts["sub"])
                        win32gui.SetTextColor(mem, _rgb(theme["sub"]))
                        win32gui.DrawText(
                            mem, sub, -1, (pad_x, y, w - pad_x, h - pad_y),
                            win32con.DT_LEFT | win32con.DT_SINGLELINE |
                            win32con.DT_TOP | win32con.DT_NOPREFIX |
                            win32con.DT_END_ELLIPSIS)
                finally:
                    for fh in fonts.values():
                        win32gui.DeleteObject(fh)

                win32gui.BitBlt(hdc, 0, 0, w, h, mem, 0, 0, win32con.SRCCOPY)
            finally:
                win32gui.SelectObject(mem, old_bmp)
                win32gui.DeleteObject(bmp)
                win32gui.DeleteDC(mem)
        finally:
            win32gui.EndPaint(hwnd, _paint)


# ---------------------------------------------------------------- 文案拼装

def battery_ok(bat):
    """电量是否可读。单独抽出来，自测不必构造完整 device 就能验证。"""
    if not isinstance(bat, dict):
        return False
    pct = bat.get("percent")
    return bool(bat.get("supported")) and isinstance(pct, (int, float))


def format_lines(device):
    """把一台设备拼成提示窗的两行文字，返回 ``(title, sub)``。

    需求 4：悬停要能看到**设备名称和型号**。所以第一行直接给
    ``display_name``（``devices.display_name_of`` 产出的「罗技 M350
    Wireless Mouse」—— 品牌 + 型号，本身**不带 PID**）；第二行补真正有用的
    上下文：连接方式、电量、今日用电。

    为什么第二行不重复型号：界面上堆技术串是用户明确反感过的（见
    ``devices.display_name_of`` 的注释）。PID 只在型号名**读不到**时补位，
    否则用户连「这是哪只」都判断不了。
    """
    if not device:
        return "未知设备", ""

    name = (device.get("display_name") or "").strip()
    raw = (device.get("name") or "").strip()
    title = name or raw or "未知设备"

    parts = []
    conn = (device.get("connection") or {}).get("label")
    if conn:
        parts.append(conn)

    bat = device.get("battery") or {}
    if battery_ok(bat):
        parts.append("电量 %d%%" % int(bat.get("percent")))
    else:
        parts.append("电量未知")

    used = device.get("today_used")
    if isinstance(used, (int, float)):
        parts.append("今日用电 %d%%" % int(used))

    if not raw and device.get("pid"):
        parts.append("PID %s" % device.get("pid"))

    status = (device.get("status_text") or "").strip()
    if status and status not in ("在线",):
        parts.append(status)

    return title, " · ".join(parts)
