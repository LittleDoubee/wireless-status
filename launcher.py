# -*- coding: utf-8 -*-
"""脱离 Job Object 的启动通道 —— 让程序不在调用者的进程树里

======================================================================
为什么需要这个模块（别删）
======================================================================
本机上有这么个环境：DSH（DeepSeek Harness）会把**自己拉起的子进程**放进
一个 Job Object，并且带 ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``。后果是：

    在 DSH 的终端里敲 ``python main.py`` 启动本程序
    → 进程在 DSH 的 Job 里
    → DSH 一关（甚至只是关掉那个终端页），Job 被关闭
    → **本程序跟着被杀**

用户看到的现象就是「程序莫名其妙闪退」，而且必现 —— 退出时机和 DSH 关闭
时机完全一致。

同一台机器上的兄弟项目 ``workbuddy-usage`` 早就踩过这个坑，它的结论是
（原文见那个项目的 autostart.py）：

    普通 Popen 拉起的进程在 DSH 的 Job Object 里，DSH 一关就跟着没。
    schtasks 拉起的进程父进程是 svchost，脱离调用者进程树，dsh 关掉后
    浮层仍在跑。

所以这里实现同样的做法：**用 Task Scheduler 拉起**。

为什么 Task Scheduler 能脱离：

    schtasks /run 触发的任务，由 **svchost**（Task Scheduler 服务）来启动，
    进程树挂在 svchost 底下，不挂在调用者底下，也就不在调用者的 Job 里。
    临时任务跑完立刻 /delete 掉，任务计划程序里不留垃圾。

实现要点（每一条都是踩出来的）：

* 用 ``once`` 触发器而不是 ``onlogon`` —— 后者要管理员权限，实测被拒；
* 创建后立刻 ``/run``，等一会儿让进程真的起来，再 ``/delete`` 任务本体
  —— 实测删除一个**已经触发**的任务不会杀掉它的进程；
* ``schtasks /tr`` 有 261 字符上限，超了直接回退，免得留下半截命令；
* **每次用唯一任务名** —— 复用同一个名字时，第二次 ``/create /f`` 覆盖后
  旧实例的状态可能还是"正在运行"，``/run`` 会打印「信息: 计划任务 "X" 正在
  运行。」并**仍然返回 rc=0，但不会启动新进程**。调用方看到 rc=0 以为拉起来
  了，然后白等（那个项目实测白等 20 秒），最后误判"启动失败"再拉一次。

用法::

    python launcher.py --start     :: 脱离 DSH job 启动本程序（推荐）
    python launcher.py --start --show   :: 同上，但启动时直接显示窗口
"""

import os
import subprocess
import sys
import time

#: 临时任务名前缀。带 pid + 毫秒时间戳，保证每次都是全新任务。
TASK_PREFIX = "WirelessStatusLaunch"

#: ``schtasks /tr`` 的命令行长度上限（Windows 硬限制）。
SCHTASKS_CMD_LIMIT = 261

CREATE_NO_WINDOW = 0x08000000

IS_WINDOWS = os.name == "nt"


def app_dir():
    """程序目录（exe 同目录 / 脚本目录，与 apppath.app_dir 一致）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def pythonw_path():
    """当前解释器对应的 pythonw.exe；找不到退回 python.exe。

    用 python.exe 会常驻一个黑色控制台窗口 —— 双击 start.bat 看到的
    "闪一下黑框"就是它。pythonw 是同一套解释器，只是没有控制台。
    """
    base = os.path.dirname(sys.executable or "")
    for name in ("pythonw.exe", "python.exe"):
        cand = os.path.join(base, name)
        if os.path.exists(cand):
            return cand
    return sys.executable


def _schtasks(args):
    """跑一条 schtasks，返回 ``(rc, 输出)``。失败不抛。"""
    try:
        proc = subprocess.run(
            ["schtasks"] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:
        return -1, "%s: %s" % (type(exc).__name__, exc)
    out = (proc.stdout or b"").decode("utf-8", "replace")
    return proc.returncode, out


def detach_launch(args_list, label="程序", wait_seconds=3.0):
    """用 Task Scheduler 拉起一个进程，使它**脱离调用者的 Job Object**。

    ``args_list`` 是完整命令行（``[解释器, 脚本, 参数...]``）；``label``
    只用于日志。成功返回 ``(True, "")``；不可用时返回 ``(False, 原因)``，
    调用方应回退到普通启动（那样虽然可能跟着 DSH 一起没，至少能用）。

    详见模块头部那段说明 —— 每一个实现细节背后都有一个踩过的坑。
    """
    if not IS_WINDOWS:
        return False, "非 Windows 平台不支持 schtasks 通道"
    if not args_list:
        return False, "命令行是空的"

    cmd = " ".join('"%s"' % str(a) if (" " in str(a) or not str(a)) else str(a)
                   for a in args_list)
    if len(cmd) > SCHTASKS_CMD_LIMIT:
        return False, ("命令行 %d 字符，超过 schtasks /tr 的 %d 上限"
                       % (len(cmd), SCHTASKS_CMD_LIMIT))

    # 唯一任务名：复用名字会踩"任务正在运行 → /run 返回 0 但不启动"的坑
    task = "%s_%d_%d" % (TASK_PREFIX, os.getpid(),
                         int(time.time() * 1000) % 1000000)
    rc, out = _schtasks(["/create", "/tn", task, "/tr", cmd,
                         "/sc", "once", "/st", "23:59", "/f"])
    if rc != 0:
        return False, "schtasks /create 失败（rc=%s）：%s" % (rc, out[:200])

    rc2, out2 = _schtasks(["/run", "/tn", task])
    if rc2 != 0:
        _schtasks(["/delete", "/tn", task, "/f"])
        return False, "schtasks /run 失败（rc=%s）：%s" % (rc2, out2[:200])

    # 给进程一点时间真正起来，再把临时任务删掉。
    # 删一个**已经触发**的任务不会杀掉它的进程 —— 实测过。
    time.sleep(max(0.0, float(wait_seconds)))
    _schtasks(["/delete", "/tn", task, "/f"])
    return True, ""


def launch_args(show=False):
    """本程序的完整启动命令行（脱离 job 用）。"""
    script = os.path.join(app_dir(), "main.py")
    args = [pythonw_path(), script]
    if show:
        args.append("--show")
    else:
        args.append("--silent")
    return args


def launch_detached(show=False, wait_seconds=2.0):
    """脱离 Job Object 启动本程序。返回 ``(ok, 原因)``。

    注意：这条通道**不会**等待程序真正就绪（pywebview 冷启动要十几秒），
    只保证进程已经被 Task Scheduler 拉起来了。想确认是否活着，看
    ``lifecycle.log`` 里的「启动 pid=...」那一行。
    """
    return detach_launch(launch_args(show=show), label="Wireless Status",
                         wait_seconds=wait_seconds)


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--start":
        show = "--show" in argv[1:]
        ok, why = launch_detached(show=show)
        if ok:
            print("已通过 Task Scheduler 脱离父进程树启动（父进程=svchost）")
            return 0
        print("脱离启动失败：%s" % why)
        print("回退到普通启动（注意：在 DSH 终端里启动会跟着 DSH 一起退出）")
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
