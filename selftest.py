# -*- coding: utf-8 -*-
"""自测 —— 全部离线可跑，不需要插鼠标

    python selftest.py
    python selftest.py -v         # 显示每个用例的名字

覆盖（对应用户验收标准里的离线部分）：

  1. app_dir() / resource_dir() 在源码运行和模拟 frozen(_MEIPASS) 两种情形下
     解析出的目录是否正确 —— 这是「设置保存不住」的头号元凶；
  2. config.json 读写往返：改一个值、重新加载、值还在；
  3. 只读目录降级：写不进去时不抛异常，readonly 置 True；
  4. DPI 边界校验：clamp_dpi 的取整、夹取、步进对齐；decode_dpi_list 的
     List / Range / 单值 / 空表四种编码；
  5. 无设备时的降级路径：snapshot() 的结构完整、字段不缺失、不抛异常；
  6. 窗口状态保存恢复；
  7. 型号解析失败时的降级字符串；
  8. 静态资源与配置路径分离（web 目录取 resource_dir，json 取 app_dir）。

写测试时注意：**不能**真的去碰 HID 设备。所有涉及 MouseSource 的用例都走
「link/device 为 None」的降级分支，所以插没插鼠标、别人机器上有没有罗技
设备都不影响结果。
"""

import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import apppath
import autostart
import devices
import mouse_source
import server


class FrozenEnv(object):
    """上下文管理器：模拟 PyInstaller 的 frozen 状态。

    PyInstaller 的运行时特征有两个，都必须模拟，只改一个测不出真问题：
      * sys.frozen = True
      * sys._MEIPASS = 解包临时目录
    并且把 sys.executable 换成「exe 路径」——app_dir() 在 frozen 分支下读的
    是它，不是 __file__。
    """

    def __init__(self, exe_dir, meipass):
        self.exe_dir = exe_dir
        self.meipass = meipass
        self._saved = {}

    def __enter__(self):
        self._saved["frozen"] = getattr(sys, "frozen", None)
        self._saved["meipass"] = getattr(sys, "_MEIPASS", None)
        self._saved["exe"] = sys.executable
        sys.frozen = True
        sys._MEIPASS = self.meipass
        sys.executable = os.path.join(self.exe_dir, "WirelessStatus.exe")
        return self

    def __exit__(self, *exc):
        for key, attr in (("frozen", "frozen"), ("meipass", "_MEIPASS")):
            if self._saved[key] is None:
                try:
                    delattr(sys, attr)
                except AttributeError:
                    pass
            else:
                setattr(sys, attr, self._saved[key])
        sys.executable = self._saved["exe"]
        return False


class TempDir(object):
    def __init__(self, prefix="wireless-status-"):
        self.prefix = prefix
        self.path = None

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix=self.prefix)
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)
        return False


# ================================================================= 1. 路径

class TestPaths(unittest.TestCase):

    def test_source_run_app_dir_is_script_dir(self):
        """源码运行时 app_dir() 必须是脚本目录（不是 cwd）。"""
        self.assertEqual(os.path.abspath(apppath.app_dir()),
                         os.path.abspath(HERE))

    def test_source_run_ignores_meipass(self):
        """源码运行没有 _MEIPASS；即便有也不该影响 app_dir()。"""
        with TempDir() as tmp:
            with FrozenEnv(tmp, os.path.join(tmp, "_internal")):
                # frozen 分支下 app_dir() = exe 所在目录 = tmp
                self.assertEqual(os.path.abspath(apppath.app_dir()), os.path.abspath(tmp))

    def test_frozen_app_dir_is_exe_dir_not_meipass(self):
        """核心回归：exe 运行时配置目录是 exe 同目录，**绝不能**是 _MEIPASS。

        写成 _MEIPASS 的症状是「改完设置重启就没了」，因为那是解包临时目录，
        进程退出即删。
        """
        with TempDir() as exe_dir, TempDir() as mei:
            with FrozenEnv(exe_dir, mei):
                cfg = apppath.config_path()
                self.assertEqual(os.path.dirname(os.path.abspath(cfg)),
                                 os.path.abspath(exe_dir))
                self.assertNotIn(os.path.abspath(mei), os.path.abspath(cfg))

    def test_frozen_resource_dir_is_meipass(self):
        """静态资源反过来：exe 里必须走 _MEIPASS，不然 web/ 找不到。"""
        with TempDir() as exe_dir, TempDir() as mei:
            with FrozenEnv(exe_dir, mei):
                self.assertEqual(os.path.abspath(apppath.resource_dir()),
                                 os.path.abspath(mei))
                self.assertEqual(os.path.abspath(apppath.web_dir()),
                                 os.path.abspath(os.path.join(mei, "web")))

    def test_config_and_state_same_dir(self):
        self.assertEqual(os.path.dirname(apppath.config_path()),
                         os.path.dirname(apppath.state_path()))

    def test_web_files_separated_from_config(self):
        """web/index.html 在 resource_dir，config.json 在 app_dir。"""
        self.assertTrue(os.path.exists(os.path.join(apppath.web_dir(), "index.html")))
        self.assertFalse(os.path.exists(os.path.join(apppath.web_dir(), "config.json")))


# ============================================================ 2. 配置往返

class TestConfigRoundTrip(unittest.TestCase):

    def test_write_read_round_trip(self):
        with TempDir() as tmp:
            path = os.path.join(tmp, "config.json")
            data = {"poll_interval": 7.5, "device_index": 2,
                    "window": {"width": 400, "height": 600, "x": 10, "y": 20}}
            self.assertTrue(apppath._write_json(path, data))
            back = apppath._read_json(path)
            self.assertEqual(back["poll_interval"], 7.5)
            self.assertEqual(back["device_index"], 2)
            self.assertEqual(back["window"]["x"], 10)

    def test_missing_file_creates_defaults(self):
        with TempDir() as tmp:
            path = os.path.join(tmp, "config.json")
            data = apppath._load_or_create(path, apppath.DEFAULT_CONFIG)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(data["poll_interval"],
                             apppath.DEFAULT_CONFIG["poll_interval"])

    def test_partial_file_is_merged_not_replaced(self):
        """旧版 config.json 只有一两个键时，不能把缺的键变成 None。"""
        with TempDir() as tmp:
            path = os.path.join(tmp, "config.json")
            apppath._write_json(path, {"poll_interval": 3.0})
            data = apppath._load_or_create(path, apppath.DEFAULT_CONFIG)
            self.assertEqual(data["poll_interval"], 3.0)
            self.assertIn("window", data)
            self.assertEqual(data["window"]["width"],
                             apppath.DEFAULT_CONFIG["window"]["width"])

    def test_unknown_keys_preserved(self):
        """别的版本写的键不静默丢弃（向下兼容）。"""
        with TempDir() as tmp:
            path = os.path.join(tmp, "config.json")
            apppath._write_json(path, {"future_key": {"a": 1}})
            data = apppath._load_or_create(path, apppath.DEFAULT_CONFIG)
            self.assertEqual(data["future_key"], {"a": 1})

    def test_corrupt_json_falls_back_to_defaults(self):
        """半个 json（断电写坏）不能让程序起不来。"""
        with TempDir() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"poll_interval": 5.0, "wind')
            data = apppath._load_or_create(path, apppath.DEFAULT_CONFIG)
            self.assertEqual(data["poll_interval"],
                             apppath.DEFAULT_CONFIG["poll_interval"])

    def test_atomic_write_leaves_no_tmp(self):
        with TempDir() as tmp:
            path = os.path.join(tmp, "config.json")
            apppath._write_json(path, {"a": 1})
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_store_set_config_round_trip(self):
        with TempDir() as tmp, FrozenEnv(tmp, tmp):
            store = apppath.Store()
            self.assertTrue(store.set_config("poll_interval", 9.0))
            self.assertEqual(store.config["poll_interval"], 9.0)
            reloaded = apppath._read_json(store.config_file)
            self.assertEqual(reloaded["poll_interval"], 9.0)

    def test_store_rejects_unknown_key(self):
        """界面传来一个不存在的键不能凭空塞进配置文件。"""
        with TempDir() as tmp, FrozenEnv(tmp, tmp):
            store = apppath.Store()
            self.assertFalse(store.set_config("evil_key", 1))
            self.assertNotIn("evil_key", store.config)


# ========================================================== 3. 只读降级

class TestReadonlyFallback(unittest.TestCase):

    def test_unwritable_dir_is_detected(self):
        """目录不可写时要能探出来，而不是等用户点保存才失败。"""
        with TempDir() as tmp:
            self.assertTrue(apppath._probe_writable(tmp))
            missing = os.path.join(tmp, "nope", "deeper")
            self.assertFalse(apppath._probe_writable(missing))

    def test_write_failure_returns_false_not_raise(self):
        bad = os.path.join(tempfile.gettempdir(), "no-such-dir-xyz", "config.json")
        self.assertFalse(apppath._write_json(bad, {"a": 1}))

    def test_store_marks_readonly(self):
        with TempDir() as tmp, FrozenEnv(os.path.join(tmp, "missing"), tmp):
            store = apppath.Store()
            self.assertTrue(store.readonly)
            # 内存里的值仍然可读可改，只是落不了盘
            self.assertIn("poll_interval", store.config)
            store.config["poll_interval"] = 4.0
            self.assertEqual(store.config["poll_interval"], 4.0)


# ============================================================ 4. DPI 校验

class TestDpiBounds(unittest.TestCase):

    def test_clamp_inside_range(self):
        self.assertEqual(mouse_source.clamp_dpi(1000, 400, 4000, 100), 1000)

    def test_clamp_low(self):
        self.assertEqual(mouse_source.clamp_dpi(1, 400, 4000, 100), 400)

    def test_clamp_high(self):
        self.assertEqual(mouse_source.clamp_dpi(99999, 400, 4000, 100), 4000)

    def test_step_alignment(self):
        # 从下限开始按步进对齐，不是从 0 对齐（很多鼠标下限是 400 不是步进整数倍）
        self.assertEqual(mouse_source.clamp_dpi(437, 400, 4000, 100), 400)
        self.assertEqual(mouse_source.clamp_dpi(455, 400, 4000, 100), 500)

    def test_alignment_never_exceeds_high(self):
        """对齐是从下限起算的，3990 会贴到 4000（正好在上限上），不能越界。"""
        self.assertEqual(mouse_source.clamp_dpi(3990, 400, 4000, 100), 4000)
        self.assertEqual(mouse_source.clamp_dpi(4001, 400, 4000, 100), 4000)

    def test_step_one_is_passthrough(self):
        self.assertEqual(mouse_source.clamp_dpi(1234, 1, 57343, 1), 1234)

    def test_zero_step_does_not_crash(self):
        self.assertEqual(mouse_source.clamp_dpi(700, 400, 4000, 0), 700)

    # ---- decode_dpi_list：三种编码都要覆盖

    def test_decode_range_form(self):
        """Range：[低, 连字符(步进), 高, 0] —— 本机实测就是这一种。"""
        lo, hi, step = mouse_source.decode_dpi_list([0x0190, 0xE064, 0x0FA0, 0x0000])
        self.assertEqual((lo, hi, step), (400, 4000, 100))

    def test_decode_list_form(self):
        """List：全是离散值，步进取最小间隔。"""
        lo, hi, step = mouse_source.decode_dpi_list([400, 800, 1600, 0])
        self.assertEqual((lo, hi), (400, 1600))
        self.assertGreaterEqual(step, 1)

    def test_decode_single_value(self):
        lo, hi, step = mouse_source.decode_dpi_list([1000, 0])
        self.assertEqual(lo, 1000)
        self.assertEqual(hi, 1000)

    def test_decode_empty(self):
        lo, hi, step = mouse_source.decode_dpi_list([])
        self.assertEqual(lo, mouse_source.FALLBACK_DPI_MIN)
        self.assertEqual(hi, mouse_source.FALLBACK_DPI_MAX)

    def test_decode_hyphen_flag(self):
        """bit15..13 = 111 才是连字符，其余是普通数值。"""
        # 只有连字符没有端点：信息不全，退回推断范围而不是给出 0
        lo, hi, step = mouse_source.decode_dpi_list([0xE064, 0])
        self.assertEqual((lo, hi), (mouse_source.FALLBACK_DPI_MIN,
                                    mouse_source.FALLBACK_DPI_MAX))
        self.assertEqual(step, 100)
        # 0x1FFF 是普通值（bit15..13 = 001），不是连字符
        lo, hi, step = mouse_source.decode_dpi_list([0x1FFF, 0x2000, 0])
        self.assertGreaterEqual(step, 1)
        self.assertEqual(lo, 0x1FFF)


# ====================================================== 5. 无设备降级路径

class TestNoDeviceFallback(unittest.TestCase):

    def _blank(self):
        src = mouse_source.MouseSource()
        return src

    def test_snapshot_structure_when_no_device(self):
        src = self._blank()
        snap = src.snapshot()
        for key in ("ok", "status", "status_text", "battery", "dpi", "idle",
                    "connection", "receiver", "devices", "display_name",
                    "updated", "log"):
            self.assertIn(key, snap, "缺字段：%s" % key)
        self.assertEqual(snap["status"], "offline")
        self.assertEqual(snap["devices"], [])

    def test_snapshot_dpi_defaults_are_marked_inferred(self):
        """没有设备时 DPI 范围是推断值，界面必须标注。"""
        snap = self._blank().snapshot()
        self.assertFalse(snap["dpi"]["supported"])
        self.assertTrue(snap["dpi"]["inferred"])
        self.assertEqual(snap["dpi"]["min"], mouse_source.FALLBACK_DPI_MIN)
        self.assertEqual(snap["dpi"]["max"], mouse_source.FALLBACK_DPI_MAX)

    def test_snapshot_battery_not_supported_is_not_zero_percent(self):
        """需求：无电量上报要显示「不支持」，不能显示 0%。"""
        snap = self._blank().snapshot()
        self.assertFalse(snap["battery"]["supported"])
        self.assertIsNone(snap["battery"]["percent"])

    def test_snapshot_is_json_serialisable(self):
        """pywebview 的 js_api 要 json 序列化，混进 set/对象会直接报错。"""
        snap = self._blank().snapshot()
        json.dumps(snap, ensure_ascii=False)

    def test_snapshot_deep_copy(self):
        """外部改了 snapshot 不能影响内部状态。"""
        src = self._blank()
        snap = src.snapshot()
        snap["battery"]["percent"] = 99
        self.assertIsNone(src.snapshot()["battery"]["percent"])

    def test_display_name_not_empty_when_offline(self):
        """降级显示：断线时也不能是空串（界面上会留一片空白）。"""
        snap = self._blank().snapshot()
        self.assertTrue(isinstance(snap["display_name"], str))

    def test_write_dpi_without_device_reports_reason(self):
        """没有设备时写 DPI 不能抛异常，且**必须给出可读原因**。

        v2 起 set_dpi 是同步等结果的（排队不返回会被前端当成成功，用户
        看不到任何失败提示）。这里把等待窗口压到 50ms，避免自测等 4 秒。
        """
        src = self._blank()
        old = mouse_source.SYNC_WAIT_S
        mouse_source.SYNC_WAIT_S = 0.05
        try:
            res = src.set_dpi(800)
        finally:
            mouse_source.SYNC_WAIT_S = old
        self.assertIsInstance(res, dict)
        self.assertFalse(res["ok"])
        self.assertEqual(res["requested"], 800)
        self.assertTrue(res["message"])        # 不能是空串
        self.assertIn("超时", res["message"])

    def test_pending_rejected_when_poll_fails(self):
        """设备不可写时，排队的命令要被立刻拒绝而不是空等到超时。"""
        src = self._blank()
        src._pending = (1, 800)
        src._dpi_event.clear()
        src._reject_stale_pending()
        self.assertIsNone(src._pending)
        self.assertTrue(src._dpi_event.is_set())
        res = src._dpi_result
        self.assertFalse(res["ok"])
        self.assertTrue(res["message"])


# ================================================== 6. 窗口状态保存恢复

class TestWindowState(unittest.TestCase):

    def test_state_round_trip(self):
        with TempDir() as tmp, FrozenEnv(tmp, tmp):
            store = apppath.Store()
            self.assertTrue(store.save_state(x=123, y=456, width=400, height=600))
            fresh = apppath.Store()
            self.assertEqual(fresh.state["x"], 123)
            self.assertEqual(fresh.state["y"], 456)
            self.assertEqual(fresh.state["width"], 400)

    def test_partial_update_keeps_other_keys(self):
        """只更新 x 时 y/width 不能被写成 None。"""
        with TempDir() as tmp, FrozenEnv(tmp, tmp):
            store = apppath.Store()
            store.save_state(x=10, y=20, width=400, height=600)
            store.save_state(x=99)
            self.assertEqual(store.state["y"], 20)
            self.assertEqual(store.state["width"], 400)

    def test_main_rect_ignores_saved_position(self):
        """本轮改了定位策略：每次显示都按任务栏重算，不读 window_state 的坐标。

        所以旧的 _clamp_to_screen 已经被删掉了。这里改成验证新契约 ——
        存了一个离谱的坐标，主窗位置也不能被它带跑。
        """
        import main as app_main
        with TempDir() as tmp, FrozenEnv(tmp, tmp):
            store = apppath.Store()
            store.save_state(x=-5000, y=-5000)
            app = app_main.WirelessStatusApp.__new__(app_main.WirelessStatusApp)
            app.store = store
            w, h, x, y = app.main_rect()
            left, top, sw, sh = app_main.screen_work_area()
            self.assertEqual(w, app_main.MAIN_WIDTH)
            self.assertGreaterEqual(x, left)
            self.assertGreaterEqual(y, top)
            # 不能因为旧坐标是 -5000 就跑到屏幕左边外面
            self.assertNotEqual(x, -5000)

    def test_main_rect_sits_above_taskbar(self):
        """主窗底边要贴工作区下沿（就是任务栏上沿），不能压到任务栏里。"""
        import main as app_main
        app = app_main.WirelessStatusApp.__new__(app_main.WirelessStatusApp)
        # 必须套临时目录：Store() 构造时会「文件不存在就用默认值生成」，
        # 裸调会把 window_state.json 写进项目目录（自测污染）。
        with TempDir() as tmp, FrozenEnv(tmp, tmp):
            app.store = apppath.Store()
            _, h, x, y = app.main_rect()
        left, top, sw, sh = app_main.screen_work_area()
        self.assertGreaterEqual(y, top)
        # 底边最多到工作区底部，允许几像素误差（DPI 取整）
        self.assertLessEqual(y + h, sh + 4)
        # 右边贴右侧
        self.assertLessEqual(x + app_main.MAIN_WIDTH, sw + 4)


class TestDpiCoordinateSpace(unittest.TestCase):
    """DPI 缩放下的坐标系换算。

    踩过的坑：进程开了 DPI 感知后 SystemParametersInfoW(SPI_GETWORKAREA)
    返回**物理像素**，而 pywebview 的窗口坐标是**逻辑像素**。150% 缩放下
    不换算会把窗口放到屏幕外（实测 x 算成 2144，物理落点 3216 > 屏宽 2560，
    窗口整个不可见）。
    """

    def test_dpi_scale_is_positive(self):
        import main as app_main
        self.assertGreater(app_main.dpi_scale(), 0.0)

    def test_dpi_scale_returns_float(self):
        import main as app_main
        self.assertIsInstance(app_main.dpi_scale(), float)

    def test_work_area_fits_logical_screen(self):
        """工作区必须是逻辑像素：宽度不能超过逻辑屏宽。"""
        import main as app_main
        left, top, sw, sh = app_main.screen_work_area()
        self.assertGreaterEqual(left, 0)
        self.assertGreaterEqual(top, 0)
        self.assertGreater(sw, 0)
        self.assertGreater(sh, 0)
        # 物理屏宽 / scale 应约等于逻辑屏宽；用大量级兜底防止又把物理值漏出来
        self.assertLess(sw, 10000)

    def test_first_run_window_inside_work_area(self):
        """首次启动（state 里没有 x/y）算出的窗口位置必须落在工作区内。"""
        import main as app_main
        _, _, sw, sh = app_main.screen_work_area()
        w, h = 392, 588
        x = max(0, sw - w - 24)
        y = max(0, (sh - h) // 2)
        self.assertGreaterEqual(x, 0)
        self.assertLessEqual(x + w, sw)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(y + h, sh)

    def test_main_rect_uses_logical_work_area(self):
        """主窗位置必须落在**逻辑**工作区内。

        这是上一轮踩过的坑：150% 缩放下物理坐标算出的 x 是 2144，而逻辑
        屏宽只有 1707，窗口会整个跑到屏幕外。所以 main_rect() 必须以
        screen_work_area()（已除过倍率）为基准。
        """
        import main as app_main
        app = app_main.WirelessStatusApp.__new__(app_main.WirelessStatusApp)
        with TempDir() as tmp, FrozenEnv(tmp, tmp):
            app.store = apppath.Store()
            _, _, x, y = app.main_rect()
        left, top, sw, sh = app_main.screen_work_area()
        self.assertLessEqual(x + app_main.MAIN_WIDTH, sw + 4)
        self.assertLessEqual(y + app_main.MAIN_HEIGHT, sh + 4)
        self.assertGreaterEqual(x, left)
        self.assertGreaterEqual(y, top)


# =================================================== 7. 型号降级字符串

class TestNameFallback(unittest.TestCase):

    def test_receiver_kind_known(self):
        kind, label = mouse_source.receiver_kind(0xC52F)[0:2] if isinstance(
            mouse_source.receiver_kind(0xC52F), tuple) else ("", "")
        self.assertEqual(kind, "usb_receiver")

    def test_receiver_kind_unknown_pid(self):
        """未知 PID 不能抛异常，要给一个可读标签。"""
        res = mouse_source.receiver_kind(0x1234)
        self.assertTrue(isinstance(res, tuple))

    def test_format_idle(self):
        self.assertEqual(mouse_source.format_idle(0), "0 秒")
        self.assertIn("分", mouse_source.format_idle(125))
        self.assertIn("小时", mouse_source.format_idle(7300))

    def test_display_name_adds_brand(self):
        """型号展示 = 品牌 + 型号，不带 PID（需求 4 / 用户选 A）。"""
        self.assertEqual(devices.display_name_of("M350 Wireless Mouse"),
                         "罗技 M350 Wireless Mouse")
        self.assertEqual(devices.display_name_of("M350 Wireless Mouse", "B021"),
                         "罗技 M350 Wireless Mouse")

    def test_display_name_does_not_double_brand(self):
        """名字本身已带品牌前缀时不重复加。"""
        self.assertEqual(devices.display_name_of("Logitech M350"),
                         "Logitech M350")
        self.assertEqual(devices.display_name_of("罗技 M350"), "罗技 M350")
        self.assertEqual(devices.display_name_of("logi M350"), "logi M350")

    def test_display_name_never_leaks_pid(self):
        """读不到名字时也不能把 PID 摆到界面上。"""
        out = devices.display_name_of("", "C52F")
        self.assertNotIn("C52F", out)
        self.assertNotIn("PID", out)
        self.assertIn("罗技", out)

    def test_pid_hex_uppercase_four_digits(self):
        """PID 显示成大写四位十六进制，与设备管理器一致。"""
        dev = mouse_source.MouseDevice.__new__(mouse_source.MouseDevice)
        dev.pid = 0xB021
        self.assertEqual(dev.pid_hex, "B021")
        dev.pid = 0x0A
        self.assertEqual(dev.pid_hex, "000A")

    def test_pid_hex_empty_when_unknown(self):
        dev = mouse_source.MouseDevice.__new__(mouse_source.MouseDevice)
        dev.pid = 0
        self.assertEqual(dev.pid_hex, "")


# =========================================== 8. 静态服务与配置分离

class TestServerSafety(unittest.TestCase):

    def test_static_whitelist_blocks_json(self):
        """config.json 不能出现在 HTTP 面上。"""
        self.assertNotIn(".json", server.STATIC_EXT)
        self.assertNotIn(".py", server.STATIC_EXT)

    def test_static_whitelist_allows_web_assets(self):
        for ext in (".html", ".css", ".js", ".svg", ".ico"):
            self.assertIn(ext, server.STATIC_EXT)

    def test_path_traversal_blocked(self):
        """web/../../config.json 必须被归一化后挡掉。"""
        import posixpath
        rel = posixpath.normpath("../../config.json")
        self.assertTrue(rel.startswith(".."))

    def test_loopback_hosts(self):
        self.assertIn("127.0.0.1", server.LOOPBACK_HOSTS)
        self.assertIn("localhost", server.LOOPBACK_HOSTS)
        self.assertNotIn("evil.com", server.LOOPBACK_HOSTS)


# ============================================================ 9. 自启动

class TestAutostart(unittest.TestCase):

    def test_launch_command_quoted(self):
        """路径带空格时必须加引号，否则开机启动静默失败。"""
        cmd = autostart.launch_command()
        self.assertTrue(cmd.startswith('"'))
        self.assertGreaterEqual(cmd.count('"'), 2)

    def test_launch_command_carries_silent_flag(self):
        """开机自启的命令行必须显式带 --silent（需求 1：不弹窗）。

        只靠程序默认值不够：默认值以后改了，开机这条路径就会悄悄退化成
        「弹窗」。写进注册表 = 显式意图，也方便在进程列表里认出来源。
        """
        cmd = autostart.launch_command()
        self.assertIn(autostart.SILENT_FLAG, cmd,
                      "开机自启命令行里必须有 %s" % autostart.SILENT_FLAG)

    def test_target_exists_in_source_run(self):
        self.assertTrue(os.path.exists(autostart.target_path()))

    def test_frozen_launch_command_is_exe_only(self):
        """frozen 下注册的应该是 exe 自身，不是 pythonw + 脚本。"""
        with TempDir() as exe_dir, TempDir() as mei:
            with FrozenEnv(exe_dir, mei):
                cmd = autostart.launch_command()
                self.assertIn("WirelessStatus.exe", cmd)
                self.assertNotIn("pythonw", cmd.lower())
                self.assertNotIn(".py", cmd)
                self.assertIn(autostart.SILENT_FLAG, cmd)

    def test_first_path_parsing(self):
        self.assertEqual(
            autostart._first_path('"C:\\a b\\x.exe" "C:\\c d\\y.py"'),
            "C:\\a b\\x.exe")
        self.assertEqual(autostart._first_path("C:\\plain\\x.exe -arg"),
                         "C:\\plain\\x.exe")
        self.assertEqual(autostart._first_path(""), "")

    def test_current_command_never_raises(self):
        """读注册表失败（无 pywin32 / 无该项）必须返回 None 而不是抛。"""
        val = autostart.current_command()
        self.assertTrue(val is None or isinstance(val, str))

    # ---- 重复自启项清理（开机弹窗的根因）

    def test_own_command_matching(self):
        """按**命令行内容**识别本程序 —— 这是清理重复项的唯一依据。"""
        self.assertTrue(autostart._looks_like_own_command(
            '"D:\\application\\python3.10.11\\pythonw.exe" '
            '"D:\\code\\dsh-workspace\\plugin\\wireless-status\\main.py"'))
        self.assertTrue(autostart._looks_like_own_command(
            '"C:\\Tools\\WirelessStatus.exe"'))
        self.assertFalse(autostart._looks_like_own_command(
            '"C:\\Other\\Notepad.exe"'))
        self.assertFalse(autostart._looks_like_own_command(""))
        # 别的 python 脚本不能被误判（只有带本程序路径的才算）
        self.assertFalse(autostart._looks_like_own_command(
            '"D:\\application\\python3.10.11\\pythonw.exe" '
            '"D:\\code\\other-tool\\autostart.py"'))

    def test_no_legacy_name_hardcoded(self):
        """不许再靠"历史值名黑名单"清理 —— 旧名字不该留在代码里。

        这条断言是给以后的自己看的：遇到重复自启项，改**判断逻辑**
        （按命令行内容识别），不要把某个旧名字写回常量里。
        """
        self.assertFalse(hasattr(autostart, "LEGACY_VALUE_NAMES"),
                         "不该再有 LEGACY_VALUE_NAMES 这种硬编码黑名单")

    def test_repo_has_no_stale_project_name(self):
        """整个仓库不该再出现老项目名（会被当成还在维护的名字）。

        老名字以**拆分拼接**的方式存进断言里，避免这行本身又变成一处"包含
        该字样"的文件 —— 那样 grep 出来永远有一条，反而碍事。
        """
        stale = "Mouse" + "Overlay"
        roots = [".", "web", "icon", "tools", "docs"]
        hits = []
        for root in roots:
            for base, _dirs, files in os.walk(os.path.join(HERE, root)):
                if any(skip in base for skip in ("build", "dist", "__pycache__",
                                                 ".git")):
                    continue
                for name in files:
                    if not name.lower().endswith(
                            (".py", ".js", ".css", ".html", ".md", ".json",
                             ".bat", ".spec", ".txt", ".svg")):
                        continue
                    path = os.path.join(base, name)
                    try:
                        with io.open(path, encoding="utf-8",
                                     errors="ignore") as fh:
                            if stale.lower() in fh.read().lower():
                                hits.append(os.path.relpath(path, HERE))
                    except OSError:
                        pass
        self.assertEqual(hits, [], "这些文件里还留着老项目名：%s" % hits)

    def test_prune_legacy_never_raises(self):
        """清理函数在没有该项/无权限时必须静默返回，不能影响启动。"""
        self.assertIsInstance(autostart.prune_legacy(), int)


# =============================================== 9b. 静默启动（需求 1）

class TestSilentStartup(unittest.TestCase):
    """开机自启后不再弹窗：参数解析 + 二次启动路径都要静默。"""

    def setUp(self):
        import main
        self.main = main

    def test_default_is_silent(self):
        show, silent = self.main._parse_args([])
        self.assertFalse(show, "默认启动不该显示窗口")
        self.assertFalse(silent, "默认启动没有 --silent 标记")

    def test_silent_flag_parsed(self):
        show, silent = self.main._parse_args(["--silent"])
        self.assertFalse(show)
        self.assertTrue(silent)

    def test_show_flag_parsed(self):
        show, _silent = self.main._parse_args(["--show"])
        self.assertTrue(show)

    def test_show_wins_over_silent(self):
        """两个都给时以 --show 为准（调试时要能看到窗口）。"""
        show, _silent = self.main._parse_args(["--silent", "--show"])
        self.assertTrue(show)

    def test_flag_case_and_slash_forms(self):
        for arg in ("--SILENT", "/silent", "-silent"):
            self.assertTrue(self.main._parse_args([arg])[1],
                            "%s 应被识别为静默开关" % arg)
        for arg in ("--SHOW", "/show", "-show"):
            self.assertTrue(self.main._parse_args([arg])[0],
                            "%s 应被识别为显示开关" % arg)

    def test_second_instance_has_no_messagebox(self):
        """二次启动路径里**不许**再有系统弹窗（需求 1）。

        老代码在唤窗失败时弹 MessageBox「已经在运行了」，开机时第二个实例
        必然走到那一支 —— 那就是用户看到的弹窗。这条断言把路彻底堵死。
        """
        import inspect
        src = inspect.getsource(self.main.main)
        self.assertNotIn("MessageBoxW", src,
                         "二次启动/主流程里不该再出现 MessageBoxW")

    def test_fatal_error_is_silent_when_autostarted(self):
        """开机路径下启动失败也必须静默（只落盘）—— 否则照样弹窗。

        开机时弹一个模态框会挡住后面所有自启项，用户还常常不在机器前。
        """
        import inspect
        src = inspect.getsource(self.main._report_fatal)
        self.assertIn("if silent:", src, "静默分支必须存在")
        # 弹窗调用必须在静默分支之后（即被 silent 守住）
        self.assertLess(src.index("if silent:"), src.index("MessageBoxW"),
                        "MessageBoxW 必须在静默早退之后")

    def test_wake_only_when_show_requested(self):
        """只有带了 --show 才允许唤窗；静默路径不能碰已有窗口。"""
        import inspect
        src = inspect.getsource(self.main.main)
        self.assertIn("if show_at_start:", src,
                      "唤窗必须被 show_at_start 守住")
        self.assertIn("wake_existing()", src)

    def test_wake_existing_tolerates_missing_event(self):
        """没有已有实例（事件不存在）时必须安静返回 False，不能抛。

        不能直接用真实的 ``WAKE_EVENT_NAME``：自测经常在**程序正在运行**时
        执行，那时事件是存在的，SetEvent 会成功 —— 断言就随环境飘。这里换成
        一个不可能存在的名字，测的才是"没有实例"这条分支。
        """
        old = self.main.WAKE_EVENT_NAME
        self.main.WAKE_EVENT_NAME = "WirelessStatus.NoSuchEvent.selftest"
        try:
            self.assertFalse(self.main.wake_existing(show=False))
        finally:
            self.main.WAKE_EVENT_NAME = old

    def test_wake_existing_signals_real_event(self):
        """事件存在时 SetEvent 必须成功 —— 第二个实例靠它唤起已有窗口。"""
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateEventW.restype = ctypes.c_void_p
        name = "WirelessStatus.SelftestEvent"
        ev = kernel32.CreateEventW(None, True, False, name)
        self.assertTrue(ev)
        old = self.main.WAKE_EVENT_NAME
        self.main.WAKE_EVENT_NAME = name
        try:
            self.assertTrue(self.main.wake_existing(show=False))
            kernel32.ResetEvent(ctypes.c_void_p(ev))
        finally:
            self.main.WAKE_EVENT_NAME = old
            kernel32.CloseHandle(ctypes.c_void_p(ev))

    def test_wake_event_name_declared(self):
        """跨进程唤窗靠命名事件，名字必须是常量（不能各处硬编码）。"""
        self.assertTrue(self.main.WAKE_EVENT_NAME)
        self.assertIn("WirelessStatus", self.main.WAKE_EVENT_NAME)


# ===================================== 9c. 悬停提示（需求 4：名称 + 型号）

class TestTipWindow(unittest.TestCase):

    def setUp(self):
        import tipwindow
        self.tip = tipwindow

    def _device(self, **kw):
        d = {
            "key": "B021_4080CFB3",
            "display_name": "罗技 M350 Wireless Mouse",
            "name": "M350 Wireless Mouse",
            "pid": "B021",
            "connection": {"label": "USB 接收器"},
            "status": "online",
            "status_text": "在线",
            "battery": {"percent": 90, "supported": True},
            "today_used": 7,
        }
        d.update(kw)
        return d

    def test_title_is_display_name(self):
        """第一行必须是「品牌 + 型号」（需求 4 的「设备名称和型号」）。"""
        title, _sub = self.tip.format_lines(self._device())
        self.assertEqual(title, "罗技 M350 Wireless Mouse")

    def test_title_has_model_not_pid(self):
        """型号读得到时，标题里不该出现 PID 这种技术串。"""
        title, sub = self.tip.format_lines(self._device())
        self.assertNotIn("B021", title)
        self.assertNotIn("PID", title)
        self.assertNotIn("PID", sub)

    def test_sub_line_carries_context(self):
        _title, sub = self.tip.format_lines(self._device())
        self.assertIn("USB 接收器", sub)
        self.assertIn("90%", sub)
        self.assertIn("今日用电", sub)

    def test_pid_fallback_when_name_missing(self):
        """型号读不到时才用 PID 补位 —— 否则用户判断不了这是哪只。"""
        dev = self._device(display_name="罗技 未知型号", name="")
        title, sub = self.tip.format_lines(dev)
        self.assertEqual(title, "罗技 未知型号")
        self.assertIn("B021", sub)

    def test_unknown_battery_does_not_fake_percent(self):
        """电量读不到就写「电量未知」，绝不显示 0%（那是在骗人）。"""
        dev = self._device(battery={"percent": None, "supported": False})
        _title, sub = self.tip.format_lines(dev)
        self.assertIn("电量未知", sub)
        self.assertNotIn("0%", sub)

    def test_offline_status_shown(self):
        dev = self._device(status="offline", status_text="离线")
        _title, sub = self.tip.format_lines(dev)
        self.assertIn("离线", sub)

    def test_none_device(self):
        title, sub = self.tip.format_lines(None)
        self.assertEqual(title, "未知设备")
        self.assertEqual(sub, "")

    def test_battery_ok_helper(self):
        self.assertTrue(self.tip.battery_ok({"percent": 50, "supported": True}))
        self.assertFalse(self.tip.battery_ok({"percent": 50, "supported": False}))
        self.assertFalse(self.tip.battery_ok({"percent": None, "supported": True}))
        self.assertFalse(self.tip.battery_ok(None))

    def test_window_not_created_before_start(self):
        """没 start 之前不能有 hwnd，hide/show 必须安静地什么都不做。"""
        win = self.tip.TipWindow()
        self.assertIsNone(win.hwnd)
        self.assertFalse(win.show("x", "y"))
        self.assertFalse(win.hide())
        self.assertFalse(win.visible)

    def test_scaling_constants_sane(self):
        """气泡尺寸是逻辑像素，档位关系要自洽（否则 150% 下会糊）。"""
        self.assertLess(self.tip.PAD_X, self.tip.MAX_WIDTH)
        self.assertGreater(self.tip.MIN_WIDTH, 0)
        self.assertLessEqual(self.tip.MIN_WIDTH, self.tip.MAX_WIDTH)
        self.assertGreaterEqual(self.tip.RADIUS, 0)


# ================================== 9d. 贴片样式（需求 2/3：扁平单行 + 环形图标）

class TestDeviceKinds(unittest.TestCase):
    """设备类型推断与图标映射 —— 面向"任意无线设备"，不写死鼠标。"""

    def test_all_kinds_have_icon_and_label(self):
        """每种类型都必须有图标名和中文名，否则界面会露 fallback。"""
        for kind in (devices.KIND_MOUSE, devices.KIND_KEYBOARD,
                     devices.KIND_HEADSET, devices.KIND_GAMEPAD,
                     devices.KIND_UNKNOWN):
            self.assertIn(kind, devices.KIND_ICON, "缺图标映射：%s" % kind)
            self.assertIn(kind, devices.KIND_LABEL, "缺中文名：%s" % kind)
            icon = devices.KIND_ICON[kind]
            path = os.path.join(HERE, "icon", icon + ".svg")
            self.assertTrue(os.path.exists(path),
                            "icon/%s.svg 不存在（类型 %s）" % (icon, kind))

    def test_unknown_is_not_mouse(self):
        """认不出来的设备不能长成鼠标 —— 那会误导用户。"""
        self.assertNotEqual(devices.KIND_ICON[devices.KIND_UNKNOWN],
                            devices.KIND_ICON[devices.KIND_MOUSE])

    def test_guess_kind_by_name(self):
        cases = [
            ("M350 Wireless Mouse", devices.KIND_MOUSE),
            ("MX Keys S Keyboard", devices.KIND_KEYBOARD),
            ("Zone Wired Headset", devices.KIND_HEADSET),
            ("F710 Gamepad", devices.KIND_GAMEPAD),
            ("罗技 无线键盘", devices.KIND_KEYBOARD),
            ("罗技 耳机", devices.KIND_HEADSET),
        ]
        for name, want in cases:
            self.assertEqual(devices.guess_kind(name), want,
                             "%r 应该判成 %s" % (name, want))

    def test_guess_kind_falls_back_to_unknown(self):
        """不认识的名字兜底 unknown，不是 mouse。"""
        self.assertEqual(devices.guess_kind("XYZ-1000"), devices.KIND_UNKNOWN)
        self.assertEqual(devices.guess_kind(""), devices.KIND_UNKNOWN)


class TestPillFlatStyle(unittest.TestCase):

    def _read(self, name):
        with io.open(os.path.join(HERE, "web", name), encoding="utf-8") as fh:
            return fh.read()

    def test_pill_radius_is_zero(self):
        """扁平样式必须直角：圆角会在四角露出与任务栏不一致的颜色。"""
        import main
        self.assertEqual(main.PILL_RADIUS, 0)

    def test_pill_is_single_line_height(self):
        """单行显示：窗口高度只够一行文字，不能是两行的高度。"""
        import main
        self.assertLessEqual(main.PILL_HEIGHT, 32,
                             "单行贴片不该高过 32 逻辑像素")
        self.assertGreaterEqual(main.PILL_HEIGHT, 20,
                                "太矮会被裁字")

    def test_css_has_no_capsule_background(self):
        """CSS 里不能再有胶囊底色/内描边（那是旧样式）。"""
        css = self._read("pill.css")
        self.assertNotIn("box-shadow: inset", css, "还有胶囊内描边")
        self.assertNotIn("border-radius: 7px", css)

    def test_js_uses_device_icon_not_ring(self):
        """贴片图标必须是**设备类型图标**（鼠标/键盘/耳机/手柄），不是电量环。

        需求：电量由数字表达，图标负责"这是什么设备"。多设备并列时尤其重要
        —— 一行四个格子全靠图标区分谁是谁。
        """
        js = self._read("pill.js")
        self.assertIn("deviceIcon", js)
        self.assertNotIn("ringIcon", js, "环形电量图标应该已经下线")
        self.assertNotIn("stroke-dasharray", js, "不再画进度弧")

    def test_device_icon_files_exist(self):
        """四类设备的图标文件都必须在 ``icon/`` 下（Lucide，ISC）。"""
        for name in ("mouse", "keyboard", "headset", "gamepad", "unknown"):
            path = os.path.join(HERE, "icon", name + ".svg")
            self.assertTrue(os.path.exists(path), "缺图标：icon/%s.svg" % name)
            with io.open(path, encoding="utf-8") as fh:
                svg = fh.read()
            self.assertIn("viewBox=\"0 0 24 24\"", svg,
                          "%s.svg 的 viewBox 不对" % name)
            self.assertIn("currentColor", svg,
                          "%s.svg 要跟随前景色（深浅主题自动适配）" % name)
            self.assertIn("ISC", svg, "%s.svg 要保留 Lucide 的 ISC 许可头" % name)

    def test_pill_colors_match_litemonitor(self):
        """数字配色对齐 LiteMonitor 的 settings.json（需求 3）。"""
        css = self._read("pill.css")
        for hexval in ("#008040", "#b57500", "#c03030"):
            self.assertIn(hexval, css.lower(),
                          "缺少 LiteMonitor 的色值 %s" % hexval)

    def test_js_reports_hover_to_backend(self):
        """悬停必须走后端（HTML tooltip 会被 SetWindowRgn 裁掉）。"""
        js = self._read("pill.js")
        self.assertIn("set_pill_hover", js)
        self.assertNotIn("chart-tip", js,
                         "不该再有本地 HTML tooltip（看不见）")

    def test_js_shows_all_devices_in_one_row(self):
        """多设备并列一行（需求 3）：不能只取第一台。"""
        js = self._read("pill.js")
        self.assertIn("MAX_PILLS", js)
        self.assertIn("slice(0, MAX_PILLS)", js)
        self.assertIn("pill-wrap", self._read("pill.html"))

    def test_html_has_no_tooltip_node(self):
        html = self._read("pill.html")
        self.assertNotIn("chart-tip", html)

    def test_backend_exposes_hover_api(self):
        """悬停靠**后端轮询光标**实现，不是网页事件。

        * ``_poll_pill_hover``：命令泵里量真实光标位置；
        * ``_pill_key_at``：把光标 x 换算成"哪台设备"；
        * ``set_pill_segments``：前端只上报布局。
        """
        import main
        app = main.WirelessStatusApp
        for name in ("_poll_pill_hover", "_pill_key_at", "_show_pill_tip",
                     "set_pill_segments", "_refresh_tip_if_stale"):
            self.assertTrue(callable(getattr(app, name, None)),
                            "App 缺方法：%s" % name)
        api = main.Api(None)
        self.assertTrue(callable(getattr(api, "set_pill_segments", None)))

    def test_js_does_not_rely_on_mouse_events(self):
        """贴片**不能**靠 mouseenter 触发悬停 —— 实测收不到。

        贴片窗是 ``Shell_TrayWnd`` 的子窗口 + ``WS_EX_NOACTIVATE``，这种状态
        下 WebView2 不把鼠标进出事件喂给页面（实测一次都不触发），所以那套
        实现"看起来对、实际永远不生效"。前端只上报布局，判断交给后端。
        """
        js = self._read("pill.js")
        self.assertNotIn('addEventListener("mouseenter"', js)
        self.assertNotIn('addEventListener("mouseleave"', js)
        self.assertIn("set_pill_segments", js)
        self.assertIn("reportSegments", js)

    def test_server_pill_payload_has_tip_fields(self):
        """气泡要用 display_name / pid / connection / battery.supported。"""
        import inspect
        src = inspect.getsource(server.Handler._pill)
        for field in ("display_name", "pid", "connection", "supported",
                      "status_text"):
            self.assertIn(field, src, "贴片接口缺字段：%s" % field)

    def test_pill_background_is_not_brightened(self):
        """扁平样式下底色必须**原样**用采样值，不能提亮。"""
        import inspect
        import main
        src = inspect.getsource(
            main.WirelessStatusApp._pill_background_color)
        self.assertNotIn("delta", src, "底色不该再提亮")


# ======================================= 9e. 图标（需求 3：环 + 闪电，无深色底）

class TestAppIcon(unittest.TestCase):

    def setUp(self):
        self.ico = os.path.join(HERE, "icon", "app.ico")
        self.png = os.path.join(HERE, "icon", "app.png")

    def test_icon_files_exist(self):
        self.assertTrue(os.path.exists(self.ico), "缺 icon/app.ico")
        self.assertTrue(os.path.exists(self.png), "缺 icon/app.png")

    def test_ico_has_required_sizes(self):
        """托盘要 16/20/24/32，资源管理器要大图 —— 缺档会拉大图缩，模糊。"""
        from PIL import Image
        with Image.open(self.ico) as im:
            sizes = set(im.ico.sizes())
        for need in ((16, 16), (20, 20), (24, 24), (32, 32), (48, 48),
                     (256, 256)):
            self.assertIn(need, sizes, "ico 缺 %dx%d" % need)

    def test_icon_corners_transparent(self):
        """需求 3：**不要**深色圆角方块 —— 四角必须是透明的。"""
        from PIL import Image
        with Image.open(self.png) as im:
            im = im.convert("RGBA")
            for xy in ((0, 0), (im.width - 1, 0),
                       (0, im.height - 1), (im.width - 1, im.height - 1)):
                alpha = im.getpixel(xy)[3]
                self.assertEqual(alpha, 0,
                                 "四角应透明（不要方块底），实测 %r" % (xy,))

    def test_icon_is_ring_not_filled_disc(self):
        """圆心要是**空的**（环），只在正中有闪电。

        采样圆心的左上方（远离闪电的一格）：那里若是实心色块，说明画成了
        圆盘而不是环。
        """
        from PIL import Image
        with Image.open(self.png) as im:
            im = im.convert("RGBA")
            w = im.width
            # 圆心正上方、环内 —— 环是 8.4/12 半径、线宽 3.4，所以
            # 0.30*w 处应在环内（透明）
            alpha = im.getpixel((w // 2, int(w * 0.30)))[3]
            self.assertEqual(alpha, 0, "环内应该是空的（透明）")

    def test_icon_uses_reference_colors(self):
        """配色对齐参考图：绿色 #14D448 与环灰 #7F8083。"""
        import tools.make_icon as maker
        self.assertEqual(maker.GREEN[:3], (19, 212, 72))
        self.assertEqual(maker.GREY[:3], (127, 128, 130))

    def test_maker_is_runnable(self):
        """生成脚本必须能独立跑（以后改配色不用求人重画）。"""
        import inspect
        import tools.make_icon as maker
        self.assertTrue(callable(maker.render))
        self.assertIn("--preview", inspect.getsource(maker.main))



# =================================================== 11. 电量换算（battery.py）

class TestBatteryCurve(unittest.TestCase):

    def setUp(self):
        import battery
        self.b = battery

    def test_curve_is_monotonic(self):
        """电压越高，换算出的百分比不能越低。"""
        prev = -1
        for mv in range(900, 1501, 25):
            pct = self.b.voltage_to_percent(mv, "alkaline")
            self.assertGreaterEqual(pct, prev, "在 %d mV 处掉头了" % mv)
            prev = pct

    def test_curve_clamps_both_ends(self):
        self.assertEqual(self.b.voltage_to_percent(5000, "alkaline"), 100)
        self.assertEqual(self.b.voltage_to_percent(100, "alkaline"), 0)

    def test_two_curves_differ(self):
        """镍氢的中段电压比碱性低，同一电压下换算结果应更高。"""
        al = self.b.voltage_to_percent(1200, "alkaline")
        ni = self.b.voltage_to_percent(1200, "nimh")
        self.assertGreater(ni, al)

    def test_unknown_curve_falls_back(self):
        self.assertEqual(self.b.voltage_to_percent(1300, "火星电池"),
                         self.b.voltage_to_percent(1300, "alkaline"))

    def test_voltage_reading_marks_estimated(self):
        """只支持电压的设备，结果必须标成「估算」而不是「设备上报」。

        payload 是 r[4:]：0x1001 的毫伏是大端 16 位，0x04E4 = 1252 mV。
        """
        r = self.b.interpret_reading(0x1001, [0x04, 0xE4], curve="alkaline")
        self.assertEqual(r["source"], self.b.SOURCE_ESTIMATED)
        self.assertEqual(r["source_label"], "估算")
        self.assertEqual(r["voltage_mv"], 1252)
        self.assertIsNotNone(r["percent"])

    def test_percent_reading_marks_reported(self):
        r = self.b.interpret_reading(0x1000, [0x5A, 0x32, 0x00])
        self.assertEqual(r["source"], self.b.SOURCE_REPORTED)
        self.assertEqual(r["percent"], 90)

    def test_median_filter_rejects_spike(self):
        """甩鼠标时电压会掉一下，中值滤波要把它吃掉。"""
        f = self.b.MedianFilter(window=5)
        for mv in (1300, 1300, 900, 1300, 1300):   # 中间那针是负载压降
            f.push(mv)
        self.assertEqual(f.current(), 1300)


# =================================================== 12. 1% 粒度

class TestPercentGranularity(unittest.TestCase):

    def test_no_rounding_to_5_or_10(self):
        """需求点名禁止 //5*5 / round(x/10)*10。"""
        import battery
        for raw in (89, 87, 83, 71, 49):
            self.assertEqual(battery.clamp_percent(raw), raw)

    def test_preserves_single_step(self):
        """90 → 89 必须能表达出来（否则就是按 5% 台阶了）。"""
        import battery
        self.assertNotEqual(battery.clamp_percent(90), battery.clamp_percent(89))
        self.assertEqual(battery.clamp_percent(90) - battery.clamp_percent(89), 1)

    def test_clamps_out_of_range(self):
        import battery
        self.assertEqual(battery.clamp_percent(-3), 0)
        self.assertEqual(battery.clamp_percent(140), 100)

    def test_snapshot_keeps_raw_value(self):
        """设备只报 5% 粒度时要保留原始值，不能自己造抖动。

        ``raw_percent`` 是 MouseSource 组快照时补上的字段（battery.py
        只负责算百分比），这里验证两件事：battery 不改动传入的整数，
        快照骨架里确实有 raw_percent 这一格给前端用。
        """
        import battery
        r = battery.make_reading(85, battery.SOURCE_REPORTED, granularity=5)
        self.assertEqual(r["percent"], 85)
        self.assertEqual(r["granularity"], 5)
        # 快照骨架（MouseSource._blank_state）里必须有 raw_percent
        import mouse_source
        src = mouse_source.MouseSource(interval=5.0)
        blank = src._blank_state()
        self.assertIn("raw_percent", blank["battery"])

    def test_granularity_detection(self):
        import battery
        t = battery.GranularityTracker()
        for pct in (90, 89, 88, 87):        # 出现非 5 倍数
            t.observe(pct)
        self.assertEqual(t.granularity(), 1)


# =================================================== 13. 电量历史（history.py）

class TestHistory(unittest.TestCase):

    def _store(self, tmp):
        import history
        return history.HistoryStore(os.path.join(tmp, "battery_history.json"))

    def test_aggregate_three_ranges(self):
        import history
        now = 1000000.0
        with TempDir() as tmp:
            store = self._store(tmp)
            for i in range(0, 200):
                store.add_sample("B021_x", 90 - i % 10,
                                 timestamp=int(now - i * 600))
            for rng in ("24h", "7d", "30d"):
                agg = store.aggregate("B021_x", rng, now=now)
                self.assertIn("points", agg)
                self.assertGreaterEqual(len(agg["points"]), 1)

    def test_battery_change_marks_and_resets(self):
        """向上跳 >20% 视为换电池：要记标记并重置基线。"""
        import history
        with TempDir() as tmp:
            store = self._store(tmp)
            store.add_sample("K", 20, timestamp=1000.0)
            action, info = store.add_sample("K", 95, timestamp=2000.0)
            self.assertEqual(action, "battery_change")
            self.assertEqual(info["from"], 20)
            self.assertEqual(info["to"], 95)
            dev = store.devices["K"]
            self.assertEqual(len(dev["battery_changes"]), 1)
            self.assertEqual(dev["baseline"]["percent"], 95)

    def test_dirty_spike_handled(self):
        """单次跳变 >15% 不能悄悄写进历史。"""
        import history
        with TempDir() as tmp:
            store = self._store(tmp)
            store.add_sample("K", 80, timestamp=1000.0)
            action, _ = store.add_sample("K", 40, timestamp=1010.0)
            self.assertIn(action, ("skipped_spike", "skipped", "added"))

    def test_thin_keeps_both_ends(self):
        import history
        samples = [[i, i % 50] for i in range(1000)]
        thin = history.thin_samples(samples, 100)
        self.assertLessEqual(len(thin), 100)
        self.assertEqual(thin[0][0], 0)
        self.assertEqual(thin[-1][0], 999)

    def test_readonly_fallback(self):
        """目录不可写时降级为内存，不抛异常。"""
        import history
        store = history.HistoryStore(os.path.join("Z:", "nowhere", "b.json"))
        store.add_sample("K", 50, timestamp=1.0)
        samples = store.devices.get("K", {}).get("samples", [])
        self.assertEqual(len(samples), 1)

    def test_device_key_stable_across_ports(self):
        """换 USB 口（index 变）不影响历史，key 由 pid + unit_id 决定。"""
        import history
        a = history.device_key(0xB021, "4080CFB3", 1)
        b = history.device_key(0xB021, "4080CFB3", 4)
        self.assertEqual(a, b)

    def test_history_key_accepts_hex_string(self):
        """devices.history_key 收的是快照里的十六进制字符串，要能转。"""
        import devices
        self.assertEqual(devices.history_key("B021", "4080CFB3", 1),
                         "B021_4080CFB3")

    def test_device_key_falls_back_to_index(self):
        import history
        self.assertEqual(history.device_key(0xB021, "", 3), "B021_i3")


# =================================================== 14. 使用时长（usage.py）

class TestUsage(unittest.TestCase):

    def test_format_duration(self):
        """需求第 4 条：「已运行」精确到秒，格式 HH:MM:SS / N天 HH:MM:SS。"""
        import usage
        self.assertEqual(usage.format_duration(0), "00:00:00")
        self.assertEqual(usage.format_duration(9), "00:00:09")
        self.assertEqual(usage.format_duration(59), "00:00:59")
        self.assertEqual(usage.format_duration(60), "00:01:00")
        self.assertEqual(usage.format_duration(3599), "00:59:59")
        self.assertEqual(usage.format_duration(3600), "01:00:00")
        self.assertEqual(usage.format_duration(3900), "01:05:00")
        # 超过 24 小时把天数提出来，小时位回到 0–23
        self.assertEqual(usage.format_duration(86400), "1天 00:00:00")
        self.assertEqual(usage.format_duration(86400 + 3900), "1天 01:05:00")
        # 负数/None 不能崩
        self.assertEqual(usage.format_duration(-5), "00:00:00")

    def test_midnight_split(self):
        """跨 00:00 的活跃段要切给两天。"""
        import usage
        with TempDir() as tmp:
            t = usage.UsageTracker(os.path.join(tmp, "usage_stats.json"))
            # 取一个确定的午夜，再从 23:59:50 起活跃 20 秒
            base = datetime.datetime(2026, 1, 1, 0, 0, 0)
            start = base.timestamp()
            t._commit_segment(start + 86390, start + 86410)
            days = t.days
            self.assertEqual(len(days), 2)
            self.assertEqual(days["2026-01-01"]["active_seconds"], 10)
            self.assertEqual(days["2026-01-02"]["active_seconds"], 10)

    def test_idle_break_at_5s(self):
        """连续 5 秒没动就要收段，不能一直累加。

        需求：手停下 5 秒后「已运行」必须停表。以前是 60 秒，用户看到的
        是「手停了数字还爬一分钟」，与直觉不符。
        """
        import usage
        self.assertEqual(usage.IDLE_TIMEOUT_SEC, 5)

    def test_sleep_not_counted(self):
        """休眠阈值必须比空闲阈值大，否则睡一觉会被当成持续使用。"""
        import usage
        self.assertGreater(usage.SLEEP_GAP_SEC, usage.IDLE_TIMEOUT_SEC)

    def test_last_active_removed(self):
        """需求第 4 条：删掉「最后操作时间」，后端不再下发。

        ``format_last_active`` 这个函数还留着（旧调用点兜底），但
        ``today()`` 的返回里必须再也看不到这两个键。
        """
        import usage
        with TempDir() as tmp:
            t = usage.UsageTracker(os.path.join(tmp, "usage_stats.json"))
            data = t.today()
            self.assertNotIn("last_active", data)
            self.assertNotIn("last_active_text", data)
            for key in ("day", "active_seconds", "text", "caliber"):
                self.assertIn(key, data)
            # text 已经是秒级格式
            self.assertRegex(data["text"], r"^(\d+天 )?\d{2}:\d{2}:\d{2}$")


# =================================================== 15. 设备列表（devices.py）

class TestDevices(unittest.TestCase):

    def _fake_snap(self, n, current_index=1):
        """造一份 MouseSource 形状的快照。

        注意 ``devices`` 里每项只有 index + current：HID++ 要一台台 ping+probe，
        轮询里只 probe 了 ``current`` 那台（性能取舍），所以完整字段在顶层
        ``name`` / ``pid`` / ``battery`` / ``dpi`` 上。
        """
        devs = [{"index": i + 1, "current": (i + 1) == current_index}
                for i in range(n)]
        return {
            "status": "online",
            "devices": devs,
            "receiver": {"pid": "C52F", "label": "Nano 接收器",
                         "kind": "usb_receiver"},
            "name": "M350 Wireless Mouse",
            "display_name": "M350 Wireless Mouse (PID B021)",
            "pid": "B021",
            "unit_id": "4080CFB3",
            "battery": {"supported": True, "percent": 90,
                        "source": "reported", "source_label": "设备上报",
                        "voltage_mv": None, "granularity": 1,
                        "status_text": "正在放电", "note": ""},
            "dpi": {"supported": True, "value": 1000, "min": 400, "max": 4000,
                    "step": 100, "inferred": False, "default": 1000},
            "idle": {"seconds": 125, "text": "2 分钟"},
            "error": "",
        }

    def _provider(self):
        import devices
        p = devices.HidppProvider.__new__(devices.HidppProvider)
        # 这些容器在 __init__ 里建，用 __new__ 绕过构造时要自己补
        p._last = {}            # key -> device dict（掉线时保留显示）
        p._granularity = {}
        p._medians = {}
        p.levels = {}           # 电量分档阈值（__init__ 里来自 config）
        return p

    def test_zero_devices(self):
        p = self._provider()
        self.assertEqual(p._translate(self._fake_snap(0)), [])

    def test_one_device(self):
        p = self._provider()
        out = p._translate(self._fake_snap(1))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "online")
        self.assertEqual(out[0]["battery"]["percent"], 90)
        self.assertIn("key", out[0])

    def test_three_devices_all_listed(self):
        """多设备要全列出来，不是让用户选一个。"""
        p = self._provider()
        out = p._translate(self._fake_snap(3))
        self.assertEqual(len(out), 3)
        self.assertEqual(len(set(d["key"] for d in out)), 3)

    def test_idle_not_leaked_to_device(self):
        """待机时间已下线（需求 5 删掉了它）。

        mouse_source 的快照里还留着 ``idle``（那是采集层的事实，别的地方
        可能用），但设备 dict 不该再透传 —— 否则前端会以为还有这一项。
        """
        p = self._provider()
        out = p._translate(self._fake_snap(1))
        self.assertNotIn("idle", out[0],
                         "设备结构里不该再有 idle（界面已删待机时间）")

    def test_offline_keeps_last_devices(self):
        """设备睡了也要把上一次的设备还回去并标离线，而不是清空列表。"""
        p = self._provider()
        p._last = {d["key"]: d for d in p._translate(self._fake_snap(1))}
        out = p._translate({"devices": [], "error": "no_device"})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "offline")

    def test_non_mouse_kind_does_not_crash(self):
        """耳机/手柄也要能渲染，不能因为不是鼠标就炸。

        注意型号名在快照**顶层**（_build 读 raw["name"]），不是 per-item。
        """
        import devices
        snap = self._fake_snap(1)
        snap["name"] = "G733 Gaming Headset"
        snap["display_name"] = "G733 Gaming Headset (PID 0AB5)"
        out = self._provider()._translate(snap)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], devices.KIND_HEADSET)
        self.assertEqual(out[0]["icon"], devices.KIND_ICON[devices.KIND_HEADSET])

    def test_guess_kind(self):
        import devices
        self.assertEqual(devices.guess_kind("M350 Wireless Mouse"),
                         devices.KIND_MOUSE)
        self.assertEqual(devices.guess_kind("G733 Headset"),
                         devices.KIND_HEADSET)
        self.assertEqual(devices.guess_kind("F710 Gamepad"),
                         devices.KIND_GAMEPAD)


# =================================================== 16. 贴片 / 毛玻璃降级

def winui_main_scale():
    """取当前 DPI 缩放；拿不到按 1.0 算（测试不该因为环境失败）。"""
    try:
        import main
        return main.dpi_scale() or 1.0
    except Exception:
        return 1.0


class TestWinui(unittest.TestCase):

    def test_pill_rect_inside_screen(self):
        import winui
        pos = winui.pill_rect(200, 28, margin=4)
        if pos is None:
            self.skipTest("拿不到工作区（非 Windows）")
        x, y = pos
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)

    def test_pill_rect_sits_left_of_notify_area(self):
        """贴片必须贴在任务栏里、通知区域（托盘图标）的左边。

        需求原话「贴近显示隐藏的图标一侧」。旧实现贴在工作区右下角，
        那是在任务栏**上沿以上**，看起来像一块多余的浮窗。
        """
        import winui
        note = winui.notify_area_rect()
        bar = winui.taskbar_rect()
        pos = winui.pill_rect(200, 28, margin=2)
        if not (note and bar and pos):
            self.skipTest("拿不到任务栏/通知区域（非 Windows 或无任务栏）")
        x, y = pos
        self.assertLess(x, note[0] / (winui_main_scale()), 
                        "贴片应该在通知区域左边")
        self.assertGreaterEqual(y, bar[1] / winui_main_scale() - 1,
                                "贴片应该在任务栏内（不能浮在任务栏上方）")

    def test_notify_area_rect_inside_taskbar(self):
        """通知区域必须是任务栏的子矩形（拿错窗口会让贴片跑飞）。"""
        import winui
        note = winui.notify_area_rect()
        bar = winui.taskbar_rect()
        if not (note and bar):
            self.skipTest("拿不到任务栏（非 Windows 或无任务栏）")
        self.assertGreaterEqual(note[0], bar[0])
        self.assertLessEqual(note[2], bar[2])
        self.assertGreaterEqual(note[1], bar[1])

    def test_apply_blur_never_raises(self):
        """毛玻璃失败必须降级返回，不能把启动流程炸掉。"""
        import winui
        res = winui.apply_blur(0)          # 0 = 无效句柄，必然失败
        self.assertIn("applied", res)
        self.assertFalse(res["applied"])

    def test_window_handle_of_none(self):
        import winui
        self.assertIsNone(winui.window_handle(None))

    def test_win32_visibility_helpers_tolerate_bad_handle(self):
        """show/hide/move_resize/set_topmost 遇到无效句柄必须返回 False，不能抛。

        这几个函数是「跨线程不碰 pywebview」的替代路径，一旦抛异常就会让
        托盘左键的整条链路断掉（表现为窗口只开不关）。
        """
        import winui
        self.assertFalse(winui.show_window(0))
        self.assertFalse(winui.hide_window(0))
        self.assertFalse(winui.move_resize(0, 0, 0, 100, 100))
        self.assertFalse(winui.set_topmost(0))
        self.assertFalse(winui.show_window(None))

    def test_win32_helpers_do_not_touch_pywebview(self):
        """静态检查：这几个函数体内不许出现 pywebview 的窗口方法调用。

        跨线程调 window.show()/hide()/move()/resize() 会永久阻塞（实测
        托盘左键第二次点击无响应），所以这里用源码文本做一道防回归 check。
        """
        import inspect
        import winui
        for fn in (winui.show_window, winui.hide_window,
                   winui.move_resize, winui.set_topmost):
            src = inspect.getsource(fn)
            body = src.split('"""')[-1] if '"""' in src else src
            for bad in (".show()", ".hide()", ".move(", ".resize("):
                self.assertNotIn(
                    "window" + bad, body,
                    "%s 里出现了 window%s，跨线程会阻塞" % (fn.__name__, bad))

    # ---- 需求第 1 条：主窗定位到托盘图标正上方

    def test_icon_point_from_lparam(self):
        """NOTIFYICON_VERSION_4 的 lParam：低 16 位 x、高 16 位 y。

        这是位置算法唯一的坐标来源，解错方向窗口就会跑到屏幕另一边。
        """
        import winui
        # x=1500, y=1050
        lp = (1050 << 16) | 1500
        self.assertEqual(winui.icon_point_from_lparam(lp), (1500, 1050))
        # 最边界：x=0、y=0 视为「没有坐标」（部分系统不填）
        self.assertIsNone(winui.icon_point_from_lparam(0))
        # x=0 但 y>0 仍是有效坐标
        self.assertEqual(winui.icon_point_from_lparam(800 << 16), (0, 800))

    def test_main_rect_near_icon_none(self):
        """没有锚点时必须返回 None，让调用方降级，而不是算出个野坐标。"""
        import winui
        self.assertIsNone(winui.main_rect_near_icon(None, 392, 400))

    def test_main_rect_near_icon_logical_and_inside(self):
        """锚点在屏幕中下部时：窗口水平居中于锚点、底边在锚点上方。"""
        import winui
        mon = winui.monitor_rect_for_point(0, 0)
        if not mon:
            self.skipTest("拿不到显示器矩形（非 Windows）")
        work = winui.monitor_work_area_for_point(0, 0)
        if not work:
            self.skipTest("拿不到显示器工作区")
        # 取屏幕水平中央、任务栏正上方一点作为图标锚点（物理像素）
        ax = (mon[0] + mon[2]) // 2
        ay = work[3] - 20
        res = winui.main_rect_near_icon((ax, ay), 392, 400, gap=8)
        self.assertIsNotNone(res)
        w, h, x, y = res
        self.assertEqual((w, h), (392, 400))
        scale = winui_main_scale()
        # 换算回物理像素做比较，避免 dpi_scale 舍入误差
        cx_phys = (x + 392 / 2.0) * scale
        self.assertAlmostEqual(cx_phys, ax, delta=scale * 2,
                               msg="窗口应该水平居中于托盘图标")
        # 窗口完全落在工作区内
        self.assertGreaterEqual(x * scale, work[0] - scale)
        self.assertLessEqual((x + 392) * scale, work[2] + scale)
        self.assertGreaterEqual(y * scale, work[1] - scale)
        self.assertLessEqual((y + 400) * scale, work[3] + scale)

    def test_main_rect_near_icon_clamps_left_edge(self):
        """图标贴屏幕左边缘时，窗口不能被推到屏幕外。"""
        import winui
        work = winui.monitor_work_area_for_point(0, 0)
        mon = winui.monitor_rect_for_point(0, 0)
        if not (work and mon):
            self.skipTest("拿不到显示器信息")
        res = winui.main_rect_near_icon((work[0] + 4, work[3] - 20), 392, 400)
        self.assertIsNotNone(res)
        _w, _h, x, _y = res
        self.assertGreaterEqual(x, work[0] / winui_main_scale() - 2)


# =================================================== 16b. 贴片嵌入任务栏

class TestPillHost(unittest.TestCase):

    def test_pill_rect_in_taskbar_fills_height(self):
        """嵌入态的贴片必须居中落在任务栏内，且比任务栏略矮。

        原先要求 ``h == bar_h``（窗口占满整条任务栏，靠 SetWindowRgn 裁成
        胶囊），实测那会在胶囊外留一圈窗口底色的白边 —— 压在浅灰任务栏上
        就是「贴上去的白补丁」。现在窗口本身就是胶囊，高度小于任务栏高度、
        垂直居中。
        """
        import winui
        res = winui.pill_rect_in_taskbar(120, 28)
        bar = winui.taskbar_rect()
        if not (res and bar):
            self.skipTest("拿不到任务栏（非 Windows 或无任务栏）")
        x, y, w, h, bar_h = res
        self.assertLessEqual(h, bar_h, "贴片不能高过任务栏")
        self.assertGreater(h, bar_h * 0.5, "贴片不能比任务栏矮太多（那就成一条线了）")
        # 垂直居中：上下留白差不超过 2 物理像素
        self.assertLessEqual(abs((y - bar[1]) - (bar[3] - (y + h))), 2,
                             "贴片应在任务栏内垂直居中")
        self.assertLess(x, bar[2], "贴片必须落在任务栏内")
        self.assertGreater(w, 0)

    def test_taskbar_metrics_shape(self):
        """前端靠这些字段分档/换色，键名不能改。"""
        import winui
        m = winui.taskbar_metrics()
        for key in ("height", "scale", "logical_height", "dark"):
            self.assertIn(key, m)
        self.assertIsInstance(m["dark"], bool)
        self.assertGreater(m["height"], 0)
        self.assertGreater(m["logical_height"], 0)

    def test_dpi_scale_at_never_raises(self):
        import winui
        scale = winui.dpi_scale_at(0, 0)
        self.assertGreater(scale, 0.5)
        self.assertLess(scale, 4.0)

    def test_embed_helpers_tolerate_bad_handle(self):
        """嵌入/脱离遇到无效句柄返回 False，不能把开关的点击炸掉。"""
        import winui
        self.assertFalse(winui.embed_in_taskbar(0, 0, 0, 100, 40))
        self.assertFalse(winui.detach_from_taskbar(0, 0, 0, 100, 40))
        self.assertFalse(winui.start_window_drag(0))
        self.assertIsNone(winui.window_rect(0))


# =================================================== 16c. 握手协议（tray.py）

class TestTrayV4(unittest.TestCase):

    def test_notifyicon_version4_constants(self):
        """v4 协议要求 NIM_SETVERSION + NOTIFYICON_VERSION_4，否则拿不到图标坐标。"""
        import tray
        self.assertEqual(tray.NIM_SETVERSION, 4)
        self.assertEqual(tray.NOTIFYICON_VERSION_4, 4)
        self.assertEqual(tray.NIN_SELECT, 0x0400)
        self.assertEqual(tray.NIN_KEYSELECT, 0x0401)

    def test_parse_point(self):
        """``_parse_point`` 是 _wnd_proc 解析锚点的唯一入口。"""
        import tray
        lp = (1000 << 16) | 2000
        self.assertEqual(tray.TrayIcon._parse_point(lp), (2000, 1000))
        self.assertIsNone(tray.TrayIcon._parse_point(0))


# =================================================== 17. 图标兜底

class TestIconFallback(unittest.TestCase):

    def test_icon_dir_exists(self):
        self.assertTrue(os.path.isdir(apppath.icon_dir()), "icon/ 目录不见了")

    def test_required_icons_present(self):
        need = ["app.ico", "app.png", "mouse.svg", "headset.svg",
                "gamepad.svg", "refresh.svg", "exit.svg", "back.svg"]
        missing = [n for n in need
                   if not os.path.isfile(os.path.join(apppath.icon_dir(), n))]
        self.assertEqual(missing, [], "缺图标：%s" % missing)

    def test_missing_icon_lookup_is_safe(self):
        """即便图标被删，拼路径也不能抛异常（前端有内联 SVG 兜底）。"""
        p = os.path.join(apppath.icon_dir(), "no-such-icon.svg")
        self.assertFalse(os.path.isfile(p))

    def test_resource_and_app_dir_do_not_mix(self):
        """静态资源走 _MEIPASS，配置走 exe 目录，两者不能串。"""
        with TempDir() as exe_dir, TempDir() as meipass:
            with FrozenEnv(exe_dir, meipass):
                res = os.path.normcase(os.path.abspath(apppath.resource_dir()))
                cfg = os.path.normcase(os.path.abspath(apppath.app_dir()))
                self.assertEqual(res, os.path.normcase(os.path.abspath(meipass)))
                self.assertEqual(cfg, os.path.normcase(os.path.abspath(exe_dir)))


# =================================================== 17b. 前后端字段对齐

class TestFieldAlignment(unittest.TestCase):
    """静态检查：后端有的字段，前端必须真的渲染。

    起因是一个真 bug —— 待机时间在 mouse_source 快照里有、devices 层
    组装时被漏掉，界面上就少一项（验收要求五项）。这种「后端有、前端没有」
    的断层不会报错，只会静默少显示一项，所以用静态检查钉住。
    """

    def _read(self, *parts):
        with io.open(os.path.join(apppath.web_dir(), *parts),
                     encoding="utf-8") as fh:
            return fh.read()

    def test_five_key_fields_have_dom_hooks(self):
        """验收五项：型号 / 连接方式 / 连接状态 / 电量 / DPI。"""
        html = self._read("index.html")
        for hook in ("detail-name", "detail-link", "detail-dot",
                     "detail-status", "battery-value", "dpi-value"):
            self.assertIn('id="%s"' % hook, html, "缺 DOM 钩子：%s" % hook)

    def test_pid_is_not_shown_in_ui(self):
        """型号展示不带 PID（需求 4）。

        PID 仍然在设备 dict 里（排查用），但界面上不能再出现 ——
        模块级 HTML 与渲染代码都必须干净。
        """
        html = self._read("index.html")
        js = self._read("app.js")
        self.assertNotIn('id="detail-pid"', html, "HTML 里还有 PID 元素")
        self.assertNotIn("detail-pid", js, "app.js 还在写 PID")
        self.assertNotIn('"PID "', js, "app.js 还在拼 PID 字符串")

    def test_idle_field_is_gone(self):
        """待机时间已下线，DOM 与渲染函数都不该留。"""
        html = self._read("index.html")
        js = self._read("app.js")
        self.assertNotIn('id="idle-value"', html)
        self.assertNotIn("renderIdle", js)

    def test_last_active_gone_everywhere(self):
        """需求第 4 条：全链路不再有「最后操作时间」。"""
        html = self._read("index.html")
        js = self._read("app.js")
        self.assertNotIn("usage-last", html, "HTML 里还有最后操作节点")
        self.assertNotIn("usage-last", js, "app.js 还在写最后操作")
        self.assertNotIn("last_active", js, "app.js 还在读 last_active")
        self.assertNotIn("最后操作", html)
        self.assertNotIn("最后操作", js)

    def test_usage_is_second_precision(self):
        """「已运行」必须是 HH:MM:SS 秒级格式，且有每秒刷新。"""
        js = self._read("app.js")
        self.assertIn("formatClock", js)
        self.assertIn("setInterval(paintUsage, 1000)", js)
        # 前端算出来的格式要和 Python 一致：1天 HH:MM:SS
        self.assertIn('天 ', js)

    def test_drag_api_wired(self):
        """需求第 5 条：界面可拖，且按钮不误触发。"""
        js = self._read("app.js")
        css = self._read("style.css")
        self.assertIn("initDrag", js)
        self.assertIn("start_drag", js)
        self.assertIn("finish_drag", js)
        self.assertIn("DRAG_BLOCK_SELECTOR", js)
        # 交互元素要排除在拖动之外
        for token in ("button", "input", ".switch", ".device-row", ".slider"):
            self.assertIn(token, js, "拖动排除列表里应该有 %s" % token)
        self.assertIn("body.dragging", css)

    def test_pill_taskbar_metrics_wired(self):
        """贴片要按任务栏高度分档、跟随深浅色。"""
        js = self._read("pill.js")
        css = self._read("pill.css")
        self.assertIn("applyTaskbarMetrics", js)
        self.assertIn("taskbar_metrics", js)
        for cls in ("bar-sm", "bar-lg", "bar-xl", "bar-dark"):
            self.assertIn(cls, js)
            self.assertIn(cls, css)

    def test_backend_drag_and_metrics_api(self):
        """Api 层必须真的暴露这三个方法（前端调用点在上面已静态检查）。"""
        import main
        api = main.Api(None)
        for name in ("start_drag", "finish_drag", "taskbar_metrics"):
            self.assertTrue(callable(getattr(api, name, None)),
                            "Api 缺方法：%s" % name)
        # App 层的贴片宿主切换函数
        self.assertTrue(callable(getattr(main.WirelessStatusApp,
                                          "_apply_pill_host", None)))
        self.assertTrue(callable(getattr(main.WirelessStatusApp,
                                          "_destroy_pill_window", None)))

    def test_drag_uses_postmessage_not_sendmessage(self):
        """``WM_NCLBUTTONDOWN`` 必须 Post 不能 Send。

        SendMessage 会一直阻塞到系统那次模态移动循环结束，js_api 的这次
        调用也就一直不返回 —— 前端看到的是「按下去没反应」，而且 UI 线程
        被占着，整个界面僵住。这条断言防的就是有人图省事改回 SendMessage。
        """
        import inspect
        import winui
        src = inspect.getsource(winui.start_window_drag)
        self.assertIn("PostMessageW", src)
        self.assertNotIn("SendMessageW", src)


# =================================================== 18. 配置 v2

class TestConfigV2(unittest.TestCase):

    def test_new_keys_have_defaults(self):
        cfg = apppath.DEFAULT_CONFIG
        for key in ("version", "poll_interval_sec", "battery_pill_enabled",
                    "battery_curve", "battery_levels", "last_selected_device",
                    "theme"):
            self.assertIn(key, cfg, "缺配置项 %s" % key)

    def test_battery_levels_shape(self):
        lv = apppath.DEFAULT_CONFIG["battery_levels"]
        self.assertEqual(sorted(lv), ["critical", "good", "low"])
        self.assertLess(lv["critical"], lv["low"])
        self.assertLess(lv["low"], lv["good"])

    def test_unknown_key_preserved_on_rewrite(self):
        """用户手改的未知键不能被我们写配置时抹掉。"""
        with TempDir() as tmp, FrozenEnv(tmp, tmp):
            with io.open(apppath.config_path(), "w", encoding="utf-8") as fh:
                json.dump({"my_own_key": 42, "poll_interval_sec": 7}, fh)
            store = apppath.Store()
            store.set_config("poll_interval_sec", 9)
            with io.open(apppath.config_path(), encoding="utf-8") as fh:
                fresh = json.load(fh)
            self.assertEqual(fresh.get("my_own_key"), 42)
            self.assertEqual(fresh.get("poll_interval_sec"), 9)

    def test_set_config_rejects_unknown(self):
        """Api.set_config 只认白名单，防止前端写坏配置。"""
        import main as app_main

        class FakeApp(object):
            def set_config(self, key, value):
                self.called = (key, value)
                return {"ok": True}

        res = app_main.Api(FakeApp()).set_config("evil_key", 1)
        self.assertFalse(res["ok"])

    def test_reads_config_with_bom(self):
        """带 BOM 的 UTF-8 配置必须能读出来。

        Windows 记事本和 PowerShell 5.1 的 `Set-Content -Encoding UTF8`
        都会加 BOM。早期实现用 encoding="utf-8" 读，抛
        `JSONDecodeError: Unexpected UTF-8 BOM`，再被当成「没有配置文件」
        把默认值写回去 —— 用户看到的就是「设置被重置」。
        """
        with TempDir() as tmp, FrozenEnv(tmp, tmp):
            path = apppath.config_path()
            body = json.dumps({"poll_interval_sec": 8}).encode("utf-8")
            with io.open(path, "wb") as fh:
                fh.write(b"\xef\xbb\xbf" + body)
            store = apppath.Store()
            self.assertEqual(store.config.get("poll_interval_sec"), 8)

    def test_unreadable_config_is_not_overwritten(self):
        """文件存在但读不懂时，不许用默认值覆盖用户文件。"""
        with TempDir() as tmp, FrozenEnv(tmp, tmp):
            path = apppath.config_path()
            broken = '{"poll_interval_sec": 8, 这里坏了'
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(broken)
            store = apppath.Store()          # 不能抛
            self.assertEqual(
                store.config.get("poll_interval_sec"),
                apppath.DEFAULT_CONFIG["poll_interval_sec"])
            with io.open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), broken, "用户文件被覆盖了")


def main():
    verbosity = 2 if "-v" in sys.argv else 1
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
