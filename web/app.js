/* 设备 —— 前端逻辑
 *
 * 两条数据通路，分清楚很重要：
 *   1. pywebview 的 window.pywebview.api.*  —— 能写（DPI、退出、开机自启、贴片开关）。
 *      只在窗口里存在；用浏览器打开 http://127.0.0.1:PORT/ 时是 undefined。
 *   2. 本地 HTTP 的 /api/* —— 只读轮询来源。好处是浏览器调试时也能看到
 *      真实数据，而且不占用 js_api 的序列化开销。
 *
 * 视图是**两页**，不是两个窗口：
 *   #page-list   设备列表（默认）
 *   #page-device 设备功能区
 * 切换只动 hidden 属性，不销毁、不重载 —— 这样返回列表时滚动位置和
 * 图表状态都还在，切换也是瞬时的。
 *
 * 所有操作都走 safe()：任何异常都翻成界面上一句中文提示，绝不静默。
 */

(function () {
  "use strict";

  // ---------------------------------------------------------------- 常量

  var API_POLL_MS = 1000;         // 界面拉快照的节奏（数据由后台线程按 5s 采）
  var REFRESH_SPIN_MS = 600;      // 刷新按钮转圈时长（需求指定）
  var CONFIRM_TIMEOUT_MS = 5000;  // 二次确认条 5 秒无操作自动收起
  var HISTORY_POLL_MS = 15000;    // 历史图刷新节奏（不需要跟快照一样快）

  // 电量分档 → CSS 变量名。**不在这里写死颜色值**，颜色全在 style.css，
  // 这样深/浅色切换只需要改 CSS。
  var LEVEL_CLASS = {
    good: "lv-good",
    low: "lv-low",
    critical: "lv-critical",
    empty: "lv-empty",
    unknown: "lv-unknown"
  };

  // 设备类型 → 内联 SVG（icon/ 缺失时的兜底，绝不允许出现裂图或 alt 文字）
  var SVG_FALLBACK = {
    mouse: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="2" width="8" height="20" rx="4"/><line x1="12" y1="6" x2="12" y2="10"/></svg>',
    headset: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14v-2a8 8 0 0 1 16 0v2"/><rect x="2" y="14" width="4" height="6" rx="1.5"/><rect x="18" y="14" width="4" height="6" rx="1.5"/></svg>',
    gamepad: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="7" width="20" height="10" rx="5"/><line x1="7" y1="10" x2="7" y2="14"/><line x1="5" y1="12" x2="9" y2="12"/><circle cx="16" cy="11" r="1"/><circle cx="18" cy="13" r="1"/></svg>',
    unknown: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="18" height="12" rx="3"/><line x1="7" y1="12" x2="11" y2="12"/></svg>'
  };

  // ---------------------------------------------------------------- 状态

  var api = null;                 // pywebview api 对象，就绪后才有
  var lastSnap = null;            // 最近一次设备快照
  var currentKey = null;          // 当前功能区展示的设备键
  var currentDevice = null;       // 当前设备 dict
  var range = "24h";              // 历史图当前档位
  var historyData = null;
  var userDpi = null;             // 用户正在拖动的 DPI 值（拖动期间不被轮询覆盖）
  var confirmTimer = null;
  var refreshTimer = null;
  var historyTimer = null;
  var chartTipBound = false;

  // ---------------------------------------------------------------- 工具

  function $(id) {
    return document.getElementById(id);
  }

  function text(id, value) {
    var el = $(id);
    if (el && el.textContent !== String(value)) {
      el.textContent = value;
    }
  }

  function show(el) {
    if (el) { el.hidden = false; }
  }

  function hide(el) {
    if (el) { el.hidden = true; }
  }

  function setSwitch(el, on) {
    if (!el) { return; }
    el.classList.toggle("on", !!on);
    el.setAttribute("aria-checked", on ? "true" : "false");
  }

  /** 把 promise / 同步值统一成 {ok, ...}，异常翻成中文提示。 */
  function safe(promise, fallback) {
    var p = promise;
    if (!p || typeof p.then !== "function") {
      p = Promise.resolve(p === undefined ? fallback : p);
    }
    return p.then(function (res) {
      return res === undefined ? fallback : res;
    }).catch(function (err) {
      var msg = (err && err.message) ? err.message : String(err);
      banner("操作失败", msg);
      return { ok: false, error: msg };
    });
  }

  function banner(msg, hint) {
    var el = $("banner");
    if (!el) { return; }
    text("banner-msg", msg || "");
    text("banner-hint", hint || "");
    if (msg) {
      el.hidden = false;
    } else {
      el.hidden = true;
    }
  }

  /** 有 pywebview 走 js_api，否则退回 fetch（浏览器调试用）。 */
  function callApi(name, args) {
    if (api && typeof api[name] === "function") {
      return api[name].apply(api, args || []);
    }
    return Promise.reject(new Error("当前环境没有 " + name + " 接口"));
  }

  function getJson(path) {
    return fetch(path, { cache: "no-store" }).then(function (res) {
      if (!res.ok) {
        throw new Error("HTTP " + res.status);
      }
      return res.json();
    });
  }

  // ---------------------------------------------------------------- 图标

  /**
   * 把 <img> 换成内联 SVG。icon/ 里文件缺失时 onerror 会调这里，
   * 保证不会出现裂图或 alt 文字（需求明确要求）。
   */
  function iconFallback(img, kind) {
    if (!img || img.dataset.fellBack) { return; }
    img.dataset.fellBack = "1";
    var svg = SVG_FALLBACK[kind] || SVG_FALLBACK.unknown;
    var holder = document.createElement("span");
    holder.className = "ico-fallback";
    holder.innerHTML = svg;
    if (img.parentNode) {
      img.parentNode.replaceChild(holder, img);
    }
  }

  function iconMarkup(kind, cls) {
    var name = kind || "unknown";
    return '<span class="' + (cls || "dev-ico") + '">' +
      '<img src="/icon/' + name + '.svg" alt="" ' +
      'onerror="window.__iconFallback(this,\'' + name + '\')">' +
      '</span>';
  }

  // 暴露给 onerror 用（内联属性里的函数必须在全局作用域）
  window.__iconFallback = iconFallback;

  // ---------------------------------------------------------------- 列表页

  function renderList(snap) {
    var list = $("device-list");
    var empty = $("list-empty");
    if (!list) { return; }

    var devices = (snap && snap.devices) || [];
    list.innerHTML = "";

    if (!devices.length) {
      show(empty);
      var hint = $("empty-hint");
      if (hint) {
        var msg = (snap && (snap.hint || snap.error)) || "";
        hint.textContent = msg ||
          "请确认罗技接收器已插好、鼠标已开机。改完硬件后点右上角刷新。";
      }
      return;
    }
    hide(empty);

    devices.forEach(function (dev) {
      list.appendChild(deviceRow(dev));
    });
  }

  function deviceRow(dev) {
    var row = document.createElement("button");
    row.className = "device-row";
    row.type = "button";
    row.dataset.key = dev.key || "";

    var level = LEVEL_CLASS[dev.level] || LEVEL_CLASS.unknown;
    var status = dev.status || "offline";
    var dotCls = "dot dot-" + status;
    var pct = dev.battery && dev.battery.percent;
    var pctText = (pct === null || pct === undefined)
      ? (dev.battery && dev.battery.source_label ? dev.battery.source_label : "不支持")
      : pct + "%";
    var lowCls = (pct !== null && pct !== undefined && pct < 5) ? " is-critical" : "";

    // 记住上次选中的设备：只高亮，不自动跳页（需求明确）
    var selected = (lastSnap && lastSnap.config &&
                    lastSnap.config.last_selected_device === dev.key) ? " is-selected" : "";

    row.innerHTML =
      iconMarkup(dev.icon || dev.kind) +
      '<span class="dev-main">' +
        '<span class="dev-name"></span>' +
        '<span class="dev-meta">' +
          '<span class="dev-conn"></span>' +
          '<span class="dot-mini ' + level + '"></span>' +
        '</span>' +
      '</span>' +
      '<span class="dev-side">' +
        '<span class="dev-pct num ' + level + lowCls + '">' + pctText + '</span>' +
        '<span class="' + dotCls + '"></span>' +
      '</span>';

    // 用 textContent 写名字，避免产品名里的特殊字符破坏 HTML
    row.querySelector(".dev-name").textContent = dev.display_name || dev.name || "未知设备";
    var conn = row.querySelector(".dev-conn");
    conn.textContent = (dev.connection && dev.connection.label) || "—";
    var dot = row.querySelector(".dot-mini");
    if (dot) { dot.title = "电量分档：" + (dev.level || "unknown"); }

    row.addEventListener("click", function () {
      openDevice(dev.key);
    });
    return row;
  }

  // ---------------------------------------------------------------- 功能区

  function openDevice(key) {
    currentKey = key;
    currentDevice = findDevice(key);
    $("page-list").hidden = true;
    $("page-device").hidden = false;
    userDpi = null;
    renderDetail(lastSnap);
    loadHistory();

    // 记住选择（下次启动高亮；不自动跳页）
    safe(callApi("set_config", ["last_selected_device", key]), null);

    // 切页后内容高度完全不同（功能区比列表高不少），重新量一次
    refitNow();
  }

  function backToList() {
    $("page-device").hidden = true;
    $("page-list").hidden = false;
    currentKey = null;
    currentDevice = null;
    // 丢弃还没应用的 DPI 选择：回到列表等于放弃这次修改，
    // 否则下次进详情页会看到一个跟设备实际值不符的数字。
    userDpi = null;
    clearInterval(historyTimer);
    historyTimer = null;

    refitNow();
  }

  function findDevice(key) {
    var devices = (lastSnap && lastSnap.devices) || [];
    for (var i = 0; i < devices.length; i++) {
      if (devices[i].key === key) { return devices[i]; }
    }
    return null;
  }

  function renderDetail(snap) {
    if (!currentKey) { return; }
    var dev = findDevice(currentKey);
    if (!dev) {
      // 设备掉线：不要清空界面，标成离线更友好
      if (currentDevice) {
        dev = Object.assign({}, currentDevice, {
          status: "offline", status_text: "离线", level: "unknown"
        });
      } else {
        text("detail-name", "设备已断开");
        return;
      }
    }
    currentDevice = dev;

    text("detail-title", dev.kind_label || "设备");
    text("detail-name", dev.display_name || dev.name || "未知设备");
    text("detail-link", (dev.connection && dev.connection.label) || "—");
    text("detail-status", dev.status_text || "—");

    var dot = $("detail-dot");
    if (dot) {
      dot.className = "dot dot-" + (dev.status || "offline");
    }

    renderBattery(dev.battery || {});
    renderDpi(dev.dpi || {});
    renderUsage(snap && snap.usage);
  }

  function renderBattery(bat) {
    var pct = bat.percent;
    var ring = $("battery-ring");
    var hasPct = (pct !== null && pct !== undefined);

    // 需求 Q2：整数不相等就重绘，绝不做 5% 台阶
    if (ring) {
      ring.style.setProperty("--pct", hasPct ? pct : 0);
      ring.classList.toggle("is-unknown", !hasPct);
      ring.classList.toggle("is-low", hasPct && pct < 20);
      ring.classList.toggle("is-critical", hasPct && pct < 5);
    }

    text("battery-value", hasPct ? (pct + "%") : "不支持");

    var note = $("battery-note");
    if (note) {
      var parts = [];
      if (bat.text) { parts.push(bat.text); }
      if (hasPct && bat.source_label) { parts.push(bat.source_label); }
      if (bat.voltage_mv) { parts.push(bat.voltage_mv + " mV"); }
      note.textContent = parts.join(" · ");
      // 估算值的详细说明放 tooltip（需求：tooltip 里附当前电压 mV）
      note.title = bat.note || "";
    }

    // 设备只报 5% 粒度时如实说明，不伪造 1% 抖动（需求 Q2）
    var badge = $("battery-badge");
    if (badge) {
      if (bat.granularity && bat.granularity > 1) {
        badge.textContent = "设备粒度 " + bat.granularity + "%";
        badge.hidden = false;
      } else if (bat.source === "estimated") {
        badge.textContent = "电压估算";
        badge.hidden = false;
      } else {
        badge.hidden = true;
      }
    }
  }

  function renderDpi(dpi) {
    var slider = $("dpi-slider");
    var lo = dpi.min || 200;
    var hi = dpi.max || 12000;
    var step = dpi.step || 50;
    // 拖动期间以 userDpi 为准（滑块位置、数字文本、应用按钮三者必须同一个源）。
    //
    // 踩过的坑：原来滑块用 userDpi、数字文本却用后端 dpi.value，于是拖到
    // 1600 后每秒轮询把数字打回设备当前值 1000，滑块还停在 1600 —— 看着像
    // 「值自己跳回去」。数字必须跟着 cur 走。
    var cur = (userDpi !== null) ? userDpi : (dpi.value || lo);

    if (slider) {
      slider.min = lo;
      slider.max = hi;
      slider.step = step;
      if (String(slider.value) !== String(cur)) { slider.value = cur; }
      slider.disabled = !dpi.supported;
    }

    // 数字文本跟 cur 同源：
    //   - 用户拖过（userDpi 非空）→ 显示用户选的值，轮询不许覆盖；
    //   - 没拖过 → 显示设备当前值；设备不支持或读不到值才显示「—」。
    // 原来这里写的是 (dpi.supported && dpi.value) ? dpi.value : "—"，
    // 于是拖动后每秒轮询都把数字打回设备当前值（1000），滑块却停在用户
    // 拖到的位置 —— 用户看到的就是「值自己跳回去」。
    if (!dpi.supported) {
      text("dpi-value", "—");
    } else if (userDpi !== null) {
      text("dpi-value", userDpi);
    } else {
      text("dpi-value", dpi.value ? dpi.value : "—");
    }
    text("dpi-range", lo + "–" + hi);
    text("dpi-min", lo);
    text("dpi-max", hi);

    var badge = $("dpi-inferred");
    if (badge) { badge.hidden = !dpi.inferred; }
  }

  // 「已运行」要精确到秒、每秒跳动。
  //
  // 不能只靠 API 轮询（1 秒一次，但网络与后端排队会让它抖）——所以后端给的
  // 秒数记下来当基准，本地再挂一个 1 秒定时器往上推。每次只改 #usage-value
  // 一个文本节点，不重排 DOM、不重建卡片，所以不会闪。
  //
  // 关键：只在**确认活跃**时才本地加秒。判断依据是「这次轮询的值比上次大」，
  // 说明这段时间内有键鼠动作；如果两次一样（人在发呆），本地就冻住，
  // 否则会凭空涨时间、还会在下次轮询时跳回去。
  var usageBase = null;      // 后端最近一次给的累计秒数
  var usageBaseAt = 0;       // 记下这个数时的时间戳（ms）
  var usageActive = false;   // 上一轮轮询是否观察到增长
  var usageTimer = null;

  /** 秒数 → HH:MM:SS / N天 HH:MM:SS（与 usage.format_duration 同一套口径） */
  function formatClock(totalSeconds) {
    var s = Math.max(0, Math.floor(totalSeconds || 0));
    var days = Math.floor(s / 86400);
    var rest = s - days * 86400;
    var hh = Math.floor(rest / 3600);
    var mm = Math.floor((rest - hh * 3600) / 60);
    var ss = rest - hh * 3600 - mm * 60;
    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
    var clock = pad(hh) + ":" + pad(mm) + ":" + pad(ss);
    return days > 0 ? (days + "天 " + clock) : clock;
  }

  function paintUsage() {
    if (usageBase === null) { return; }
    var elapsed = usageActive ? Math.floor((Date.now() - usageBaseAt) / 1000) : 0;
    text("usage-value", formatClock(usageBase + elapsed));
  }

  function startUsageTicker() {
    if (usageTimer) { return; }
    usageTimer = setInterval(paintUsage, 1000);
  }

  function renderUsage(usage) {
    if (!usage) {
      usageBase = null;
      usageActive = false;
      text("usage-value", "—");
      return;
    }
    var secs = (typeof usage.active_seconds === "number") ? usage.active_seconds : null;
    if (secs === null) {
      usageBase = null;
      usageActive = false;
      text("usage-value", "—");
      return;
    }
    // 比上次大 = 这段时间里有操作，本地可以每秒加；否则冻结在原值。
    usageActive = (usageBase !== null && secs > usageBase);
    usageBase = secs;
    usageBaseAt = Date.now();
    text("usage-value", usage.text || formatClock(secs));
    startUsageTicker();
  }

  // ---------------------------------------------------------------- 历史图

  function loadHistory() {
    if (!currentKey) { return; }
    var url = "/api/history?key=" + encodeURIComponent(currentKey) +
              "&range=" + encodeURIComponent(range);
    getJson(url).then(function (data) {
      historyData = data;
      drawHistory(data);
    }).catch(function (err) {
      var emptyEl = $("chart-empty");
      if (emptyEl) {
        emptyEl.textContent = "历史数据读取失败：" + err.message;
        emptyEl.hidden = false;
      }
    });
  }

  function drawHistory(data) {
    var canvas = $("history-chart");
    var emptyEl = $("chart-empty");
    var summary = $("history-summary");
    if (!canvas) { return; }

    var points = (data && data.points) || [];
    if (summary) {
      var used = data && data.today_used;
      var avg = data && data.daily_avg;
      summary.textContent = "今日消耗 " + (used === undefined ? "—" : used + "%") +
        " · 日均 " + (avg === undefined ? "—" : avg + "%");
    }

    if (points.length < 2) {
      if (emptyEl) { emptyEl.hidden = false; }
      var ctx0 = canvas.getContext("2d");
      ctx0.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }
    if (emptyEl) { emptyEl.hidden = true; }

    var dpr = window.devicePixelRatio || 1;
    var cssW = canvas.clientWidth || 360;
    var cssH = 132;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.height = cssH + "px";

    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var style = getComputedStyle(document.documentElement);
    var accent = style.getPropertyValue("--accent").trim() || "#2da44e";
    var grid = style.getPropertyValue("--border").trim() || "#dfe3e6";
    var faint = style.getPropertyValue("--text-faint").trim() || "#8b949e";

    var padL = 26, padR = 8, padT = 8, padB = 16;
    var plotW = cssW - padL - padR;
    var plotH = cssH - padT - padB;

    var t0 = data.start || points[0].t;
    var t1 = data.end || points[points.length - 1].t;
    if (t1 <= t0) { t1 = t0 + 1; }

    function sx(t) { return padL + ((t - t0) / (t1 - t0)) * plotW; }
    function sy(p) { return padT + (1 - p / 100) * plotH; }

    // 网格（3 条横线：0/50/100）
    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    [0, 50, 100].forEach(function (v) {
      var y = Math.round(sy(v)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(cssW - padR, y);
      ctx.stroke();
      ctx.fillStyle = faint;
      ctx.font = "9px 'Cascadia Mono', monospace";
      ctx.textAlign = "right";
      ctx.fillText(v + "%", padL - 4, y + 3);
    });

    // 折线 + 面积
    ctx.beginPath();
    points.forEach(function (p, i) {
      var x = sx(p.t), y = sy(p.pct);
      if (i === 0) { ctx.moveTo(x, y); } else { ctx.lineTo(x, y); }
    });
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.6;
    ctx.lineJoin = "round";
    ctx.stroke();

    ctx.lineTo(sx(points[points.length - 1].t), padT + plotH);
    ctx.lineTo(sx(points[0].t), padT + plotH);
    ctx.closePath();
    ctx.fillStyle = accent + "22";
    ctx.fill();

    // 换电池标记（竖线）
    (data.changes || []).forEach(function (t) {
      var x = Math.round(sx(t)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x, padT);
      ctx.lineTo(x, padT + plotH);
      ctx.strokeStyle = faint;
      ctx.setLineDash([2, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
    });

    bindChartHover(canvas, data, sx, sy, padL, padT, plotW, plotH);
  }

  function bindChartHover(canvas, data, sx, sy, padL, padT, plotW, plotH) {
    if (chartTipBound) { return; }
    chartTipBound = true;

    var tip = $("chart-tip");
    canvas.addEventListener("mousemove", function (e) {
      if (!historyData) { return; }
      var rect = canvas.getBoundingClientRect();
      var mx = e.clientX - rect.left;
      var pts = (historyData.points || []);
      if (!pts.length) { return; }

      var t0 = historyData.start || pts[0].t;
      var t1 = historyData.end || pts[pts.length - 1].t;
      if (t1 <= t0) { return; }
      var tt = t0 + ((mx - padL) / plotW) * (t1 - t0);

      // 找最近的采样点
      var best = null, bestD = Infinity;
      pts.forEach(function (p) {
        var d = Math.abs(p.t - tt);
        if (d < bestD) { bestD = d; best = p; }
      });
      if (!best || !tip) { return; }

      var when = new Date(best.t * 1000);
      var label = pad2(when.getMonth() + 1) + "-" + pad2(when.getDate()) +
                  " " + pad2(when.getHours()) + ":" + pad2(when.getMinutes());
      tip.innerHTML = label + "<br>" + best.pct + "%" +
                      (best.mv ? ("<br>" + best.mv + " mV") : "");
      tip.hidden = false;
      var left = Math.min(Math.max(4, mx + 10), canvas.clientWidth - 90);
      tip.style.left = left + "px";
      tip.style.top = Math.max(4, (e.clientY - rect.top) - 34) + "px";
    });

    canvas.addEventListener("mouseleave", function () {
      if (tip) { tip.hidden = true; }
    });
  }

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  // ---------------------------------------------------------------- 数据轮询

  function pull() {
    var p = (api && typeof api.snapshot === "function")
      ? safe(api.snapshot(), null)
      : getJson("/api/snapshot");
    return p.then(function (snap) {
      if (!snap) { return; }
      lastSnap = snap;
      if (!snap.config) { snap.config = {}; }
      handleVisibility(snap);
      renderList(snap);
      if (currentKey) { renderDetail(snap); }
      renderBanner(snap);
      return snap;
    }).catch(function (err) {
      banner("无法读取设备状态", err.message || String(err));
    });
  }

  /**
   * 收起 → 再打开时退回设备列表页（需求 4）。
   *
   * 窗口是隐藏而不是销毁，页面一直在跑定时器，所以停在详情页时再点托盘
   * 图标，看到的还是上次那台设备。后端在 ``show_window()`` 里置一次性
   * 标志 ``want_list``，随快照下发，前端消费后立刻退回列表页。
   *
   * 为什么不用比对 ``visible`` 的 false → true：窗口隐藏时 WebView2 会把
   * ``setInterval`` 降频到秒级甚至暂停，前端很可能整段错过「隐藏」那一帧，
   * 跳变检测会漏。一次性标志不依赖有没有看到中间状态。
   */
  function handleVisibility(snap) {
    if (!snap.want_list) { return; }
    if (currentKey) { backToList(); }
  }

  function renderBanner(snap) {
    if (!snap) { return; }
    if (snap.ok === false && snap.error) {
      banner(snap.error, snap.hint || "");
      return;
    }
    if (snap.writable === false) {
      banner("配置目录不可写", "设置只会保存在内存里，退出后丢失");
      return;
    }
    // 托盘起不来不是致命错误（窗口照常可用），但必须让用户看得见，
    // 否则只会表现为「托盘区没图标」，查不出所以然。
    if (snap.tray_error) {
      banner(snap.tray_error, "窗口仍可正常使用，只是没有托盘图标");
      return;
    }
    banner("", "");
  }

  // ---------------------------------------------------------------- 交互绑定

  function bindSwitch(el, onChange) {
    if (!el) { return; }
    function toggle() {
      var on = !el.classList.contains("on");
      setSwitch(el, on);
      onChange(on);
    }
    el.addEventListener("click", toggle);
    el.addEventListener("keydown", function (e) {
      if (e.key === " " || e.key === "Enter") {
        e.preventDefault();
        toggle();
      }
    });
  }

  function initListPage() {
    // 刷新：转圈 600ms 作为反馈
    var refreshBtn = $("btn-refresh");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", function () {
        if (refreshBtn.classList.contains("is-spinning")) { return; }
        refreshBtn.classList.add("is-spinning");
        safe(callApi("refresh"), null).then(function () {
          return pull();
        });
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(function () {
          refreshBtn.classList.remove("is-spinning");
        }, REFRESH_SPIN_MS);
      });
    }

    // 退出：二次确认（页面内确认条，5 秒自动收起）
    var quitBtn = $("btn-quit");
    var bar = $("confirm-bar");
    if (quitBtn && bar) {
      quitBtn.addEventListener("click", function () {
        bar.hidden = false;
        clearTimeout(confirmTimer);
        confirmTimer = setTimeout(function () {
          bar.hidden = true;
        }, CONFIRM_TIMEOUT_MS);
      });
    }
    var cancelBtn = $("btn-cancel-quit");
    if (cancelBtn) {
      cancelBtn.addEventListener("click", function () {
        clearTimeout(confirmTimer);
        if (bar) { bar.hidden = true; }
      });
    }
    var okBtn = $("btn-confirm-quit");
    if (okBtn) {
      okBtn.addEventListener("click", function () {
        clearTimeout(confirmTimer);
        safe(callApi("quit"), null);
      });
    }

    // 贴片开关
    var pill = $("pill-switch");
    bindSwitch(pill, function (on) {
      safe(callApi("set_config", ["battery_pill_enabled", on]), null)
        .then(function () { return pull(); });
    });
  }

  function initDetailPage() {
    var back = $("btn-back");
    if (back) { back.addEventListener("click", backToList); }

    var refreshBtn = $("btn-refresh-detail");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", function () {
        if (refreshBtn.classList.contains("is-spinning")) { return; }
        refreshBtn.classList.add("is-spinning");
        safe(callApi("refresh"), null).then(function () { return pull(); });
        setTimeout(function () {
          refreshBtn.classList.remove("is-spinning");
        }, REFRESH_SPIN_MS);
      });
    }

    // DPI 滑块：拖动时只更新显示，松手才写设备
    var slider = $("dpi-slider");
    if (slider) {
      slider.addEventListener("input", function () {
        userDpi = parseInt(slider.value, 10);
        text("dpi-value", userDpi);
      });
      slider.addEventListener("change", function () {
        userDpi = parseInt(slider.value, 10);
        text("dpi-value", userDpi);
      });
    }

    var apply = $("btn-apply-dpi");
    if (apply) {
      apply.addEventListener("click", function () {
        if (userDpi === null) {
          banner("请先拖动滑块选择 DPI", "");
          return;
        }
        apply.classList.add("is-busy");
        safe(callApi("set_dpi", [userDpi]), null).then(function (res) {
          apply.classList.remove("is-busy");
          if (res && res.ok === false) {
            banner("DPI 未生效", res.error || res.message || "");
          } else if (res && res.message) {
            banner(res.message, "");
            setTimeout(function () { banner("", ""); }, 2500);
          }
          userDpi = null;
          return pull();
        });
      });
    }

    var reset = $("btn-reset-dpi");
    if (reset) {
      reset.addEventListener("click", function () {
        var dpi = (currentDevice && currentDevice.dpi) || {};
        var target = dpi["default"] || Math.round(((dpi.min || 200) + (dpi.max || 12000)) / 2);
        userDpi = target;
        text("dpi-value", target);
        var s = $("dpi-slider");
        if (s) { s.value = target; }
      });
    }

    // 历史档位
    var tabs = $("range-tabs");
    if (tabs) {
      tabs.addEventListener("click", function (e) {
        var btn = e.target.closest ? e.target.closest(".tab") : null;
        if (!btn) { return; }
        var r = btn.dataset.range;
        if (!r || r === range) { return; }
        range = r;
        Array.prototype.forEach.call(tabs.querySelectorAll(".tab"), function (t) {
          t.classList.toggle("is-on", t === btn);
        });
        loadHistory();
      });
    }

    // 开机自启不在界面上了（需求 5）：只剩托盘右键菜单那一项。
  }

  /** 把快照里的配置状态回显到开关上（不能让前端自己猜）。 */
  function syncSwitches(snap) {
    var cfg = (snap && snap.config) || {};
    setSwitch($("pill-switch"), !!cfg.battery_pill_enabled);

    var pill = $("pill-switch");
    if (pill) {
      pill.classList.toggle("disabled", snap.writable === false);
    }
  }

  // ---------------------------------------------------------------- 启动

  function tick() {
    pull().then(function (snap) {
      if (snap) {
        syncSwitches(snap);
        fitHeight(snap);
      }
    });
  }

  /**
   * 量当前视图「真正的内容高度」。
   *
   * 不能直接用 ``scroller.scrollHeight``：``.scroll`` 是 ``flex:1 1 auto``，
   * 内容不足时会**填满剩余空间**，于是 scrollHeight 恒等于「窗口高 - 固定块」。
   * 用它做自适应会形成正反馈 —— 窗口变高 → 量到更高 → 再变高，一路顶到
   * MAX_HEIGHT 为止，界面上表现为「设备只有一行，下面一大片空白」。
   *
   * 所以改成累加滚动区里各子元素的实际高度 + 容器内边距，与容器当前
   * 高度无关。
   */
  function measureContentHeight(page) {
    var scroller = page.querySelector(".scroll");
    if (!scroller) { return 0; }

    var cs = getComputedStyle(scroller);
    var total = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);

    Array.prototype.forEach.call(scroller.children, function (child) {
      if (child.hidden) { return; }
      var s = getComputedStyle(child);
      // 有的子元素靠 margin 撑间距（.device-list 用的是 gap，不是 margin）
      total += child.offsetHeight
             + (parseFloat(s.marginTop) || 0)
             + (parseFloat(s.marginBottom) || 0);
    });
    return total;
  }

  // 上一次报给后端的内容高度。只在变化超过阈值时才发请求，避免每轮轮询
  // 都触发一次 resize。
  var _reportedHeight = 0;
  // 上一次量到的值 + 它连续出现了几次。**同一个值要连续量到两次才上报**。
  //
  // 为什么需要这道判定：切页/首帧时 DOM 还没排完（字体没载入、设备图标
  // 还没尺寸、列表项正在插入），量出来是个明显偏小的数。以前直接上报，
  // 后端照着把窗口压扁，下一轮又量回真实高度 —— 用户看到的就是「点一下
  // 界面跳一下」。现在只有「这一帧和上一帧量到的一样」才认为排版稳定了。
  //
  // 为什么不在后端拦：后端没法区分「页面还没排完的 168」和「列表页真实
  // 就是 168」，拦了就再也收不回去（踩过）。
  var _lastWant = -1;
  var _wantHits = 0;

  // 页切换时立刻重排一次，不受「连续两帧」判定阻挡 —— 切页是唯一能确定
  // 「内容高度必然变了」的时刻，而且此时 DOM 已经插好（openDevice/backToList
  // 都是先改 hidden 再调这里），没必要再等一轮轮询。等两帧的旧写法会让
  // 动画比点击晚最多 1 秒才起步（实测 602ms），观感是「点了没反应，然后突然
  // 长上去」。
  function refitNow() {
    var want = measureContent();
    if (want === undefined) { return; }
    _lastWant = want;
    _wantHits = 2;
    if (Math.abs(want - _reportedHeight) < 2) { return; }
    _reportedHeight = want;
    if (api && api.set_height) {
      api.set_height(want).then(function () {}).catch(function () {});
    }
  }

  function measureContent() {
    // 贴片页不参与（它是固定 200x28 的小窗）
    if (document.body.classList.contains("pill-page")) { return; }

    var page = $("page-device").hidden ? $("page-list") : $("page-device");
    if (!page) { return; }

    var content = measureContentHeight(page);
    // 加上 titlebar / footbar 这些固定块（不含滚动区自身）
    var fixed = 0;
    Array.prototype.forEach.call(page.children, function (child) {
      if (!child.classList.contains("scroll") && !child.hidden) {
        fixed += child.offsetHeight || 0;
      }
    });
    // 滚动区自带上下 padding，这里只补一点呼吸位（body 已无边框）
    return Math.ceil(content + fixed + 2);
  }

  function fitHeight(snap) {
    var want = measureContent();
    if (want === undefined) { return; }

    // 稳定性判定：同一数值连续两次才采纳。
    if (want === _lastWant) {
      _wantHits += 1;
    } else {
      _lastWant = want;
      _wantHits = 1;
    }
    if (_wantHits < 2) { return; }

    // 滞回从 24px 收到 2px：24px 会让「内容少了 20px」这类变化被吞掉，
    // 窗口比内容高出一截，视觉上就是设备下方那片空白（需求 1 点名要修的）。
    // 2px 既不会来回抖（后端 set_height 自身还有 4px 阈值），又能贴合内容。
    if (Math.abs(want - _reportedHeight) < 2) { return; }
    _reportedHeight = want;

    if (api && api.set_height) {
      api.set_height(want).then(function () {}).catch(function () {});
    }
  }

  // ---------------------------------------------------------------- 自动收起

  // 需求：**鼠标移出窗口 2 秒自动收起**。
  // 用 mouseleave 而不是 blur：blur 在「点窗口里的按钮」时也会触发（WebView2
  // 里焦点在文档和控件间跳），会导致刚点一下就收起来。mouseleave 只在指针
  // 真正离开窗口矩形时触发，符合「移出去才收」的直觉。
  //
  // 贴片页不参与（贴片是常驻的小胶囊，收起来就没意义了）。
  var HIDE_DELAY_MS = 2000;
  var _hideTimer = null;

  function scheduleAutoHide() {
    if (document.body.classList.contains("pill-page")) { return; }
    cancelAutoHide();
    _hideTimer = setTimeout(function () {
      _hideTimer = null;
      if (api && api.hide_window) {
        api.hide_window().catch(function () {});
      }
    }, HIDE_DELAY_MS);
  }

  function cancelAutoHide() {
    if (_hideTimer) {
      clearTimeout(_hideTimer);
      _hideTimer = null;
    }
  }

  function initAutoHide() {
    if (document.body.classList.contains("pill-page")) { return; }
    // 指针离开整个窗口 → 起 2 秒定时；回到窗口里 → 取消
    document.addEventListener("mouseleave", scheduleAutoHide);
    document.addEventListener("mouseenter", cancelAutoHide);
    // 拖动 DPI 滑块 / 输入框聚焦时，指针可能短暂划出窗口（或者系统拖拽
    // 不派发 mousemove），这些时段不该收起。
    document.addEventListener("mousedown", cancelAutoHide);
    document.addEventListener("mouseup", cancelAutoHide);
    window.addEventListener("blur", cancelAutoHide);
  }

  // ---------------------------------------------------------------- 拖动窗口

  // 需求第 5 条：整个界面（非交互区域）都能拖。
  //
  // 做法：mousedown 落在「不是按钮/开关/输入框/滑块/可点行」的地方时，
  // 交给 Python 的 winui.start_window_drag()（ReleaseCapture + WM_NCLBUTTONDOWN
  // + HTCAPTION），由系统执行窗口移动 —— 这样有边缘吸附、跟手，不抖。
  //
  // 为什么不用 CSS 的 -webkit-app-region: drag：那是 Electron 的私有属性，
  // WebView2 不认，完全没效果。
  var DRAG_BLOCK_SELECTOR = [
    "button", "input", "select", "textarea", "label",
    ".switch", ".device-row", ".slider", ".tab", ".icon-btn", ".btn",
    ".confirm-bar", ".chart", ".link", "a"
  ].join(",");

  var _dragging = false;

  function isDragAllowed(target) {
    if (!target || !target.closest) { return false; }
    // 落在可交互元素上就不拖（否则点按钮会变成拖窗口）
    if (target.closest(DRAG_BLOCK_SELECTOR)) { return false; }
    // 滚动区内部不拖：那里的空白是内容留白，用户滚动时按住空白很常见，
    // 拿来拖窗口会误伤。只让标题栏与页面上下留白可拖。
    if (target.closest(".scroll")) { return false; }
    return true;
  }

  function initDrag() {
    if (document.body.classList.contains("pill-page")) { return; }
    document.addEventListener("mousedown", function (e) {
      if (e.button !== 0) { return; }
      if (!isDragAllowed(e.target)) { return; }
      if (!api || !api.start_drag) { return; }
      _dragging = true;
      document.body.classList.add("dragging");
      cancelAutoHide();
      // 交给系统：调用之后鼠标捕获归 Windows，我们这边收不到后续事件，
      // 所以用 document 上的 mouseup + 定时轮询收尾。
      api.start_drag();
    });

    function endDrag() {
      if (!_dragging) { return; }
      _dragging = false;
      document.body.classList.remove("dragging");
      if (api && api.finish_drag) {
        api.finish_drag();
      }
    }
    document.addEventListener("mouseup", endDrag);
    // 系统接管拖动时 DOM 收不到 mouseup（指针在窗口外松开），
    // 所以松手后立刻补一次收尾；同时挂个低频兜底。
    document.addEventListener("mouseleave", function () {
      setTimeout(endDrag, 0);
    });
    window.addEventListener("blur", endDrag);
  }

  function boot() {
    initListPage();
    initDetailPage();
    initAutoHide();
    initDrag();
    tick();
    setInterval(tick, API_POLL_MS);

    // 从功能区回到列表时不需要停轮询，但历史图不必一直刷
    setInterval(function () {
      if (currentKey && !$("page-device").hidden) {
        loadHistory();
      }
    }, HISTORY_POLL_MS);
  }

  // pywebview 就绪
  window.addEventListener("pywebviewready", function () {
    api = window.pywebview && window.pywebview.api ? window.pywebview.api : null;
    // 显式告诉后端「前端好了」：它会套毛玻璃、去任务栏按钮、定位窗口。
    // 这些操作只能在 UI 线程做，而这里正是 UI 线程（见 main.py 的说明）。
    if (api && api.ready) {
      api.ready().catch(function () {});
    }
    tick();
  });

  // ---------------------------------------------------------------- 错误兜底

  // 打包成 -w 的 exe 后，WebView2 里的 JS 异常不会留下任何痕迹：
  // 界面白屏或变黑，用户只看到「点一下就报错」，而 error.log 是空的。
  // 这里把未捕获异常和未处理 rejection 都翻成：
  //   1. 界面上一条中文提示（用户看得懂）
  //   2. ui_error.log 里一条带堆栈的记录（排查用）
  // 绝不允许静默失败。
  var reportedError = false;

  function reportError(message, detail) {
    banner("界面出错", message || "未知错误");
    if (reportedError) { return; }   // 只落盘第一次，避免刷屏
    reportedError = true;
    try {
      if (api && typeof api.report_error === "function") {
        api.report_error(String(message || ""), String(detail || ""));
      }
    } catch (e) {
      /* 上报通道本身失败也不能再抛 */
    }
  }

  window.addEventListener("error", function (e) {
    var msg = (e && e.message) ? e.message : "脚本错误";
    var detail = (e && e.filename ? e.filename + ":" + e.lineno + ":" + e.colno : "");
    if (e && e.error && e.error.stack) { detail += "\n" + e.error.stack; }
    reportError(msg, detail);
  });

  window.addEventListener("unhandledrejection", function (e) {
    var reason = e && e.reason;
    var msg = (reason && reason.message) ? reason.message : String(reason);
    var detail = (reason && reason.stack) ? reason.stack : "";
    reportError(msg, detail);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
