# -*- coding: utf-8 -*-
"""系统托盘 + 弹出/收起窗口的控制

pywebview 不带托盘，这里直接用 pywin32 建一个**独立线程**的隐藏消息窗口
托管 Shell_NotifyIcon。要点：

1. 托盘的回调消息必须由**创建图标的那个线程**的消息泵接收。放到主线程
   （webview 占着）会收不到，右键菜单永远不弹。所以这里单开线程 + 自己的
   GetMessage 循环。
2. 进程退出时**必须** Shell_NotifyIcon(NIM_DELETE)，否则 Windows 会在
   托盘留一个点不动的幽灵图标，直到鼠标划过去。用 WM_CLOSE + 事件等待保证
   在退出前删掉。
3. 图标用 LoadIcon(None, IDI_APPLICATION) 兜底 —— 没有 .ico 也能显示，
   不至于因为缺资源直接看不到托盘。
"""

import os
import sys
import threading
import time
import traceback

# pywin32 在无控制台（pythonw / -w 打的 exe）下，任何未捕获异常都会写到一个
# 看不见的地方，表现为「托盘没出来但也没报错」。所以这里全部包 try。
import win32api
import win32con
import win32gui
import win32ui

WM_TRAY = win32con.WM_USER + 20          # 托盘回调消息
WM_TRAY_LEFT = win32con.WM_USER + 21     # 左键单击：我们的自定义消息用不上，
                                         # 但保留常量以示区分（见 _on_notify）

#: Shell_NotifyIcon 的版本协商。NOTIFYICON_VERSION_4 之后回调消息的
#: **wParam 低 16 位**才是事件码，**lParam 高/低 16 位**是图标中心坐标 ——
#: 这是需求第 1 条「窗口定在托盘图标正上方」唯一靠谱的锚点来源。
#: （v3 及以前 lParam 只有事件码，没有坐标，只能猜通知区域中心。）
NIM_SETVERSION = 4
NOTIFYICON_VERSION_4 = 4

#: v4 的事件码（wParam 低 16 位）
NIN_SELECT = win32con.WM_USER + 0        # 0x0400 左键单击
NIN_KEYSELECT = win32con.WM_USER + 1     # 0x0401 键盘激活（回车/空格）
NIN_BALLOONUSERCLICK = win32con.WM_USER + 5
NIN_POPUPOPEN = win32con.WM_USER + 6
NIN_POPUPCLOSE = win32con.WM_USER + 7

# 菜单项 id。注意：需求 6 删掉了「显示 / 收起」，只留刷新、开机自启、退出。
# 编号不复用，避免和旧版本残留的菜单消息撞上。
MENU_SHOW = 1            # 已废弃（保留编号占位，不再出现在菜单里）
MENU_REFRESH = 2
MENU_AUTOSTART = 3
MENU_EXIT = 4

CLASS_NAME = "WirelessStatusTrayWindow"


def resource_path(name):
    """静态资源定位：exe 里在 _MEIPASS，源码里在脚本目录。

    v2 起图标统一放在 ``icon/`` 子目录（``icon/app.ico``），所以调用点传的是
    ``icon/app.ico`` 而不是裸文件名。这里统一走 apppath.resource_dir()，
    不再自己拼 _MEIPASS —— 两处各拼一套迟早会漂移。
    """
    import apppath
    return os.path.join(apppath.resource_dir(), name)


class TrayIcon(object):
    """托盘图标。``on_toggle`` / ``on_refresh`` / ``on_autostart`` / ``on_exit``
    都是无参回调，在**托盘线程**里被调用。

    回调本身要轻：webview 的窗口操作必须回到自己线程做，所以正式实现里
    回调只往队列里塞一个事件（见 main.py 的 WirelessStatusApp._post）。
    """

    def __init__(self, tooltip="Wireless Status", on_toggle=None, on_refresh=None,
                 on_autostart=None, on_exit=None, icon_path=None,
                 on_show=None):
        self.tooltip = tooltip[:120]     # NOTIFYICONDATA 的 szTip 上限 128
        self.on_show = on_show          # 左键：只显示（需求 6）
        self.on_toggle = on_toggle      # 旧的切换语义，保留给兼容/兜底
        self.on_refresh = on_refresh
        self.on_autostart = on_autostart
        self.on_exit = on_exit
        # 图标统一在 icon/ 下，用 app.ico（多尺寸，托盘会自己挑 16/32）
        self.icon_path = icon_path or resource_path(os.path.join("icon", "app.ico"))
        self.hwnd = None
        self._thread = None
        self._ready = threading.Event()
        self._fail = None
        self._hicon = None
        self._need_destroy_icon = False
        #: 最近一次左键点击时图标中心（**物理像素**）。需求第 1 条要靠它把
        #: 主窗摆到图标正上方；拿不到（老的 v3 回调）就是 None，调用方降级。
        self.icon_point = None

    # ---- 生命周期
    def start(self):
        """起托盘线程并等它就绪（图标真正挂上去才返回）。

        必须等：主程序可能紧接着就崩溃退出，那时如果图标还没挂上，
        NIM_DELETE 会失败，但也没留下幽灵；反过来如果挂上了却不等就退出，
        幽灵图标就留下了。
        """
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        return self._fail is None

    @property
    def error(self):
        return self._fail

    def stop(self):
        """通知托盘线程退出并等它把图标删掉。"""
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3.0)

    # ---- 线程体
    def _run(self):
        try:
            self._create_window()
        except Exception as exc:
            self._fail = "%s: %s" % (type(exc).__name__, exc)
            traceback.print_exc()
            self._log_fail(exc)
            self._ready.set()
            return

        self._log_ok()
        self._ready.set()
        try:
            win32gui.PumpMessages()
        except Exception:
            traceback.print_exc()
        finally:
            self._remove_icon()
            if self._need_destroy_icon and self._hicon:
                try:
                    win32gui.DestroyIcon(self._hicon)
                except Exception:
                    pass

    # 托盘是「静默失败」的高危区：没有控制台时，NIM_ADD 失败或
    # RegisterClass 抛异常，用户只看到「没有图标」，什么线索都没有。
    # 这两处落盘（成功也记一行 hwnd），排查 tray 问题先看它。
    def _log_path(self):
        try:
            import apppath
            return os.path.join(apppath.app_dir(), "ui_error.log")
        except Exception:
            return ""

    def _log(self, text):
        path = self._log_path()
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as fp:
                fp.write("[%s] tray: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        except Exception:
            pass

    def _log_fail(self, exc):
        self._log("创建失败 %s: %s\n%s" % (
            type(exc).__name__, exc, traceback.format_exc()))

    def _log_ok(self):
        self._log("已创建 hwnd=%s tooltip=%r icon=%r" % (
            self.hwnd, getattr(self, "tooltip", ""), self.icon_path))

    def _create_window(self):
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = CLASS_NAME
        wc.lpfnWndProc = self._wnd_proc
        try:
            self._class_atom = win32gui.RegisterClass(wc)
        except Exception:
            # 类已注册（上一轮没清干净）时直接用
            self._class_atom = None
        self.hwnd = win32gui.CreateWindow(
            CLASS_NAME, self.tooltip, win32con.WS_OVERLAPPED,
            0, 0, 0, 0, 0, 0, wc.hInstance, None)
        win32gui.UpdateWindow(self.hwnd)

        self._hicon = self._load_icon()
        self._add_icon()

    def _load_icon(self):
        """优先用自带的 .ico；没有就退回系统默认图标（IDI_APPLICATION）。"""
        if self.icon_path and os.path.exists(self.icon_path):
            try:
                # LR_LOADFROMFILE 会返回一个需要手动 DestroyIcon 的句柄
                h = win32gui.LoadImage(
                    0, self.icon_path, win32con.IMAGE_ICON,
                    0, 0, win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
                if h:
                    self._need_destroy_icon = True
                    return h
            except Exception:
                pass
        self._need_destroy_icon = False
        return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    def _add_icon(self):
        flags = (win32gui.NIF_ICON | win32gui.NIF_MESSAGE
                 | win32gui.NIF_TIP)
        nid = (self.hwnd, 0, flags, WM_TRAY, self._hicon, self.tooltip)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
        except Exception as exc:
            raise RuntimeError(self._explain(exc)) from exc
        # 协商 v4 协议：回调的 lParam 才会带图标中心坐标。
        # 失败不致命（老系统或某些壳层不支持），只是拿不到精确锚点，
        # 主窗会降级到「通知区域中心正上方」。
        try:
            win32gui.Shell_NotifyIcon(
                NIM_SETVERSION, (self.hwnd, 0, NOTIFYICON_VERSION_4))
        except Exception:
            pass

    @staticmethod
    def _explain(exc):
        """把 Shell_NotifyIcon 的原始错误翻译成能照着做的中文。

        「拒绝访问」(error 5) 不是权限设置问题，而是**进程完整性级别低于
        任务栏**：explorer 跑在 Medium IL，如果本进程因为所在目录带低完整性
        标签（或用低权限方式启动）而处于 Low IL，UIPI 会静默拦掉这条消息。
        实测：同一个 exe，放在普通目录可正常挂图标，放在带
        S-1-4-... 标签的目录就必然 error 5。
        """
        text = "%s" % (exc,)
        if "拒绝访问" in text or "Access is denied" in text or "(5," in text:
            return (
                "%s —— 进程权限低于任务栏（UIPI 拦截）。"
                "常见原因是程序所在目录带低完整性标签，或被以低权限方式启动；"
                "把程序挪到普通目录（如桌面）再运行即可" % text)
        return text

    def _remove_icon(self):
        if not self.hwnd:
            return
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
        except Exception:
            pass

    def set_tooltip(self, text):
        """更新悬停提示（电量/DPI 变化时刷新）。"""
        if not self.hwnd:
            return
        self.tooltip = (text or "")[:120]
        try:
            nid = (self.hwnd, 0, win32gui.NIF_TIP, WM_TRAY, self._hicon, self.tooltip)
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)
        except Exception:
            pass

    def balloon(self, title, text):
        """气泡提示（低电量用）。静默失败：老系统/关闭通知时不该报错。"""
        if not self.hwnd:
            return
        try:
            nid = (self.hwnd, 0, win32gui.NIF_INFO, WM_TRAY, self._hicon,
                   self.tooltip, text[:250], 1000, title[:60],
                   win32gui.NIIF_INFO)
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, nid)
        except Exception:
            pass

    # ---- 消息处理
    @staticmethod
    def _parse_point(lparam):
        """把 v4 回调的 lparam 解析成图标中心坐标（物理像素）。

        转调 ``winui.icon_point_from_lparam`` —— 解析逻辑放在 winui 里，
        托盘线程和窗口定位共用同一套语义，避免两处各算一套。
        解析不出来返回 None（调用方降级到通知区域中心）。
        """
        try:
            import winui
            return winui.icon_point_from_lparam(lparam)
        except Exception:
            return None

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            # v4 之后语义变了：**wParam 低 16 位是事件码**，lParam 是图标
            # 中心坐标（低 16 位 x、高 16 位 y，物理像素）。
            # v3 及以前是反过来的（lParam 低 16 位是事件码）。两种都兼容：
            # 先按 v4 解析，事件码不认识再按旧语义试一遍。
            event = wparam & 0xFFFF
            handled_v4 = True
            if event == NIN_SELECT or event == NIN_KEYSELECT:
                point = self._parse_point(lparam)
                self.icon_point = point
                # 需求 6：左键**只显示**，收起交给「鼠标移出窗口 2 秒」。
                self._safe(self.on_show or self.on_toggle)
            elif event == NIN_BALLOONUSERCLICK:
                point = self._parse_point(lparam)
                self.icon_point = point
                self._safe(self.on_show or self.on_toggle)
            elif event == win32con.WM_RBUTTONUP or event == win32con.WM_CONTEXTMENU:
                self._show_menu()
            else:
                handled_v4 = False

            if not handled_v4:
                # 旧语义（v3）：lParam 低 16 位是事件码
                legacy = lparam & 0xFFFF
                if legacy in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
                    self.icon_point = None
                    self._safe(self.on_show or self.on_toggle)
                elif legacy == win32con.WM_RBUTTONUP:
                    self._show_menu()
            return 0
        if msg == win32con.WM_COMMAND:
            cmd = wparam & 0xFFFF
            if cmd == MENU_SHOW:
                self._safe(self.on_toggle)
            elif cmd == MENU_REFRESH:
                self._safe(self.on_refresh)
            elif cmd == MENU_AUTOSTART:
                self._safe(self.on_autostart)
            elif cmd == MENU_EXIT:
                # 先让主程序做事（它会 stop() 这个托盘），再退出消息泵
                self._safe(self.on_exit)
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            return 0
        if msg == win32con.WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            win32gui.PostQuitMessage(0)
            return 0
        if msg == win32con.WM_DESTROY:
            self._remove_icon()
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _show_menu(self):
        """右键菜单。TrackPopupMenu 要求窗口必须是前台，否则点菜单外面
        不消失（经典 "菜单卡住" bug），所以先 SetForegroundWindow。"""
        menu = win32gui.CreatePopupMenu()
        try:
            autostart_on = False
            try:
                import autostart
                autostart_on = autostart.is_enabled()
            except Exception:
                pass
            # 分隔符的文本要传空串，不能传 None —— 新版 pywin32 会
            # 抛 TypeError: None is not a valid string in this context，
            # 症状是「右键托盘图标，菜单不弹、控制台在刷堆栈」。
            # 需求 6：没有「显示 / 收起」—— 左键点图标就是显示，
            # 鼠标移开 2 秒自动收起，菜单里不需要这一项。
            win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_REFRESH, "立即刷新")
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            flags = win32con.MF_STRING | (win32con.MF_CHECKED if autostart_on else 0)
            win32gui.AppendMenu(menu, flags, MENU_AUTOSTART, "开机自启")
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_EXIT, "退出")

            x, y = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(self.hwnd)
            win32gui.TrackPopupMenu(
                menu,
                win32con.TPM_LEFTALIGN | win32con.TPM_BOTTOMALIGN | win32con.TPM_RIGHTBUTTON,
                x, y, 0, self.hwnd, None)
            # 这条 PostMessage 是 TrackPopupMenu 的官方配套动作，缺了菜单
            # 会保持模态，第二次右键就打死不开
            win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        finally:
            win32gui.DestroyMenu(menu)

    def _safe(self, fn):
        if not fn:
            return
        try:
            fn()
        except Exception:
            traceback.print_exc()
