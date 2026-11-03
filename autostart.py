# -*- coding: utf-8 -*-
"""开机自动启动 —— 写 HKCU\\...\\CurrentVersion\\Run

与 WorkBuddy 悬浮窗的 autostart.py 同一套做法，但多处理了一个客观问题：
**源码运行和 exe 运行注册的命令行不一样**，切换运行方式时必须自愈。

  * 源码运行：  "pythonw.exe" "D:\\...\\wireless-status\\main.py"
  * exe 运行：   "D:\\...\\WirelessStatus.exe"

常见 bug：先用 pythonw 注册了，后来改成 exe 发行，注册表里还留着
pythonw + 脚本路径。别人把 exe 拷走后（脚本路径不存在了）开机报错弹窗。
所以每次启动都校验一次：注册表里的值和「当前运行方式应有的值」不一致
就重写。
"""

import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "WirelessStatus"


def app_dir():
    """用户数据目录：exe 同目录 / 脚本目录（与 apppath.app_dir 一致）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def target_path():
    """应当被写进注册表的可执行文件路径。"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.join(app_dir(), "main.py")


def pythonw_path():
    """当前解释器对应的 pythonw.exe；找不到就退回 python.exe。

    用 python.exe 启动会常驻一个黑色控制台窗口 —— 用户双击 start.bat 看到的
    "闪一下黑框" 就是它。pythonw 是同一套解释器，只是没有控制台。
    """
    exe = sys.executable or ""
    base = os.path.dirname(exe)
    for name in ("pythonw.exe", "python.exe"):
        cand = os.path.join(base, name)
        if os.path.exists(cand):
            return cand
    return exe


def launch_command():
    """注册表里应该写的完整命令行。

    两个路径都加引号：用户名带空格（C:\\Users\\Zhang San\\...）时不加引号
    Windows 会把空格当参数分隔符，开机启动直接失败，而且**不报错**。
    """
    if getattr(sys, "frozen", False):
        return '"%s"' % target_path()
    return '"%s" "%s"' % (pythonw_path(), target_path())


def _open_key(write=False):
    import winreg
    access = winreg.KEY_READ
    if write:
        access = winreg.KEY_READ | winreg.KEY_SET_VALUE
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def current_command():
    """读注册表里现有的值；没有这一项返回 None。

    注意：返回 None 和 "读到空字符串" 是两回事 —— 后者说明有人写了空值，
    也算「没启用」，这里统一按未启用处理。
    """
    import winreg
    try:
        with _open_key(write=False) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return value or None
    except FileNotFoundError:
        return None
    except OSError:
        return None
    except ImportError:
        # 非 Windows（或 pywin32 缺失）—— 功能不可用，但不该让程序起不来
        return None


def is_enabled():
    """注册表里有这一项且指向存在的文件，才算真的启用。"""
    cmd = current_command()
    if not cmd:
        return False
    path = _first_path(cmd)
    return bool(path) and os.path.exists(path)


def _first_path(command):
    """从 '"C:\\a b\\x.exe" "C:\\c.py"' 里抠出第一个带引号的路径。

    没有引号时取第一段（空格前），只用来做存在性校验，不追求完备。
    """
    text = (command or "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 1:
            return text[1:end]
    return text.split(" ", 1)[0]


def enable():
    """写注册表。返回 (ok, message)。"""
    import winreg
    cmd = launch_command()
    path = target_path()
    if not os.path.exists(path):
        return False, "目标文件不存在：%s" % path
    try:
        with _open_key(write=True) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
        return True, "已开启开机自启"
    except ImportError:
        return False, "缺少 pywin32，无法写注册表（python -m pip install pywin32）"
    except OSError as exc:
        return False, "写注册表失败：%s" % exc


def disable():
    """删注册表项。本来就没有也算成功（幂等）。"""
    import winreg
    try:
        with _open_key(write=True) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True, "已关闭开机自启"
    except FileNotFoundError:
        return True, "开机自启本来就是关闭的"
    except ImportError:
        return False, "缺少 pywin32，无法写注册表"
    except OSError as exc:
        return False, "删除注册表项失败：%s" % exc


def set_enabled(on):
    return enable() if on else disable()


def heal():
    """启动时自愈：注册表里的值指向的命令行与当前运行方式不符就重写。

    只在「用户确实启用过」时动手 —— 不启用时一个字节都不碰，避免把别人
    手动加的项悄悄改掉。返回 True 表示改动过。
    """
    cmd = current_command()
    if not cmd:
        return False
    want = launch_command()
    if cmd.strip().lower() == want.strip().lower():
        return False
    path = _first_path(cmd)
    if path and os.path.exists(path):
        # 老路径仍然有效（比如同时留着源码和 exe），但运行方式可能变了，
        # 一律改成当前方式，保证「切换运行方式能自愈」。
        pass
    ok, _msg = enable()
    return bool(ok)
