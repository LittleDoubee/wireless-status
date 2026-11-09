# -*- coding: utf-8 -*-
"""鼠标小窗主程序 —— 设备列表 → 设备功能区，两页结构

================================================================ 结构
  * ``Api``            pywebview 的 js_api 桥（前端 window.pywebview.api.*）
  * ``WirelessStatusApp`` 生命周期：采集线程、HTTP、托盘、两个窗口（主窗 + 贴片）
  * ``main()``         单实例 + 异常兜底

================================================================ 三个必须保持的坑
1. ``COMPLUS_LoadFromRemoteSources=1`` **必须在 import webview 之前**设置。
   .NET 4.0+ 默认禁止从远程/不受信位置加载程序集，而 PyInstaller onefile
   把 dll 解到 %TEMP%\\_MEIxxxx，CLR 判定为不受信来源，结果打包后报
   ``RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize``。
   见 ``prepare_dotnet_runtime()``。

2. ``screen_work_area()`` 必须返回**逻辑像素**（已经除过 ``dpi_scale()``）。
   DPI 感知进程的 SPI_GETWORKAREA 给的是物理像素（150% 缩放下 2560），
   而 pywebview 的 create_window 收逻辑像素（1707）。混用会让窗口跑到
   屏幕外，界面上什么都看不到 —— 这个坑踩过一次，别再踩。

3. ``web/style.css`` 不能带 ``# -*- coding: utf-8 -*-`` 文件头。CSS 里 ``#``
   是 ID 选择器，那行会把紧跟的 ``:root {...}`` 整块吃掉，所有颜色和圆角
   全失效，症状是「文字能看见，但按钮白字白底完全不可见」。

================================================================ 窗口行为（v2 改动）
  * 主窗**不再可拖动**，每次显示都按 ``place_above_taskbar()`` 重算位置，
    贴在主屏任务栏上沿。``window_state.json`` 继续写，但只作记录/诊断，
    **启动时不读它的坐标**（需求明确要求）。
  * 主窗与贴片窗都设 ``WS_EX_TOOLWINDOW`` 去掉 ``WS_EX_APPWINDOW``，
    所以任务栏不会出现应用按钮；owner 设成托盘的隐藏窗口，保证
    「点任务栏图标不会单独把窗口调到前台」这类怪异行为不会发生。
  * 启动静默：默认只在托盘出现，不起窗。``--show`` 参数给调试用。
"""

import os
import sys
import json
import time
import queue
import ctypes
import threading
import traceback

import apppath
import autostart
import devices
import history
import mouse_source
import server
import tipwindow
import usage
import winui

APP_TITLE = "鼠标电量"
PILL_TITLE = "鼠标电量贴片"
APP_VERSION = apppath.APP_VERSION   # 收口到 apppath，托盘提示和 /api/ping 必须是同一个

MUTEX_NAME = "WirelessStatus.SingleInstance.v3"
ERROR_ALREADY_EXISTS = 183

#: ``SetEvent`` 需要的访问权限（winnt.h 的 EVENT_MODIFY_STATE）。
EVENT_MODIFY_STATE = 0x0002

#: 命令泵的取队列超时（秒）。空闲时按这个节奏做维护，也按它轮询光标
#: 是否已经离开主窗（自动收起用）。0.2s 是为了让「离开 2 秒收起」的判定
#: 误差不超过 200ms；不能更小，否则每秒要好几次 provider 快照。
PUMP_INTERVAL = 0.2

#: 指针离开主窗多久后自动收起（秒）。以前全靠前端 ``document.mouseleave``，
#: 但那个事件在「快速甩出去」「点完按钮顺手移开」「窗口失焦」这些真实操作
#: 里经常不触发，用户就会觉得「移开了却不收」。改成后端量真实光标位置，
#: 不依赖任何 DOM 事件。
AUTO_HIDE_SEC = 2.0

#: 主窗尺寸（逻辑像素）。高度按「列表页 3 行 + 贴片开关」调过。
MAIN_WIDTH = 392
MAIN_HEIGHT = 588          # 初始高度，之后由前端 set_height() 按内容调

#: 主窗高度夹取范围。低于 MIN 时两页都挤不下，高于 MAX 就没必要了。
#: 主窗高度范围（逻辑像素）。下限按「标题栏 40 + 一行设备 77 + 底栏 39」
#: 再加一点余量取 168 —— 列表页只有一台设备时窗口就是个紧凑小卡片，
#: 不会出现一大片空白；设备多了窗口往上长，超过 MAX_HEIGHT 后内部滚动。
MIN_HEIGHT = 168
MAX_HEIGHT = 760

#: 高度变化的动画时长（秒）与帧间隔。用户要求「从列表到详情，高度变大的过程
#: 要有动画，看起来更顺畅」。180ms 是肉眼刚好能看出是「长上去」而不至于
#: 让人等的一档；16ms ≈ 60fps。
ANIM_MS = 180
ANIM_FRAME_SEC = 0.016

#: 贴片尺寸（逻辑像素）。
#:
#: 宽 200 是**窗口**宽度 —— 内容只有那么宽，靠右贴住托盘图标，左边那段空白
#: 靠窗口区域裁剪彻底剪掉（见 ``winui.clip_round_rect``），所以视觉上只看得到
#: 一行文字，不会是一条长条。
#:
#: 高度 26 是**单行文字**的高度：图标 16 + 上下各 5 的余量。任务栏 32 逻辑
#: 像素时上下各留 3 像素，文字正好和托盘图标同一水平线居中。
#:
#: ⚠ 已**没有**「胶囊」这个概念了（用户需求 2）：贴片不再是一枚带底色、
#: 带描边、带圆角的胶囊，而是像 LiteMonitor 那样**直接画在任务栏上的文字**。
#: 窗口底色 = 任务栏采样色（见 ``_pill_background_color``），圆角半径 0，
#: 于是窗口边界看不出来，只剩「图标 + 数值」浮在任务栏上。
PILL_WIDTH = 200
PILL_HEIGHT = 26

#: 贴片圆角半径（逻辑像素）。**0 = 直角**。
#:
#: 扁平样式必须直角：带圆角时窗口四角会被裁掉，而窗口底色就是任务栏色，
#: 裁掉之后四角露出的是**任务栏自己**，颜色对不上时就成了一圈浅色缺口。
#: 直角 + 同色 = 完全看不出边界。
PILL_RADIUS = 0

#: 贴片窗口宽度范围（逻辑像素）。前端量出内容实际宽度后报告，窗口按这个
#: 宽度收窄/加宽，避免多余空白挡住任务栏点击。
#:
#: 下限 60 是「一个图标 + 一个百分比」的极限（实测 90% 时约 46 宽，留余量）；
#: 上限 420 是**一行并列 4 台设备**的宽度（图标 + 两位数字 + 间隔 ≈ 60/台，
#: 加 +N）。再宽就该考虑收进 +N 了，否则会顶到任务栏左边的应用图标区。
PILL_MIN_WIDTH = 60
PILL_MAX_WIDTH = 420

#: 窗口贴任务栏上沿时留的边距（逻辑像素）。
TASKBAR_MARGIN = 8

#: 自定义消息：第二次启动时用它唤起已有实例的窗口。
WM_SHOW_EXISTING = ctypes.windll.user32.RegisterWindowMessageW(
    "WirelessStatusShowExisting") if sys.platform == "win32" else 0

#: 「请把主窗显示出来」的跨进程信号。
#:
#: 为什么不用 ``PostMessage(WM_SHOW_EXISTING)``：主窗的 WndProc 是 pywebview
#: （WinForms）的，我们没法从外部挂自己的消息处理函数 —— 发过去只会落进
#: WinForms 的默认分支，什么都不会发生。命名事件是纯 Win32、跨进程、
#: 不需要任何窗口句柄，命令泵每 0.2s 顺手查一次即可。
WAKE_EVENT_NAME = "WirelessStatus.ShowRequest.v3"


# ---------------------------------------------------------------- 环境准备

def prepare_dotnet_runtime():
    """让 CLR 允许从 PyInstaller 的解包目录加载程序集。

    必须在 ``import webview`` 之前调用，因为 pywebview 在 Windows 上会
    立刻 `import clr`（走 pythonnet + WinForms 后端）。

    背景（血泪）：.NET Framework 4.0 起，默认策略不允许从「远程或不受信
    位置」加载程序集。PyInstaller onefile 把 Python.Runtime.dll 解到
    ``%TEMP%\\_MEIxxxxxx``，CLR 把它当远程来源，于是
    ``pyclr_get_function`` 对所有程序集（包括系统 mscorlib）返回 NULL，
    pythonnet 报 ``Failed to resolve Python.Runtime.Loader.Initialize``。

    试过但**无效**的方案（不要再试）：
      * ``--collect-all pythonnet`` / ``--collect-all clr_loader``
      * ``os.add_dll_directory(_MEIPASS)``、把 dll 复制到 exe 同目录
      * onedir 打包、exe.config 加 supportedRuntime、COMPLUS_Version
      * 切 coreclr（.NET 8 的 WinForms 删了 ContextMenu，WebView2 起不来）
      * pythonnet 2.5.x / 3.0.5 各种组合

    唯一有效的是这个环境变量。
    """
    os.environ.setdefault("COMPLUS_LoadFromRemoteSources", "1")


def prepare_working_directory():
    """把进程 CWD 挪到运行时目录，别让第三方 DLL 往 exe 旁边写日志。

    ============================================================
    这个函数解决的是 ``debug.log``（搜狗输入法 SGRenderLog）
    ============================================================
    现象：exe 一启动，同目录（双击时是 exe 目录）就多出一个
    ``debug.log``，内容是 ``t_skinfilemap.cpp`` 的 ``SGRenderLog`` 警告，
    指向 ``Local\\sgime_skin.v2.filemap.sogouime``。跟本程序毫无关系。

    根因：搜狗输入法的 TSF 组件（``SogouTSF.ime`` / ``SogouPY.ime`` /
    ``Resource.dll`` / ``isgpet_bundle64.dll``）会注入到任何带窗口的进程，
    包括我们的 exe 和 WebView2 的浏览器进程。它用 Chromium 的日志设施
    打警告，而 Chromium 在没指定 ``--log-file`` 时默认写到**当前工作目录**
    的 ``debug.log``。

    为什么不能靠关日志：pywebview 在 ``platforms/edgechromium.py:82`` 里
    硬编码 ``props.AdditionalBrowserArguments = '--disable-features=...'``，
    会覆盖 ``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`` 环境变量，加
    ``--disable-logging`` / ``--log-level=3`` 都不生效（实测过）。

    所以只能改 CWD。本程序所有文件访问都走绝对路径（``apppath.app_dir()``
    / ``resource_dir()``），全项目没有一处相对路径 ``open()``，改 CWD
    不影响功能。

    顺带清掉旧版本留在 exe 旁边的同名垃圾（``apppath.purge_cwd_junk``），
    用户升级后不会再看到历史遗留的那个文件。
    """
    try:
        apppath.purge_cwd_junk(apppath.app_dir())
    except Exception:
        pass
    target = None
    try:
        target = apppath.runtime_dir()
    except Exception:
        target = None
    if not target:
        return None
    try:
        os.chdir(target)
        return target
    except Exception:
        # 换目录失败也不致命 —— 只是垃圾会继续落在原 CWD。
        return None


def enable_dpi_awareness():
    """声明 DPI 感知，否则 150% 缩放下界面是糊的。

    优先用 Per-Monitor V2（``SetProcessDpiAwarenessContext(-4)``），
    老系统退回 shcore 的 ``SetProcessDpiAwareness(2)``。
    """
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _setup_logging():
    """把 Python 侧异常写进 ``ui_error.log``。

    为什么必须做：pywebview 的 ``js_bridge_call`` 把 API 抛出的异常只交给
    ``logger.error`` —— 而 ``pythonw.exe`` 和 ``-w`` 打的 exe **没有控制台**，
    也没有装 logging handler，于是异常彻底消失：点击没反应、界面卡住或
    变黑，磁盘上一点痕迹都没有。这正是「点了就报错却查不到」的根源。

    这里挂一个 FileHandler 到 pywebview 的 logger 上，任何 API 异常都会带
    完整堆栈落盘，用户把 ui_error.log 发过来就能定位。
    """
    import logging
    try:
        log = os.path.join(apppath.app_dir(), "ui_error.log")
        # delay=True：没出错就不生成空文件，目录保持干净
        handler = logging.FileHandler(log, encoding="utf-8", delay=True)
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
        for name in ("pywebview", "webview"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.WARNING)
            lg.addHandler(handler)
            lg.propagate = False
    except Exception:
        # 日志通道本身失败也绝不能拦住启动
        pass


def dpi_scale():
    """当前主屏缩放倍率（1.0 / 1.25 / 1.5 / 2.0…）。失败返回 1.0。

    为什么要它：进程声明 DPI 感知后，Win32 的坐标 API 都返回**物理像素**，
    而 pywebview（WinForms）用的是**逻辑像素**。两者相差正好是这个倍率。

    ⚠ 这里曾经只走 ``GetDC(0)`` + ``GetDeviceCaps`` 一条路，而这两个函数
    **没有声明 argtypes/restype**：64 位下 HDC 是指针，ctypes 默认按
    ``c_int`` 收发，句柄被截断，``GetDeviceCaps`` / ``ReleaseDC`` 偶发抛
    ``OverflowError: int too long to convert``，整个函数掉进 except 返回
    **1.0**。于是 ``main_rect()`` 按 1.0 算出一套逻辑坐标，``move_resize``
    又乘 1.5 变成物理坐标：横向 2152 逻辑 → 3228 物理（屏幕才 2560 宽），
    纵向 1352 逻辑 → 2028 物理（屏幕才 1600 高）。用户看到的就是
    「打开后位置错位、会漂移、有时候一半超出右边屏幕」。

    所以现在优先走**不碰句柄**的 ``GetDpiForSystem``（150% 缩放返回 144），
    它返回的正是系统 DPI，也正好是 WinForms 做逻辑坐标换算的倍率。次选
    ``GetDpiForWindow(桌面窗口)``。``GetDC`` 那条路降为最后兜底，并且补齐
    了 argtypes/restype。
    """
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

    # 最后兜底：GetDeviceCaps。必须显式声明类型，否则就是上面说的截断问题。
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
        if dpi:
            return float(dpi) / 96.0
    except Exception:
        pass
    return 1.0


def screen_work_area():
    """主屏工作区（**逻辑像素**）：``(left, top, width, height)``。

    SPI_GETWORKAREA 拿到的已经是「去掉任务栏」的区域，所以贴它的底边
    就是任务栏上沿。返回值统一除以 ``dpi_scale()``，与 pywebview 对齐。
    """
    scale = dpi_scale() or 1.0
    try:
        from ctypes import wintypes

        rect = wintypes.RECT()
        SPI_GETWORKAREA = 0x0030
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        if ok:
            return (int(rect.left / scale), int(rect.top / scale),
                    int((rect.right - rect.left) / scale),
                    int((rect.bottom - rect.top) / scale))
    except Exception:
        pass
    # 兜底：假设 1920x1080 且任务栏 48 高
    return (0, 0, 1920, 1032)


def logical_screen():
    """整屏尺寸（逻辑像素），给贴片算右对齐用。"""
    scale = dpi_scale() or 1.0
    try:
        return (int(ctypes.windll.user32.GetSystemMetrics(0) / scale),
                int(ctypes.windll.user32.GetSystemMetrics(1) / scale))
    except Exception:
        return (1920, 1080)


# ---------------------------------------------------------------- js_api

class Api(object):
    """暴露给前端的接口（``window.pywebview.api.*``）。

    注意：**别在这里放 ``self.app``**，pywebview 会把 js_api 对象的属性
    递归序列化传给 JS，遇到 native 对象会爆栈，症状是「界面一直转圈、
    Python 侧没有任何报错」。所以统一用 ``self._app``（下划线开头，
    pywebview 会跳过）。
    """

    def __init__(self, app):
        self._app = app

    # ---- 视图 / 窗口（前端可能在任何一次 api 调用时首次触发就绪流程）
    def ready(self):
        """前端加载完后主动调一次：套装饰 + 定位。

        单独给一个 ready() 而不是只靠 snapshot()，是因为前端可能先渲染
        静态骨架、稍后才拉数据；有个显式钩子更容易排查「装饰没生效」。
        """
        self._app._ensure_ready()
        return {"ok": True}

    def set_height(self, height):
        """前端按内容量报告需要的高度，主窗跟着调（避免大片空白）。"""
        return self._app.set_height(height)

    def set_pill_width(self, width):
        """前端量出贴片胶囊的实际宽度，窗口跟着收窄（剪掉左边空白）。"""
        return self._app.set_pill_width(width)

    def set_pill_hover(self, key="", hovering=False):
        """贴片悬停：显示 / 收起设备信息气泡（需求 4）。

        气泡是**独立的 Win32 顶层窗**（``tipwindow.py``）—— WebView2 画不出
        贴片窗口之外的东西，用 HTML tooltip 会被 SetWindowRgn 裁掉。
        """
        return self._app.set_pill_hover(key, hovering)

    def open_window(self):
        return self._app.open_window()

    # ---- 数据
    def snapshot(self):
        return self._app.snapshot()

    def devices(self):
        return self._app.devices()

    def device(self, key):
        return self._app.device(key)

    def history(self, range_name="24h", key=""):
        # 注意：**不能写 self._app.history(...)** —— WirelessStatusApp 上
        # ``self.history`` 是 HistoryStore **实例属性**，会把同名方法整个
        # 遮蔽掉，调用直接抛 TypeError: 'HistoryStore' object is not callable。
        # 这正是「点了界面就报错」的根因之一，务必走 history_data()。
        return self._app.history_data(range_name, key)

    def usage(self):
        # 同上：self._app.usage 是 UsageTracker 实例，会遮蔽方法。
        return self._app.usage_data()

    # ---- 操作
    def refresh(self):
        return self._app.refresh()

    def set_dpi(self, value, key=""):
        return self._app.set_dpi(value, key)

    def set_autostart(self, on):
        return self._app.set_autostart(on)

    def set_pill_enabled(self, on):
        return self._app.set_pill_enabled(on)

    def select_device(self, key):
        return self._app.select_device(key)

    def set_config(self, key, value):
        """通用配置写入。白名单在 Api 层拦一道，App 层再拦一道。

        Api 层先挡一次，是为了「未知键」这种情况不必真的构造 App 就能拒绝
        （自测里空 App 也能验证）；App 层是真正的写入点，它还要处理
        battery_pill_enabled 这类需要立刻产生副作用的键。
        """
        if key not in apppath.DEFAULT_CONFIG:
            return {"ok": False, "error": "未知配置项：%s" % key}
        return self._app.set_config(key, value)

    def hide_window(self):
        return self._app.hide_window()

    def start_drag(self):
        """拖动把手按下时调：交给系统做窗口移动。"""
        return self._app.start_drag()

    def finish_drag(self):
        """拖动松手时调：记下位置并落盘。"""
        return self._app.finish_drag()

    def taskbar_metrics(self):
        """贴片前端要的任务栏度量（高度档位 / 深浅色）。"""
        return self._app.taskbar_metrics()

    def quit(self):
        return self._app.quit()

    def status(self):
        return self._app.status()

    def set_visible(self, visible):
        return self._app.set_visible(visible)

    def report_error(self, message, detail=""):
        """前端把未捕获异常报上来，落盘到 ui_error.log。

        为什么需要这条通道：exe 是 ``-w`` 打的，WebView2 里的 JS 异常
        默认什么都不留 —— 界面白屏或变黑，用户只看到「点一下就报错」，
        而 ``error.log`` 里干干净净。有了它，前端任何未捕获异常都会带
        堆栈落到程序目录，可复现、可排查。
        """
        try:
            log = os.path.join(apppath.app_dir(), "ui_error.log")
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(log, "a", encoding="utf-8") as fh:
                fh.write("[%s] %s\n%s\n\n" % (stamp, message, detail))
        except Exception:
            pass
        return {"ok": True}


# ---------------------------------------------------------------- 主程序

class WirelessStatusApp(object):
    def __init__(self, show_at_start=False):
        self.store = apppath.Store()
        cfg = self.store.config

        # ---- 采集层：复用验证过的 MouseSource，外面套 HidppProvider
        self.source = mouse_source.MouseSource(
            interval=float(cfg.get("poll_interval_sec") or cfg.get("poll_interval") or 5.0),
            interval_hidden=float(cfg.get("poll_interval_hidden") or 30.0),
            device_index=int(cfg.get("device_index") or 0),
        )
        self.provider = devices.HidppProvider(
            source=self.source,
            curve=cfg.get("battery_curve") or "alkaline",
            levels=cfg.get("battery_levels") or {},
        )
        self.source.battery_curve = cfg.get("battery_curve") or "alkaline"

        # ---- 历史与使用时长
        self.history = history.HistoryStore(apppath.history_path())
        self.usage = usage.UsageTracker(apppath.usage_path())

        self.httpd = None
        self.window = None
        self.pill_window = None
        # 贴片窗当前宽度：前端量出胶囊实际宽度后调窄，避免空白区挡住
        # 任务栏上的其它图标（见 set_pill_width）
        self._pill_width = PILL_WIDTH
        self.tray = None
        self.api = Api(self)

        self._queue = queue.Queue()
        self._pump_stop = threading.Event()
        self._visible = False
        self._quitting = False
        self._tray_hint = ""
        self._show_at_start = show_at_start
        self._selected = cfg.get("last_selected_device")
        self._pill_error = ""
        self._hwnd_cache = {}
        #: 首次就绪（套装饰 + 定位）是否已做，见 _ensure_ready
        self._ready_done = False
        #: 窗口装饰是否已套（见 _apply_window_chrome）
        self._chrome_done = False
        #: 当前窗口高度（前端会按内容调，见 set_height）
        self._cur_height = MAIN_HEIGHT
        #: 最近一次点托盘图标时的图标中心（物理像素）。主窗据此摆到图标正上方。
        self._anchor = None
        #: 「下一次 show 用这个锚点」—— 托盘线程设置、命令泵消费，避免
        #: 跨线程直接读写同一个状态。
        self._pending_anchor = None
        #: 用户是否手动拖过窗口。拖过之后**只沿用它的 x**，y 永远由任务栏
        #: 上沿反推（见 ``main_rect``）—— 窗口是「底边贴任务栏、往上长」，
        #: 这样切换设备导致高度变化时底边不动，视觉上位置是稳的（需求 1）。
        #:
        #: 启动时**一律 False**：需求 1 要求「设备界面在托盘图标上方」，
        #: 若沿用 window_state 里的旧坐标，首次显示会被带到上次的位置。
        self._dragged = False
        #: 锁定的横向位置（逻辑像素）。第一次摆好后就固定下来，
        #: 之后切设备 / 改高度 / 重排都复用它 —— 需求 1 的「位置不变」。
        #:
        #: **一旦锁住就不再重算**：以前每次点托盘图标都清掉它重算，
        #: 而「按图标中心」和「按通知区中心」两个基准差 40 多像素，
        #: 于是每点一次托盘窗口就横跳一次。稳定比精确更重要。
        self._fixed_x = None
        #: 指针离开主窗的起始时间（见 ``_poll_cursor_leave``）。
        #: 前端 ``document.mouseleave`` 在快速移出/点击后经常不触发，
        #: 收起改由这里按真实光标位置判定。
        self._cursor_out_since = None
        #: 上一次生效的内容高度。和 ``_cur_height`` 分开：后者可能被
        #: 前端首帧误报的极小值污染，这里只记「真正应用过的」高度。
        self._applied_height = MAIN_HEIGHT
        #: 是否正在拖动。拖动时不做「指针离开就收起」的判定。
        self._dragging = False
        #: 托盘溢出面板（^ 弹出的那块）上一次的开合状态。None = 还没测过。
        #: 见 ``_poll_panel_state``：状态一变就重排主窗，避免面板盖住窗口。
        self._panel_open = None
        #: 高度动画的目标值 / 是否已有动画线程在跑 / 保护这两个字段的锁。
        #: 见 ``_animate_to``：动画期间又来了新目标就只改目标值，不另起线程。
        self._anim_target = None
        self._anim_running = False
        self._anim_lock = threading.Lock()
        #: 一次性信号：下次取快照时让前端退回设备列表页（需求 4）。
        #: ``show_window`` 置 True，``snapshot`` 消费后清掉。
        self._want_list = False
        #: 电量历史的写入节流时间戳（见 ``_record_history``）
        self._last_history = 0.0
        #: 上一次喂给历史库的采样时刻（见 ``_housekeeping``）
        self._last_sample = 0.0
        #: 最近一次快照里的设备列表。前端线程写、命令泵线程读，只做整体替换。
        self._last_devices = []
        #: 贴片是不是真的挂进了任务栏（False = 走降级浮窗路径）
        self._pill_embedded = False
        #: 贴片所在任务栏的物理高度（给前端选字号档位）
        self._pill_bar_height = 0
        #: 贴片自己的物理矩形 (x, y, w, h)。前端采样任务栏底色时按它取
        #: 邻域 —— 任务栏底色横向有渐变，全条众数会偏暗十几级；提示气泡
        #: 也按它定位（摆在它正上方）。
        self._pill_geom = None
        #: 悬停提示气泡（独立 Win32 顶层窗，见 tipwindow.py）。懒启动 ——
        #: 贴片开关关着时完全不会创建线程。
        self._tip = None
        #: 当前指针悬停的设备 key（前端上报），None = 没悬停
        self._pill_hover_key = None
        #: 「第二个实例请我显示窗口」的命名事件句柄（见 _poll_post_message）
        self._wake_event = None

    # ---------------------------------------------------------------- 启动

    def run(self):
        prepare_dotnet_runtime()      # 必须在 import webview 之前
        _setup_logging()              # pywebview 的异常也要落盘（见函数说明)

        # 关掉 WebView2 的「按显示器色彩配置文件做色彩管理」。
        #
        # 为什么：贴片底色是从任务栏截图采样出来的**屏幕像素值**，再原样
        # 写成 CSS 颜色。不关色彩管理时 WebView2 会按 ICC 配置文件做一次
        # 变换，实测写 (226,246,246) 显示成 (219,249,249) —— 蓝绿通道各
        # 偏了 3-7 级，看起来就是「颜色差不多但就是不对」。
        # 这个开关必须在 create_core_webview 之前设，pywebview 里没有暴露
        # 对应参数，只能走环境变量。
        os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                              "--force-color-profile=srgb")

        import webview

        # 把 CWD 挪到运行时目录：搜狗输入法注入到本进程/WebView2 的 TSF
        # 组件会用 Chromium 日志设施往 CWD 写 debug.log，必须换地方。
        # 详见 prepare_working_directory() 的说明。
        prepare_working_directory()

        enable_dpi_awareness()
        autostart.heal()              # 切换过运行方式就把注册表改回来

        self.source.start()
        self.usage.start()

        self.httpd = server.LocalServer(
            provider=self.provider,
            history=self.history,
            usage=self.usage,
        )
        url = self.httpd.start()

        self._start_tray()

        # ---- 主窗：不可拖动、贴任务栏上沿、不占任务栏按钮
        w, h, x, y = self.main_rect()
        self.window = webview.create_window(
            APP_TITLE,
            url=url,
            js_api=self.api,
            width=w,
            height=h,
            x=x,
            y=y,
            frameless=True,
            easy_drag=False,          # 不拖动，位置由程序算
            on_top=True,              # 贴片/浮窗都要置顶
            transparent=True,         # 无边框 + 真圆角
            # pywebview 只认 3 位或 6 位 hex（正则 ^#(?:[0-9a-fA-F]{3}){1,2}$），
            # 写 #00000000 会直接抛 ValueError。透明度由 transparent=True 负责。
            background_color="#000000",
            resizable=False,
            # min_size 也必须跟着降到 MIN_HEIGHT：pywebview 的 resize() 受
            # 系统最小尺寸约束，写死 420 的话前端报 168 会被 WinForms 弹回
            # 420（实测：列表页单设备时窗口停在 630 下不去）。
            min_size=(MAIN_WIDTH - 40, MIN_HEIGHT),
            # 启动静默：不给焦点，避免抢走用户正在打的字
            focus=False,
            shadow=False,             # 毛玻璃自绘阴影，系统阴影会留一圈灰边
            text_select=False,
            hidden=not self._show_at_start,
        )

        # ---- 贴片窗：极小、不可拖动、不占任务栏
        if self.store.config.get("battery_pill_enabled"):
            self._create_pill_window(url)

        # 唤醒事件：第二个实例带 --show 时会 SetEvent，命令泵负责转成
        # show_window()（见 _poll_post_message）。放在 _start_pump 之前，
        # 免得第一拍轮询时句柄还是 None。
        self._wake_event = self._open_wake_event()

        self._start_pump()
        try:
            webview.start(self._after_start, gui="edgechromium", debug=False)
        finally:
            self.shutdown()

    def _start_tray(self):
        from tray import TrayIcon
        self.tray = TrayIcon(
            tooltip="%s %s" % (APP_TITLE, APP_VERSION),
            # 需求 6：左键只显示（收起由前端「鼠标移出 2 秒」触发）
            # 需求第 1 条：顺带把图标中心传过去，窗口据此摆到图标正上方。
            on_show=self._on_tray_click,
            on_toggle=lambda: self.post("toggle"),
            on_refresh=lambda: self.post("refresh"),
            on_autostart=lambda: self.post("autostart"),
            on_exit=lambda: self.post("quit"),
        )
        if not self.tray.start():
            # 托盘起不来不该让程序打不开 —— 窗口照常可用，只是没有图标。
            self._tray_hint = "托盘图标创建失败：%s" % (self.tray.error or "未知原因")

    def _on_tray_click(self):
        """托盘左键：记下图标中心，再让命令泵去显示窗口。

        为什么不直接在托盘线程里定位窗口：定位要 ``SetWindowPos``，
        而命令泵线程才是几何操作的唯一入口（跨线程碰 pywebview 会死锁）。
        这里只存一个坐标，动作交给 ``post("show")``。
        """
        try:
            point = getattr(self.tray, "icon_point", None)
        except Exception:
            point = None
        if point:
            self._pending_anchor = point
        self.post("show")

    def _create_pill_window(self, base_url):
        import webview
        pw, ph, px, py = self.pill_rect()
        try:
            self.pill_window = webview.create_window(
                PILL_TITLE,
                url=base_url.rstrip("/") + "/pill",
                # 贴片窗也要挂 js_api：前端量出胶囊实际宽度后要调
                # api.set_pill_width() 把窗口收窄（否则左边空白会挡住
                # 任务栏下层的托盘图标）。不挂的话 window.pywebview 是空的，
                # 前端那句调用静默失效，窗口永远停在初始宽度。
                js_api=self.api,
                width=pw,
                height=ph,
                x=px,
                y=py,
                frameless=True,
                easy_drag=False,
                on_top=True,
                # 贴片**不要** transparent=True：那会让 pywebview 把窗口做成
                # WS_EX_LAYERED + AllowsTransparency，实测在 WebView2 上只会
                # 留下一整块不透明黑底，把任务栏盖住。改成普通不透明窗口，
                # 底色直接取任务栏采样色（见 ``_pill_background_color``）——
                # 扁平样式下「看不见窗口边界」就是这么来的。
                transparent=False,
                # 窗口底色 = 任务栏采样色（**不再提亮**）。页面起来之前 WebView2
                # 先画的是这一块，采样值一致才不会闪一下。
                background_color=self._pill_background_color(),
                resizable=False,
                # pywebview 默认 min_size=(200, 100)，PILL_HEIGHT 高的贴片会被
                # 强行撑到 100 逻辑像素（150% 缩放下就是 150 物理像素），
                # 结果贴片是个大方块。必须显式给小尺寸。
                # 左边界给 PILL_MIN_WIDTH：多设备变宽/变窄时窗口要能跟着变。
                min_size=(PILL_MIN_WIDTH, PILL_HEIGHT),
                focus=False,
                shadow=False,
                text_select=False,
            )
            # 立刻去任务栏按钮：等 _apply_window_chrome 的话，窗口已经
            # 在任务栏闪了一下图标才消失（需求 2 明确说「开启后会在任务栏
            # 弹出一个软件图标，需要去除」）。这里创建完马上处理。
            try:
                ph_hwnd = winui.window_handle(self.pill_window)
                if not ph_hwnd:
                    ph_hwnd = self._window_hwnd(PILL_TITLE, timeout=1.5)
                if ph_hwnd:
                    self._hwnd_cache["pill"] = ph_hwnd
                    winui.hide_from_taskbar(ph_hwnd,
                                            self._main_hwnd() or None)
                    # 需求第 2 条：挂成任务栏子窗口，跟任务栏同图层。
                    # 形状裁剪由 _apply_pill_host() 在设完几何之后统一做
                    # （必须先几何后裁剪，见那里的注释）。
                    # 失败会自动退回置顶浮窗并把原因写进 _pill_error。
                    self._apply_pill_host()
                self._ensure_tip_window()
            except Exception:
                traceback.print_exc()
        except Exception:
            traceback.print_exc()
            self._pill_error = "贴片窗口创建失败"

    def _ensure_tip_window(self):
        """按需创建悬停提示窗（独立线程，见 tipwindow.py）。

        懒启动的理由：贴片开关关着时用户根本不会再悬停到它上面，为一个
        永远不显示的气泡常驻一条消息泵线程不值得。失败只记一笔，不抛 ——
        提示窗坏了不该让贴片或主程序跟着挂。
        """
        if self._tip is not None:
            return self._tip
        try:
            tip = tipwindow.TipWindow()
            if tip.start():
                self._tip = tip
            else:
                self._pill_error = "悬停提示窗创建失败：%s" % (tip.error or "未知原因")
        except Exception:
            traceback.print_exc()
            self._pill_error = "悬停提示窗创建失败"
        return self._tip

    def _after_start(self):
        """``webview.start()`` 的回调 —— **这里什么窗口操作都不能做**。

        ============================================================
        踩过的大坑（务必保留这段说明，不然下次还会踩）
        ============================================================
        pywebview 的 ``start()`` 实现是：

            if func: thread.start()          # ← 我们的回调，在新线程
            guilib.create_window(windows[0]) # ← 窗口才刚开建

        回调是在**另一个线程**里、在窗口创建**之前**启动的。此时：

          * ``window.native`` 还是 None；
          * 任何 ``window.show()`` / ``move()`` / ``resize()`` 都会卡死；
          * 任何给窗口发同步消息的 Win32 调用（SetWindowLongPtr +
            SetWindowPos 那种）也会卡死 —— 因为 UI 线程正卡在建
            WebView2 控件（``winforms.py:258 BrowserView.__init__``），
            进不了消息循环，两边互等。

        所以这个回调**什么都不做**。所有窗口装饰与定位都推迟到第一次
        ``api.snapshot()``（那时前端已加载、UI 在跑消息循环）。
        """
        return

    def _ensure_ready(self):
        """前端就绪后的初始化（**必须在 UI 线程调用**）。

        做两件事，都只做一次：
          1. 套窗口装饰（毛玻璃 / 去任务栏按钮 / owner）
          2. 按当前工作区把窗口摆到位

        为什么拖到这时候：``webview.start()`` 的 func 回调跑在另一个线程、
        且早于窗口创建，那时做任何窗口操作都会死锁 —— 详见
        ``_after_start`` 的 docstring。
        """
        if self._ready_done:
            return
        self._ready_done = True

        self._apply_window_chrome()
        self._reposition()

        # 启动静默模式下，窗口是 hidden 建的；用户要求「双击后只在托盘出现」，
        # 所以这里不主动 show，等托盘/贴片触发。
        #
        # 注意：_reposition() 里的 window.move()/resize() 会顺手把窗口显示
        # 出来 —— WinForms 的 Location/Size setter 对隐藏窗口也会触发一次
        # 显示（实测：静默启动 8 秒后主窗 IsWindowVisible 已经是 True）。
        # 所以定位完必须再 hide 一次才能真正静默。
        if self._show_at_start:
            # **必须同步维护 _visible**：前端随后会调 set_height() 量高度，
            # 而 set_height() 结尾会调 _restore_visibility() 按 _visible 摆状态。
            # 只 show 不置位的话，_visible 还是 False，第一次 set_height 就把
            # 刚显示的窗口又藏回去 —— 症状就是「窗口闪一下就消失」。
            self._visible = True
            self.source.set_active(True)
            try:
                self.window.show()
            except Exception:
                traceback.print_exc()
        else:
            self._hide_quietly()

    def _window_hwnd(self, title, timeout=5.0):
        """按窗口标题等 HWND 出现（纯 Win32，不碰 pythonnet）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                hwnd = ctypes.windll.user32.FindWindowW(None, title)
                if hwnd:
                    return hwnd
            except Exception:
                pass
            time.sleep(0.1)
        return 0

    def _apply_window_chrome(self):
        """给主窗和贴片窗套上毛玻璃、去掉任务栏按钮、设 owner。

        ============================================================
        为什么只能在 UI 线程上做（血泪）
        ============================================================
        ``SetWindowLongPtrW(GWL_EXSTYLE)`` 和 ``SetWindowPos(SWP_FRAMECHANGED)``
        都会**同步**给目标窗口发消息（WM_STYLECHANGING / WM_NCCALCSIZE）。
        如果目标窗口的 UI 线程此刻没在消息循环里（比如正卡在
        ``BrowserView.__init__`` 建 WebView2 控件），发送方就会一直阻塞。

        实测的僵死现场（faulthandler 抓的栈，别删）：

            Thread 主线程:  webview/platforms/winforms.py:258 in __init__
                            webview/platforms/winforms.py:773 in create
            Thread worker:  winui.py:314 in hide_from_taskbar
                            main.py worker

        也就是「回调线程等 UI 线程、UI 线程等 WebView2 初始化」，两边互等，
        整个进程僵住，窗口永远不出现。

        ⇒ 结论：**只在 UI 线程（也就是 js_api 被调用的线程）上做这件事**。
        前端加载完会立刻调 ``api.snapshot()``，那时窗口已建好、UI 在跑消息
        循环，所有调用都是安全的。
        """
        if self._chrome_done:
            return
        self._chrome_done = True

        try:
            owner = self.tray.hwnd if self.tray is not None else None
        except Exception:
            owner = None

        targets = [(self.window, "main")]
        if self.pill_window is not None:
            targets.append((self.pill_window, "pill"))

        for win, name in targets:
            if win is None:
                continue
            try:
                hwnd = winui.window_handle(win)
            except Exception:
                hwnd = None
            if not hwnd:
                # 退回按标题找（window_handle 在某些 pywebview 版本上拿不到）
                hwnd = self._window_hwnd(
                    APP_TITLE if name == "main" else PILL_TITLE, timeout=0.5)
            if not hwnd:
                continue
            self._hwnd_cache[name] = hwnd
            if name == "main":
                try:
                    self._blur_info = winui.apply_blur(hwnd)
                except Exception:
                    traceback.print_exc()
            # 贴片窗**不套毛玻璃**：DWM 的亚克力/模糊是按窗口
            # **包围盒**画的，不认 SetWindowRgn 裁出来的形状 —— 结果
            # 胶囊四个圆角外侧会冒出一圈深灰（实测 #3D3D3D），
            # 看起来像贴片背后垫了一块黑方块。贴片只需要纯色胶囊。
            try:
                winui.hide_from_taskbar(hwnd, owner)
            except Exception:
                traceback.print_exc()
            if name == "pill":
                try:
                    winui.set_noactivate(hwnd)
                except Exception:
                    pass
                # 需求第 2 条：套完装饰再挂进任务栏。
                # 为什么不能只靠 _create_pill_window 里那一次：那时窗口刚
                # create_window 出来，pywebview 的 HWND 有时还拿不到
                # （window_handle() 返回 0、按标题也搜不到），那一次嵌不进去
                # 就再也没人补了 —— 实测表现是「贴片一直是浮窗，看不出嵌入」。
                # 这里在 UI 线程、窗口确定已建好的时机再挂一次，幂等。
                try:
                    self._apply_pill_host()
                except Exception:
                    traceback.print_exc()

    # ---------------------------------------------------------------- 命令泵

    def _start_pump(self):
        def loop():
            while not self._pump_stop.is_set():
                try:
                    cmd = self._queue.get(timeout=PUMP_INTERVAL)
                except queue.Empty:
                    # 空闲时顺手做点维护：历史抽稀、用量落盘
                    self._housekeeping()
                    try:
                        self._poll_post_message()
                    except Exception:
                        pass
                    try:
                        self._poll_panel_state()
                    except Exception:
                        pass
                    try:
                        self._poll_cursor_leave()
                    except Exception:
                        pass
                    continue
                try:
                    self._run_command(cmd)
                except Exception:
                    traceback.print_exc()
                # 有命令也在判：用户可能是点了按钮又把鼠标甩出去的
                try:
                    self._poll_panel_state()
                except Exception:
                    pass
                try:
                    self._poll_cursor_leave()
                except Exception:
                    pass
        t = threading.Thread(target=loop, name="cmd-pump", daemon=True)
        t.start()

    def _poll_post_message(self):
        """检查「第二个实例请我显示窗口」的命名事件（``--show`` 用）。

        为什么用事件而不是窗口消息：主窗的 WndProc 是 pywebview（WinForms）
        的，外部挂不上自己的消息处理函数，发 ``WM_*`` 进去只会被默认分支
        吃掉。命名事件是纯 Win32、跨进程、不需要窗口句柄；这里每 0.2s
        查一次，有信号就走 ``show_window()``（那条路径才是线程安全的）。

        事件是**手动重置**的：Handle 之后要自己 ``ResetEvent``，否则下一次
        启动再 Set 时状态一直保持"已触发"，会变成每次轮询都弹窗。
        """
        if self._wake_event is None:
            return
        try:
            kernel32 = ctypes.windll.kernel32
            if not kernel32.WaitForSingleObject(self._wake_event, 0) == 0:
                return
            kernel32.ResetEvent(self._wake_event)
        except Exception:
            return
        try:
            self.show_window()
        except Exception:
            traceback.print_exc()

    def _open_wake_event(self):
        """创建（或打开）唤醒事件。失败返回 None —— 唤窗功能降级，不影响运行。"""
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateEventW.restype = ctypes.c_void_p
            kernel32.OpenEventW.restype = ctypes.c_void_p
            # CreateEventW(attr, bManualReset=True, bInitialState=False, name)
            handle = kernel32.CreateEventW(None, True, False, WAKE_EVENT_NAME)
            return handle or None
        except Exception:
            return None

    def _housekeeping(self):
        """低频维护：每 5 分钟清一次过期历史。放在命令泵里跑，不另起线程。"""
        now = time.time()
        # 采集到的电量先入库（隐藏状态下前端不轮询，这里兜底）。
        # 命令泵 0.2s 一拍，但历史采样没必要这么快：add_sample 自身按
        # 「1% 变化 / 15 分钟心跳」去重，喂太勤只是白问 provider 要快照。
        if now - getattr(self, "_last_sample", 0.0) >= 5.0:
            self._last_sample = now
            try:
                self._record_history()
            except Exception:
                pass
        if now - getattr(self, "_last_house", 0) < 300:
            return
        self._last_house = now
        try:
            history.purge_old(self.history)
        except Exception:
            pass
        try:
            # 落盘：以前只在 shutdown 里存过，强杀就丢光内存样本
            self.history.save()
        except Exception:
            pass
        try:
            usage.purge_old(self.usage)
        except Exception:
            pass

    def _record_history(self, devices=None):
        """把当前一轮设备读数写进历史库（需求 3 的根因修复）。

        全项目此前只有自测在调 ``history.add_sample``，主流程一次都没写过，
        所以 ``battery_history.json`` 永远是空的、图表也永远是「没有数据」。

        这里**直接问 provider 要最新读数**，不用 ``_last_devices`` —— 那个
        只有窗口可见（前端在轮询）时才更新，而窗口收起后才是采样最需要的
        时候（用户不看着，电量照样在掉）。

        ``add_sample`` 自带去重：变化 <1% 且距上次样本 <15 分钟就跳过，
        所以这里可以放心地每轮都调。落盘另外按 60 秒节流 —— 写文件比
        写内存贵得多，而丢掉最后几十秒的样本无所谓。
        """
        if devices is None:
            try:
                devices = self._decorate(
                    self.provider.snapshot().get("devices") or [])
            except Exception:
                return
        if not devices:
            return
        try:
            history.track_snapshot(self.history, devices)
        except Exception:
            traceback.print_exc()
            return
        now = time.time()
        if now - getattr(self, "_last_history", 0.0) < 60:
            return
        self._last_history = now
        try:
            self.history.save()
        except Exception:
            pass

    def _poll_panel_state(self):
        """托盘溢出面板（^ 弹出的那块）状态变了就重排主窗（用户需求）。

        主窗底边原本钉在任务栏上沿，面板一弹出来就盖住窗口下半截，Windows
        还会把窗口往上顶 —— 用户看到的「界面漂移」就是它。``main_rect()``
        已经会避让，这里只负责**发现面板开合**并触发一次重排。

        只在窗口可见时做（藏起来时不用管，下次 ``show_window`` 会重算）。
        面板宽高不变、只切可见性，所以状态变化就是一个 bool。
        """
        if not self._visible:
            self._panel_open = None
            return
        try:
            open_now = winui.overflow_panel_rect() is not None
        except Exception:
            return
        prev = getattr(self, "_panel_open", None)
        self._panel_open = open_now
        if prev is None or prev == open_now:
            return
        try:
            self._reposition()
        except Exception:
            traceback.print_exc()

    def _poll_cursor_leave(self):
        """指针离开主窗满 ``AUTO_HIDE_SEC`` 就自动收起（需求 3）。

        为什么不用前端的 ``document.mouseleave``：那个事件只认浏览器视口，
        鼠标「唰」地甩出窗口、点完按钮手往外一甩、窗口被别的程序夺焦这几种
        情况下都不触发（实测把窗口藏起来要等下一次 mouseenter/mouseleave
        配对，而收起后视口没了，配对永远等不到）。改成后端按命令泵的节奏
        （``PUMP_INTERVAL`` = 0.2s）量一次真实光标位置和窗口矩形，跟 DOM
        事件一点关系都没有。

        只在窗口可见时判定；拖动过程中不收起。
        """
        if not self._visible:
            self._cursor_out_since = None
            return
        if getattr(self, "_dragging", False):
            self._cursor_out_since = None
            return
        hwnd = self._main_hwnd()
        if not hwnd:
            return
        rect = winui.window_rect(hwnd)
        pos = winui.cursor_pos()
        if not rect or not pos:
            return
        left, top, right, bottom = rect
        x, y = pos
        # 给窗口四周留 6 物理像素的容差：贴边移动时 GetCursorPos 和窗口
        # 矩形的取整会差一两像素，不宽容会出现「明明还在窗口里却被收起」。
        inside = (left - 6) <= x <= (right + 6) and (top - 6) <= y <= (bottom + 6)
        if inside:
            self._cursor_out_since = None
            return
        now = time.time()
        since = getattr(self, "_cursor_out_since", None)
        if since is None:
            self._cursor_out_since = now
            return
        if now - since >= AUTO_HIDE_SEC:
            self._cursor_out_since = None
            try:
                self.hide_window()
            except Exception:
                traceback.print_exc()

    def post(self, cmd):
        """从任意线程投递一个主线程命令。立即返回，不阻塞调用方。"""
        self._queue.put(cmd)
        return {"ok": True}

    def _run_command(self, cmd):
        if cmd == "show":
            self.show_window()
        elif cmd == "hide":
            self.hide_window()
        elif cmd == "toggle":
            if self._visible:
                self.hide_window()
            else:
                self.show_window()
        elif cmd == "refresh":
            self.refresh()
        elif cmd == "autostart":
            self.set_autostart(not autostart.is_enabled())
        elif cmd == "quit":
            self.quit()

    # ---------------------------------------------------------------- 窗口

    def main_rect(self, height=None):
        """主窗位置。返回 ``(w, h, x, y)``，全部**逻辑像素**。

        三档优先级（对应需求第 1、5 条）：

          1. **用户手动拖过** → 沿用 ``window_state.json`` 里记下的 **x**，
             y 仍然由任务栏上沿反推。所以「拖完重启还在原来的横向位置」，
             但窗口高度变化时底边不会跟着跑（见下方「位置不变」）；
          2. **点过托盘图标** → 摆在图标**正上方**（``_anchor`` 是图标中心，
             由 v4 托盘回调的 lParam 给出），水平以图标居中、底边距图标 8px，
             并夹进该图标所在显示器的工作区；上方放不下就翻到下方；
          3. 两者都没有（首启、或托盘回调没给坐标）→ 退到「通知区域中心
             正上方」，这是没有精确锚点时的最好近似。

        **位置不变**（需求第 1 条的「切换设备后仍保持位置」）靠两件事：

          * y 一律 = 任务栏上沿 - 高度 - gap，也就是**底边钉死在任务栏
            上方**，窗口只往上长。切换设备导致高度变化时底边不动，
            视觉上不会「跳」；反之若沿用保存的 y，高度一变整个窗口就会
            上下窜一截。
          * x 算出来之后就锁进 ``_fixed_x``，后续（包括切换设备、前端调
            ``set_height``）都复用它。托盘图标的 lParam 每次点击可能有
            一两像素抖动，不锁的话窗口会左右轻移。

        **不主动读 window_state 的 y** —— 否则用户换分辨率/换显示器后
        窗口会跑到屏幕外。
        """
        if height is None:
            # 用 getattr 兜底：自测里会用 __new__ 造一个没跑 __init__ 的实例，
            # 直接读属性会 AttributeError。
            # 优先用**已应用过**的高度（见 _applied_height 的注释）。
            height = getattr(self, "_applied_height",
                             getattr(self, "_cur_height", MAIN_HEIGHT))
        h = int(height)
        left, top, sw, sh = screen_work_area()

        # 底边锚点：任务栏上沿（逻辑像素）。拿不到就退回工作区底边。
        bar_top = None
        bar = winui.taskbar_rect()
        scale = dpi_scale() or 1.0
        if bar:
            bar_top = int(bar[1] / scale)
        if bar_top is None:
            bar_top = top + sh

        # 「显示隐藏的图标」面板弹出来时，它有可能会挡在上面，所以要再往上让。
        # 面板未弹出时这一步是空操作，与原来行为完全一致。
        # 已找到面板窗口类名 TopLevelWindowForOverflowXamlIsland（Win11），
        # 靠 IsWindowVisible 判断开合。见 winui.overflow_panel_rect 的注释。
        panel = winui.overflow_panel_rect()
        if panel:
            panel_top = int(panel[1] / scale)
            if panel_top < bar_top:
                bar_top = panel_top

        fixed_x = getattr(self, "_fixed_x", None)

        # ---- 1) 用户拖过：沿用保存的 x（y 仍按任务栏上沿反推）
        if getattr(self, "_dragged", False):
            pos = self._saved_position()
            if pos:
                x = fixed_x if fixed_x is not None else int(pos[0])
                x = max(left, min(int(x), left + max(0, sw - MAIN_WIDTH)))
                y = max(top, min(bar_top - h - TASKBAR_MARGIN,
                                 top + max(0, sh - h)))
                return MAIN_WIDTH, h, int(x), int(y)

        # ---- 2) 有托盘图标锚点：摆到图标正上方
        anchor = getattr(self, "_anchor", None)
        if anchor:
            rect = winui.main_rect_near_icon(anchor, MAIN_WIDTH, h,
                                             gap=TASKBAR_MARGIN)
            if rect:
                w, hh, x, y = rect
                if fixed_x is not None:
                    x = max(left, min(int(fixed_x), left + max(0, sw - w)))
                return w, hh, int(x), int(y)

        # ---- 3) 降级：通知区域正上方
        note = winui.notify_area_rect()
        if note:
            note_cx = int((note[0] + note[2]) / 2.0 / scale)
            x = (fixed_x if fixed_x is not None
                 else note_cx - MAIN_WIDTH // 2)
            x = max(left, min(int(x), left + max(0, sw - MAIN_WIDTH)))
            y = max(top, min(bar_top - h - TASKBAR_MARGIN,
                             top + max(0, sh - h)))
            return MAIN_WIDTH, h, int(x), int(y)

        # ---- 4) 什么都拿不到：贴工作区右下角
        return (MAIN_WIDTH, h,
                left + max(0, sw - MAIN_WIDTH - TASKBAR_MARGIN),
                top + max(0, sh - h - TASKBAR_MARGIN))

    def _lock_x(self, x):
        """记住当前的横向位置，后续重排一律复用它（需求 1 的「位置不变」）。"""
        if x is None:
            return
        self._fixed_x = int(x)

    def _saved_position(self):
        """``window_state.json`` 里记的窗口坐标（逻辑像素）。没有返回 None。"""
        try:
            state = self.store.state or {}
            x, y = state.get("x"), state.get("y")
            if x is None or y is None:
                return None
            return int(x), int(y)
        except Exception:
            return None

    def pill_rect(self):
        """贴片位置：任务栏内、通知区域左边。返回 ``(w, h, x, y)`` 逻辑像素。

        嵌入任务栏时这个返回值只用于**创建窗口**（``create_window`` 要逻辑
        像素的初始位置），真正的几何由 ``_apply_pill_host()`` 按物理像素重算。
        拿不到任务栏时退回屏幕右下。
        """
        w = int(getattr(self, "_pill_width", PILL_WIDTH))
        pos = winui.pill_rect(w, PILL_HEIGHT, margin=2)
        if pos is None:
            left, top, sw, sh = screen_work_area()
            pos = (left + max(0, sw - w - 16),
                   top + max(0, sh - PILL_HEIGHT - 4))
        return w, PILL_HEIGHT, pos[0], pos[1]

    def _pill_background_color(self):
        """贴片窗的**首帧**底色：**原样**用任务栏采样色。

        扁平样式（需求 2）里贴片不是一枚有底色的胶囊，而是「直接写在任务栏
        上的文字」。所以窗口底色必须和任务栏**完全一致** —— 早期实现会提亮
        2~6 级（那是为了做胶囊的层次感），现在提亮一点点都会让贴片变成任务栏
        上一块淡淡的方印子。

        采样失败退回中性浅灰/深灰：宁可有点色差，也不能闪一块纯黑或纯白。
        """
        gray = "#e9eaeb"
        try:
            bg = winui.taskbar_background()
            m = winui.taskbar_metrics() or {}
            dark = bool(m.get("dark"))
        except Exception:
            return gray
        if not bg:
            return "#2b2b2b" if dark else gray
        try:
            return "#%02x%02x%02x" % tuple(int(c) for c in bg[:3])
        except Exception:
            return gray

    def _apply_pill_host(self):
        """把贴片挂进任务栏（``SetParent``），失败则走降级浮窗。

        需求第 2 条要求贴片跟任务栏**同一图层**：任务栏自动隐藏时一起隐藏、
        被开始菜单盖住时一起被盖住。只有 ``SetParent(hwnd, Shell_TrayWnd)``
        能拿到这个行为，把窗口用 TOPMOST 摆在任务栏上方做不到。

        降级条件（README 里也写了）：Win11 新版任务栏是 XAML 合成的，
        跨进程 ``SetParent`` 在某些版本上会被裁掉或直接不可见；另外
        ``FindWindow("Shell_TrayWnd")`` 拿不到（任务栏被替换/自动隐藏）时
        也会失败。两种情况下退回「紧贴任务栏的置顶无边框浮窗」，尺寸与
        位置和嵌入态**完全一致**，只是不随任务栏一起隐藏。

        返回 True 表示嵌入成功。
        """
        hwnd = self._pill_hwnd()
        if not hwnd:
            return False
        geom = winui.pill_rect_in_taskbar(
            int(getattr(self, "_pill_width", PILL_WIDTH)), PILL_HEIGHT)
        if not geom:
            self._pill_error = "任务栏几何读取失败，贴片退回浮窗模式"
            self._pill_embedded = False
            return False

        x, y, w, h, bar_h = geom
        # 记下贴片自己的物理矩形，前端采样任务栏底色时按它取邻域
        self._pill_geom = (x, y, w, h)

        # 先改几何、再裁形状。顺序不能反：``SetWindowRgn`` 的坐标是**窗口
        # 坐标**，窗口随后被 SetWindowPos 改大改小时区域**不会跟着缩放** ——
        # 先裁就会按旧尺寸裁，出现「右边缺一块」或「胶囊比窗口窄一截」。
        ok = winui.embed_in_taskbar(hwnd, x, y, w, h)
        if ok:
            self._pill_embedded = True
            self._pill_bar_height = bar_h
            winui.clip_round_rect(hwnd, PILL_RADIUS)
            return True

        # 降级：摘成独立置顶浮窗，几何保持一致（视觉上看不出区别，
        # 区别只在「任务栏自动隐藏时它不会跟着隐藏」）
        self._pill_embedded = False
        self._pill_bar_height = bar_h
        try:
            winui.set_noactivate(hwnd)
            winui.detach_from_taskbar(hwnd, x, y, w, h)
            winui.clip_round_rect(hwnd, PILL_RADIUS)
            self._pill_error = (
                "贴片未能嵌入任务栏（SetParent 被系统拒绝），"
                "已退回置顶浮窗：外观一致，但任务栏自动隐藏时它不会跟着隐藏")
        except Exception:
            traceback.print_exc()
            self._pill_error = "贴片宿主设置失败"
        return False

    def set_pill_width(self, width):
        """前端报告贴片内容的实际宽度，窗口跟着收窄或加宽。

        贴片窗一开始按 ``PILL_WIDTH`` 建，但内容（图标 + 数值）只有那么宽。
        不跟着收窄的话，窗口两边会有几十到上百逻辑像素的空白 —— 虽然底色
        和任务栏一样、看不出边界，**但会挡住任务栏上那块区域的点击**
        （用户点不到下面的托盘图标、也点不到 ^ 箭头）。所以必须按内容宽度
        真实收窄。
        """
        try:
            w = int(width)
        except (TypeError, ValueError):
            return {"ok": False, "error": "宽度无效"}
        w = max(PILL_MIN_WIDTH, min(PILL_MAX_WIDTH, w))
        if w == getattr(self, "_pill_width", None):
            return {"ok": True, "width": w, "changed": False}
        self._pill_width = w
        hwnd = self._pill_hwnd()
        if hwnd:
            try:
                # 嵌入态要按任务栏客户区重算，不能只 move_resize —— 子窗口
                # 的坐标是相对任务栏的，用屏幕坐标会被摆到奇怪的地方。
                if getattr(self, "_pill_embedded", False):
                    self._apply_pill_host()
                else:
                    pw, ph, px, py = self.pill_rect()
                    winui.move_resize(hwnd, px, py, pw, ph)
                    winui.clip_round_rect(hwnd, PILL_RADIUS)
            except Exception:
                traceback.print_exc()
        return {"ok": True, "width": w, "changed": True}

    def set_pill_hover(self, key=None, hovering=False):
        """贴片前端报告「指针在不在某个设备上」，后端负责弹/收提示气泡。

        为什么要绕后端一圈：提示窗是独立的 Win32 顶层窗口（见
        ``tipwindow.py`` 的说明），WebView2 画不到贴片窗口之外的地方，
        所以前端只能把「现在悬停的是谁」告诉 Python，由 Python 去摆那个窗。

        参数 ``key`` 是设备的 ``key``（``B021_4080CFB3`` 这种）；``hovering``
        False 表示指针离开了（此时 key 可省略）。

        返回 ``{"ok": True, "shown": bool}`` —— 前端不依赖返回值，但它能在
        调试时说明「后端到底有没有弹」。
        """
        if not hovering:
            self._pill_hover_key = None
            if self._tip is not None:
                try:
                    self._tip.hide()
                except Exception:
                    pass
            return {"ok": True, "shown": False}

        key = (key or "").strip()
        if not key:
            return {"ok": False, "error": "缺少设备 key"}
        self._pill_hover_key = key
        shown = self._show_pill_tip(key)
        return {"ok": True, "shown": bool(shown)}

    def _show_pill_tip(self, key):
        """按 key 找到设备并弹出提示气泡。返回是否显示成功。"""
        if self._tip is None:
            return False
        device = None
        for d in (self._last_devices or []):
            if d.get("key") == key:
                device = d
                break
        if device is None:
            # 隐藏状态下前端不轮询，_last_devices 可能是空的 —— 现取一次
            try:
                for d in self._decorate(
                        self.provider.snapshot().get("devices") or []):
                    if d.get("key") == key:
                        device = d
                        break
            except Exception:
                pass
        if device is None:
            return False

        title, sub = tipwindow.format_lines(device)
        anchor = getattr(self, "_pill_geom", None)
        if not anchor:
            try:
                g = winui.pill_rect_in_taskbar(
                    int(getattr(self, "_pill_width", PILL_WIDTH)), PILL_HEIGHT)
                anchor = (g[0], g[1], g[2], g[3]) if g else None
            except Exception:
                anchor = None
        dark = False
        try:
            dark = bool((winui.taskbar_metrics() or {}).get("dark"))
        except Exception:
            pass
        try:
            self._tip.show(title, sub, anchor=anchor, dark=dark)
            return True
        except Exception:
            traceback.print_exc()
            return False

    def _reposition(self):
        """按当前工作区把两个窗口挪到正确位置。

        窗口显示前、显示后、分辨率变化后都要调 —— 所以单独抽出来。

        全部走 ``winui.move_resize``（纯 Win32）而不是 pywebview 的
        ``move()/resize()``：这个方法会被命令泵线程调用，跨线程碰 pywebview
        的窗口对象会永久阻塞（见 ``show_window`` 的说明）。纯 Win32 的
        ``SetWindowPos`` 不带 ``SWP_SHOWWINDOW``，所以也不会顺手把隐藏的
        窗口「顶」出来 —— 以前为了让静默启动生效而补的 ``_restore_visibility()``
        现在只是兜底。
        """
        hwnd = self._main_hwnd()
        if hwnd:
            try:
                # 用**已应用过的高度**，不是 ``_cur_height``：后者会被前端
                # 首帧误报的小值污染，拿它重排会把窗口压扁（需求 1 的
                # 「开关贴片就漂」就是这么来的 —— set_pill_enabled 会走到
                # 这里，而那时 _cur_height 已是 168）。
                w, h, x, y = self.main_rect(
                    height=getattr(self, "_applied_height", MAIN_HEIGHT))
                self._lock_x(x)
                winui.move_resize(hwnd, x, y, w, h, clamp=True)
            except Exception:
                traceback.print_exc()

        pill = self._pill_hwnd()
        if pill:
            try:
                if getattr(self, "_pill_embedded", False):
                    # 嵌入态：坐标相对任务栏客户区，必须按宿主重算
                    self._apply_pill_host()
                else:
                    pw, ph, px, py = self.pill_rect()
                    winui.move_resize(pill, px, py, pw, ph)
                    # 浮窗态也要跟着窗口尺寸重新裁（SetWindowRgn 的区域
                    # 是相对窗口的，不随 SetWindowPos 缩放）
                    winui.clip_round_rect(pill, PILL_RADIUS)
            except Exception:
                traceback.print_exc()
        self._restore_visibility()

    def _pill_hwnd(self):
        if self.pill_window is None:
            return None
        return winui.window_handle(self.pill_window)

    def open_window(self):
        """显示主窗（贴片点击、托盘左键、第二次启动都走这里）。"""
        return self.show_window()

    def set_height(self, height):
        """前端报告内容高度，主窗跟着伸缩（列表页设备少时不至于一大片空白）。

        夹在 [MIN_HEIGHT, MAX_HEIGHT] 之间；位置按「底边贴任务栏上沿」重算，
        所以变高是**往上长**，不会顶到任务栏里。

        **不在这里拦「看起来太小」的值**。一开始想在后端把贴到 MIN_HEIGHT
        的上报当成「页面还没排完」丢掉，结果列表页真实的 168 也一起丢了，
        窗口再也收不回去。判断「量得准不准」只有前端知道（它能看 DOM、
        能等两帧），所以稳定性判定放在 ``web/app.js`` 的 ``fitHeight``；
        后端只保证「按收到的值摆好，并且横向不动」。

        高度变化走**动画**（``_animate_to``）：用户要求从列表点进详情时
        窗口「长上去」的过程要顺畅。这里立刻返回，动画在独立线程里逐帧
        ``SetWindowPos``，不阻塞调用方（js_api 那边等着回复的）。
        """
        try:
            h = int(height)
        except (TypeError, ValueError):
            return {"ok": False, "error": "高度必须是数字"}
        h = max(MIN_HEIGHT, min(MAX_HEIGHT, h))

        if abs(h - self._cur_height) < 4:
            return {"ok": True, "height": self._cur_height, "changed": False}
        self._cur_height = h
        self._applied_height = h
        self._animate_to(h)
        return {"ok": True, "height": h, "changed": True}

    # ---- 高度动画

    def _animate_to(self, target):
        """把主窗高度平滑过渡到 ``target``（逻辑像素）。

        为什么不在 ``set_height`` 里直接 ``move_resize``：一步到位会让窗口
        「啪」地跳一下，用户明确要求加过渡。为什么不在前端做：窗口高度是
        Win32 的，CSS 只能改内容、改不了外框，抖起来会露出背景。

        动画期间又来了新目标（用户连点两台设备）不会另起线程，循环会读到
        新目标并从**当前真实高度**重新计时，所以不会打架、也不会堆线程。
        """
        target = int(target)
        with self._anim_lock:
            self._anim_target = target
            if self._anim_running:
                return
            self._anim_running = True
        threading.Thread(target=self._anim_loop, name="h-anim",
                         daemon=True).start()

    def _anim_loop(self):
        try:
            while True:
                with self._anim_lock:
                    target = self._anim_target
                if target is None:
                    break
                start = self._real_height()
                if abs(target - start) < 2:
                    self._apply_height(target, restore=False)
                    with self._anim_lock:
                        if self._anim_target == target:
                            self._anim_target = None
                            break
                    continue

                t0 = time.time()
                duration = ANIM_MS / 1000.0
                retarget = False
                while True:
                    with self._anim_lock:
                        if self._anim_target != target:
                            retarget = True
                            break
                    p = min(1.0, (time.time() - t0) / duration)
                    eased = 1.0 - (1.0 - p) ** 3      # ease-out cubic
                    self._apply_height(int(round(start + (target - start) * eased)),
                                       restore=False)
                    if p >= 1.0:
                        break
                    time.sleep(ANIM_FRAME_SEC)

                if retarget:
                    continue

                self._apply_height(target)
                with self._anim_lock:
                    if self._anim_target == target:
                        self._anim_target = None
                        break
        except Exception:
            traceback.print_exc()
        finally:
            with self._anim_lock:
                self._anim_running = False

    def _real_height(self):
        """主窗**当前真实**高度（逻辑像素）。动画起点用它，避免用目标值起跳。"""
        hwnd = self._main_hwnd()
        rect = winui.window_rect(hwnd) if hwnd else None
        if rect:
            scale = dpi_scale() or 1.0
            h = int(round((rect[3] - rect[1]) / scale))
            if h > 0:
                return h
        return int(getattr(self, "_cur_height", MAIN_HEIGHT))

    def _apply_height(self, h, restore=True):
        """按高度 ``h`` 摆好主窗（保持底边贴任务栏、横向不动）。

        ``restore=False`` 供动画的中间帧用：那些帧不需要（也不应该）反复
        ``ShowWindow``，否则每 16ms 激活一次窗口，用户正在打字的窗口会被
        抢焦点。
        """
        hwnd = self._main_hwnd()
        if not hwnd:
            return
        try:
            w, hh, x, y = self.main_rect(height=h)
            self._lock_x(x)
            winui.move_resize(hwnd, x, y, w, hh, clamp=True)
            if restore:
                self._restore_visibility()
        except Exception:
            traceback.print_exc()

    def show_window(self):
        """显示主窗。

        **几何与显隐全部走 Win32**（``winui.move_resize`` / ``winui.show_window``），
        不碰 ``self.window.move/resize/show``。原因：本方法跑在命令泵线程，
        pywebview 的窗口方法内部要等 UI 线程，跨线程调用会永久阻塞 ——
        实测症状是「点托盘左键，窗口出来了但再点就收不起来」，因为
        ``_visible = True`` 那行之后的语句全卡住了。

        需求第 1 / 5 条：每次从托盘图标点开，都**重新**按图标中心定位
        （所以先清掉 ``_dragged``）；用户手动拖过的位置只在拖动后到下次
        点托盘图标之前生效。
        """
        # 消费托盘线程留下的锚点（有新锚点 = 这次是点图标打开的）
        anchor = getattr(self, "_pending_anchor", None)
        if anchor:
            self._anchor = anchor
            self._pending_anchor = None
            self._dragged = False
            # 只有**图标真的挪了位置**才允许重算横向位置：托盘图标在
            # 溢出面板内外切换时 lParam 会变，但用户的直觉是「窗口别动」。
            # 用 8px 容差吸收图标自身的一两像素抖动。
            prev = getattr(self, "_anchor_x_used", None)
            ax = int(anchor[0]) if anchor else 0
            if prev is None or abs(ax - prev) > 8:
                self._fixed_x = None
                self._anchor_x_used = ax

        self._visible = True
        self.source.set_active(True)
        # 指针离开计时从此开始干净
        self._cursor_out_since = None
        # 每次重新打开都要回到设备列表页（需求 4）
        self._want_list = True
        hwnd = self._main_hwnd()
        if not hwnd:
            # 窗口还没建好（启动极早期）—— 记下意图，等 UI 线程就绪后补做
            return {"ok": True, "pending": True}
        try:
            # 每次显示都重算位置：用户可能刚改过缩放或换了显示器。
            # 高度用**已应用过的**值（前端按内容量出来的），不是 MAIN_HEIGHT
            # 常量，也不是可能被误报污染的 _cur_height。
            w, h, x, y = self.main_rect(
                height=getattr(self, "_applied_height", MAIN_HEIGHT))
            self._lock_x(x)
            winui.move_resize(hwnd, x, y, w, h, clamp=True)
            winui.show_window(hwnd)
            self._set_topmost(hwnd)
        except Exception:
            traceback.print_exc()
        return {"ok": True}

    def _main_hwnd(self):
        """主窗 HWND（带缓存）。拿不到返回 None。"""
        if self.window is None:
            return None
        return winui.window_handle(self.window)

    def _set_topmost(self, hwnd):
        """把窗口钉在最前。纯 Win32，理由同 show_window。"""
        try:
            winui.set_topmost(hwnd)
        except Exception:
            pass

    def _hide_quietly(self):
        """不动采集状态、不打日志地把窗口藏起来（静默启动用）。

        与 ``hide_window()`` 的区别：那个是用户主动收窗，要顺带降频；这里只
        是想把 ``_reposition()`` 顺手带出来的窗口重新藏好，采集该多快还多快。
        """
        hwnd = self._main_hwnd()
        if hwnd:
            winui.hide_window(hwnd)

    def _restore_visibility(self):
        """挪完窗口后，按 ``self._visible`` 把显示/隐藏状态摆回去。

        pywebview 的 ``move()`` 会强制 show（见 ``_reposition`` 的说明），
        所以每次几何变更之后都得走一遍这里，否则静默启动的窗口会自己冒出来。
        走 Win32，不从 UI 线程以外的调用 pywebview。
        """
        hwnd = self._main_hwnd()
        if not hwnd:
            return
        if self._visible:
            winui.show_window(hwnd)
        else:
            winui.hide_window(hwnd)

    def hide_window(self):
        self._visible = False
        self._cursor_out_since = None
        # 需求第 1 条：窗口隐藏时降频（30 秒一次，不是完全停）
        self.source.set_active(False)
        hwnd = self._main_hwnd()
        if hwnd:
            winui.hide_window(hwnd)
        return {"ok": True}

    def quit(self):
        """真退出：停采集、删托盘图标、销毁窗口、结束进程。

        顺序很重要：先销毁窗口（webview.start 会在窗口销毁后返回），
        再删托盘图标。反过来的话，托盘图标先没了、窗口却还开着几百毫秒，
        用户会看到「图标消失但窗口还在」。
        """
        if self._quitting:
            return {"ok": True}
        self._quitting = True
        self.persist_geometry()
        # 贴片窗先关，否则主窗销毁后 webview 退出时它可能还挂着。
        # 走 _destroy_pill_window 而不是裸 destroy()：它会把句柄缓存一起清掉，
        # 保证退出后不留任何残留窗口（需求第 3 条最后一句）。
        try:
            if self.pill_window is not None:
                self._destroy_pill_window()
        except Exception:
            traceback.print_exc()
        try:
            self.window.destroy()
        except Exception:
            pass
        return {"ok": True}

    def shutdown(self):
        self._pump_stop.set()
        self.persist_geometry()
        if self._tip is not None:
            try:
                self._tip.stop()
            except Exception:
                pass
            self._tip = None
        if self.tray is not None:
            self.tray.stop()
        try:
            self.usage.stop()
        except Exception:
            pass
        try:
            self.source.stop()
        except Exception:
            pass
        try:
            self.history.save()
        except Exception:
            pass
        if self.httpd is not None:
            self.httpd.stop()

    # ---- 几何（只写不读，见 main_rect 的注释）

    def persist_geometry(self):
        """把当前窗口位置写进 window_state.json（逻辑像素）。

        读的是 ``winui.window_rect``（真实 Win32 矩形）而不是
        ``self.window.x / .y`` —— 需求第 5 条允许用户拖动窗口，拖动是
        ``WM_NCLBUTTONDOWN`` 交给系统做的，pywebview 那边的 ``x/y``
        属性**不会更新**，照它写盘只会把旧坐标再存一遍。

        物理 → 逻辑除以 ``dpi_scale()``，和 ``create_window`` /
        ``main_rect`` 用同一套坐标，下次启动读回来才能对上。
        """
        if self.window is None:
            return
        hwnd = self._main_hwnd()
        rect = winui.window_rect(hwnd) if hwnd else None
        if rect:
            scale = dpi_scale() or 1.0
            x = int(round(rect[0] / scale))
            y = int(round(rect[1] / scale))
            w = int(round((rect[2] - rect[0]) / scale))
            h = int(round((rect[3] - rect[1]) / scale))
        else:
            # 拿不到 HWND（启动极早期 / 非 Windows）：退回 pywebview 的属性，
            # 至少别把文件写坏。
            try:
                x, y = self.window.x, self.window.y
                w, h = self.window.width, self.window.height
            except Exception:
                return
        try:
            self.store.save_state(x=int(x), y=int(y),
                                  width=int(w or MAIN_WIDTH),
                                  height=int(h or MAIN_HEIGHT))
        except Exception:
            pass

    # ---------------------------------------------------------------- 数据

    def snapshot(self):
        """前端主通路。把 provider 的设备列表 + 使用时长 + 配置摘要拼在一起。

        顺带做「首次就绪」的两件事：套窗口装饰、按当前工作区定位。
        这两件事**只能在 UI 线程做**（原因见 ``_after_start`` 的说明），
        而 js_api 的调用正好就发生在 UI 线程上。
        """
        self._ensure_ready()
        snap = self.provider.snapshot()
        snap["devices"] = self._decorate(snap.get("devices") or [])
        # 记住这一轮读数：隐藏时前端不轮询，命令泵靠它往里补样本（需求 3）
        self._last_devices = snap["devices"]
        snap["version"] = APP_VERSION
        snap["readonly"] = bool(self.store.readonly)
        snap["usage"] = self.usage_data()
        # 前端据此判断「从收起变回显示」→ 退回设备列表页（需求 4）
        snap["visible"] = bool(self._visible)
        # 一次性信号：每次重新打开都把页面踢回列表页（需求 4）。
        # 用一次性标志而不是只看 visible 跳变 —— 隐藏时 WebView2 会把
        # 定时器降频，前端未必能观察到 false 那一帧。
        snap["want_list"] = bool(getattr(self, "_want_list", False))
        self._want_list = False
        snap["config"] = {
            "battery_pill_enabled": bool(self.store.config.get("battery_pill_enabled")),
            "battery_curve": self.store.config.get("battery_curve"),
            "battery_levels": self.store.config.get("battery_levels"),
            "autostart": autostart.is_enabled(),
            "last_selected_device": self._selected,
            "theme": self.store.config.get("theme") or "auto",
        }
        if self._tray_hint:
            snap["tray_error"] = self._tray_hint
        if self._pill_error:
            snap["pill_error"] = self._pill_error
        self._update_tray_tip(snap)
        return snap

    def _decorate(self, devices):
        """给每台设备补「今日用电量」（前端列表页与贴片都要显示）。"""
        out = []
        for d in devices:
            item = dict(d)
            item["today_used"] = self._today_used(d.get("key"))
            out.append(item)
        return out

    def _today_used(self, key):
        if not key:
            return None
        try:
            return self.history.aggregate(key, "24h").get("today_used")
        except Exception:
            return None

    def devices(self):
        return {"ok": True, "devices": self._decorate(
            self.provider.snapshot().get("devices") or [])}

    def device(self, key):
        for d in self.devices().get("devices") or []:
            if d.get("key") == key:
                return {"ok": True, "device": d}
        return {"ok": False, "error": "找不到设备 %s" % key}

    def history_data(self, range_name="24h", key=""):
        if range_name not in history.RANGES:
            range_name = "24h"
        if not key:
            devs = self.provider.snapshot().get("devices") or []
            key = devs[0].get("key") if devs else ""
        if not key:
            return dict(history.empty_aggregate(range_name), ok=True, key="")
        try:
            agg = self.history.aggregate(key, range_name)
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                    "range": range_name, "points": []}
        agg["ok"] = True
        agg["key"] = key
        agg["readonly"] = bool(self.history.readonly)
        return agg

    def history(self, range_name="24h", key=""):
        return self.history_data(range_name, key)

    def usage_data(self):
        try:
            today = self.usage.today()
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        today["ok"] = True
        today["readonly"] = bool(self.usage.readonly)
        return today

    def usage(self):
        return self.usage_data()

    def _update_tray_tip(self, snap):
        """托盘悬停提示跟着数据走：设备名 + 型号 + 电量（需求 4 的托盘版）。

        拼装逻辑复用 ``tipwindow.format_lines`` —— 贴片气泡和托盘提示都是
        「设备名一行 + 关键信息一行」的同一套口径，两处各拼一套迟早会漂移
        （比如一边写「今日用电」一边写「今日用量」）。

        托盘提示是单行的（``szTip`` 128 字符上限），所以两行拼成一行。
        """
        if self.tray is None:
            return
        devs = snap.get("devices") or []
        online = [d for d in devs if d.get("status") == "online"]
        if not devs:
            tip = "%s — 未检测到设备" % APP_TITLE
        elif not online:
            tip = "%s — %d 台设备均已离线" % (APP_TITLE, len(devs))
        else:
            title, sub = tipwindow.format_lines(online[0])
            tip = "%s — %s" % (title, sub) if sub else title
            if len(devs) > 1:
                tip += "（共 %d 台）" % len(devs)
        if tip != getattr(self, "_last_tip", None):
            self._last_tip = tip
            try:
                self.tray.set_tooltip(tip)
            except Exception:
                pass

    def status(self):
        return {
            "ok": True,
            "version": APP_VERSION,
            "autostart": self.autostart_state(),
            "readonly": bool(self.store.readonly),
            "app_dir": apppath.app_dir(),
            "web_dir": apppath.web_dir(),
            "icon_dir": apppath.icon_dir(),
            "data_paths": apppath.data_paths(),
            "tray_error": self._tray_hint,
            "pill_error": self._pill_error,
            "visible": self._visible,
        }

    def config_view(self):
        return {
            "config_path": self.store.config_file,
            "state_path": self.store.state_file,
            "config": self.store.config,
            "state": self.store.state,
            "readonly": bool(self.store.readonly),
        }

    def refresh(self):
        self.source.request_refresh()
        return {"ok": True}

    # ---------------------------------------------------------------- 操作

    def set_dpi(self, value, key=""):
        """写 DPI。key 目前只用于多设备时校验，实际写的是当前选中设备。"""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return {"ok": False, "error": "DPI 必须是数字"}
        try:
            result = self.source.set_dpi(value)
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        return result

    def set_autostart(self, on):
        on = bool(on)
        ok, message = autostart.set_enabled(on)
        self.store.set_config("autostart", on)
        return {"ok": ok, "enabled": self.autostart_state(), "message": message}

    def autostart_state(self):
        try:
            return bool(autostart.is_enabled())
        except Exception:
            return False

    def set_pill_enabled(self, on):
        """贴片开关：立刻生效（建/销毁窗口）并持久化。

        需求第 3 条：贴片由这个开关**独占控制** —— 打开后一直显示，没有任何
        定时/超时隐藏；唯一的关闭入口就是这个开关和退出程序。所以这里
        没有也不需要「空闲多少秒自动关」的逻辑。
        """
        on = bool(on)
        self.store.set_config("battery_pill_enabled", on)
        try:
            if on:
                if self.pill_window is None:
                    url = self.httpd.url if self.httpd else ""
                    self._create_pill_window(url)
                    # 新建窗口后要补一次窗口装饰
                    self._apply_window_chrome()
                    self._reposition()
            else:
                if self.pill_window is not None:
                    self._destroy_pill_window()
        except Exception as exc:
            traceback.print_exc()
            return {"ok": False, "enabled": on,
                    "error": "贴片窗口切换失败：%s" % exc}
        return {"ok": True, "enabled": on}

    def _destroy_pill_window(self):
        """彻底销毁贴片窗：销毁窗口 + 清所有句柄缓存。

        为什么不能只 ``pill_window.destroy()``：窗口对象丢了但
        ``_hwnd_cache["pill"]`` 还留着旧句柄，``_pill_hwnd()`` 在拿不到
        窗口时会走缓存，下次 ``_apply_pill_host()`` 拿着一个死句柄去
        ``SetParent`` —— 不报错但也不生效，表现是「关了再开，贴片没了」。
        所以这里三个地方一起清。

        提示气泡也一起收掉：贴片没了就没人能悬停到它上面，留着会是一个
        永远盖在任务栏上方的空窗。
        """
        win = self.pill_window
        self.pill_window = None
        self._pill_embedded = False
        self._pill_bar_height = 0
        self._pill_geom = None
        self._pill_hover_key = None
        try:
            if self._tip is not None:
                self._tip.hide()
        except Exception:
            pass
        try:
            if win is not None:
                win.destroy()
        except Exception:
            traceback.print_exc()
        # 清缓存（winui 内部那份 + 本类的）
        try:
            winui._hwnd_cache.pop(id(win), None)
        except Exception:
            pass
        self._hwnd_cache.pop("pill", None)

    def select_device(self, key):
        """记住上次选中的设备（下次启动高亮，但不自动跳页）。"""
        self._selected = key or None
        self.store.set_config("last_selected_device", self._selected)
        return {"ok": True, "selected": self._selected}

    def set_config(self, key, value):
        """通用配置写入，只接受 DEFAULT_CONFIG 里已有的键。

        遇到 ``battery_pill_enabled`` 这种「改了要立刻有副作用」的键，
        转交给对应的 setter，不能只写文件。
        """
        if not isinstance(key, str) or not key:
            return {"ok": False, "error": "配置键无效"}
        if key == "battery_pill_enabled":
            return self.set_pill_enabled(value)
        if key == "autostart":
            return self.set_autostart(value)
        if key == "last_selected_device":
            return self.select_device(value)
        if key not in apppath.DEFAULT_CONFIG:
            return {"ok": False, "error": "未知配置项：%s" % key}
        ok = self.store.set_config(key, value)
        return {"ok": bool(ok), "key": key, "value": value,
                "readonly": bool(self.store.readonly)}

    def set_visible(self, visible):
        self._visible = bool(visible)
        self.source.set_active(self._visible)
        return {"ok": True, "visible": self._visible}

    def start_drag(self):
        """前端在非交互区域按下鼠标时调用：让系统接管这次拖动。

        走 ``winui.start_window_drag``（``ReleaseCapture`` + ``WM_NCLBUTTONDOWN``
        + ``HTCAPTION``），不是自己算坐标差 —— 后者没有边缘吸附，还会和
        WebView2 的输入线程抢消息，拖起来一顿一顿的。

        拖动期间 ``_visible`` 保持 True，采集照旧（用户正在看窗口）。
        结束位置由前端 mouseup 后调 ``finish_drag()`` 落盘。
        """
        hwnd = self._main_hwnd()
        if not hwnd:
            return {"ok": False, "error": "窗口还没准备好"}
        # 拖动期间别让「指针离开」判定把窗口收掉
        self._dragging = True
        self._cursor_out_since = None
        ok = winui.start_window_drag(hwnd)
        return {"ok": bool(ok)}

    def finish_drag(self):
        """拖动结束：记下「用户手动定位过」并把坐标落盘。

        ``_dragged`` 一旦置 True，``main_rect`` 就优先用保存的坐标而不是
        「托盘图标正上方」；下次点托盘图标时 ``show_window`` 会清掉它，
        重新按图标居中（需求第 5 条）。
        """
        self._dragging = False
        self._dragged = True
        self.persist_geometry()
        return {"ok": True}

    def taskbar_metrics(self):
        """贴片前端要的任务栏度量（高度档位 / 深浅色 / 校准后的底色）。

        带上贴片自己的矩形 ``_pill_geom``（物理像素）：任务栏底色横向有
        渐变，全条取众数会得到一个比贴片周围暗十几级的灰 —— 实测贴片位置
        是薄荷色 (229,245,246)、整条众数是 (209,221,221)，贴片就像是按在
        任务栏上的一个坑。传 near 让采样只看贴片自己的横向区间（上下留白
        那两行），拿到的才是「这个位置本该是什么颜色」。

        ============================================================
        底色为什么要**校准**（务必保留这段）
        ============================================================
        扁平样式的贴片窗口底色必须和任务栏**逐像素一致**，否则就是一块
        淡淡的方印子。而实测存在一条稳定偏移：

            任务栏自己的像素       (221, 233, 233)
            CSS 写 #dce8e8         (220, 232, 232)
            屏幕上窗口实际渲染      (216, 234, 234)   ← R−4 G+2 B+2

        纯 GDI 的 tkinter 窗口填同一个值，屏幕像素**同样**是
        (216,234,234) —— 所以既不是采样错，也不是 WebView2 的锅，而是
        显示器色彩管理在窗口合成那条路上做了一次变换（任务栏走另一条路）。

        对策：每次上报前读回**上一次真正显示成什么颜色**
        （``winui.window_interior_color``），把差值当补偿量加上去。不假设
        任何 ICC 曲线，换显示器/换配置都能自己收敛。
        """
        geom = getattr(self, "_pill_geom", None)
        if not geom:
            # 贴片页往往比主窗先启动，那时 ``_apply_pill_host`` 还没跑过，
            # ``_pill_geom`` 仍是 None。几何本身是可以现算的（只取决于任务栏
            # 和贴片宽度），不补这一步就会退回「整条任务栏取众数」，
            # 得到一个比周围暗十几级的灰 —— 实测 (212,220,220) vs 邻域
            # (228,244,244)，在屏幕上就是一块凹下去的补丁。
            try:
                _g = winui.pill_rect_in_taskbar(
                    int(getattr(self, "_pill_width", PILL_WIDTH)), PILL_HEIGHT)
                if _g:
                    geom = (_g[0], _g[1], _g[2], _g[3])
            except Exception:
                geom = None

        metrics = winui.taskbar_metrics(geom) or {}

        # 底色：沿贴片宽度采一组点，前端画成横向渐变。
        #
        # 为什么要渐变而不是纯色：Win11 任务栏横向有 8~20 级的渐变（实测通知
        # 区附近 x=1700 是 (209,221,221)、x=2000 已是 (230,234,234)），贴片
        # 宽 90px 就横跨这段。填一个纯色，无论取哪个点总有一条边对不上 ——
        # 那就是肉眼看到的「一块淡淡的方印子」。
        #
        # 为什么**不**再叠色彩校准：实测过「读回窗口实际像素、把差值补上去」
        # 的做法，它在这个环境下不收敛（写 (225,233,233) 屏幕回 (228,231,233)，
        # 目标 (222,234,234) —— 每拍都在目标两侧来回跳，越修越花）。根因是
        # 显示器色彩管理对窗口合成做的变换既不是加性偏移、也不完全单调。
        # 渐变采样点已经是「这个位置本该是什么颜色」的最佳答案，误差落在
        # 一两级以内，肉眼不可辨；再叠一层不可控的补偿只会更糟。
        try:
            prof = winui.taskbar_background_profile(geom) if geom else None
        except Exception:
            prof = None
        metrics["profile"] = prof
        if prof:
            mid = prof[len(prof) // 2]
            metrics["background"] = [mid[1], mid[2], mid[3]]
        return metrics

# ---------------------------------------------------------------- 单实例

def acquire_single_instance():
    """拿单实例锁。拿不到返回 None（调用方应唤起已有实例并退出）。"""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
        if not handle:
            return False
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            # 锁已存在：说明有另一个实例在跑，把它的窗口叫出来
            return None
        return handle
    except Exception:
        # 非 Windows / 拿不到锁：宁可不做限制也不能起不来
        return False


def wake_existing(show=True):
    """第二次启动时：让**已经在跑的那个实例**把窗口显示出来（可选）。

    ⚠ 开机自启路径（``--silent``）不会走到这里 —— 详见 ``main()``：
    需求 1 要求开机后静默，第二个实例必须悄悄退出，绝不能把窗口顶出来。

    用命名事件而不是 ``PostMessage``：主窗的 WndProc 属于 pywebview
    （WinForms），外部挂不上自己的消息处理，发自定义消息进去会被默认分支
    吃掉、什么都不会发生。事件是纯 Win32，跨进程、无需窗口句柄，已有实例
    的命令泵会顺手查到并自己走 ``show_window()``（那条路径才是线程安全的）。

    拿不到事件（非 Windows / 权限）时退回老办法：直接 ``ShowWindow``。

    返回 True 表示「已有实例收到了请求」。
    """
    signaled = False
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenEventW.restype = ctypes.c_void_p
        ev = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, WAKE_EVENT_NAME)
        if ev:
            try:
                kernel32.SetEvent(ctypes.c_void_p(ev))
                signaled = True
            finally:
                kernel32.CloseHandle(ctypes.c_void_p(ev))
    except Exception:
        signaled = False

    if not signaled and show:
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, APP_TITLE)
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 5)   # SW_SHOW
                signaled = True
        except Exception:
            pass
    return signaled


def _parse_args(argv):
    """解析启动开关，返回 ``(show_at_start, silent)``。

    两种形态，语义都是「这次启动**不要**弹窗」：

      * 默认（裸启动）：静默 —— 只在托盘出现，不显示主窗；
      * ``--silent``：同上，并且是**开机自启**写进注册表的那一条，
        显式的意图声明（见 autostart.SILENT_FLAG）；
      * ``--show``：调试用，起来就把主窗显示出来。

    以前只有一个 ``--show`` 判断、其它一律静默，但「第二次启动」那条路径
    会把已有实例的窗口顶出来 —— 需求 1 要的正是把那条路也堵上。所以现在
    额外返回 ``explicit_show``：只有用户真的带上 ``--show`` 才允许唤窗。
    """
    args = [a.strip().lower() for a in (argv or [])]
    explicit_show = any(a in ("--show", "/show", "-show") for a in args)
    silent = any(a in ("--silent", "/silent", "-silent") for a in args)
    # --show 优先于 --silent：调试时有 --show 就想看到窗口
    return explicit_show, silent


def _report_fatal(exc, silent=False):
    """启动失败时落盘 + （非静默时）弹窗。

    为什么静默路径**不弹**：``--silent`` 是开机自启那条路径，用户这时候
    可能根本没坐在机器前。开机卡一个模态对话框，既挡住了后面所有自启项
    （Windows 会等这个进程），又违背需求 1「静默开启」。日志照样写，
    用户之后能查。
    """
    detail = traceback.format_exc()
    try:
        log = os.path.join(apppath.app_dir(), "error.log")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(detail + "\n")
    except Exception:
        log = "(日志写入失败)"
    if silent:
        return log
    try:
        # exe 是 -w 打的，没有控制台，异常必须弹出来，
        # 否则用户只看到「双击没反应」。
        ctypes.windll.user32.MessageBoxW(
            None, "启动失败：%s\n\n%s\n\n详细堆栈：%s"
            % (type(exc).__name__, exc, log), APP_TITLE, 0x10)
    except Exception:
        pass
    return log


def main():
    show_at_start, silent = _parse_args(sys.argv[1:])

    handle = acquire_single_instance()
    if handle is None:
        # 已经在跑。这里**绝不弹任何对话框**（需求 1：静默）：
        #   * 带 --show：说明用户是主动要打开界面 → 唤已有窗口；
        #   * 其余（含开机自启的 --silent）：直接退出，什么都不做。
        # 老代码在唤窗失败时还会弹一个「已经在运行了」的系统对话框，
        # 开机时第二个实例必然走到那一支 —— 就是那个弹窗的来源。
        if show_at_start:
            wake_existing()
        return 0

    app = WirelessStatusApp(show_at_start=show_at_start)
    try:
        app.run()
    except Exception as exc:
        # 静默启动（开机那条）只落盘不弹窗，见 _report_fatal 的说明。
        _report_fatal(exc, silent=not show_at_start)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
