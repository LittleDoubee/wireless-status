# -*- coding: utf-8 -*-
"""生成应用图标：环形电量表（灰底环 + 绿色进度弧）+ 环内绿色闪电

需求来源：用户给的参考图（一张环形电量表 + 中心闪电的图标），要求「不套深色
圆角方块，只要环 + 闪电」。所以这里画的是**透明背景**上的两件东西：

  1. 一整圈灰色圆环 —— 电量表的「底环」，表示 100% 的位置；
  2. 从 12 点方向顺时针的一段绿色圆弧 —— 电量进度；
  3. 环正中的绿色闪电。

为什么用 Pillow 现画而不是塞一张 png：
  * 托盘要 16/20/24/32/40/48，exe 要 256，一共 7 档。手上一张 png 缩放出来
    的小图边缘全是糊的，而每一档从矢量参数重绘再降采样才干净；
  * 以后要改颜色/进度只改这里的常量，不用求人重画。

用法：
    python tools/make_icon.py            # 写 icon/app.ico + icon/app.png
    python tools/make_icon.py --preview  # 额外出 icon/_preview.png 看各档效果

依赖 Pillow（只在生成图标时需要，运行时不依赖）：
    python -m pip install pillow
"""

import os
import sys

GRID = 1024          # 超采样画布边长：先大后缩，小尺寸才有抗锯齿
GREEN = (19, 212, 72, 255)      # 取自参考图的绿 #14D448
GREY = (127, 128, 130, 255)     # 取自参考图的环灰 #7F8083
FILL = 0.72          # 进度弧占比（示意值，不是实时电量）
GAP_DEG = 0.0        # 两段之间不留缺口：底环 + 进度弧首尾相接才像电量表


def _ring(im, draw, radius, width, color, start_deg, end_deg):
    """画一段圆环。

    角度用 **12 点方向为 0、顺时针为正**（和电量表的直觉一致），
    换成 Pillow 的「3 点方向为 0、顺时针」就是 ``deg - 90``。
    """
    box = (im.width / 2 - radius, im.height / 2 - radius,
           im.width / 2 + radius, im.height / 2 + radius)
    draw.arc(box, start=start_deg - 90, end=end_deg - 90,
             fill=color, width=width)


def _bolt(draw, cx, cy, size, widen=1.0):
    """环内的闪电。

    归一化坐标（0..1 的方框，x 向右、y 向下），照着参考图的折线取的点：
    右上起笔往左下斜、中段横向折回、再往右下收尾。乘以 ``size`` 后整体
    居中到 ``(cx, cy)``。

    ``widen`` 只横向拉伸。16px 上闪电本身只剩 3~4 像素，按原比例画出来
    会糊成一粒绿点；拉宽一点才看得出是闪电。
    """
    pts = [
        (0.60, 0.02),   # 右上尖
        (0.18, 0.56),   # 左下（长斜边）
        (0.45, 0.56),   # 中段折回
        (0.38, 0.98),   # 下方尖
        (0.82, 0.42),   # 右上折回
        (0.54, 0.42),   # 中段收口
    ]
    draw.polygon([(cx + (px - 0.5) * size * widen, cy + (py - 0.5) * size)
                  for px, py in pts], fill=GREEN)


def render(size):
    """渲染一档尺寸，返回 RGBA Image。

    所有几何量都按 ``size`` 的比例给，所以 16px 和 256px 长得一样，
    只是线宽像素不同 —— 不能按固定像素写，否则小图上环会糊成一坨。
    """
    from PIL import Image, ImageDraw

    im = Image.new("RGBA", (GRID, GRID), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    # 环的几何：外径 95% 画布（四周留一点给抗锯齿），线宽占外径的 13%。
    # 参考图里环相当粗；细了在 16px 上就只剩一圈灰边、认不出是电量表。
    outer = GRID * 0.475
    # 小尺寸（16/20/24）单独加粗环、缩小闪电：这几档最终只有 2~3 像素的线宽，
    # 按 13% 画出来灰底环会被绿色盖掉、只剩一个小绿点。粗一点才看得出「环」。
    small = size <= 24
    ring_w = int(round(GRID * (0.155 if small else 0.125)))
    mid = outer - ring_w / 2.0

    _ring(im, d, mid, ring_w, GREY, 0, 360)
    if FILL > 0:
        _ring(im, d, mid, ring_w, GREEN, 0, 360 * FILL - GAP_DEG)

    # 闪电：大尺寸按参考图比例；16/20/24 这几档拉宽一点、稍微放大，
    # 否则缩小后是一粒绿点，认不出是闪电。
    if small:
        _bolt(d, GRID / 2.0, GRID / 2.0, GRID * 0.34, widen=1.35)
    else:
        _bolt(d, GRID / 2.0, GRID / 2.0 + GRID * 0.01, GRID * 0.33)

    return im.resize((size, size), Image.LANCZOS)


def main(argv):
    from PIL import Image

    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(os.path.dirname(here), "icon")
    os.makedirs(out_dir, exist_ok=True)

    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    imgs = {s: render(s) for s in sizes}

    # .ico 里必须**按尺寸从小到大**排列，Windows 挑图标的逻辑依赖这个顺序
    # （顺序反了会拿 256 去缩成 16，托盘里糊成一团）。
    ico = os.path.join(out_dir, "app.ico")
    imgs[256].save(ico, format="ICO",
                   sizes=[(s, s) for s in sizes])
    png = os.path.join(out_dir, "app.png")
    imgs[256].save(png, format="PNG")
    print("写出 %s（%d 档：%s）" % (ico, len(sizes), ", ".join(map(str, sizes))))
    print("写出 %s" % png)

    if "--preview" in argv:
        # 各档并排 + 放大 4 倍，肉眼确认 16px 还能不能认出「环 + 闪电」
        pad = 12
        total = sum(s * 4 + pad for s in sizes) + pad
        sheet = Image.new("RGBA", (total, 256 + pad * 2), (243, 243, 243, 255))
        x = pad
        for s in sizes:
            big = imgs[s].resize((s * 4, s * 4), Image.NEAREST)
            sheet.alpha_composite(big, (x, pad))
            x += s * 4 + pad
        prev = os.path.join(out_dir, "_preview.png")
        sheet.save(prev)
        print("写出预览 %s" % prev)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
