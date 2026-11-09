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

  //: 档位 → 环的颜色。与 pill.css 里 .lv-* 的色值保持一致：同一块屏幕上
  //: 「环」和「数字」表达同一个状态，颜色不能打架。
  var LEVEL_COLOR = {
    good: "#12b51f",
    low: "#b57500",
    critical: "#d1541f",
    empty: "#cf222e",
    unknown: "#8a8f96"
  };

  var LEVEL_COLOR_DARK = {
    good: "#45e05a",
    low: "#e3b341",
    critical: "#ff8a5c",
    empty: "#ff7b72",
    unknown: "#b8bcc2"
  };

  var wrap = null;
  var api = null;
  var lastWidth = 0;
  var widthTimer = null;
  var isDark = false;
  //: 当前悬停的设备 key。重复上报同一台时不再打 api（避免每帧一次跨进程调用）。
  var hoverKey = null;

  function el(tag, cls) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    return e;
  }

  // ------------------------------------------------------------ 环形电量图标

  /**
   * 画一枚环形电量表（内联 SVG，不发请求）。
   *
   * 为什么内联而不是用 icon/*.svg：
   *   * 每台设备的弧长不同，静态文件表达不了；
   *   * 贴片页只有一个窗口、最多 4 个图标，内联省掉 4 次 HTTP + 一次
   *     onerror 兜底分支 —— 之前那套 `img.onerror → 换内联 SVG` 的双路径
   *     在 exe 里一旦路径不对就整块空掉，实测过。
   *
   * 几何：viewBox 24×24，半径 8.4、线宽 3.4（=外径 76%，和 app.ico 一致）。
   * 进度从 12 点方向顺时针 —— `rotate(-90)` 把 SVG 的 3 点起点转到 12 点。
   */
  function ringIcon(percent, level) {
    var palette = isDark ? LEVEL_COLOR_DARK : LEVEL_COLOR;
    var color = palette[level] || palette.unknown;
    var pct = (percent === null || percent === undefined) ? 0 : percent;
    pct = Math.max(0, Math.min(100, pct));

    var r = 8.4;
    var circ = 2 * Math.PI * r;
    var dash = circ * pct / 100;

    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");

    // 底环：整圈灰，表示 100% 的位置
    var base = document.createElementNS(ns, "circle");
    base.setAttribute("cx", "12");
    base.setAttribute("cy", "12");
    base.setAttribute("r", String(r));
    base.setAttribute("fill", "none");
    base.setAttribute("stroke", isDark ? "rgba(255,255,255,0.30)" : "rgba(0,0,0,0.26)");
    base.setAttribute("stroke-width", "3.4");
    svg.appendChild(base);

    // 进度弧
    if (pct > 0) {
      var arc = document.createElementNS(ns, "circle");
      arc.setAttribute("cx", "12");
      arc.setAttribute("cy", "12");
      arc.setAttribute("r", String(r));
      arc.setAttribute("fill", "none");
      arc.setAttribute("stroke", color);
      arc.setAttribute("stroke-width", "3.4");
      arc.setAttribute("stroke-linecap", "butt");
      arc.setAttribute("stroke-dasharray", dash.toFixed(2) + " " + circ.toFixed(2));
      // -90°：把弧的起点从 3 点转到 12 点
      arc.setAttribute("transform", "rotate(-90 12 12)");
      svg.appendChild(arc);
    }

    // 环内闪电（和 app.ico 同一个折线）
    var bolt = document.createElementNS(ns, "path");
    bolt.setAttribute("d", "M14.9 5.4 L9.6 12.3 L12.4 12.3 L11.4 18.6 L16.3 11.3 L13.5 11.3 Z");
    bolt.setAttribute("fill", color);
    svg.appendChild(bolt);

    return svg;
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
    for (var i = 0; i < kids.length; i++) {
      total += kids[i].getBoundingClientRect().width;
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

  // ------------------------------------------------------------ 悬停

  /**
   * 把悬停状态报给 Python。
   *
   * 后端会据此弹一个独立的 Win32 气泡（设备名 + 型号/连接/电量）。
   * 这里**不做任何本地 tooltip** —— WebView2 画不出被 SetWindowRgn 裁掉的
   * 窗口边界之外，本地 tooltip 一定看不见。
   *
   * 去重：同一台设备连续 mouseenter 只上报一次；mouseleave 只在"确实悬停着"
   * 时上报，避免指针在格子之间移动时把气泡收掉又立刻弹出来（会闪）。
   */
  function reportHover(key, hovering) {
    if (!api || !api.set_pill_hover) { return; }
    if (hovering) {
      if (hoverKey === key) { return; }
      hoverKey = key;
      api.set_pill_hover(key, true);
    } else {
      if (hoverKey === null) { return; }
      hoverKey = null;
      api.set_pill_hover("", false);
    }
  }

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
    ico.appendChild(ringIcon(pct, level));

    var txt = el("span", "pill-pct " + cls);
    txt.textContent = (pct === null || pct === undefined) ? "—" : (pct + "%");

    pill.appendChild(ico);
    pill.appendChild(txt);

    pill.addEventListener("click", function () {
      if (api && api.open_window) { api.open_window(); }
    });
    // 指针进出都上报；格子之间移动会先 leave 再 enter，靠后端去重吸收
    pill.addEventListener("mouseenter", function () {
      reportHover(dev.key || "", true);
    });
    pill.addEventListener("mouseleave", function () {
      reportHover("", false);
    });

    return pill;
  }

  function render(devices) {
    if (!wrap) { return; }
    var online = (devices || []).filter(function (d) {
      return d.status === "online";
    });

    wrap.innerHTML = "";
    hoverKey = null;
    if (api && api.set_pill_hover) { api.set_pill_hover("", false); }

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
