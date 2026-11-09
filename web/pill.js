/* 任务栏电量贴片 —— 单行「图标 + 数值」，多设备并列
 *
 * 约束（都是需求点名的）：
 *   * 扁平样式：没有胶囊底色、没有描边、没有圆角，就是写在任务栏上的字
 *     （对齐 LiteMonitor 的任务栏显示；用户要求**单行**）
 *   * 图标 = 环形电量表：灰底环 + 绿色进度弧 + 环内闪电，按**这台设备
 *     自己的**电量画弧长、按档位换色（需求 3 的形状）
 *   * 多设备**并列**在同一行，不换行、不省略到只剩一台（需求 3）
 *   * 点任意一处打开设备列表窗口
 *   * 悬停把「现在悬停的是哪台」报给 Python，由它弹独立的 Win32 气泡
 *     （HTML tooltip 会被 SetWindowRgn 裁掉，见 tipwindow.py 的说明）
 *
 * 数据来源：/api/pill（轻量接口，只返回贴片和气泡需要的那几个字段）。
 */

(function () {
  "use strict";

  var POLL_MS = 5000;
  //: 一行最多画几台。再多就把窗口撑得比任务栏空位还宽，反而挡住托盘图标。
  //: 超出的收进「+N」。
  var MAX_PILLS = 4;

  var LEVEL_CLASS = {
    good: "lv-good",
    low: "lv-low",
    critical: "lv-critical",
    empty: "lv-empty",
    unknown: "lv-unknown"
  };

  var wrap = null;
  var api = null;
  var lastWidth = 0;
  var widthTimer = null;
  var isDark = false;

  function el(tag, cls) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    return e;
  }

  // ------------------------------------------------------------ 设备图标

  /**
   * 取一台设备的**类型图标**（鼠标 / 键盘 / 耳机 / 手柄…）。
   *
   * 需求：贴片要显示"这是什么设备"，而不是一个和电量绑定的环形表。
   * 电量已经由旁边的数字表达了，图标再画一遍电量是重复信息；用设备形状
   * 反而能一眼看出"这一格是鼠标、那一格是耳机"——多设备并列时尤其重要。
   *
   * 图标来自 ``icon/<kind>.svg``（Lucide，ISC 许可，stroke=currentColor），
   * 由后端 ``/api/pill`` 的 ``icon`` 字段给名字。加载失败时退回内置的
   * 极简兜底形状，绝不留白（空图标在 16px 下就是一个洞）。
   */
  var DEV_FALLBACK = {
    mouse: '<rect x="5" y="2" width="14" height="20" rx="7"/><path d="M12 6v4"/>',
    keyboard: '<rect width="20" height="16" x="2" y="4" rx="2"/>' +
              '<path d="M6 8h.01M10 8h.01M14 8h.01M18 8h.01M8 12h.01M12 12h.01M16 12h.01M7 16h10"/>',
    headset: '<path d="M3 14h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a9 9 0 0 1 18 0v7a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3"/>',
    gamepad: '<rect width="20" height="12" x="2" y="6" rx="2"/>' +
             '<path d="M6 12h4M8 10v4"/>' +
             '<path d="M15 13h.01M18 11h.01"/>',
    unknown: '<rect x="4" y="7" width="16" height="10" rx="2"/>'
  };

  /** 把 icon 名字规范成兜底表里的键。后端给的是 ``kind``/``icon`` 字段。 */
  function iconKey(name) {
    var k = String(name || "").toLowerCase();
    if (k === "headphones" || k === "headphone" || k === "earbud" ) { return "headset"; }
    if (k === "keyboard" || k === "mouse" || k === "gamepad" || k === "headset") { return k; }
    return "unknown";
  }

  /**
   * 造一个设备图标节点。
   *
   * 优先用 ``<img src="/icon/<name>.svg">``（后端能换图标文件，不用改 JS）；
   * 加载失败时替换成内联 SVG 兜底。两者尺寸/描边完全一致 —— 图标是
   * ``stroke=currentColor``，颜色跟着数字所在行的前景走，深浅主题自动适配。
   */
  function deviceIcon(name) {
    var key = iconKey(name);
    var holder = el("span", "dev-ico");

    var img = document.createElement("img");
    img.alt = "";
    img.width = 16;
    img.height = 16;
    img.src = "/icon/" + (key === "unknown" ? "unknown" : key) + ".svg";
    img.onerror = function () {
      var ns = "http://www.w3.org/2000/svg";
      var svg = document.createElementNS(ns, "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("aria-hidden", "true");
      svg.innerHTML = DEV_FALLBACK[key] || DEV_FALLBACK.unknown;
      if (img.parentNode) { img.parentNode.replaceChild(svg, img); }
    };
    holder.appendChild(img);
    return holder;
  }

  // ------------------------------------------------------------ 底色推导

  /** 十六进制化。 */
  function hex(rgb) {
    return "#" + rgb.map(function (v) {
      var s = Math.max(0, Math.min(255, Math.round(v))).toString(16);
      return s.length < 2 ? "0" + s : s;
    }).join("");
  }

  /**
   * 把任务栏采样色写进 CSS 变量。
   *
   * 扁平样式**不做任何提亮**：窗口底色必须和任务栏完全一致，边界才会消失。
   * 早期版本提亮 2~6 级（那是给胶囊做层次感），在这个样式下会立刻暴露成
   * 「任务栏上一块淡淡的方印子」。区分元素交给文字与图形本身。
   *
   * **渐变**：任务栏横向有 8~20 级的渐变（实测通知区附近 x=1700 是
   * (209,221,221)、x=2000 已是 (230,234,234)），贴片宽 90px 就横跨这段。
   * 填一个纯色，无论取哪个点，总有一条边对不上 —— 所以后端给一组沿宽度的
   * 采样点（``profile``），这里直接画成 ``linear-gradient(to right, ...)``。
   * 拿不到 profile 才退回单色。
   *
   * hover/active 底色：扁平元素套一个明显的 hover 底色就又变回胶囊了，
   * 所以只改 opacity（在 CSS 里），这三个变量保持与底色一致。
   */
  function applyPalette(barBg, dark, profile) {
    var root = document.documentElement;
    var base;
    if (!barBg) {
      base = dark ? [43, 43, 43] : [233, 234, 235];
    } else {
      base = [barBg[0], barBg[1], barBg[2]];
    }

    var bgCss = hex(base);
    if (profile && profile.length >= 2) {
      var stops = profile.map(function (s) {
        return "rgb(" + s[1] + "," + s[2] + "," + s[3] + ") " +
               Math.round(s[0] * 1000) / 10 + "%";
      });
      bgCss = "linear-gradient(to right, " + stops.join(", ") + ")";
    }

    root.style.setProperty("--pill-bg", bgCss);
    root.style.setProperty("--pill-bg-hover", bgCss);
    root.style.setProperty("--pill-bg-active", bgCss);
    root.style.setProperty("--pill-text", dark ? "#f2f2f2" : "#1c1f21");
    root.style.setProperty("--pill-dim", dark ? "#b8bcc2" : "#5c6169");
    document.body.style.background = bgCss;
  }

  // ------------------------------------------------------------ 量宽

  /**
   * 量出所有设备的实际总宽度，报告给 Python 改窗口几何。
   *
   * 必须量：窗口是按固定宽度建的，内容只有那么宽。不跟着收窄的话，两边
   * 的空白虽然和任务栏同色看不见，**但仍然占着任务栏的点击区域**，会挡住
   * 下面真正的托盘图标和 ^ 箭头。
   *
   * 不能读 wrap.scrollWidth：.pill-wrap 是 width:100% 的 flex 容器，
   * scrollWidth 恒等于窗口宽度，永远是初始值。必须累加子元素宽度 +
   * gap + 左右 padding。
   */
  function reportSize() {
    if (!wrap) { return; }
    var kids = wrap.children;
    if (!kids.length) { return; }

    var total = 0;
    var segments = [];
    for (var i = 0; i < kids.length; i++) {
      var box = kids[i].getBoundingClientRect();
      total += box.width;
      // 只把**设备位**报给后端（「+N」「无设备」不是设备，悬停它们没意义）
      var key = kids[i].dataset ? kids[i].dataset.key : "";
      if (key) {
        segments.push([key, box.left, box.width]);
      }
    }

    var cs = window.getComputedStyle(wrap);
    var gap = parseFloat(cs.columnGap || cs.gap);
    if (isNaN(gap)) { gap = 0; }
    if (kids.length > 1) { total += gap * (kids.length - 1); }

    var padL = parseFloat(cs.paddingLeft) || 0;
    var padR = parseFloat(cs.paddingRight) || 0;
    total += padL + padR;

    var w = Math.ceil(total);
    if (w && w !== lastWidth) {
      lastWidth = w;
      if (api && api.set_pill_width) { api.set_pill_width(w); }
    }
    reportSegments(segments);
  }

  //: 上一次上报的分段，避免每 60ms 重复跨进程调用
  var lastSegSig = "";

  /**
   * 把每个设备位的横坐标/宽度报给后端。
   *
   * 后端靠它把「光标 x」换算成「哪台设备」—— 悬停气泡**不再依赖网页的
   * mouseenter**：贴片窗是任务栏子窗口 + WS_EX_NOACTIVATE，实测 WebView2
   * 在这种窗口里收不到鼠标进出事件（前端 mouseenter 一次都不触发，往
   * set_pill_hover 里打日志从头到尾是空的）。所以前端只上报"布局"，
   * 判断"在不在、在谁身上"由后端量真实光标做。
   *
   * 坐标用 getBoundingClientRect 的视口坐标 —— 贴片页的视口就是窗口本身，
   * 所以它天然就是"相对贴片左边缘"的像素，后端再乘 DPI 换算成屏幕坐标。
   */
  function reportSegments(segments) {
    if (!api || !api.set_pill_segments) { return; }
    var sig = JSON.stringify(segments);
    if (sig === lastSegSig) { return; }
    lastSegSig = sig;
    api.set_pill_segments(segments);
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

  // ---------------------------------------------------------- 悬停（后端驱动）

  /* 悬停提示**完全由后端驱动**：后端命令泵每 0.2s 量一次真实光标位置，
     判断指针在不在贴片矩形里、落在哪个设备位上，然后自己弹 tipwindow。

     前端在这里只做一件事：把每台设备占的横坐标/宽度报上去
     （reportSegments），否则后端不知道"光标在这台身上"是哪一台。

     **不要**在贴片上加 mouseenter/mouseleave —— 实测那几个事件在任务栏
     子窗口里一次都不触发（原因见 reportSegments 的注释）。
  */

  // ------------------------------------------------------------ 渲染

  function makePill(dev) {
    var pct = (dev.battery && dev.battery.percent !== undefined)
      ? dev.battery.percent : null;
    var level = dev.level || "unknown";
    var cls = LEVEL_CLASS[level] || LEVEL_CLASS.unknown;

    var pill = el("button", "pill");
    pill.type = "button";
    pill.dataset.key = dev.key || "";

    var ico = el("span", "dev-ico");
    ico.appendChild(deviceIcon(dev.icon || dev.kind));

    var txt = el("span", "pill-pct " + cls);
    txt.textContent = (pct === null || pct === undefined) ? "—" : (pct + "%");

    pill.appendChild(ico);
    pill.appendChild(txt);

    pill.addEventListener("click", function () {
      if (api && api.open_window) { api.open_window(); }
    });
    return pill;
  }

  function render(devices) {
    if (!wrap) { return; }
    var online = (devices || []).filter(function (d) {
      return d.status === "online";
    });

    wrap.innerHTML = "";
    reportSegments([]);

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

  /** 按任务栏高度选字号档位，并按采样到的任务栏底色推贴片底色。
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

      var darkChanged = (dark !== isDark);
      isDark = dark;

      // 底色每次都要同步（用户可能换了壁纸/主题），所以不能跟 cls 一起早退
      applyPalette(m.background, dark, m.profile);

      if (cls !== _barClass) {
        _barClass = cls;
        document.body.className = "pill-page " + cls;
        scheduleReportSize();
      }
      // 深色切换会改变环的颜色，必须重画（否则弧还是旧色）
      if (darkChanged) { poll(); }
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
