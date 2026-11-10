# -*- coding: utf-8 -*-
"""Win32 窗口层 —— 毛玻璃、贴任务栏、不上任务栏按钮

这个模块做四件事，每一件都踩过坑：

  1. **毛玻璃背景**
     Win11 22H2+ 走 ``DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE)``
     设成 ``DWMSBT_TRANSIENTWINDOW``(3) 或 ``DWMSBT_MAINWINDOW``(2)；
     Win10 走 undocumented 的 ``SetWindowCompositionAttribute`` +
     ``ACCENT_ENABLE_ACRYLICBLURBEHIND``。
     两者都要配 ``DwmExtendFrameIntoClientArea`` 全 -1，否则只有客户区模糊、
     边框还是实心的，看起来像贴了一张纸。
     **可读性优先**：任何一步失败都返回 False，由前端退回
     ``backdrop-filter: blur(20px)`` + 半透明卡片 —— 模糊只是观感层，
     正文对比度不能靠它。

  2. **不上任务栏**
     ``GWL_EXSTYLE`` 加 ``WS_EX_TOOLWINDOW``、去掉 ``WS_EX_APPWINDOW``，
     再把 owner 设成托盘的隐藏窗口。光加 TOOLWINDOW 有时不够（先显示后
     设置会残留），所以再用 ``ITaskbarList::DeleteTab`` 兜一次底。

  3. **贴任务栏上沿**
     ``screen_work_area()`` 已经返回**逻辑像素**（除过 dpi_scale），
     这里直接拿来算，不再自己乘系数 —— 坐标系统一在 main.py 里解决。

  4. **不抢焦点**
     贴片和主窗都不应该激活时抢走当前窗口的键盘焦点（``SW_SHOWNOACTIVATE``）。

所有函数在非 Windows 或调用失败时都**只返回 False/None，不抛异常** ——
这个模块的失败必须是无害降级，不能连累主程序。
"""

import os
import sys

IS_WINDOWS = (sys.platform == "win32")

# ---------------------------------------------------------------- 常量

#: DwmSetWindowAttribute 的 attribute id
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38       # Win11 22H2 (build 22621) 起才有

#: DWMWA_SYSTEMBACKDROP_TYPE 的取值
DWMSBT_AUTO = 0
DWMSBT_NONE = 1
DWMSBT_MAINWINDOW = 2                # Mica
DWMSBT_TRANSIENTWINDOW = 3           # Acrylic
DWMSBT_TABBEDWINDOW = 4              # Mica Alt

#: DwmSetWindowAttribute(DWMWA_WINDOW_CORNER_PREFERENCE) 的取值
DWMWCP_DEFAULT = 0
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
DWMWCP_ROUNDSMALL = 3

#: SetWindowCompositionAttribute（Win10 Acrylic）用到的常量
WCA_ACCENT_POLICY = 19
ACCENT_DISABLED = 0
ACCENT_ENABLE_BLURBEHIND = 3
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
ACCENT_ENABLE_HOSTBACKDROP = 5

#: 窗口扩展样式
GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000

#: 窗口样式（改「独立窗口 ↔ 任务栏子窗口」时用）
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_CLIPCHILDREN = 0x02000000

#: SetWindowPos 的标志
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040

#: ShowWindow
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_HIDE = 0

#: 窗口位置缓存：拿到 hwnd 之后不必每次都去问 WinForms
_hwnd_cache = {}


# ---------------------------------------------------------------- 取 hwnd

def window_handle(window):
    """从 pywebview 的 Window 对象里挖出真正的 HWND。

    pywebview 6.x 在 Windows 上 ``window.native`` 是
    ``webview.platforms.winforms.BrowserView.BrowserForm``（一个
    ``System.Windows.Forms.Form`` 子类），它的 ``Handle`` 是
    **``System.IntPtr``**。

    这里踩过一个坑：pythonnet 并**不**把 ``IntPtr.ToInt64()`` 暴露成
    可调用的 Python 方法（``getattr(handle, "ToInt64")`` 拿到 None），
    而 ``int(handle)`` 会抛 ``TypeError: int() argument must be ... not
    'IntPtr'``。真正能用的只有 ``str(handle)`` —— IntPtr 的 ToString()
    返回十进制的句柄值。

    全都拿不到就返回 None，调用方据此降级（毛玻璃不生效而已，窗口照常）。
    """
    if window is None:
        return None
    cached = _hwnd_cache.get(id(window))
    if cached and is_window(cached):
        return cached
    if cached:
        # 缓存里的句柄已经失效（窗口被销毁过，id 又被复用）——
        # 不清掉的话，贴片关掉再开会一直拿到死句柄，表现为「贴片不见了」。
        _hwnd_cache.pop(id(window), None)
    try:
        native = getattr(window, "native", None)
        if native is None:
            return None
        handle = getattr(native, "Handle", None)
        if handle is None:
            return None
        hwnd = _intptr_to_int(handle)
        if hwnd:
            _hwnd_cache[id(window)] = hwnd
            return hwnd
    except Exception:
        pass
    return None


def _intptr_to_int(value):
    """``System.IntPtr`` → Python int。多种写法都试一遍，全失败返回 0。"""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    # 1) pythonnet 少数版本会暴露 ToInt64
    for name in ("ToInt64", "ToInt32"):
        fn = getattr(value, name, None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:
                pass
    # 2) IntPtr.ToString() 给十进制句柄 —— 这是实测唯一稳定的路径
    try:
        return int(str(value))
    except (TypeError, ValueError):
        pass
    # 3) 有些包装对象带 .value 字段
    inner = getattr(value, "value", None)
    if inner is not None:
        try:
            return int(inner)
        except (TypeError, ValueError):
            pass
    return 0


# ---------------------------------------------------------------- 托盘图标锚点

def icon_point_from_lparam(lparam):
    """从 ``NIN_SELECT`` / ``NIN_BALLOONUSERCLICK`` 的 lParam 取图标中心点。

    ``NOTIFYICON_VERSION_4`` 之后，托盘回调的 lParam 语义变了：

        LOWORD  = 图标中心的 x（**物理像素**）
        HIWORD  = 图标中心的 y

    早期版本（v3 及以前）lParam 只低 16 位放鼠标事件码，没有坐标。所以
    调用方要先用 v4 注册（``NIM_SETVERSION``），否则拿到的「坐标」其实是
    事件码左移出来的垃圾值，窗口会跑到屏幕角落。

    解析失败返回 None（调用方退回「任务栏通知区域中心」这个近似锚点）。
    """
    try:
        v = int(lparam) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return None
    x = v & 0xFFFF
    y = (v >> 16) & 0xFFFF
    if x == 0 and y == 0:
        return None
    # 坐标是 16 位有符号数？不 —— 托盘坐标是 unsigned 16 位，屏幕宽度
    # 一般不会超过 65535，直接用即可。
    return (x, y)


def notify_icon_anchor():
    """没拿到 lParam 坐标时的近似锚点：通知区域中心（物理像素）。

    比「屏幕右下角」准得多：右下角还有托盘图标、时钟，会偏出去几十到
    上百像素；通知区域中心至少落在托盘那一块里。
    """
    note = notify_area_rect()
    if not note:
        return None
    return (int((note[0] + note[2]) / 2), int((note[1] + note[3]) / 2))


def main_rect_near_icon(anchor, width, height, gap=8):
    """把主窗摆在托盘图标**正上方**，返回**逻辑像素** ``(w, h, x, y)``。

    参数 ``anchor`` 是图标中心的**物理像素** ``(x, y)``。

    规则（对应用户需求第 1 条）：
      * 水平：以图标中心为基准居中 —— 这是「在图标正上方」的关键，
        不是贴屏幕右边缘（那样会偏出去半个窗口宽）；
      * 垂直：窗口底边 = 图标顶边上方 ``gap`` 像素。图标矩形拿不到精确
        高度，用任务栏高度当图标高度，即底边 = 任务栏上沿 - gap；
      * 夹取：用 ``MonitorFromPoint`` 取**该点所在显示器**的工作区，
        水平优先夹取，再夹垂直；上方放不下时翻到下方；
      * 任务栏在左/右/上时：工作区本身已经排除了任务栏，锚点仍按图标
        真实位置算，所以上/左/右三种任务栏方向都能落进工作区。

    拿不到锚点时返回 None，由调用方降级。
    """
    if not anchor:
        return None
    ax, ay = int(anchor[0]), int(anchor[1])

    work = monitor_work_area_for_point(ax, ay)
    mon = monitor_rect_for_point(ax, ay) or work
    if not work:
        return None
    scale = dpi_scale_at(ax, ay) or 1.0

    bar = taskbar_rect()
    w = max(1, int(width))
    h = max(1, int(height))

    # 逻辑像素下的工作区
    wl, wt = int(work[0] / scale), int(work[1] / scale)
    wr, wb = int(work[2] / scale), int(work[3] / scale)
    x = int(round(ax / scale)) - w // 2

    # 任务栏上沿：优先用「显示器整块矩形 ∩ 工作区」的差集判断方向，
    # 而不是只看全局 Shell_TrayWnd —— 多显示器时任务栏可能只在主屏。
    bar_top = None
    if bar:
        # 任务栏和这块显示器有交集才算数
        if mon and not (bar[2] <= mon[0] or bar[0] >= mon[2]
                        or bar[3] <= mon[1] or bar[1] >= mon[3]):
            bar_top = int(bar[1] / scale)
    if bar_top is None:
        # 用工作区反推：工作区上边界 < 显示器上边界 ⇒ 任务栏在顶部
        if mon:
            mon_t = int(mon[1] / scale)
            mon_b = int(mon[3] / scale)
            if wt > mon_t:
                bar_top = mon_t          # 任务栏在上方
            elif wb < mon_b:
                bar_top = wb             # 任务栏在下方
        if bar_top is None:
            bar_top = wb                 # 兜底：贴工作区底边

    y = bar_top - h - int(gap)

    # 避让「显示隐藏的图标」溢出面板（用户需求）。
    #
    # 面板没弹出时底边钉在任务栏上沿；一旦 ^ 弹出，面板正好从任务栏往上
    # 长（实测 1986,1357,2337,1528），会盖住窗口下半截，Windows 还会为了
    # 不让两个窗口抢同一块区域把窗口往上顶 —— 表象就是「界面漂移」。
    # 所以面板可见时把底边改钉在面板上沿，窗口整体上移，两者互不遮挡。
    panel = overflow_panel_rect()
    if panel:
        panel_top = int(panel[1] / scale)
        if panel_top < bar_top:
            y = panel_top - h - int(gap)

    flipped = False
    if y < wt:
        # 上方放不下（窗口太高 / 任务栏在上方）：翻到图标下方
        if mon:
            bar_bottom = int(mon[3] / scale)
        else:
            bar_bottom = wb
        y = bar_bottom + int(gap)
        flipped = True

    # 夹进工作区（水平优先，其次垂直）
    x = max(wl, min(x, wr - w))
    y = max(wt, min(y, wb - h))

    return w, h, int(x), int(y)


# ---------------------------------------------------------------- 任务栏嵌入

def taskbar_hwnd():
    """任务栏窗口句柄；拿不到返回 0。"""
    if not IS_WINDOWS:
        return 0
    try:
        import ctypes
        return int(ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None) or 0)
    except Exception:
        return 0


#: ``embed_in_taskbar`` 上一次失败的原因（成功时置空）。
#:
#: 为什么要有：``embed_in_taskbar`` 只会返回 True/False，而它内部有四个
#: 失败点（拿不到任务栏 / SetParent 失败 / 拿不到任务栏矩形 / 异常）。
#: 只看 False 根本不知道卡在哪一步 —— 排查"贴片没挂进任务栏"时无从下手。
#: 主程序读它写进生命周期日志。
LAST_EMBED_ERROR = ""


def window_style(hwnd):
    """取窗口的样式位（只用于排查）。取不到返回 0。"""
    if not IS_WINDOWS or not hwnd:
        return 0
    try:
        import ctypes
        return int(ctypes.windll.user32.GetWindowLongW(
            ctypes.c_void_p(hwnd), -16) & 0xFFFFFFFF)
    except Exception:
        return 0


def window_parent(hwnd):
    """取窗口的父窗口句柄；取不到返回 0。

    用于**确认** ``SetParent`` 真的生效了 —— ``SetParent`` 的返回值是
    "旧父窗口"，旧父是 NULL 时它返回 0，那是"原来没有父"而不是失败，
    根本不能拿来判断成功与否。看 ``GetParent`` 的**当前值**才准。
    """
    if not IS_WINDOWS or not hwnd:
        return 0
    try:
        import ctypes
        return int(ctypes.windll.user32.GetParent(ctypes.c_void_p(hwnd)) or 0)
    except Exception:
        return 0


def embed_in_taskbar(hwnd, x, y, w, h, show=True):
    """把贴片窗 ``SetParent`` 挂成任务栏的子窗口。

    参数 ``x, y, w, h`` 是**任务栏客户区坐标系**下的物理像素。

    为什么这样做（需求第 2 条）：独立顶层窗口哪怕按 TOPMOST 摆在任务栏
    上方，也和任务栏是**两个图层** —— 任务栏自动隐藏时它不跟着隐藏、
    任务栏被开始菜单盖住时它仍然露在上面。挂成子窗口之后它就跟输入法
    候选框、时钟一样，属于任务栏本身。

    ``show=False`` 时**不**带 ``SWP_SHOWWINDOW``：贴片开关关着的时候要
    建好窗口但别让它闪一下（窗口同生共死，见 main._create_pill_window）。

    返回 True/False。失败时调用方必须退回 ``place_over_taskbar``
    （置顶浮窗），**不许**静默什么都不做。
    """
    global LAST_EMBED_ERROR
    LAST_EMBED_ERROR = ""
    if not IS_WINDOWS or not hwnd:
        LAST_EMBED_ERROR = "句柄无效"
        return False
    host = taskbar_hwnd()
    if not host:
        LAST_EMBED_ERROR = "找不到 Shell_TrayWnd"
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32

        # 先改成子窗口样式：WS_CHILD 去掉 WS_POPUP，否则 SetParent
        # 会失败（跨进程父子 + 样式不匹配时 Windows 直接拒绝）。
        GWL_STYLE = -16
        style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_STYLE)
        style = (style & ~WS_POPUP) | WS_CHILD | WS_CLIPCHILDREN
        user32.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_STYLE, style)

        # SetParent 返回的是**旧父窗口**。旧父是 NULL（顶层窗口）时它返回
        # 0 —— 那不是失败，是"原来没有父"！所以这里不能只看返回值真假，
        # 得看 GetParent 是不是真的换成了任务栏。
        prev = user32.SetParent(ctypes.c_void_p(hwnd), ctypes.c_void_p(host))
        now = user32.GetParent(ctypes.c_void_p(hwnd))
        if now != host:
            LAST_EMBED_ERROR = ("SetParent 未生效（prev=%s now=%s host=%s）"
                               % (prev, now, host))
            return False

        # 子窗口用**客户区坐标**，相对任务栏左上角。
        bar = taskbar_rect()
        if not bar:
            LAST_EMBED_ERROR = "拿不到任务栏矩形"
            return False
        flags = SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
        if show:
            flags |= SWP_SHOWWINDOW
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd), None,
            int(x - bar[0]), int(y - bar[1]),
            max(1, int(w)), max(1, int(h)),
            flags)
        return True
    except Exception as exc:
        LAST_EMBED_ERROR = "%s: %s" % (type(exc).__name__, exc)
        return False


def detach_from_taskbar(hwnd, x, y, w, h, show=True):
    """把贴片窗从任务栏上摘下来，恢复成独立无边框浮窗（降级路径）。

    参数 ``x, y, w, h`` 是**屏幕坐标**物理像素。返回 True/False。
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32

        user32.SetParent(ctypes.c_void_p(hwnd), None)
        GWL_STYLE = -16
        style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_STYLE)
        style = (style & ~WS_CHILD) | WS_POPUP
        user32.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_STYLE, style)
        flags = SWP_NOACTIVATE | SWP_FRAMECHANGED
        if show:
            flags |= SWP_SHOWWINDOW
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd), -1,          # HWND_TOPMOST
            int(x), int(y), max(1, int(w)), max(1, int(h)),
            flags)
        return True
    except Exception:
        return False


def clip_in_taskbar(hwnd, radius):
    """贴片在任务栏里的圆角裁剪（逻辑像素半径，内部乘倍率）。"""
    return clip_round_rect(hwnd, radius)


# ---------------------------------------------------------------- 拖动

def start_window_drag(hwnd):
    """让系统接管一次「拖动窗口」：``ReleaseCapture`` + ``WM_NCLBUTTONDOWN``。

    这是在无边框窗口上实现「抓着哪都能拖」的标准做法：先放开鼠标捕获，
    再给窗口发一条「你在标题栏上被按下了」的消息，Windows 自己的移动
    逻辑就接管了 —— 拖动跟手、支持吸附到屏幕边缘、支持双击最大化
    （无边框窗没有标题栏，双击行为由我们自己决定，一般不会触发）。

    **不能自己算坐标差再 SetWindowPos**：那样没有窗口吸附、拖动时会
    和 WebView2 的输入线程抢消息，表现为「拖起来一顿一顿的」。

    返回 True/False。
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        WM_NCLBUTTONDOWN = 0x00A1
        HTCAPTION = 2
        user32.ReleaseCapture()
        # 用 **PostMessage** 而不是 SendMessage：SendMessage 会一直阻塞到
        # 拖动结束（Windows 的移动循环是模态的），js_api 的这次调用也就
        # 一直不返回，前端拿不到 promise、看起来像「点了没反应」。
        # 投递进队列后立刻返回，模态循环照样会把它取出来执行。
        user32.PostMessageW(ctypes.c_void_p(hwnd), WM_NCLBUTTONDOWN,
                             ctypes.c_void_p(HTCAPTION), None)
        return True
    except Exception:
        return False


def window_rect(hwnd):
    """窗口的屏幕矩形（物理像素）：``(left, top, right, bottom)``。失败 None。"""
    if not IS_WINDOWS or not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd),
                                              ctypes.byref(rect)):
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 毛玻璃

def _dwm_blur(hwnd):
    """Win11 22H2+ 的系统背景材质。成功返回 True。"""
    try:
        import ctypes
        try:
            dwmapi = ctypes.windll.dwmapi
        except Exception:
            return False
        # Acrylic：半透明 + 模糊，比 Mica 更适合小窗（Mica 对纯色桌面不明显）
        value = ctypes.c_int(DWMSBT_TRANSIENTWINDOW)
        hr = dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_int(DWMWA_SYSTEMBACKDROP_TYPE),
            ctypes.byref(value),
            ctypes.sizeof(value))
        if hr != 0:
            return False
        # 圆角（Win11 自带；老系统上这个调用会失败，忽略）
        corner = ctypes.c_int(DWMWCP_ROUND)
        dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_int(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(corner),
            ctypes.sizeof(corner))
        return True
    except Exception:
        return False


def _dwm_extend_frame(hwnd):
    """让整个窗口（含边框）都参与模糊。**必须**跟 blur 一起用。"""
    try:
        import ctypes
        import ctypes.wintypes as wt

        class MARGINS(ctypes.Structure):
            _fields_ = [("cxLeftWidth", ctypes.c_int),
                        ("cxRightWidth", ctypes.c_int),
                        ("cyTopHeight", ctypes.c_int),
                        ("cyBottomHeight", ctypes.c_int)]

        margins = MARGINS(-1, -1, -1, -1)
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
            ctypes.c_void_p(hwnd), ctypes.byref(margins))
        return True
    except Exception:
        return False


def _accent_blur(hwnd, tint=0x00000000, opacity=0xCC):
    """Win10 的 Acrylic（undocumented）。成功返回 True。

    ``tint`` 是 AABBGGRR 顺序（注意不是 RGBA），这里给个中性深色 + 高透明度；
    真正的深浅色由前端 CSS 决定，这一层只提供「透过去看得见」的底。
    """
    try:
        import ctypes

        class ACCENTPOLICY(ctypes.Structure):
            _fields_ = [("AccentState", ctypes.c_int),
                        ("AccentFlags", ctypes.c_int),
                        ("GradientColor", ctypes.c_uint),
                        ("AnimationId", ctypes.c_int)]

        class WINCOMPATTRDATA(ctypes.Structure):
            _fields_ = [("Attribute", ctypes.c_int),
                        ("Data", ctypes.c_void_p),
                        ("SizeOfData", ctypes.c_size_t)]

        policy = ACCENTPOLICY()
        policy.AccentState = ACCENT_ENABLE_ACRYLICBLURBEHIND
        policy.AccentFlags = 0x20 | 0x40 | 0x80 | 0x100   # 各边都画
        policy.GradientColor = (opacity << 24) | tint
        policy.AnimationId = 0

        data = WINCOMPATTRDATA()
        data.Attribute = WCA_ACCENT_POLICY
        data.Data = ctypes.cast(ctypes.byref(policy), ctypes.c_void_p)
        data.SizeOfData = ctypes.sizeof(policy)

        fn = getattr(ctypes.windll.user32, "SetWindowCompositionAttribute", None)
        if fn is None:
            return False
        return bool(fn(ctypes.c_void_p(hwnd), ctypes.byref(data)))
    except Exception:
        return False


def apply_blur(hwnd, prefer_acrylic=True):
    """给窗口上毛玻璃。返回 ``{"applied": bool, "method": "..."}``。

    顺序：Win11 系统材质 → Win10 Acrylic → 放弃（前端 CSS 兜底）。
    ``method`` 会写进日志，排查「为什么没糊」时有用。
    """
    if not IS_WINDOWS or not hwnd:
        return {"applied": False, "method": "unsupported"}
    # 不管用哪种，边框一起糊是前提
    _dwm_extend_frame(hwnd)
    if prefer_acrylic and _dwm_blur(hwnd):
        return {"applied": True, "method": "dwm-systembackdrop"}
    if _accent_blur(hwnd):
        return {"applied": True, "method": "accent-acrylic"}
    return {"applied": False, "method": "css-fallback"}


def set_dark_titlebar(hwnd, dark=True):
    """标题栏跟随深浅色（无边框窗口用不到，但贴片和调试时有用）。"""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        import ctypes
        value = ctypes.c_int(1 if dark else 0)
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_int(DWMWA_USE_IMMERSIVE_DARK_MODE),
            ctypes.byref(value),
            ctypes.sizeof(value))
        return hr == 0
    except Exception:
        return False


# ---------------------------------------------------------------- 不上任务栏

def hide_from_taskbar(hwnd, owner_hwnd=None):
    """让窗口不出现在任务栏。返回实际生效的方法名（调试用）。

    三步都做，因为单做一步在某些时机下会失效：
      1. owner 设成托盘隐藏窗口（pywebview 创建时通常没有 owner）
      2. exstyle 加 WS_EX_TOOLWINDOW / 去掉 WS_EX_APPWINDOW
      3. ITaskbarList::DeleteTab 兜底
    """
    if not IS_WINDOWS or not hwnd:
        return "unsupported"
    methods = []
    try:
        import ctypes
        user32 = ctypes.windll.user32

        # 1) owner
        if owner_hwnd:
            try:
                GWLP_HWNDPARENT = -8
                setter = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
                setter(ctypes.c_void_p(hwnd), ctypes.c_int(GWLP_HWNDPARENT),
                       ctypes.c_void_p(owner_hwnd))
                methods.append("owner")
            except Exception:
                pass

        # 2) exstyle
        try:
            getter = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
            setter = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
            style = getter(ctypes.c_void_p(hwnd), ctypes.c_int(GWL_EXSTYLE))
            new_style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            if new_style != style:
                setter(ctypes.c_void_p(hwnd), ctypes.c_int(GWL_EXSTYLE),
                       ctypes.c_void_p(new_style))
                methods.append("exstyle")
            # 应用样式变更需要重设一次位置才生效
            user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER |
                                SWP_NOACTIVATE | SWP_FRAMECHANGED)
        except Exception:
            pass

        # 3) ITaskbarList 兜底
        if _delete_tab(hwnd):
            methods.append("taskbarlist")
    except Exception:
        pass
    return "+".join(methods) if methods else "failed"


def _delete_tab(hwnd):
    """ITaskbarList::DeleteTab —— 用 ctypes 手工调 COM，**不碰 pythoncom**。

    为什么不用 ``win32com.client.Dispatch``：那条路会在后台线程里
    ``pythoncom.CoInitialize()``，而本项目里调用它的线程是个普通
    threading.Thread；pythoncom 的初始化会连带拉 CLR / 全局 COM 状态，
    与主线程上正在跑的 WinForms 窗口创建互相等，**直接死锁**
    （实测：整个进程僵住，日志停在 ``_after_window_chrome`` 那一行）。

    ctypes 直接 vtable 调用没有这个问题 —— 只是几次函数指针调用。
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        ole32 = ctypes.windll.ole32
        # CoInitializeEx(NULL, COINIT_APARTMENTTHREADED=2)
        # 返回值 S_OK(0) / S_FALSE(1) / RPC_E_CHANGED_MODE(0x80010106) 都继续
        ole32.CoInitializeEx(None, 2)

        CLSID_TaskbarList = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
        IID_ITaskbarList = "{56FDF342-FD6D-11d0-958A-006097C9A090}"

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                        ("Data3", ctypes.c_uint16),
                        ("Data4", ctypes.c_ubyte * 8)]

        def make_guid(text):
            g = GUID()
            ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
            return g

        clsid = make_guid(CLSID_TaskbarList)
        iid = make_guid(IID_ITaskbarList)

        ptr = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1,  # CLSCTX_INPROC_SERVER
                                    ctypes.byref(iid), ctypes.byref(ptr))
        if hr != 0 or not ptr.value:
            return False
        try:
            # vtable: 0=QueryInterface 1=AddRef 2=Release 3=HrInit 4=AddTab
            #         5=DeleteTab 6=ActivateTab 7=SetActiveAlt
            vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            hr_init = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[3])
            hr_init(ptr)
            delete = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                        wintypes.HWND)(vtbl[5])
            delete(ptr, hwnd)
            release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
            release(ptr)
            return True
        except Exception:
            return False
    except Exception:
        return False


def set_noactivate(hwnd):
    """贴片窗口不该抢焦点。"""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        getter = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        setter = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        style = getter(ctypes.c_void_p(hwnd), ctypes.c_int(GWL_EXSTYLE))
        setter(ctypes.c_void_p(hwnd), ctypes.c_int(GWL_EXSTYLE),
               ctypes.c_void_p(style | WS_EX_NOACTIVATE))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 位置

def place_above_taskbar(window, width, height, margin=8):
    """把窗口贴到主屏工作区右下（任务栏上沿），**不读 window_state**。

    参数 ``width`` / ``height`` 是逻辑像素；``screen_work_area()`` 给的
    也是逻辑像素，所以这里直接算，不再乘 dpi_scale —— 那个换算只在
    main.py 里做一次，避免两处各乘一遍导致重复缩放。

    返回 ``(x, y)``，失败返回 None。
    """
    try:
        import main as _main   # 延迟导入，避免循环依赖
    except Exception:
        return None
    try:
        left, top, sw, sh = _main.screen_work_area()
    except Exception:
        return None
    if sw <= 0 or sh <= 0:
        return None
    w = max(1, int(width))
    h = max(1, int(height))
    # 右下角，贴任务栏上沿
    x = left + max(0, sw - w - margin)
    y = top + max(0, sh - h - margin)
    try:
        if window is not None:
            window.move(int(x), int(y))
            window.resize(int(w), int(h))
        return x, y
    except Exception:
        return None


def notify_area_rect():
    """任务栏「通知区域」的屏幕矩形（**物理像素**）：``(left, top, right, bottom)``。

    路径：``Shell_TrayWnd`` → ``TrayNotifyWnd``。后者就是屏幕最右下那块
    装托盘图标 + 隐藏图标箭头（^）的区域，贴片要贴在它**左边**。

    踩过的坑：``TrayNotifyWnd`` 的子窗口用 ``EnumChildWindows`` 枚举不到
    （Win11 之后图标是 XAML 画的，不是 Win32 子控件），所以只能整块矩形
    定位，拿不到「隐藏图标按钮」的精确坐标。拿它的左边界足够用。

    失败返回 None（副屏、任务栏自动隐藏、非 Windows 等），调用方降级。
    """
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        if not tray:
            return None
        note = user32.FindWindowExW(ctypes.c_void_p(tray), None,
                                    "TrayNotifyWnd", None)
        if not note:
            return None
        rect = wt.RECT()
        if not user32.GetWindowRect(ctypes.c_void_p(note), ctypes.byref(rect)):
            return None
        if rect.right <= rect.left:
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        return None


def taskbar_rect():
    """整条任务栏的屏幕矩形（物理像素）。失败返回 None。"""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        if not tray:
            return None
        rect = wt.RECT()
        if not user32.GetWindowRect(ctypes.c_void_p(tray), ctypes.byref(rect)):
            return None
        if rect.bottom <= rect.top:
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        return None


# 「显示隐藏的图标」溢出面板的窗口类名（Win11 22H2 起实测）。
#
# 名字看起来很怪，但这是 Windows 自己起的：这个类名的窗口就是点任务栏
# 那个 ^ 箭头之后弹出来的托盘溢出面板（标题「系统托盘溢出窗口。」）。
# Win10/更早的 21H2 上没有这个类名，此时函数返回 None，调用方退回旧行为
# （只按任务栏上沿算，不避让面板）。
OVERFLOW_PANEL_CLASSES = (
    "TopLevelWindowForOverflowXamlIsland",
    "TopLevelWindowForOverflowXamlIsland_WASDK",
    "NotifyIconOverflowWindow",          # Win10 经典实现
)


def overflow_panel_rect():
    """托盘溢出面板（^ 弹出的那块）的屏幕矩形（**物理像素**）。

    返回 ``(left, top, right, bottom)``；**面板没弹出时返回 None**。

    为什么要它（用户需求）：主窗底边一直钉在任务栏上沿，可溢出面板
    正好也从任务栏往上长 —— 面板一弹出来就会盖住主窗下半截，Windows
    为了不让两个窗口抢同一块区域还会把主窗往上顶，用户看到的就是
    「界面漂移」。所以主窗必须避让：面板可见时，底边改钉在面板上沿。

    判定用 ``IsWindowVisible`` —— 实测这个窗口一直存在（矩形固定
    1986,1357,2337,1528），点 ^ 就是把它的可见性来回切：
    ``vis=False → True → False``。所以**不能**靠「窗口存在」判断。
    """
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wt.HWND
        for cls in OVERFLOW_PANEL_CLASSES:
            hwnd = user32.FindWindowW(cls, None)
            if not hwnd:
                continue
            if not user32.IsWindowVisible(hwnd):
                continue
            rect = wt.RECT()
            if not user32.GetWindowRect(ctypes.c_void_p(hwnd),
                                        ctypes.byref(rect)):
                continue
            # 空矩形（有些版本会用 0x0 占位）不算数
            if rect.right <= rect.left or rect.bottom <= rect.top:
                continue
            return (rect.left, rect.top, rect.right, rect.bottom)
        return None
    except Exception:
        return None


def pill_rect(pill_width, pill_height, margin=2):
    """任务栏电量贴片的位置：**贴在通知区域左边**，与托盘图标同一行居中。

    返回 ``(x, y)`` 逻辑像素（调用方按 dpi_scale 换回物理，或者直接用
    ``place_pill``）。

    设计要点（对应需求「贴片应出现在任务栏、贴近隐藏图标一侧」）：
      * x = 通知区域左边界 - 贴片宽 - 边距 —— 即紧挨着托盘图标左边
      * y = 任务栏垂直居中，让胶囊和托盘图标在同一条水平线上
      * 通知区域拿不到时退回「任务栏右侧」（老逻辑），不会跑到屏幕外

    踩过的坑：早期版本用 ``screen_work_area()`` 算右下角，那是**任务栏
    上沿以上**的位置 —— 贴片浮在任务栏头顶，看起来像一块多余的浮窗，
    而不是嵌在任务栏里。
    """
    try:
        import main as _main
        scale = _main.dpi_scale() or 1.0
    except Exception:
        scale = 1.0

    w = max(1, int(pill_width))
    h = max(1, int(pill_height))
    m = int(margin)

    note = notify_area_rect()
    bar = taskbar_rect()
    if note and bar:
        # 物理 → 逻辑
        bar_left = int(bar[0] / scale)
        bar_top = int(bar[1] / scale)
        bar_h = int((bar[3] - bar[1]) / scale)
        note_left = int(note[0] / scale)
        x = max(bar_left + m, note_left - w - m)
        y = bar_top + max(0, (bar_h - h) // 2)
        return x, y

    # 降级：不知道通知区域在哪，就贴任务栏右端
    if bar:
        bar_left = int(bar[0] / scale)
        bar_top = int(bar[1] / scale)
        bar_w = int((bar[2] - bar[0]) / scale)
        bar_h = int((bar[3] - bar[1]) / scale)
        return (bar_left + max(0, bar_w - w - m * 4),
                bar_top + max(0, (bar_h - h) // 2))
    return None


def pill_rect_in_taskbar(pill_width, pill_height):
    """贴片嵌进任务栏时用的几何（**全部物理像素**）。

    返回 ``(x, y, w, h, bar_h)``：

      * ``x, y`` —— 屏幕坐标（``embed_in_taskbar`` 会自己减掉任务栏原点）
      * ``w`` —— 贴片宽（逻辑像素乘倍率，150% 下文字大小才一致）
      * ``h`` —— 窗口高 = 内容高（单行文字），不是整条任务栏
      * ``bar_h`` —— 任务栏物理高度，供前端选字号档位、给提示窗定位

    拿不到任务栏时返回 None，调用方降级到 ``pill_rect``。

    关于高度：扁平样式下窗口底色**就是**任务栏采样色，所以窗口不必等于
    胶囊那样的紧凑形状 —— 但也不能占满整条任务栏：底色是**局部采样**的
    值，铺满整条会和两端的渐变对不上，露出一条淡淡的横带。按内容高、垂直
    居中，是让「不可见」这个目标最稳的做法。
    """
    bar = taskbar_rect()
    note = notify_area_rect()
    if not bar:
        return None
    if not note:
        # 通知区（``TrayNotifyWnd``）**还不存在** —— explorer 刚重启的
        # 那几秒里就是这样。以前这里直接 return None，贴片就永远挂不上
        # 任务栏（表现：explorer 一重启，贴片变成飘在桌面上的顶层窗口，
        # 而且再也回不去了）。现在退回"贴任务栏最右端"，至少能挂上，
        # 等通知区出现后 ``_poll_taskbar_change`` 会再摆正一次。
        note = (bar[2], bar[1], bar[2], bar[3])
    bar_left, bar_top, bar_right, bar_bottom = bar
    bar_h = bar_bottom - bar_top
    if bar_h <= 0:
        return None

    anchor_x = int((bar_left + bar_right) / 2)
    scale = dpi_scale_at(anchor_x, int((bar_top + bar_bottom) / 2)) or 1.0

    w = max(1, int(round(max(1, int(pill_width)) * scale)))
    h = max(1, int(round(max(1, int(pill_height)) * scale)))
    if h > bar_h - 2:
        # 任务栏太矮（小图标档）时留 1 物理像素上下余量，别顶到边
        h = max(1, bar_h - 2)
    y = bar_top + (bar_h - h) // 2

    # 贴片右边界贴住通知区域左边（^ 按钮就在通知区域最左侧）
    x = int(note[0]) - w
    x = max(bar_left, x)
    return x, y, w, h, bar_h


def taskbar_background(near=None):
    """采样任务栏底色，返回 ``(r, g, b)``；失败返回 None。

    为什么必须运行时采样：Win11 任务栏是亚克力/云母材质，颜色跟着壁纸、
    主题、透明度设置走 —— 实测同一台机器同一分钟内，任务栏底色在
    (232,232,232) 到 (252,252,252) 之间浮动。写死任何一个值，在某些壁纸上
    都会让贴片看起来像「糊上去的一块白补丁」。

    为什么还要 ``near``：任务栏底色**横向有渐变**。实测同一帧里贴片位置是
    (229,245,246) 的薄荷色，而整条任务栏的众数是 (209,221,221) 的灰色 ——
    取了众数就得到一个比周围暗 15 级的灰块，看着像在任务栏上按了个坑。
    ``near`` 传贴片自己的矩形（物理像素 ``(x, y, w, h)``）时，只在它左右
    各 80px 的邻域里采样，取到的就是肉眼看到的那个颜色。

    采样方式：BitBlt 把任务栏整条抓下来，横向取点做直方图取**众数**。
    不用 ``GetPixel``，它对亚克力层会返回 DWM 合成前的值（实测取到 255
    纯白，和眼睛看到的不是一回事）。

    跳过 alpha<250 的像素（圆角/阴影边缘的半透明过渡），也跳过纯黑
    （任务栏上的图标笔画），以及贴片自己那块区域（否则会采到自己）。
    """
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
        user32.FindWindowW.restype = wt.HWND
        user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        user32.GetDC.argtypes = [wt.HWND]
        user32.GetDC.restype = wt.HDC
        user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]

        gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
        gdi32.CreateCompatibleDC.restype = wt.HDC
        gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT,
                                           ctypes.POINTER(ctypes.c_void_p),
                                           wt.HANDLE, wt.DWORD]
        gdi32.CreateDIBSection.restype = wt.HANDLE
        gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
        gdi32.SelectObject.restype = wt.HANDLE
        gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wt.HDC,
                                 ctypes.c_int, ctypes.c_int, wt.DWORD]
        gdi32.BitBlt.restype = wt.BOOL
        gdi32.GdiFlush.restype = wt.BOOL
        gdi32.DeleteObject.argtypes = [wt.HANDLE]
        gdi32.DeleteDC.argtypes = [wt.HDC]

        tray = user32.FindWindowW("Shell_TrayWnd", None)
        if not tray:
            return None
        rect = wt.RECT()
        if not user32.GetWindowRect(tray, ctypes.byref(rect)):
            return None
        left, top = int(rect.left), int(rect.top)
        width, height = int(rect.right - rect.left), int(rect.bottom - rect.top)
        if width <= 8 or height <= 8:
            return None

        class _BIH(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                        ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                        ("biClrImportant", wt.DWORD)]

        bmi = _BIH()
        bmi.biSize = ctypes.sizeof(_BIH)
        bmi.biWidth = width
        bmi.biHeight = -height       # 负 = 自上而下，省一次翻转
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0        # BI_RGB

        bits = ctypes.c_void_p()
        src_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(src_dc)
        try:
            hbmp = gdi32.CreateDIBSection(src_dc, ctypes.byref(bmi), 0,
                                          ctypes.byref(bits), None, 0)
            if not hbmp or not bits:
                return None
            try:
                gdi32.SelectObject(mem_dc, hbmp)
                if not gdi32.BitBlt(mem_dc, 0, 0, width, height, src_dc,
                                    left, top, 0x00CC0020):   # SRCCOPY
                    return None
                gdi32.GdiFlush()
                raw = ctypes.string_at(bits, width * height * 4)
            finally:
                gdi32.DeleteObject(hbmp)
        finally:
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, src_dc)

        # 采样横向范围：给了 near 就用**贴片自己的横向区间**，但只取它
        # 上方/下方那两条任务栏空白（贴片只占任务栏高度的一部分，上下必然
        # 留白）。否则退到左侧邻域，再否则全条扫。
        #
        # 为什么不能用「左侧邻域」：Win11 任务栏是亚克力/云母材质，颜色跟着
        # **壁纸**走，同一条任务栏上横向有渐变（实测贴片左边是薄荷
        # (221,234,234)、右边已经是中性灰 (219,222,222)，中间差 12 级）。
        # 采样左边得到"左邻域的颜色"，贴在中间就成了一条淡淡的竖带。
        # 贴片正上方/正下方那两行是同一条竖线上、没被遮住的任务栏本身，
        # 取它才是"贴片这个位置本该是什么颜色"的正确答案。
        x0, x1 = 4, width - 4
        row_sets = [int(height * f) for f in (0.25, 0.5, 0.75)]
        skip_box = None
        if near:
            try:
                nx, ny, nw, nh = (int(v) for v in near[:4])
                lx = nx - left                      # 任务栏局部坐标
                lrx = lx + nw
                ly = ny - top
                lby = ly + nh
                col0 = max(4, lx)
                col1 = min(width - 4, lrx)
                if col1 - col0 >= 8:
                    x0, x1 = col0, col1
                    # 纵向：贴片上边之上、下边之下各取几条线。
                    # 每边留 1px 余量，避开窗口边缘的抗锯齿像素。
                    above = list(range(2, max(3, ly - 1)))
                    below = list(range(min(height - 3, lby + 1), height - 1))
                    rows = above[-3:] + below[:3]
                    if rows:
                        row_sets = rows
                    # 贴片自己的矩形（换成任务栏局部坐标），采样时排除掉
                    skip_box = (lx, ly, nw, nh)
                else:
                    # 贴片太靠边（占满整条宽度）：退回左侧邻域
                    lx0 = max(4, lx - 200)
                    lx1 = lx - 6
                    if lx1 - lx0 >= 40:
                        x0, x1 = lx0, lx1
                    skip_box = (lx, ly, nw, nh)
            except Exception:
                x0, x1 = 4, width - 4
                skip_box = None

        # 横向扫描若干行，把颜色量化到 4 的倍数后**分桶**找众数，
        # 再返回桶内像素的**精确平均值**。
        #
        # ⚠ 为什么不能直接返回量化后的桶键：扁平样式的贴片底色必须和任务栏
        # 逐像素一致，而量化会把 0~3 级的误差当精度丢掉 —— 实测任务栏真实
        # 是 (222,234,234)，量化到 4 的倍数就成了 (220,232,232)，贴上去
        # 立刻显出一条淡淡的方印子。分桶只用来"找出哪种颜色占多数"，
        # 具体数值必须回到原始像素去算。
        hist = {}
        step = 1 if (x1 - x0) <= 400 else 3
        for row in row_sets:
            if row < 0 or row >= height:
                continue
            base = row * width * 4
            for col in range(x0, x1, step):
                if skip_box:
                    sx, sy, sw, sh = skip_box
                    if sx - 2 <= col <= sx + sw + 2 and sy - 2 <= row <= sy + sh + 2:
                        continue          # 采到自己窗口上就不是任务栏底色了
                off = base + col * 4
                b, g, r, a = raw[off], raw[off + 1], raw[off + 2], raw[off + 3]
                if a < 250:
                    continue
                if r < 24 and g < 24 and b < 24:
                    continue          # 图标笔画，不是底色
                key = (r // 4, g // 4, b // 4)
                slot = hist.get(key)
                if slot is None:
                    hist[key] = [1, r, g, b]
                else:
                    slot[0] += 1
                    slot[1] += r
                    slot[2] += g
                    slot[3] += b
        if not hist:
            return None
        best = max(hist.values(), key=lambda v: v[0])
        n = best[0]
        return (int(round(best[1] / n)),
                int(round(best[2] / n)),
                int(round(best[3] / n)))
    except Exception:
        return None


def taskbar_metrics(near=None):
    """任务栏的度量信息，给前端选外观档位用。

    返回 dict：
      * ``height``  —— 任务栏物理高度（40 / 48 / 60…）
      * ``scale``   —— 该任务栏所在显示器的缩放倍率
      * ``dark``    —— 任务栏是不是深色（Win11 深色主题时是 True）
      * ``logical_height`` —— 逻辑高度（40px 任务栏在 150% 下是 60 物理）

    拿不到返回 None。深色判断走的是「屏幕右下角采样」这种不可靠办法，
    这里改用注册表 ``SystemUsesLightTheme`` —— 那是任务栏自己的主题开关，
    比 ``AppsUseLightTheme``（应用主题）更准确。

    ``near``（物理像素 ``(x, y, w, h)``）是贴片自己的矩形，透传给
    ``taskbar_background()`` 做邻域采样 —— 任务栏底色横向有渐变，
    全条取众数会得到比贴片周围暗十几级的灰。
    """
    bar = taskbar_rect()
    if not bar:
        return None
    bar_h = int(bar[3] - bar[1])
    anchor_x = int((bar[0] + bar[2]) / 2)
    anchor_y = int((bar[1] + bar[3]) / 2)
    scale = dpi_scale_at(anchor_x, anchor_y) or 1.0
    dark = _taskbar_is_dark()
    bg = taskbar_background(near)
    return {
        "height": bar_h,
        "scale": scale,
        "logical_height": int(round(bar_h / scale)),
        "dark": dark,
        # 任务栏真实底色（None = 没采到，前端退回按 dark 推导的默认值）。
        # 贴片胶囊底色要跟它几乎同色，才不会像贴在任务栏上的一块补丁。
        "background": list(bg) if bg else None,
    }


def taskbar_background_profile(near, stops=7):
    """沿贴片自己的横向区间采样**若干点**的任务栏底色，返回 ``[[t, r, g, b], ...]``。

    为什么必须是"若干点"而不是一个色：Win11 任务栏是亚克力/云母材质，颜色
    跟着壁纸走，同一条任务栏上**横向有明显渐变**。实测通知区附近：

        x=1700 → (209, 221, 221)
        x=1850 → (216, 229, 228)
        x=1900 → (224, 236, 233)
        x=2000 → (230, 234, 234)

    90 像素宽的贴片横跨这段就是 8~20 级的差 —— 填一个纯色，无论取哪个
    采样点，总有一条边对不上，视觉上就是「任务栏上一块淡淡的方印子」。
    所以前端拿这组点画一条 ``linear-gradient(to right, ...)``。

    ``t`` 是 0..1 的归一化位置（贴片左边界 = 0、右边界 = 1）。纵向取贴片
    上方/下方那两行空白（贴片只占任务栏高度的一部分，上下必然留白），
    这样采到的是"贴片这个位置本该是什么颜色"。

    采样失败返回 None，调用方退回单色 ``taskbar_background``。
    """
    if not IS_WINDOWS or not near:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        user32.GetDC.argtypes = [wt.HWND]
        user32.GetDC.restype = wt.HDC
        user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
        gdi32.CreateCompatibleDC.restype = wt.HDC
        gdi32.CreateDIBSection.restype = wt.HANDLE
        gdi32.BitBlt.restype = wt.BOOL

        tray = user32.FindWindowW("Shell_TrayWnd", None)
        if not tray:
            return None
        rect = wt.RECT()
        if not user32.GetWindowRect(tray, ctypes.byref(rect)):
            return None
        left, top = int(rect.left), int(rect.top)
        width, height = int(rect.right - rect.left), int(rect.bottom - rect.top)
        if width <= 8 or height <= 8:
            return None

        nx, ny, nw, nh = (int(v) for v in near[:4])
        lx = max(1, nx - left)
        rx = min(width - 1, lx + nw)
        if rx - lx < 8:
            return None
        ly = ny - top
        lby = ly + nh

        class _BIH(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                        ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                        ("biClrImportant", wt.DWORD)]

        bmi = _BIH()
        bmi.biSize = ctypes.sizeof(_BIH)
        bmi.biWidth = width
        bmi.biHeight = -height
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        bits = ctypes.c_void_p()
        src_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(src_dc)
        try:
            hbmp = gdi32.CreateDIBSection(src_dc, ctypes.byref(bmi), 0,
                                          ctypes.byref(bits), None, 0)
            if not hbmp or not bits:
                return None
            try:
                gdi32.SelectObject(mem_dc, hbmp)
                if not gdi32.BitBlt(mem_dc, 0, 0, width, height, src_dc,
                                    left, top, 0x00CC0020):
                    return None
                gdi32.GdiFlush()
                raw = ctypes.string_at(bits, width * height * 4)
            finally:
                gdi32.DeleteObject(hbmp)
        finally:
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, src_dc)

        # 纵向采样行：贴片上方之上、下方之下各几条，都避开窗口边缘 2px
        rows = []
        for y in range(max(1, ly - 3), max(1, ly - 1)):
            rows.append(y)
        for y in range(min(height - 2, lby + 1), min(height - 1, lby + 3)):
            rows.append(y)
        if not rows:
            return None

        out = []
        span = max(1, rx - lx)
        for i in range(stops):
            t = i / float(stops - 1) if stops > 1 else 0.0
            col = lx + int(round(t * (span - 1)))
            col = max(1, min(width - 2, col))
            acc = [0, 0, 0, 0]
            for y in rows:
                off = y * width * 4 + col * 4
                b, g, r, a = raw[off], raw[off + 1], raw[off + 2], raw[off + 3]
                if a < 250:
                    continue
                acc[0] += r
                acc[1] += g
                acc[2] += b
                acc[3] += 1
            if not acc[3]:
                continue
            n = acc[3]
            out.append([round(t, 3),
                        int(round(acc[0] / n)),
                        int(round(acc[1] / n)),
                        int(round(acc[2] / n))])
        return out if len(out) >= 2 else None
    except Exception:
        return None


def window_interior_profile(hwnd, stops=7, rows=(0.07, 0.93)):
    """沿窗口宽度采样**若干点**的屏幕像素颜色，返回 ``[[t, r, g, b], ...]``。

    只用于色彩校准：要知道「我们写进去的渐变，屏幕上到底变成了什么」。

    为什么采 ``rows=(0.07, 0.93)``：贴片内容是单行文字，垂直居中占中间约
    16px；上下那 7% 的位置整条都是纯背景，横跨全宽都能安全采样，不需要
    额外留白。**不能**在垂直中点采样 —— 那里是图标和「90%」的笔画。

    返回 None 表示读不到（窗口不可见/被遮挡/非 Windows）。
    """
    if not IS_WINDOWS or not hwnd:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        user32.IsWindowVisible.argtypes = [wt.HWND]
        user32.IsIconic.argtypes = [wt.HWND]

        if not user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
            return None
        if user32.IsIconic(ctypes.c_void_p(hwnd)):
            return None
        r = wt.RECT()
        if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
            return None
        left, top = int(r.left), int(r.top)
        w, h = int(r.right - r.left), int(r.bottom - r.top)
        if w <= 16 or h <= 8:
            return None

        ys = [top + max(1, min(h - 2, int(round(f * (h - 1))))) for f in rows]

        from PIL import ImageGrab  # 只在需要校准时才 import
        out = []
        for i in range(stops):
            t = i / float(stops - 1) if stops > 1 else 0.0
            cx = left + int(round(t * (w - 1)))
            cx = max(left + 1, min(left + w - 2, cx))
            acc = [0, 0, 0, 0]
            for cy in ys:
                img = ImageGrab.grab(bbox=(cx - 1, cy - 1, cx + 2, cy + 2))
                for p in img.convert("RGB").getdata():
                    acc[0] += p[0]
                    acc[1] += p[1]
                    acc[2] += p[2]
                    acc[3] += 1
            if not acc[3]:
                continue
            n = acc[3]
            out.append([round(t, 3),
                        int(round(acc[0] / n)),
                        int(round(acc[1] / n)),
                        int(round(acc[2] / n))])
        return out if len(out) >= 2 else None
    except Exception:
        return None


def window_interior_color(hwnd, x_frac=0.5, y_frac=0.5):
    """读**窗口自己渲染出来的**屏幕像素颜色，返回 ``(r, g, b)`` 中位数。

    ``x_frac`` / ``y_frac`` 是采样点在窗口内的归一化位置。要和渐变采样点
    对齐时必须传同一个位置 —— 拿"窗口左边缘的颜色"去比"渐变中段的颜色"
    算出来的差值毫无意义（早期就是这么错的，越补越偏）。

    用途只有一个：**色彩校准**。

    ============================================================
    为什么需要它（实测数据，别删）
    ============================================================
    同一块屏幕上：

      * 任务栏自己的像素是 ``(221, 233, 233)``；
      * 窗口 CSS 写 ``#dce8e8`` = ``(220, 232, 232)``；
      * 屏幕上量到的窗口像素却是 ``(216, 234, 234)`` —— R 少 4、G 多 2。

    这不是采样错，也不是 WebView2 的锅：纯 GDI 的 tkinter 窗口填同一个
    ``#dce8e8``，屏幕像素同样是 ``(216,234,234)``。根因是显示器色彩管理在
    窗口合成这条路上做了一次变换，而任务栏走另一条路径。

    对扁平贴片这是致命的：底色只要差几级，就是一块淡淡的方印子。

    ``winui`` 只负责"读回来"，换算成补偿量在 ``main._calibrated_*`` 里做。
    窗口没显示 / 被完全遮挡时返回 None，调用方沿用上次的补偿量。
    """
    if not IS_WINDOWS or not hwnd:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        user32.IsWindowVisible.argtypes = [wt.HWND]
        user32.IsIconic.argtypes = [wt.HWND]

        if not user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
            return None
        if user32.IsIconic(ctypes.c_void_p(hwnd)):
            return None
        r = wt.RECT()
        if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
            return None
        left, top = int(r.left), int(r.top)
        w, h = int(r.right - r.left), int(r.bottom - r.top)
        if w <= 8 or h <= 8:
            return None

        cx = left + int(round(max(0.08, min(0.92, x_frac)) * (w - 1)))
        cy = top + int(round(max(0.15, min(0.85, y_frac)) * (h - 1)))

        from PIL import ImageGrab  # 只在需要校准时才 import
        img = ImageGrab.grab(bbox=(cx - 1, cy - 1, cx + 2, cy + 2))
        px = list(img.convert("RGB").getdata())
        if not px:
            return None
        # 逐通道取中位数：3×3 里只要多数像素是纯背景，结果就是背景色
        out = []
        for i in range(3):
            vals = sorted(p[i] for p in px)
            out.append(vals[len(vals) // 2])
        return tuple(out)
    except Exception:
        return None


def _taskbar_is_dark():
    """任务栏是否为深色。读不到就当浅色（默认外观）。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        try:
            value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        finally:
            winreg.CloseKey(key)
        return not bool(value)
    except Exception:
        return False


def clip_round_rect(hwnd, radius):
    """把窗口裁成圆角矩形。``radius`` 传**逻辑像素**，**0 = 不裁（直角）**。

    为什么必须剪裁，而不是靠 CSS 的圆角 + 透明背景：
    pywebview 的 ``transparent=True`` 在 WinForms + WebView2 上并没有真正
    实现逐像素透明 —— 实测贴片窗照样是一整块不透明矩形，只是底色变成了
    黑色（``background_color="#000000"``），压在任务栏上就是一条黑带。
    用 ``SetWindowRgn`` 直接裁窗口形状最稳：像素级生效，不依赖合成器，
    而且连 WebView2 的子窗口一起被裁掉。

    ``radius <= 0`` 时**清掉区域**（``SetWindowRgn(hwnd, None)``），让窗口
    保持原本的矩形：扁平样式的贴片窗口底色就是任务栏采样色，任何圆角都会
    在四角露出一点任务栏本身，颜色对不齐时反而显出一圈缺口。
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        import ctypes
        import ctypes.wintypes as wt

        scale = _dpi_scale() or 1.0
        px = int(round(max(0, int(radius)) * scale))
        if px <= 0:
            return bool(ctypes.windll.user32.SetWindowRgn(
                ctypes.c_void_p(hwnd), None, True))

        r = wt.RECT()
        ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
        w = max(1, r.right - r.left)
        h = max(1, r.bottom - r.top)
        # 上限不超过半高（否则不是圆角矩形是椭圆）
        px = min(px, h // 2)
        # CreateRoundRectRgn 的椭圆宽高是**直径**，所以传 2*px
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(
            0, 0, w + 1, h + 1, px * 2, px * 2)
        if not rgn:
            return False
        # 第 3 个参数 bRedraw=True。区域句柄交给系统后**不能**再 DeleteObject
        # （系统会接管所有权），否则下次重绘访问已释放句柄会崩。
        ok = ctypes.windll.user32.SetWindowRgn(
            ctypes.c_void_p(hwnd), ctypes.c_void_p(rgn), True)
        return bool(ok)
    except Exception:
        return False


def show_no_activate(window):
    """显示窗口但不激活（贴片用）。"""
    hwnd = window_handle(window)
    if hwnd and IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOWNOACTIVATE)
            return True
        except Exception:
            pass
    try:
        window.show()
        return True
    except Exception:
        return False


def show_window(hwnd):
    """把窗口显示出来（会激活）。纯 Win32，**不经过 pywebview 的 window 对象**。

    为什么要绕开 ``window.show()``：从非 UI 线程（命令泵 / 托盘线程）调用
    pywebview 的窗口方法会在内部等 UI 线程，而 UI 线程此时正卡在别处，
    于是**调用方永久阻塞**。实测症状：点托盘左键，窗口被 ``move()`` 里的
    ``SWP_SHOWWINDOW`` 带出来了，但 ``show_window()`` 卡在
    ``self.window.resize(...)`` 再也不往下走，``_visible`` 停在 True 之前
    的状态 —— 再点一次也不收起，表现成「托盘左键只能开、不能关」。
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        import ctypes
        ctypes.windll.user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOW)
        return True
    except Exception:
        return False


def hide_window(hwnd):
    """把窗口藏起来。纯 Win32，理由同 ``show_window``。"""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        import ctypes
        ctypes.windll.user32.ShowWindow(ctypes.c_void_p(hwnd), SW_HIDE)
        return True
    except Exception:
        return False


def set_topmost(hwnd, on=True):
    """把窗口钉在最前（或取消）。纯 Win32，理由同 show_window。"""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        import ctypes
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(hwnd), HWND_TOPMOST if on else HWND_NOTOPMOST,
            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        return True
    except Exception:
        return False


def _dpi_scale():
    """当前主屏缩放倍率（1.0 / 1.25 / 1.5 / 2.0…）。失败返回 1.0。

    为什么不直接复用 ``main.dpi_scale()``：那会造成 winui ↔ main 循环导入
    （main 在模块级就已经 ``import winui``）。这里自己读一遍，算法保持一致。

    ⚠ 与 ``main.dpi_scale()`` 同一个坑：``GetDC`` / ``GetDeviceCaps`` 不声明
    类型时 HDC 会被截断，``ReleaseDC`` 偶发 ``OverflowError: int too long to
    convert``，于是这里返回 1.0，``move_resize`` 少乘 1.5 → 窗口只有设计尺寸
    的 2/3 并整体偏向左上。改成优先 ``GetDpiForSystem``（不碰句柄）。
    """
    if not IS_WINDOWS:
        return 1.0
    import ctypes

    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
        if dpi > 0:
            return float(dpi) / 96.0
    except Exception:
        pass
    try:
        desk = ctypes.windll.user32.GetDesktopWindow()
        dpi = int(ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(desk)))
        if dpi > 0:
            return float(dpi) / 96.0
    except Exception:
        pass
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetDC.restype = ctypes.c_void_p
        user32.GetDC.argtypes = [ctypes.c_void_p]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.GetDeviceCaps.restype = ctypes.c_int
        gdi32.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]

        hdc = user32.GetDC(None)
        if not hdc:
            return 1.0
        try:
            LOGPIXELSX = 88
            dpi = gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
        finally:
            user32.ReleaseDC(None, hdc)
        return (float(dpi) / 96.0) if dpi else 1.0
    except Exception:
        return 1.0


def dpi_scale_at(x, y):
    """点 ``(x, y)``（**物理像素**）所在显示器的缩放倍率。失败退回主屏的。

    为什么要按点算：多显示器各自 DPI 不同（笔记本 150% + 外接 100% 很常见）。
    用主屏倍率去换算副屏上的窗口，位置会整体偏掉一大截。这里走
    ``MonitorFromPoint`` + ``shcore.GetDpiForMonitor``，老系统退回 ``GetDpiForSystem``。
    """
    if not IS_WINDOWS:
        return 1.0
    try:
        import ctypes
        from ctypes import wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        user32 = ctypes.windll.user32
        MONITOR_DEFAULTTONEAREST = 2
        hmon = user32.MonitorFromPoint(POINT(int(x), int(y)),
                                       MONITOR_DEFAULTTONEAREST)
        if hmon:
            dpi_x = ctypes.c_uint(96)
            dpi_y = ctypes.c_uint(96)
            try:
                MDT_EFFECTIVE_DPI = 0
                # shcore.GetDpiForMonitor 在 Win8.1+ 才有；返回 0 表示成功
                hr = ctypes.windll.shcore.GetDpiForMonitor(
                    ctypes.c_void_p(hmon), MDT_EFFECTIVE_DPI,
                    ctypes.byref(dpi_x), ctypes.byref(dpi_y))
                if hr == 0 and dpi_x.value:
                    return float(dpi_x.value) / 96.0
            except Exception:
                pass
        # 退一步：整系统的 DPI（Win10 1607+）
        try:
            dpi = int(user32.GetDpiForSystem())
            if dpi:
                return float(dpi) / 96.0
        except Exception:
            pass
    except Exception:
        pass
    return _dpi_scale()


# ---------------------------------------------------------------- 显示器

def monitor_work_area_for_point(x, y):
    """点 ``(x, y)``（**物理像素**）所在显示器的工作区（物理像素）。

    返回 ``(left, top, right, bottom)``；失败返回 None（调用方降级到主屏）。

    为什么不用 ``SystemParametersInfo(SPI_GETWORKAREA)``：那个只认**主屏**。
    任务栏在副屏上、或副屏与主屏尺寸不同时，主屏的工作区会把窗口夹到
    错误的位置（表现是「点托盘图标，窗口跑到另一块屏幕上去了」）。
    """
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        MONITOR_DEFAULTTONEAREST = 2
        hmon = user32.MonitorFromPoint(POINT(int(x), int(y)),
                                       MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return None
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
            return None
        r = mi.rcWork
        if r.right <= r.left or r.bottom <= r.top:
            return None
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def monitor_rect_for_point(x, y):
    """点所在显示器的**整块**矩形（物理像素，含任务栏区域）。失败 None。"""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        MONITOR_DEFAULTTONEAREST = 2
        hmon = user32.MonitorFromPoint(POINT(int(x), int(y)),
                                       MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return None
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
            return None
        r = mi.rcMonitor
        if r.right <= r.left or r.bottom <= r.top:
            return None
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def cursor_pos():
    """当前鼠标位置（物理像素）。失败返回 None。"""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        pt = POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return (pt.x, pt.y)
    except Exception:
        pass
    return None


def _clamp_to_monitor(px, py, pw, ph):
    """把物理像素矩形夹进它所在显示器的**整块**屏幕范围，返回新的 ``(px, py)``。

    为什么需要这一步（用户报「有时候有一半超出右边屏幕」）：横向位置是
    「图标中心 - 窗口宽/2」算出来的，一旦 ``dpi_scale()`` 拿到错误的值、
    或者锚点坐标是别的显示器的，算出来的 x 就可能落在屏幕外。与其在上游
    猜哪一步错了，不如在最后一道关口 ``SetWindowPos`` 之前硬夹一次 ——
    无论前面怎么算，窗口都不会跑到看不见的地方去。

    用**整块屏幕**而不是工作区：窗口本来就贴在任务栏上方，用工作区夹会
    让它在任务栏横向让位时被额外推走。
    """
    mon = monitor_rect_for_point(px + pw // 2, py + ph // 2)
    if not mon:
        return px, py
    left, top, right, bottom = mon
    if pw >= right - left:
        px = left
    else:
        px = max(left, min(px, right - pw))
    if ph >= bottom - top:
        py = top
    else:
        py = max(top, min(py, bottom - ph))
    return px, py


def move_resize(hwnd, x, y, w, h, clamp=False):
    """移动并缩放窗口，纯 Win32（不经过 pywebview，理由同 show_window）。

    参数是**逻辑像素**，函数内部按 ``_dpi_scale()`` 换算成物理像素。

    ``clamp=True`` 时在 ``SetWindowPos`` 之前把矩形硬夹进显示器范围，
    保证窗口永远不会有一半跑到屏幕外（见 ``_clamp_to_monitor``）。
    贴片窗**不要**传 True：它是任务栏的子窗口，坐标是相对宿主的。

    ⚠ 这里踩过一个很隐蔽的坑（2026-11-02）：``SetWindowPos`` 收的是
    **物理像素**，而本模块的 ``pill_rect()`` 和 main 里的 ``main_rect()``
    为了跟 pywebview（WinForms，逻辑像素）对齐，返回的都是**逻辑像素**。
    早期把逻辑像素直接丢给 ``SetWindowPos``，在 150% 缩放的机器上窗口
    只有设计尺寸的 2/3（392 逻辑被当成 392 物理 = 261 逻辑），位置也整体
    偏向左上 —— 表现是「窗口窄了一圈、还没贴到任务栏」。

    用 ``SWP_NOZORDER`` 而不是默认值：窗口顺序由 ``on_top`` 管，
    这里只关心几何。不带 ``SWP_SHOWWINDOW``，所以不会顺手把窗口显示出来。
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        import ctypes
        scale = _dpi_scale() or 1.0
        px = int(round(x * scale))
        pw = max(1, int(round(w * scale)))
        ph = max(1, int(round(h * scale)))
        if clamp:
            # **底边取整**而不是顶边取整：主窗是「底边钉在任务栏上沿、只往上
            # 长」，顶边和高度各自 round 会让底边在 ±1 物理像素之间抖（实测
            # 切页动画里出现过 1515 / 1516 交替），肉眼就是窗口底边轻轻颤动。
            # 先按底边定位再回推顶边，底边就严格对齐 round((y+h)*scale)。
            py = int(round((y + h) * scale)) - ph
            px, py = _clamp_to_monitor(px, py, pw, ph)
        else:
            py = int(round(y * scale))
        ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(hwnd), None,
            px, py, pw, ph,
            SWP_NOZORDER | SWP_NOACTIVATE)
        return True
    except Exception:
        return False


def window_rect(hwnd):
    """窗口的屏幕矩形（**物理像素**）``(left, top, right, bottom)``。失败返回 None。

    为什么不读 pywebview 的 ``window.x/y``：用户拖过窗口之后，位置是系统
    通过 ``WM_NCLBUTTONDOWN`` 改的，pywebview 那边的属性**不会跟着更新**。
    要做「拖动后记住位置」就只能问 Win32 要真实矩形。"""
    if not IS_WINDOWS or not hwnd:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        rect = wt.RECT()
        if not ctypes.windll.user32.GetWindowRect(
                ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return None
        if rect.right <= rect.left:
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        return None


def is_window(hwnd):
    """句柄还有效吗。

    为什么要这一步：``_hwnd_cache`` 里的句柄可能已经随窗口销毁失效
    （贴片开关关了就 DestroyWindow），拿旧句柄去 ``SetWindowPos`` 会失败
    但不报错，症状是「关了再开，贴片不见了」。
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsWindow(ctypes.c_void_p(int(hwnd))))
    except Exception:
        return False


def main():
    """命令行自检：打印当前系统能做到哪一步。"""
    print("platform       :", sys.platform)
    print("windows        :", IS_WINDOWS)
    if IS_WINDOWS:
        try:
            import platform
            print("windows version:", platform.version())
        except Exception:
            pass
    # 无窗口时只能测「调用不炸」
    print("apply_blur(None)       ->", apply_blur(None))
    print("hide_from_taskbar(None) ->", hide_from_taskbar(None))
    print("pill_rect(200, 24)     ->", pill_rect(200, 24))
    return 0


if __name__ == "__main__":
    sys.exit(main())
