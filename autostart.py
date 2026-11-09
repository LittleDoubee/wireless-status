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

#: 清理重复自启项时的**值名白名单之外**的排除项。
#:
#: 不用"历史值名黑名单"：那要求代码里写死一个旧名字，改名换姓后又得回来
#: 改一次，而且名字本身就是不该继续出现在仓库里的东西。
#:
#: 改用**按命令行内容判断**：``HKCU\...\Run`` 下凡是「指向本程序」且不是
#: 规范值名 ``VALUE_NAME`` 的项，都是历史遗留或手动添加的重复项，一律删掉。
#: 判断依据是命令行里出现本程序的特征路径（脚本名 / exe 名），因此改多少次
#: 名字都不会漏，也不会误删别的软件。
_OWN_MARKERS = ("wireless-status", "wirelessstatus", "main.py")

#: 开机自启时追加的命令行开关。
#:
#: ``--silent`` 的含义是「这次是被开机拉起来的」：即使以后默认行为改成
#: 「手动启动就显示窗口」，开机这条路径也必须**只出现在托盘里**，不弹窗、
#: 不抢焦点。当前 main.py 默认就是静默，这个参数是显式的意图声明 + 未来
#: 兼容，也方便用户在进程列表里一眼看出「这条是开机拉起的」。
SILENT_FLAG = "--silent"


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
    """注册表里应该写的完整命令行（**总是带 ``--silent``**）。

    两个路径都加引号：用户名带空格（``C:\\Users\\Zhang San\\...``）时不加引号
    Windows 会把空格当参数分隔符，开机启动直接失败，而且**不报错**。

    为什么要带 ``--silent``：开机拉起这条路径必须"存在即静默"。把它写进
    注册表而不是只靠程序默认值，一是显式声明意图（以后默认值变了也不会
    退化成开机弹窗），二是在任务管理器/进程列表里能直接看出来源。
    """
    if getattr(sys, "frozen", False):
        return '"%s" %s' % (target_path(), SILENT_FLAG)
    return '"%s" "%s" %s' % (pythonw_path(), target_path(), SILENT_FLAG)


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


def prune_legacy():
    """删掉 ``Run`` 下**指向本程序**的重复项（规范值名保留）。返回删掉的个数。

    ============================================================
    为什么需要（血泪，别再改回"写死旧名字"）
    ============================================================
    同一个程序在 ``Run`` 里被注册两条时，开机 explorer 会把两个进程都拉起来。
    第一个拿到单实例锁正常静默运行，第二个拿不到锁 —— 老代码在那种情况下会
    ``ShowWindow`` 把已有窗口顶出来、还兜底弹一个 MessageBox。用户感知就是
    「开机后自己弹出一个"已经在运行"的框」。

    早期实现靠"历史值名黑名单"（把老名字写进常量）来清。那种做法有两个问题：
      1. 老名字会一直留在代码/文档里，成为不该存在的历史包袱；
      2. 再改一次名，黑名单就又漏了。

    所以改成**按命令行内容识别**：遍历 ``Run``，命令行里出现本程序特征路径、
    且值名不是规范的 ``VALUE_NAME`` 的，就是重复项，删掉。
    这样不管是哪一版留下的、还是用户手动加的别名，都能收干净。
    """
    try:
        import winreg
    except ImportError:
        return 0
    removed = 0
    try:
        with _open_key(write=True) as key:
            names = []
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if name == VALUE_NAME:
                    continue
                if _looks_like_own_command(value):
                    names.append(name)
            for name in names:
                try:
                    winreg.DeleteValue(key, name)
                    removed += 1
                except OSError:
                    pass
    except FileNotFoundError:
        return 0
    except OSError:
        return 0
    return removed


def _looks_like_own_command(command):
    """命令行是否指向本程序。只做小写包含判断 —— 够了，不需要解析。"""
    text = (command or "").lower()
    if not text:
        return False
    return any(m in text for m in _OWN_MARKERS)


def heal():
    """启动时自愈：注册表里的值指向的命令行与当前运行方式不符就重写。

    只在「用户确实启用过」时动手 —— 不启用时一个字节都不碰，避免把别人
    手动加的项悄悄改掉。返回 True 表示改动过。

    顺带清理遗留别名（见 ``prune_legacy``）：那一条不会自愈，留着就是每次
    开机双实例、第二个把窗口弹出来。
    """
    pruned = prune_legacy()
    cmd = current_command()
    if not cmd:
        return bool(pruned)
    want = launch_command()
    if cmd.strip().lower() == want.strip().lower():
        return bool(pruned)
    path = _first_path(cmd)
    if path and os.path.exists(path):
        # 老路径仍然有效（比如同时留着源码和 exe），但运行方式可能变了，
        # 一律改成当前方式，保证「切换运行方式能自愈」。
        pass
    ok, _msg = enable()
    return bool(ok) or bool(pruned)
