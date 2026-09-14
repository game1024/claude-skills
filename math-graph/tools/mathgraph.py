#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数学函数图像渲染（300x300 · 轻手绘）

按 spec 文件绘制函数曲线、点及带箭头的坐标轴，输出 PNG。
默认：无网格线、曲线半透明、点标注在点右侧、坐标轴带箭头并标 O。
spec 文件格式见 references/spec.md。

用法:
  python mathgraph.py parabola.graph.py                    # 输出同目录 parabola.png
  python mathgraph.py parabola.graph.py -o out/demo.png    # 指定输出路径
  python mathgraph.py parabola.graph.py --seed 7           # 换一种抖动形态
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import matplotlib
    matplotlib.use("Agg")
    import numpy as np
    import matplotlib.pyplot as plt
    from matplotlib.patheffects import withStroke
except ImportError:
    print("未安装 matplotlib，无法绘图。请先执行：pip install matplotlib", file=sys.stderr)
    sys.exit(1)

from mgconfig import (
    FIGSIZE, DPI, SUBPLOT,
    SKETCH, SKETCH_AXIS, ALPHA_CURVE, HALO_WIDTH, LABEL_PAD,
    POINT_LABEL_OFFSET, LW_CURVE, LW_AXIS,
    COLORS, COLOR_AXIS, COLOR_POINT,
    MATHTEXT_FONTSET, MATHTEXT_DEFAULT, CJK_FONT,
    AUTO_X_RANGE, AUTO_SAMPLES, AUTO_PAD, AUTO_CLIP_PCT,
    SEGMENT_LENGTH,
)

# 标注文字背后的白色光晕：曲线穿过文字时保证字仍清晰
HALO = [withStroke(linewidth=HALO_WIDTH, foreground="white")]

# 允许在 spec 的表达式里使用的数学函数（不给 builtins，避免任意代码执行）
_ALLOWED = (
    "sin cos tan arcsin arccos arctan sinh cosh tanh "
    "exp log log2 log10 sqrt abs sign floor ceil "
    "power minimum maximum where pi e"
).split()


def _ns(**vars):
    """构造表达式求值命名空间：numpy 数学函数 + 变量。"""
    ns = {name: getattr(np, name) for name in _ALLOWED}
    ns["__builtins__"] = {}
    ns.update(vars)
    return ns


def _need(cond, msg):
    """校验失败时以中文提示退出（说明怎么改，而不只报错）。"""
    if not cond:
        print("spec 文件有误：%s" % msg, file=sys.stderr)
        sys.exit(1)


def _eval(expr, **vars):
    """求值 spec 里的表达式，失败时给出中文提示。"""
    try:
        return np.asarray(eval(expr, _ns(**vars)), dtype=float)
    except Exception as e:
        print("spec 文件有误：表达式 %r 无法求值（%s）" % (expr, e), file=sys.stderr)
        sys.exit(1)


def load_spec(path):
    """读取 spec 文件里的模块级变量（xlim/ylim/curves/points）。"""
    if not os.path.isfile(path):
        print("找不到 spec 文件：%s" % path, file=sys.stderr)
        sys.exit(1)

    ns = {"__name__": "mathgraph_spec", "__file__": os.path.abspath(path)}
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    try:
        exec(compile(src, path, "exec"), ns)
    except Exception as e:
        print("spec 文件执行失败：%s\n  位置：%s" % (e, path), file=sys.stderr)
        sys.exit(1)

    spec = {
        "xlim": ns.get("xlim"),
        "ylim": ns.get("ylim"),
        "aspect": ns.get("aspect"),
        "curves": ns.get("curves") or [],
        "points": ns.get("points") or [],
    }
    _need(isinstance(spec["curves"], (list, tuple)),
          "curves 应为列表，例如 curves = [dict(expr=\"x**2\")]")
    _need(isinstance(spec["points"], (list, tuple)),
          "points 应为列表，例如 points = [dict(at=(0, 0), label=\"$p$\")]")
    return spec


def classify(curve):
    """判别曲线形式：expr（显式）/ fxy（隐函数）/ 参数方程。"""
    _need(isinstance(curve, dict), "curves 的每一项都应是 dict，例如 dict(expr=\"x**2\")")
    for key in ("expr", "fxy"):
        if curve.get(key) is not None:
            return "explicit" if key == "expr" else "implicit"
    if all(curve.get(k) is not None for k in ("x", "y", "t")):
        return "parametric"
    print(
        "spec 文件有误：曲线缺少形式声明。三选一：\n"
        "  dict(expr=\"x**2\")                 # 显式 y=f(x)\n"
        "  dict(fxy=\"x**2 + y**2 - 4\")       # 隐函数 F(x,y)=0\n"
        "  dict(x=\"2*cos(t)\", y=\"2*sin(t)\", t=(-3.14, 3.14))  # 参数方程",
        file=sys.stderr,
    )
    sys.exit(1)


def auto_window(spec):
    """决定数据窗口：用户给了就用，否则按曲线采样自动定（分位裁剪，避开渐近线）。"""
    xlim = spec["xlim"] or AUTO_X_RANGE
    x = np.linspace(xlim[0], xlim[1], AUTO_SAMPLES)

    ys = []
    for curve in spec["curves"]:
        if classify(curve) == "explicit":
            a = _eval(curve["expr"], x=x)
            ys.append(a[np.isfinite(a)])

    ylim = spec["ylim"]
    if ylim is None and ys and any(a.size for a in ys):
        flat = np.concatenate([a for a in ys if a.size])
        lo, hi = np.percentile(flat, AUTO_CLIP_PCT)
        pad = (hi - lo) * AUTO_PAD
        ylim = (float(lo - pad), float(hi + pad))
    if ylim is None:
        ylim = AUTO_X_RANGE
    return tuple(xlim), tuple(ylim)


def axis_positions(xlim, ylim):
    """轴线位置：含 0 时过原点，否则贴到对应边上，避免轴跑到画布外。"""
    ax_y = 0.0 if ylim[0] <= 0 <= ylim[1] else ylim[0]
    ax_x = 0.0 if xlim[0] <= 0 <= xlim[1] else xlim[0]
    return ax_x, ax_y


def apply_equal_aspect(xlim, ylim):
    """aspect='equal' 时把窗口较短的一边扩到与长边等长，保证形状不被拉伸。

    坐标区是正方形，只有两轴跨度相等时数据单位才等长。圆、椭圆、参数曲线
    必须开这个，否则圆会被画成椭圆（跨度 7:5 时椭圆宽高比会从 1.50 变成 1.07）。
    只扩不缩，所以不会裁掉任何数据。
    """
    sx = (xlim[1] - xlim[0]) or 1.0
    sy = (ylim[1] - ylim[0]) or 1.0
    if abs(sx - sy) < 1e-9:
        return xlim, ylim
    if sx < sy:
        c = (xlim[0] + xlim[1]) / 2
        xlim = (c - sy / 2, c + sy / 2)
    else:
        c = (ylim[0] + ylim[1]) / 2
        ylim = (c - sx / 2, c + sx / 2)
    return xlim, ylim


def split_finite(pts):
    """按 nan 把折线拆成若干连续段（显式函数在渐近线处会断开）。"""
    ok = np.isfinite(pts).all(axis=1)
    runs, start = [], None
    for i, good in enumerate(ok):
        if good and start is None:
            start = i
        elif not good and start is not None:
            if i - start >= 2:
                runs.append(pts[start:i])
            start = None
    if start is not None and len(pts) - start >= 2:
        runs.append(pts[start:])
    return runs


def resample(pts, max_span):
    """按弧长等距重采样。

    抖动是按顶点位移的，顶点密度直接决定观感：隐函数的等值线来自网格，
    顶点比参数方程密得多，同一形状会显得更毛糙。统一重采样后，
    抖动频率只由画布尺寸决定，三种曲线形式观感一致。
    """
    d = np.hypot(*np.diff(pts, axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(d)])
    total = s[-1]
    if total <= 0:
        return pts
    n = int(min(4000, max(8, total / (SEGMENT_LENGTH * max_span))))
    t = np.linspace(0.0, total, n)
    return np.column_stack([np.interp(t, s, pts[:, 0]),
                            np.interp(t, s, pts[:, 1])])


def curve_polylines(curve, ax, xlim, ylim):
    """把三种曲线形式统一抽成折线（数据坐标），供重采样与绘制。"""
    kind = classify(curve)

    if kind == "explicit":
        x = np.linspace(xlim[0], xlim[1], AUTO_SAMPLES * 4)
        y = _eval(curve["expr"], x=x)
        # 渐近线处断开，避免竖直连线
        y[np.abs(y) > 10 * max(abs(ylim[0]), abs(ylim[1]), 1)] = np.nan
        return split_finite(np.column_stack([x, y]))

    if kind == "parametric":
        t = np.linspace(curve["t"][0], curve["t"][1], AUTO_SAMPLES * 4)
        return [np.column_stack([_eval(curve["x"], t=t), _eval(curve["y"], t=t)])]

    # 隐函数：借 contour 求等值线，只要折线不要它的绘制结果
    gx = np.linspace(xlim[0], xlim[1], AUTO_SAMPLES)
    gy = np.linspace(ylim[0], ylim[1], AUTO_SAMPLES)
    gxx, gyy = np.meshgrid(gx, gy)
    z = _eval(curve["fxy"], x=gxx, y=gyy)
    cs = ax.contour(gxx, gyy, z, levels=[0], colors="none")
    segs = [np.asarray(s, dtype=float) for s in cs.allsegs[0] if len(s) >= 2]
    cs.remove()
    return segs


def draw_curves(ax, curves, xlim, ylim):
    """绘制曲线：重采样 → 施加轻手绘抖动与半透明 → 标注曲线名。

    返回待定位的曲线名列表，交给 resolve_labels 统一避让。
    """
    max_span = max(xlim[1] - xlim[0], ylim[1] - ylim[0]) or 1.0
    pending = []

    for i, curve in enumerate(curves):
        color = curve.get("color") or COLORS[i % len(COLORS)]
        segs = [resample(p, max_span) for p in curve_polylines(curve, ax, xlim, ylim)]

        for pts in segs:
            # 裁剪在坐标区内：tan 这类无界函数的分支会超出窗口，
            # 不裁剪就会一路画出画布再被画布边缘硬裁
            line, = ax.plot(pts[:, 0], pts[:, 1], color=color, lw=LW_CURVE,
                            zorder=2, alpha=ALPHA_CURVE)
            line.set_solid_capstyle("round")
            line.set_solid_joinstyle("round")
            # 轻手绘抖动：matplotlib 3.11 用 Artist.set_sketch_params，
            # 旧版的 patheffects.Sketch 类已移除
            line.set_sketch_params(**SKETCH)

        label = curve.get("label")
        if not label:
            continue
        cands = _label_candidates([s for s in segs if len(s)], xlim, ylim)
        if not cands:
            continue
        (lx, ly), ha = cands[0]
        text = ax.text(lx, ly, label, ha=ha,
                       va="center", color=color, zorder=4, clip_on=False,
                       path_effects=HALO)
        pending.append((text, cands))
    return pending


def _label_candidates(segs, xlim, ylim, limit=12):
    """曲线名的候选位置：按「离坐标轴远」排序，并保证彼此分得开。

    曲线可能是闭合的（圆），没有「端点」可言，所以按空间挑位置而非按端点。
    多给几个候选是为了让 resolve_labels 在重叠时能换一个位置。
    """
    pts = np.vstack(segs)
    if not len(pts):
        return []

    ax_x, ax_y = axis_positions(xlim, ylim)
    span_x = (xlim[1] - xlim[0]) or 1.0
    span_y = (ylim[1] - ylim[0]) or 1.0

    # 离两条轴线越远，标签越不容易压到轴
    score = np.abs(pts[:, 0] - ax_x) / span_x + np.abs(pts[:, 1] - ax_y) / span_y
    centroid = pts.mean(axis=0)
    min_sep = 0.20 * max(span_x, span_y)

    cands, chosen = [], []
    for i in np.argsort(-score):
        p = pts[i]
        if any(np.hypot(*(p - c)) < min_sep for c in chosen):
            continue
        chosen.append(p)

        # 沿「背离曲线重心」的方向外移，避免标签压在曲线上
        d = p - centroid
        n = float(np.hypot(*d))
        out = d / n if n else np.array([0.6, 0.6])
        lx = min(max(p[0] + out[0] * span_x * 0.08, xlim[0] + span_x * 0.03),
                 xlim[1] - span_x * 0.03)
        ly = min(max(p[1] + out[1] * span_y * 0.08, ylim[0] + span_y * 0.03),
                 ylim[1] - span_y * 0.03)
        cands.append(((lx, ly), "center"))
        if len(cands) >= limit:
            break
    return cands


def resolve_labels(fig, pending, obstacles=()):
    """量文字包围盒，挑一个不重叠、不出画布的候选位置。

    标注带白色光晕，压在别的文字上会把对方盖没（光圈是白的），
    所以必须避让而不是单纯叠加。点标注在前、曲线名在后，前者锚定更死、
    候选更少，让它先占位。轴名 x/y/O 位置固定，作为障碍物先进 placed。
    """
    if not pending and not obstacles:
        return
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    fw, fh = fig.bbox.width, fig.bbox.height

    def hits(bb, boxes):
        """带余量的碰撞检测：只判重叠会让标签紧贴着轴名，看着很挤。"""
        for p in boxes:
            if (bb.x0 - LABEL_PAD < p.x1 and bb.x1 + LABEL_PAD > p.x0
                    and bb.y0 - LABEL_PAD < p.y1 and bb.y1 + LABEL_PAD > p.y0):
                return True
        return False

    placed = [t.get_window_extent(renderer) for t in obstacles]
    for text, cands in pending:
        best, best_overflow = None, None
        for pos, ha in cands:
            text.set_position(pos)
            text.set_ha(ha)
            bb = text.get_window_extent(renderer)
            if hits(bb, placed):
                continue
            # 画布外溢出量，越小越好
            overflow = (max(0.0, 1 - bb.x0) + max(0.0, bb.x1 - (fw - 1))
                        + max(0.0, 1 - bb.y0) + max(0.0, bb.y1 - (fh - 1)))
            if overflow == 0:
                best = (pos, ha)
                break
            if best_overflow is None or overflow < best_overflow:
                best, best_overflow = (pos, ha), overflow

        if best:
            text.set_position(best[0])
            text.set_ha(best[1])
        placed.append(text.get_window_extent(renderer))


def draw_axes(ax, xlim, ylim, points=()):
    """画带箭头的 x/y 轴、轴名和原点 O（set_axis_off 已去掉边框与刻度）。"""
    ax_x, ax_y = axis_positions(xlim, ylim)
    common = dict(arrowstyle="-|>", color=COLOR_AXIS, lw=LW_AXIS,
                  shrinkA=0, shrinkB=0, capstyle="round", clip_on=False)

    for end, start in (((xlim[1], ax_y), (xlim[0], ax_y)),
                       ((ax_x, ylim[1]), (ax_x, ylim[0]))):
        arrow = ax.annotate("", xy=end, xytext=start, zorder=1,
                            arrowprops=dict(**common))
        # SKETCH_AXIS = None 时显式关掉抖动，画出干净直线
        arrow.arrow_patch.set_sketch_params(**(SKETCH_AXIS or {}))

    # 轴名标在箭头尖端外侧。位置固定，但要在 resolve_labels 里当作障碍物，
    # 否则点/曲线标注会紧贴上来。
    # y 用居中而非右对齐：右对齐会让标签紧贴轴线吊在左上角，
    # 看起来像箭头偏右（实测箭头本身是正的，是标签造成的错觉）
    fixed = [
        ax.text(xlim[1], ax_y, "$x$", ha="left", va="top", color=COLOR_AXIS,
                zorder=1, clip_on=False, path_effects=HALO),
        ax.annotate("$y$", xy=(ax_x, ylim[1]), xytext=(0, 3),
                    textcoords="offset points", ha="center", va="bottom",
                    color=COLOR_AXIS, zorder=1, clip_on=False, path_effects=HALO),
    ]

    # 原点 O 放在左下方（不压轴线）；轴不在原点相交时不画。
    # 有标注点落在原点附近时也省略 O，否则 O 会和点标记及其标注挤在一起
    near_origin = any(
        abs(p["at"][0]) < 0.08 * (xlim[1] - xlim[0])
        and abs(p["at"][1]) < 0.08 * (ylim[1] - ylim[0])
        for p in points if p.get("at") and len(p["at"]) == 2
    )
    if xlim[0] <= 0 <= xlim[1] and ylim[0] <= 0 <= ylim[1] and not near_origin:
        fixed.append(
            ax.annotate("$O$", xy=(0, 0), xytext=(-4, -4),
                        textcoords="offset points", ha="right", va="top",
                        color=COLOR_AXIS, zorder=4, path_effects=HALO))
    return fixed


def draw_points(ax, points):
    """画点，标注默认放在点的右侧（抽象符号，不带具体数值）。

    返回待定位的标注列表，交给 resolve_labels 统一避让。
    """
    dx, dy = POINT_LABEL_OFFSET
    # 首选右侧（需求默认），右侧会出画布或撞上别的标注时依次退让
    fallbacks = [
        ((dx, dy), "left"),
        ((-dx, dy), "right"),
        ((dx, -dy - 7), "left"),
        ((0, dy + 7), "center"),
    ]
    pending = []

    for pt in points:
        _need(pt.get("at") is not None and len(pt["at"]) == 2,
              "points 的每一项要有 at=(x, y)，例如 dict(at=(0, 0), label=\"$p(x_0, y_0)$\")")
        x, y = pt["at"]
        color = pt.get("color") or COLOR_POINT
        ax.plot([x], [y], marker="o", markersize=4.5, color=color,
                zorder=3, clip_on=False)
        label = pt.get("label")
        if not label:
            continue
        ann = ax.annotate(label, xy=(x, y), xytext=fallbacks[0][0],
                          textcoords="offset points", ha=fallbacks[0][1],
                          va="center", color=color, zorder=4, clip_on=False,
                          path_effects=HALO)
        pending.append((ann, fallbacks))
    return pending


def setup_rc():
    """全局字体与数学排版设置（只影响本进程，不动用户配置）。"""
    matplotlib.rcParams["mathtext.fontset"] = MATHTEXT_FONTSET
    matplotlib.rcParams["mathtext.default"] = MATHTEXT_DEFAULT
    # 中文兜底用楷体，与手绘调性一致
    matplotlib.rcParams["font.sans-serif"] = [CJK_FONT, "SimHei", "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    # 抖动作用在采样点上，关掉路径简化，避免顶点被悄悄合并后抖动形态变得不可预测
    matplotlib.rcParams["path.simplify"] = False


def render(spec, out_path):
    """渲染并保存 PNG（尺寸固定 300x300）。"""
    setup_rc()

    xlim, ylim = auto_window(spec)
    # 圆、椭圆、参数曲线需要 aspect="equal"，否则形状会被坐标轴拉伸
    if spec.get("aspect") == "equal":
        xlim, ylim = apply_equal_aspect(xlim, ylim)

    fig = plt.figure(figsize=FIGSIZE, dpi=DPI)
    ax = fig.add_subplot(111)
    # 用 subplots_adjust 留边距，画布尺寸保持不变
    # （不能用 bbox_inches="tight"，那会裁成非 300x300）
    fig.subplots_adjust(**SUBPLOT)
    ax.set_axis_off()
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    # 网格线默认关闭，这里显式再关一次，防止 spec 或全局样式打开
    ax.grid(False)

    axis_labels = draw_axes(ax, xlim, ylim, spec["points"])
    # 点标注先占位（锚定在点上、候选少），曲线名再挑剩下的空间
    point_labels = draw_points(ax, spec["points"])
    curve_labels = draw_curves(ax, spec["curves"], xlim, ylim)
    resolve_labels(fig, point_labels + curve_labels, axis_labels)

    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    # 白底不透明：贴进笔记/文档时不必再管背景透明问题
    fig.patch.set_facecolor("white")
    fig.savefig(out_path, dpi=DPI, facecolor="white")
    plt.close(fig)
    return out_path


def main():
    ap = argparse.ArgumentParser(description="数学函数图像渲染（300x300 · 轻手绘）")
    ap.add_argument("spec", help="spec 文件路径（.graph.py）")
    ap.add_argument("-o", "--out", help="输出 PNG 路径（默认与 spec 同目录同名）")
    args = ap.parse_args()

    spec = load_spec(args.spec)
    out = args.out
    if not out:
        base = os.path.splitext(os.path.abspath(args.spec))[0]
        # xxx.graph → xxx
        if base.endswith(".graph"):
            base = base[: -len(".graph")]
        out = base + ".png"

    render(spec, out)
    print("已生成 %s（%dx%d）" % (out, FIGSIZE[0] * DPI, FIGSIZE[1] * DPI))
    return 0


if __name__ == "__main__":
    sys.exit(main())
