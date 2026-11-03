# -*- coding: utf-8 -*-
"""本地只读服务 —— 给 pywebview 提供页面，以及一个可离线验证的 HTTP 数据口

为什么不用 file:// 直接加载 index.html：

  * WebView2 在 file:// 下的 `fetch()` 会被 CORS 拦掉，前端想拿数据只能全靠
    js_api，调试时（浏览器里开页面）什么都看不到；
  * 起了本地 HTTP 后，selftest / curl 也能直接验证「设备数据是不是对」，
    不用去点窗口。

做法与 WorkBuddy 用量悬浮窗的 server.py 一致：ThreadingHTTPServer + 静态
白名单 + 只认 loopback Host。区别是这里**不写盘、不做任何设备控制**：
写 DPI、改配置一律走 pywebview 的 js_api，HTTP 面只读，避免任何网页
（包括误开的其他站点）能通过 127.0.0.1 改你的鼠标设置。

端口用 0（系统分配），把真实地址回填给 webview —— 固定端口在别人机器上
很容易被占，占不到就整个程序起不来。

路由（v2 新增的都在这里，括号里是用途）：

    /                       主窗口（列表页 + 功能区）
    /pill                   任务栏贴片
    /web/<file>             静态资源（.html/.css/.js/.svg/.png/.ico）
    /icon/<file>            图标资源（跟 web 分开，源码时是 icon/ 目录）
    /api/ping               健康检查
    /api/snapshot           完整快照（列表 + 功能区都用它）
    /api/devices            只有设备列表（贴片、外部脚本用）
    /api/device/<key>       单个设备的详情（key 是 pid_unitid）
    /api/history?range=     电量历史聚合（24h / 7d / 30d）
    /api/usage              今日使用时长
    /api/pill               贴片专用的轻量数据（在线设备 + 今日用电）
"""

import json
import os
import posixpath
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote, parse_qs

import apppath

# 只发这几种静态资源。不放行 .py / .json / .log —— 尤其是 config.json
# 与日志，它们是本地文件，不该出现在 HTTP 面上。
STATIC_EXT = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}

#: 历史接口允许的档位，防止 ?range=../../ 之类乱传
HISTORY_RANGES = ("24h", "7d", "30d")


class Handler(BaseHTTPRequestHandler):
    server_version = "WirelessStatus/0.3"

    # 挂进来的钩子，由 LocalServer.start() 设置。
    # 用类属性是因为 BaseHTTPRequestHandler 每个请求都会新建实例。
    provider = None      # devices.HidppProvider
    history = None       # history.HistoryStore
    usage = None         # usage.UsageTracker

    def log_message(self, fmt, *args):
        """默认实现往 stderr 打日志，pythonw 下没有 stderr，会抛异常。"""
        return

    # ---- 路由
    def do_GET(self):
        if not self._host_ok():
            self._text(403, "forbidden")
            return
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query or "")

        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if path in ("/pill", "/pill.html"):
            self._serve_static("pill.html")
            return
        if path.startswith("/web/"):
            self._serve_static(unquote(path[len("/web/"):]))
            return
        if path.startswith("/icon/"):
            self._serve_icon(unquote(path[len("/icon/"):]))
            return

        # ---- API
        if path == "/api/ping":
            self._json({"ok": True, "version": apppath.APP_VERSION})
            return
        if path == "/api/snapshot":
            self._json(self._snapshot())
            return
        if path == "/api/devices":
            self._json(self._devices())
            return
        if path.startswith("/api/device/"):
            key = unquote(path[len("/api/device/"):])
            self._json(self._device(key))
            return
        if path == "/api/history":
            rng = (query.get("range") or ["24h"])[0]
            key = (query.get("key") or [""])[0]
            self._json(self._history(rng, key))
            return
        if path == "/api/usage":
            self._json(self._usage())
            return
        if path == "/api/pill":
            self._json(self._pill())
            return

        self._text(404, "not found")

    def do_POST(self):
        # 明确拒绝所有写操作：DPI、配置只能从窗口里经 js_api 改，
        # 不能让任意网页对着 127.0.0.1 的端口改鼠标。
        self._text(405, "read only")

    # ---- 工具
    def _host_ok(self):
        """只认 loopback 的 Host，挡住 DNS rebinding。"""
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return True
        if host.startswith("["):
            name = host.split("]")[0] + "]"
        else:
            name = host.rsplit(":", 1)[0]
        return name.lower() in LOOPBACK_HOSTS

    # ---- API 实现
    def _snapshot(self):
        """完整快照：设备列表 + 采集状态 + 使用时长 + 配置摘要。

        前端主通路就是这个（pywebview 里走 api.snapshot()，浏览器调试走
        fetch('/api/snapshot')，两条路返回的结构完全一样）。
        """
        provider = self.provider
        if provider is None:
            return {"ok": False, "error": "采集线程未启动", "devices": []}
        try:
            snap = provider.snapshot()
        except Exception as exc:
            return {"ok": False, "devices": [],
                    "error": "%s: %s" % (type(exc).__name__, exc)}

        snap["devices"] = self._decorate(snap.get("devices") or [])
        today = self._usage_today()
        snap["usage"] = today
        # 贴片开关等界面状态由 main.py 填，这里给个空壳避免前端 undefined
        snap.setdefault("config", {})
        return snap

    def _devices(self):
        """只要设备列表 —— 贴片、外部脚本用得上的轻量口。"""
        provider = self.provider
        if provider is None:
            return {"ok": False, "error": "采集线程未启动", "devices": []}
        try:
            snap = provider.snapshot()
        except Exception as exc:
            return {"ok": False, "devices": [],
                    "error": "%s: %s" % (type(exc).__name__, exc)}
        return {
            "ok": snap.get("ok", False),
            "error": snap.get("error") or "",
            "hint": snap.get("hint") or "",
            "receiver": snap.get("receiver") or {},
            "updated": snap.get("updated") or 0,
            "devices": self._decorate(snap.get("devices") or []),
        }

    def _decorate(self, devices):
        """给每个设备补上「今日用电量」，列表页与贴片都要用这个值。"""
        out = []
        for d in devices:
            item = dict(d)
            item["today_used"] = self._today_used(d.get("key"))
            out.append(item)
        return out

    def _today_used(self, key):
        store = self.history
        if store is None or not key:
            return None
        try:
            agg = store.aggregate(key, "24h")
        except Exception:
            return None
        return agg.get("today_used")

    def _device(self, key):
        provider = self.provider
        if provider is None:
            return {"ok": False, "error": "采集线程未启动"}
        try:
            devices = provider.snapshot().get("devices") or []
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        for d in devices:
            if d.get("key") == key:
                item = dict(d)
                item["today_used"] = self._today_used(key)
                return {"ok": True, "device": item}
        return {"ok": False, "error": "找不到设备 %s" % key}

    def _history(self, range_name, key=""):
        store = self.history
        if store is None:
            return {"ok": False, "error": "历史库未启动",
                    "range": range_name, "points": []}
        if range_name not in HISTORY_RANGES:
            range_name = "24h"
        if not key:
            # 没指定设备就取第一台（单设备场景下就是它）
            key = (store.devices and sorted(store.devices.keys())[0]) or ""
        if not key:
            return {"ok": True, "range": range_name, "points": [],
                    "today_used": 0, "daily_avg": 0, "key": ""}
        try:
            agg = store.aggregate(key, range_name)
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                    "range": range_name, "points": []}
        agg["ok"] = True
        agg["key"] = key
        agg["readonly"] = bool(getattr(store, "readonly", False))
        return agg

    def _usage(self):
        return self._usage_today()

    def _usage_today(self):
        tracker = self.usage
        if tracker is None:
            return {"ok": False, "error": "使用时长统计未启动"}
        try:
            today = tracker.today()
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        today["ok"] = True
        today["readonly"] = bool(getattr(tracker, "readonly", False))
        return today

    def _pill(self):
        """贴片专用：只给在线设备的三四个字段，不拉整份快照。"""
        provider = self.provider
        if provider is None:
            return {"ok": False, "devices": []}
        try:
            devices = provider.snapshot().get("devices") or []
        except Exception:
            return {"ok": False, "devices": []}

        out = []
        for d in devices:
            if d.get("status") != "online":
                continue
            bat = d.get("battery") or {}
            out.append({
                "key": d.get("key"),
                "icon": d.get("icon") or d.get("kind") or "unknown",
                "kind": d.get("kind"),
                "name": d.get("name") or "",
                "display_name": d.get("display_name") or "",
                "status": d.get("status"),
                "level": d.get("level") or "unknown",
                "battery": {"percent": bat.get("percent")},
                "today_used": self._today_used(d.get("key")),
            })
        return {"ok": True, "devices": out, "total": len(devices)}

    # ---- 静态资源
    def _serve_static(self, rel):
        """把相对路径拼到 web/ 下，并挡住 ../ 穿越。"""
        self._serve_from(apppath.web_dir(), rel)

    def _serve_icon(self, rel):
        """图标单独走 apppath.icon_dir()（源码是 icon/，exe 里是 _MEIPASS/icon）。"""
        self._serve_from(apppath.icon_dir(), rel)

    def _serve_from(self, root, rel):
        rel = (rel or "").replace("\\", "/").lstrip("/")
        # 归一化后再拼：否则 "web/../../../config.json" 能读到配置
        rel = posixpath.normpath(rel)
        if rel in (".", "") or rel.startswith(".."):
            self._text(404, "not found")
            return
        ext = os.path.splitext(rel)[1].lower()
        ctype = STATIC_EXT.get(ext)
        if not ctype:
            self._text(404, "not found")
            return
        root = os.path.abspath(root)
        full = os.path.abspath(os.path.join(root, *rel.split("/")))
        if not full.startswith(root + os.sep):
            self._text(404, "not found")
            return
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            self._text(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 本地静态资源，禁掉缓存：改完 app.js 刷新就能看到，不用清缓存
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code, text):
        body = (text or "").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LocalServer(object):
    """把服务线程包一层。``url`` 在 start() 之后才有值。"""

    def __init__(self, provider=None, history=None, usage=None,
                 host="127.0.0.1", port=0):
        self.provider = provider
        self.history = history
        self.usage = usage
        self.host = host
        self.port = port
        self.httpd = None
        self.thread = None
        self.url = ""

    def start(self):
        Handler.provider = self.provider
        Handler.history = self.history
        Handler.usage = self.usage
        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        real_port = self.httpd.server_address[1]
        self.url = "http://%s:%d/" % (self.host, real_port)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="wireless-status-http", daemon=True)
        self.thread.start()
        return self.url

    def stop(self):
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
            try:
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None


if __name__ == "__main__":
    # 单独跑起来，用浏览器看页面（没有 pywebview 也能调界面）：
    #   python server.py
    # 这时界面上「写 DPI」会提示 js_api 不可用，其余只读功能正常。
    import devices
    import history
    import usage

    prov = devices.HidppProvider()
    prov.source.start()
    srv = LocalServer(provider=prov,
                      history=history.HistoryStore(),
                      usage=usage.UsageTracker())
    print("serving on", srv.start())
    print("按 Ctrl+C 停止")
    try:
        while True:
            threading.Event().wait(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
        prov.source.stop()
