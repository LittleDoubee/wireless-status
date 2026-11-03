# -*- coding: utf-8 -*-
"""路径与配置 —— 绿色便携形态的唯一入口

两个目录必须**分开**，混用是单文件 exe 最常见的坑：

  * ``app_dir()``      —— 用户数据目录。源码运行时是脚本目录，exe 运行时是
    exe 同目录。``config.json`` / ``window_state.json`` 都写这里，所以把整个
    目录（或 exe + 两个 json）拷到别处，设置能跟着走。
  * ``resource_dir()`` —— 只读静态资源目录（``web/``）。exe 里 PyInstaller
    会把它解到 ``sys._MEIPASS``，那是个临时目录，**每次启动都不同、进程退出
    就删**。拿它存配置等于每次启动都读回内置默认值。

所以：读 web 用 resource_dir()，读写 json 用 app_dir()。写反了不会报错，
只会表现为「设置保存不住」，特别难查。

第三个目录 ``runtime_dir()`` 是进程的**工作目录**，专门用来接住第三方
DLL（搜狗输入法）往 CWD 写的 ``debug.log``，见它的说明。
"""

import os
import sys
import json
import time

APP_NAME = "WirelessStatus"
APP_VERSION = "0.3.0"

#: 配置文件里出现的键与默认值。缺键时按这里补，多余键原样保留
#: （向前兼容：旧版本写的键不丢，新版本只是不认识）。
DEFAULT_CONFIG = {
    # ---- 旧键（保留，v1 → v2 迁移时不清掉，未知键也原样写回）
    "poll_interval": 5.0,          # 窗口可见时的轮询间隔（秒）
    "poll_interval_hidden": 30.0,  # 窗口隐藏/未查看时的间隔（秒），需求第 1 条
    "device_index": 0,             # 0 = 自动选第一个应答的设备
    "autostart": False,            # 开机自启（真实状态以注册表为准，这里只做期望值缓存）
    "always_on_top": True,
    "low_battery_threshold": 5,    # 百分比，低于它红色高亮
    "window": {
        "width": 392,
        "height": 588,
        "x": None,                  # None = 首次按「屏幕右侧居中」摆放
        "y": None,
    },
    # 设备上报的 DPI 能力快照。只是缓存，用来在没有设备时也能画出合理滑块；
    # 有设备时一律以设备实时上报为准（inferred=False）。
    "dpi_range": {"min": 200, "max": 12000, "step": 50, "inferred": True},

    # ---- v2 新增
    "version": 2,
    "poll_interval_sec": 5,        # v2 起的规范键；下面 poll_interval 依然兼容
    "battery_pill_enabled": False, # 任务栏电量贴片，默认关（需求：可选）
    "battery_curve": "alkaline",   # "alkaline" | "nimh"，电压换算用
    "battery_levels": {"good": 50, "low": 20, "critical": 5},
    "last_selected_device": None,  # 上次选中的设备键（只高亮，不自动跳页）
    "theme": "auto",               # auto | light | dark（auto = 跟系统）
}

DEFAULT_STATE = {
    "x": None,
    "y": None,
    "width": 392,
    "height": 588,
}


def app_dir():
    """用户数据目录：exe 同目录 / 脚本目录。

    **不要**用 ``sys._MEIPASS``：那是 PyInstaller 的解包临时目录，
    重启即丢，配置写进去等于没写。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_dir():
    """只读资源目录（web/、icon.ico 所在）。exe 里是 _MEIPASS。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return base
    return os.path.dirname(os.path.abspath(__file__))


def web_dir():
    return os.path.join(resource_dir(), "web")


CONFIG_NAME = "config.json"
STATE_NAME = "window_state.json"
HISTORY_NAME = "battery_history.json"
USAGE_NAME = "usage_stats.json"


def config_path():
    return os.path.join(app_dir(), CONFIG_NAME)


def state_path():
    return os.path.join(app_dir(), STATE_NAME)


def history_path():
    """电量历史。**走 app_dir()**，跟 config 一样是可写的用户数据。

    别放 _MEIPASS：那是解包临时目录，程序一退就删，历史写进去等于没写。
    """
    return os.path.join(app_dir(), HISTORY_NAME)


def usage_path():
    """使用时长统计。同样走 app_dir()，便携拷走时要跟着一起走。"""
    return os.path.join(app_dir(), USAGE_NAME)


def data_paths():
    """一次性列出所有「用户数据文件」，打包时靠它自查有没有漏。"""
    return {
        "config": config_path(),
        "state": state_path(),
        "history": history_path(),
        "usage": usage_path(),
    }


def icon_dir():
    """图标目录（只读资源）。跟 web/ 一样走 resource_dir()。"""
    return os.path.join(resource_dir(), "icon")


#: 运行时垃圾目录名（见 runtime_dir 的说明）。
RUNTIME_DIR_NAME = "runtime"


def runtime_dir():
    """进程的「工作目录」—— 专门用来接住第三方 DLL 往 CWD 写的垃圾。

    ============================================================
    为什么需要这个目录（血泪，别删）
    ============================================================
    搜狗输入法的 TSF 组件会注入到任何有窗口的进程里（我们的 exe 和
    WebView2 的浏览器进程都躲不开）。它内部用 Chromium 的日志设施打
    ``SGRenderLog`` 警告，而 Chromium 在没给 ``--log-file`` 时，默认把
    日志写到**当前工作目录**下的 ``debug.log``。

    结果：程序一启动，exe 同目录（或从资源管理器双击时的那个目录）就
    凭空多出一个 ``debug.log``，内容是：

        [pid:tid:MMDD/HHMMSS.mmm:WARNING:t_skinfilemap.cpp(56)]
        SGRenderLog:CheckUpdate newMem.OpenMem failed,
        szNewMem:Local\\sgime_skin.v2.filemap.sogouime err:系统错误2...

    这跟本程序毫无关系，但用户看到 exe 旁边冒出日志文件，第一反应就是
    「这软件在报错」。

    试过但**无效**的方案（不要再试）：
      * ``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--disable-logging``
        —— pywebview 在 ``platforms/edgechromium.py`` 里硬编码
        ``props.AdditionalBrowserArguments = '--disable-features=...'``，
        程序化赋值会盖掉环境变量。
      * 等输入法自己修 —— 不可控。

    唯一可靠的做法：**把 CWD 挪到我们自己指定的目录**。本程序所有文件
    读写都走 ``app_dir()`` / ``resource_dir()`` 的绝对路径（全项目没有
    一处相对路径的 ``open()``），所以改 CWD 不影响任何功能。

    目录放 ``%LOCALAPPDATA%\\WirelessStatus\\runtime`` 而不是 exe 同目录：
    绿色软件的 exe 目录应该只有 exe + 四个 json，不该多出日志。
    """
    base = os.environ.get("LOCALAPPDATA")
    if base:
        path = os.path.join(base, APP_NAME, RUNTIME_DIR_NAME)
    else:
        # 没有 LOCALAPPDATA（极简环境 / 服务账户）就退回用户数据目录，
        # 宁可多个子目录，也不能让 chdir 失败。
        path = os.path.join(app_dir(), RUNTIME_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        return app_dir()
    return path


#: 第三方 DLL 会往 CWD 丢的文件名。启动时顺手清掉旧版本留在别处的。
CWD_JUNK_NAMES = ("debug.log",)


def purge_cwd_junk(directory):
    """删掉 ``directory`` 里由第三方 DLL 生成的日志垃圾。返回删掉的个数。

    只在 ``directory`` 里找固定的几个文件名（见 ``CWD_JUNK_NAMES``），
    不做通配删除 —— 万一用户自己在 exe 旁边放了个同名文件，也只删这一个
    已知的垃圾名，不会误伤其它东西。
    """
    removed = 0
    for name in CWD_JUNK_NAMES:
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
        except Exception:
            pass
    return removed


# ---------------------------------------------------------------- JSON 读写

def _read_json(path):
    """读 json；文件不存在/损坏/没权限都返回 None，绝不抛。

    必须用 utf-8-sig：Windows 记事本、PowerShell 5.1 的
    `Set-Content -Encoding UTF8`、以及不少编辑器另存的「UTF-8」都是**带 BOM** 的。
    用 utf-8 读会抛 `JSONDecodeError: Unexpected UTF-8 BOM`，调用方把它当成
    「没有配置文件」→ 立刻用默认值覆盖回去，用户改过的设置全丢。
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_json(path, data):
    """原子写：先写 .tmp 再 os.replace。

    直接 open(path, "w") 写到一半被杀（任务管理器结束进程、关机）会留下
    半个 json，下次启动解析失败 → 设置全丢。os.replace 在 NTFS 上是原子的。
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        # 目录只读（U 盘写保护、装在 Program Files 又没管理员权限）时降级：
        # 调用方保持内存里的配置，界面提示一句中文，程序照常用。
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def _merge(defaults, loaded):
    """一层深合并：以 defaults 的键为准，嵌套 dict 递归。"""
    out = {}
    for key, dv in defaults.items():
        if key not in loaded:
            out[key] = dv if not isinstance(dv, dict) else _merge(dv, {})
            continue
        lv = loaded[key]
        if isinstance(dv, dict):
            out[key] = _merge(dv, lv if isinstance(lv, dict) else {})
        else:
            out[key] = lv
    # 保留配置文件里多出来的键（别的版本写的），不静默丢弃
    for key, lv in loaded.items():
        if key not in out:
            out[key] = lv
    return out


class Store(object):
    """配置 + 窗口状态的内存副本，带「写不进去」的降级标记。

    ``readonly`` 为 True 表示磁盘不可写，界面要提示一句。所有 setter 都
    先改内存再尝试落盘，所以读永远是即时的。
    """

    def __init__(self):
        self.config_file = config_path()
        self.state_file = state_path()
        self.config = _load_or_create(self.config_file, DEFAULT_CONFIG)
        self.state = _load_or_create(self.state_file, DEFAULT_STATE)
        self.readonly = not _probe_writable(app_dir())

    # ---- config
    def save_config(self):
        ok = _write_json(self.config_file, self.config)
        self.readonly = not ok
        return ok

    def set_config(self, key, value):
        """只接受已知键：界面传什么进来都不能凭空塞一个新键。"""
        if key not in DEFAULT_CONFIG:
            return False
        self.config[key] = value
        self.save_config()
        return True

    # ---- window state
    def save_state(self, **kwargs):
        for key in ("x", "y", "width", "height"):
            if kwargs.get(key) is not None:
                self.state[key] = int(kwargs[key])
        ok = _write_json(self.state_file, self.state)
        self.readonly = not ok
        return ok


def _load_or_create(path, defaults):
    """读配置；文件不存在才生成默认值。

    关键区别：**「文件不存在」和「文件读不懂」必须分开处理**。
    以前两种情况都走 `data is None` 分支，结果是「用户手改坏了 json」或
    「编码不认（BOM）」时，程序直接用默认值覆盖掉用户文件 —— 文件里的内容
    当场就没了，用户只看到「设置被重置」。所以现在：
      * 不存在 → 写默认值（首启行为）
      * 存在但读不懂 → 保持内存默认值，**不写盘**，并打一条 warn 日志
    """
    if not os.path.exists(path):
        merged = json.loads(json.dumps(defaults))
        _write_json(path, merged)
        return merged
    data = _read_json(path)
    if data is None:
        _warn_config_unreadable(path)
        return json.loads(json.dumps(defaults))
    return _merge(defaults, data)


def _warn_config_unreadable(path):
    """配置文件存在但解析不了：留痕，别静默覆盖。

    pythonw / -w exe 没有控制台，所以同时写一行到 ``ui_error.log``。
    """
    msg = ("配置文件无法解析（不是合法 JSON，或编码不被识别），本次使用"
           "内置默认值且**不会覆盖原文件**：%s" % path)
    try:
        import logging
        logging.getLogger("wireless_status").warning(msg)
    except Exception:
        pass
    try:
        line = "[%s] config: %s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        # 日志跟着配置文件走，而不是固定 app_dir()：自测用临时目录跑时不该
        # 往真实程序目录里写东西，用户也能在「配置文件旁边」直接找到它。
        log_path = os.path.join(
            os.path.dirname(os.path.abspath(path)), "ui_error.log")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass



def _probe_writable(directory):
    """探一次目录可写：写个探针文件再删。

    只读目录（绿色软件放在 Program Files、U 盘写保护）要能优雅降级，
    不能等到用户点了「保存」才发现，更不能崩。
    """
    probe = os.path.join(directory, ".write_probe_%d" % int(time.time() * 1000))
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False
