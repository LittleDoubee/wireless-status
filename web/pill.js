/* 任务栏电量贴片 —— 每个在线设备一格，整体是一枚圆角矩形
 *
 * 约束（都是需求点名的）：
 *   * 最多展示 4 个，超出用「+N」
 *   * 颜色分档与列表页一致（共用 style.css 的 --lv-*）
 *   * 悬停显示「设备名称 + 今日用电量」，用 **HTML tooltip**（不用系统气泡）
 *   * 点格子打开设备列表窗口
 *   * 底色跟任务栏走（运行时采样），不做成一块白补丁
 *
 * 数据来源：/api/pill（轻量接口，只返回贴片需要的那几个字段）。
 */

(function () {
  "use strict";

  var POLL_MS = 5000;
  var MAX_PILLS = 4;

  var LEVEL_CLASS = {
    good: "lv-good",
    low: "lv-low",
    critical: "lv-critical",
    empty: "lv-empty",
    unknown: "lv-unknown"
  };

  // 设备类型 → 内联 SVG（icon/ 缺失时兜底）。实心 + 右上角绿色闪电，
  // 和 icon/*.svg 保持同一套视觉。
  var BOLT = '<path fill="#2da44e" d="M19.1 2.2 15 8.9h2.8l-1 5 4.5-7.1h-2.9l.7-4.6Z"/>';
  var SVG = {
    mouse:
      '<svg viewBox="0 0 24 24">' +
      '<path fill="currentColor" d="M11 2.4C8.24 2.4 6 4.64 6 7.4v9.2c0 2.76 2.24 5 5 5s5-2.24 5-5V7.4c0-2.76-2.24-5-5-5Z"/>' +
      '<path fill="#fff" d="M11 4.6c.62 0 1.12.5 1.12 1.12v2.5c0 .62-.5 1.12-1.12 1.12s-1.12-.5-1.12-1.12v-2.5c0-.62.5-1.12 1.12-1.12Z"/>' +
      BOLT + '</svg>',
    headset:
      '<svg viewBox="0 0 24 24">' +
      '<path fill="currentColor" d="M12 2.4C7.25 2.4 3.4 6.25 3.4 11v2.9c0 1.1.9 2 2 2h1.2c1.1 0 2-.9 2-2V11c0-1.88 1.52-3.4 3.4-3.4S15.4 9.12 15.4 11v2.9c0 1.1.9 2 2 2h1.2c1.1 0 2-.9 2-2V11c0-4.75-3.85-8.6-8.6-8.6Z"/>' +
      '<path fill="currentColor" d="M3.6 15.9h3.4c.72 0 1.3.58 1.3 1.3v2.9c0 .72-.58 1.3-1.3 1.3H3.6c-.72 0-1.3-.58-1.3-1.3v-2.9c0-.72.58-1.3 1.3-1.3ZM17 15.9h3.4c.72 0 1.3.58 1.3 1.3v2.9c0 .72-.58 1.3-1.3 1.3H17c-.72 0-1.3-.58-1.3-1.3v-2.9c0-.72.58-1.3 1.3-1.3Z"/>' +
      BOLT + '</svg>',
    gamepad:
      '<svg viewBox="0 0 24 24">' +
      '<path fill="currentColor" d="M6.9 7.4h10.2c1.95 0 3.65 1.4 4 3.32l1.2 6.4c.28 1.5-.86 2.88-2.38 2.88-.86 0-1.66-.46-2.1-1.2l-1.15-1.95a1.6 1.6 0 0 0-1.38-.8h-6.58a1.6 1.6 0 0 0-1.38.8L7.18 18.8c-.44.74-1.24 1.2-2.1 1.2-1.52 0-2.66-1.38-2.38-2.88l1.2-6.4A3.9 3.9 0 0 1 6.9 7.4Z"/>' +
      BOLT + '</svg>',
    unknown:
      '<svg viewBox="0 0 24 24">' +
      '<path fill="currentColor" d="M6.5 8.5h10a3.5 3.5 0 0 1 3.5 3.5v1a3.5 3.5 0 0 1-3.5 3.5h-10A3.5 3.5 0 0 1 3 13v-1a3.5 3.5 0 0 1 3.5-3.5Z"/>' +
      BOLT + '</svg>'
  };

  var wrap = null;
  var api = null;
  var tip = null;
  var lastWidth = 0;
  var lastHeight = 0;
  var widthTimer = null;

  function el(tag, cls) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    return e;
  }

  // ------------------------------------------------------------ 底色推导

  /** 十六进制化。 */
  function hex(rgb) {
    return "#" + rgb.map(function (v) {
      var s = Math.max(0, Math.min(255, Math.round(v))).toString(16);
      return s.length < 2 ? "0" + s : s;
    }).join("");
  }

  function shift(rgb, delta) {
    return [rgb[0] + delta, rgb[1] + delta, rgb[2] + delta];
  }

  /**
   * 从任务栏采样色推导贴片用的四个颜色。
   *
   * 为什么不用固定色：Win11 任务栏是亚克力材质，底色跟着壁纸、主题、
   * 「透明效果」开关一起变（实测同一台机器可以从 (216,216,216) 变到
   * (252,252,252)）。固定 #f7f8fa 在浅色壁纸上是「一块白补丁」，在深色
   * 壁纸上更离谱。系统原生的托盘图标分组框也是这么做的：比任务栏略亮
   * 一点点，靠极淡的描边区分层次。
   *
   * 亮度阈值 128 用来判断深浅：深色任务栏上要提亮才是「浮起」，
   * 浅色任务栏上也是提亮（往白走），但幅度更小，免得又变成白块。
   */
  function applyPalette(barBg, dark) {
    var root = document.documentElement;
    var base;
    if (!barBg) {
      // 采样失败：按深浅给个保守值
      base = dark ? [43, 43, 43] : [233, 234, 235];
    } else {
      // near 采样（贴片左右邻域）已经采到「贴片位置本该是什么颜色」，
      // 所以**几乎不再提亮**（+2）。提亮 6 会把自己变成一个比周围都亮的
      // 亮点 —— 实测贴片 (228,253,253) vs 周围 (229,245,246)，看着更扎眼。
      // 区分「一块」和「一片」交给 body 的 inset 1px 描边，这才是系统原生
      // 分组框的做法（参考图里框内 (237,237,237) vs 框外 (232,233,234)）。
      var lum = (barBg[0] * 299 + barBg[1] * 587 + barBg[2] * 114) / 1000;
      base = shift(barBg, dark ? 6 : (lum > 128 ? 2 : 4));
    }
    root.style.setProperty("--pill-bg", hex(base));
    root.style.setProperty("--pill-bg-hover", hex(shift(base, dark ? 12 : -8)));
    root.style.setProperty("--pill-bg-active", hex(shift(base, dark ? 20 : -14)));
    root.style.setProperty("--pill-border",
      dark ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.08)");
    root.style.setProperty("--pill-text", dark ? "#f2f2f2" : "#1c1f21");
    root.style.setProperty("--pill-dim", dark ? "#b8bcc2" : "#5c6169");
    // body 的背景也要跟着走（窗口底色 = 胶囊底色）
    document.body.style.background = hex(base);
  }

  // ------------------------------------------------------------ 量宽

  /**
   * 量出所有格子的实际宽度，报告给 Python 改窗口几何。
   *
   * 必须量：窗口是按固定宽度建的，格子只有内容那么宽。不跟着收窄的话，
   * 窗口左边那段空白虽然被 SetWindowRgn 裁掉看不见，**但仍然占着任务栏的
   * 点击区域**，会挡住下面真正的托盘图标。
   *
   * 不能读 wrap.scrollWidth：.pill-wrap 是 width:100% 的 flex 容器，
   * scrollWidth 恒等于窗口宽度，永远是初始值。必须累加子元素宽度。
   */
  function reportSize() {
    if (!wrap) { return; }
    var kids = wrap.children;
    if (!kids.length) { return; }
    var total = 0;
    for (var i = 0; i < kids.length; i++) {
      total += kids[i].getBoundingClientRect().width;
    }
    var gap = 0;
    try {
      var cs = window.getComputedStyle(wrap);
      var g = parseFloat(cs.columnGap || cs.gap);
      if (!isNaN(g)) { gap = g; }
    } catch (e) { /* 用默认值 */ }
    if (kids.length > 1) { total += gap * (kids.length - 1); }

    var w = Math.ceil(total);
    if (w && w !== lastWidth) {
      lastWidth = w;
      if (api && api.set_pill_width) { api.set_pill_width(w); }
    }
  }

  function scheduleReportSize() {
    if (widthTimer) { clearTimeout(widthTimer); }
    widthTimer = setTimeout(function () {
      widthTimer = null;
      if (window.requestAnimationFrame) {
        window.requestAnimationFrame(reportSize);
      } else {
        reportSize();
      }
    }, 60);
  }

  // ------------------------------------------------------------ 渲染

  function makePill(dev) {
    var pct = dev.battery && dev.battery.percent;
    var level = LEVEL_CLASS[dev.level] || LEVEL_CLASS.unknown;

    var pill = el("button", "pill");
    pill.type = "button";
    pill.dataset.key = dev.key || "";

    var ico = el("span", "dev-ico");
    var img = document.createElement("img");
    img.src = "/icon/" + (dev.icon || dev.kind || "unknown") + ".svg";
    img.alt = "";
    img.onerror = function () {
      var holder = el("span", "ico-fallback");
      holder.innerHTML = SVG[dev.icon || dev.kind] || SVG.unknown;
      if (img.parentNode) { img.parentNode.replaceChild(holder, img); }
    };
    ico.appendChild(img);

    var txt = el("span", "pill-pct " + level);
    txt.textContent = (pct === null || pct === undefined) ? "—" : (pct + "%");

    pill.appendChild(ico);
    pill.appendChild(txt);

    pill.addEventListener("click", function () {
      if (api && api.open_window) { api.open_window(); }
    });
    pill.addEventListener("mouseenter", function () { showTip(pill, dev); });
    pill.addEventListener("mouseleave", hideTip);

    return pill;
  }

  function showTip(anchor, dev) {
    if (!tip) {
      tip = el("div", "chart-tip");
      document.body.appendChild(tip);
    }
    var used = (dev.today_used === null || dev.today_used === undefined)
      ? "—" : (dev.today_used + "%");
    tip.textContent = (dev.display_name || dev.name || "设备") +
                      " · 今日用电 " + used;
    tip.hidden = false;

    var rect = anchor.getBoundingClientRect();
    var tipRect = tip.getBoundingClientRect();
    var left = Math.max(2, Math.min(rect.left, window.innerWidth - tipRect.width - 2));
    // 贴片贴在任务栏里，tooltip 必须往上弹，否则被任务栏盖住
    var top = rect.top - tipRect.height - 4;
    if (top < 0) { top = rect.bottom + 4; }
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }

  function hideTip() {
    if (tip) { tip.hidden = true; }
  }

  function render(devices) {
    if (!wrap) { return; }
    var online = (devices || []).filter(function (d) {
      return d.status === "online";
    });

    wrap.innerHTML = "";
    hideTip();

    if (!online.length) {
      var empty = el("span", "pill-more");
      empty.textContent = "无设备";
      wrap.appendChild(empty);
      scheduleReportSize();
      return;
    }

    online.slice(0, MAX_PILLS).forEach(function (dev) {
      wrap.appendChild(makePill(dev));
    });

    if (online.length > MAX_PILLS) {
      var more = el("span", "pill-more");
      more.textContent = "+" + (online.length - MAX_PILLS);
      more.title = online.slice(MAX_PILLS).map(function (d) {
        return d.display_name || d.name || "设备";
      }).join("\n");
      wrap.appendChild(more);
    }
    scheduleReportSize();
  }

  function poll() {
    fetch("/api/pill", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) { throw new Error("HTTP " + res.status); }
        return res.json();
      })
      .then(function (data) { render((data && data.devices) || []); })
      .catch(function () {
        // 贴片失败不弹错误（用户没在看它），静默下一拍再试
      });
  }

  // ------------------------------------------------------------ 任务栏度量

  /** 按任务栏高度选字号档位，并按采样到的任务栏底色推胶囊色。
   *
   * 档位存在的理由：Windows 任务栏有大小图标两档，加上不同 DPI 会得到
   * 40/48/60 几种物理高度。字号写死会在小任务栏里顶边、在大任务栏里发空。
   */
  var _barClass = "";

  function applyTaskbarMetrics() {
    if (!api || !api.taskbar_metrics) { return; }
    api.taskbar_metrics().then(function (m) {
      if (!m) { return; }
      var dark = !!m.dark;
      var h = m.logical_height || 0;
      var cls = h >= 56 ? "bar-xl" : (h >= 44 ? "bar-lg" : "bar-sm");
      if (dark) { cls += " bar-dark"; }

      // 底色每次都要同步（用户可能换了壁纸/主题），所以不能跟 cls 一起早退
      applyPalette(m.background, dark);

      if (cls !== _barClass) {
        _barClass = cls;
        document.body.className = "pill-page " + cls;
        scheduleReportSize();
      }
    }).catch(function () { /* 拿不到度量就用默认档位 */ });
  }

  function boot() {
    wrap = document.getElementById("pill-wrap");
    poll();
    setInterval(poll, POLL_MS);
    scheduleReportSize();
    window.addEventListener("resize", scheduleReportSize);
    applyTaskbarMetrics();
    // 任务栏高度/主题/壁纸都可能变，低频复核
    setInterval(applyTaskbarMetrics, 30000);
  }

  window.addEventListener("pywebviewready", function () {
    api = window.pywebview && window.pywebview.api ? window.pywebview.api : null;
    applyTaskbarMetrics();
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
