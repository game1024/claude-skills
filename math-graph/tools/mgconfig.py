# -*- coding: utf-8 -*-
"""math-graph 默认样式参数（改观感改这里）。

本文件集中存放图像尺寸、手绘抖动、透明度、配色与字体设置，
mathgraph.py 只负责渲染流程，不写死任何样式数值。
"""

# 画布：3 x 3 英寸 @ 100 dpi = 300 x 300 像素
FIGSIZE = (3, 3)
DPI = 100

# 坐标区在画布内的占比：四周留边给箭头和标注，同时保持画布尺寸不变
# （不要改用 bbox_inches="tight"，那会把输出裁成非 300x300）
SUBPLOT = dict(left=0.06, right=0.94, bottom=0.06, top=0.94)

# 手绘抖动（Artist.set_sketch_params，单位为像素）
# scale      : 抖动幅度（像素）—— 图越小看起来越夸张，300x300 下 1.5 约等于「略抖」
# length     : 抖动波长（像素）—— 调大比调小更关键：小波长会变成毛刺噪点
# randomness : 波长随机伸缩比例，越大越潦草
# 取值依据：实测对比 (2.5,110,12) 呈高频毛刺，(1.5,400,5) 已看不出手绘，
# (1.5,200,8) 是「看得出是手绘但仍干净」的一档
SKETCH = dict(scale=1.5, length=200, randomness=8)

# 箭头部位单独用更小的幅度，避免箭头尖端被抖毛
SKETCH_ARROW = dict(scale=1.0, length=200, randomness=6)

# 重采样点距，取坐标区长边的比例。抖动按顶点位移，顶点密度会显著影响观感，
# 统一到约 3 像素一个点后，三种曲线形式的抖动频率才一致（0.012 ≈ 3px @300x300）
SEGMENT_LENGTH = 0.012

# 曲线半透明，避免遮挡文字标识
ALPHA_CURVE = 0.72

# 标注文字背后的白色光晕宽度（点）：曲线穿过文字时保证字仍清晰
HALO_WIDTH = 2.5

# 标注避让时包围盒留的余量（像素）：只判重叠会让标签紧贴着轴名，看着很挤
LABEL_PAD = 2.0

# 点标注相对点的偏移（点）+ 对齐方式：正偏移 + 左对齐 = 标签在点右侧
POINT_LABEL_OFFSET = (6, 2)

# 线宽
LW_CURVE = 1.8
LW_AXIS = 1.2

# 配色（循环取用）
COLORS = ["C0", "C1", "C2", "C3", "C4", "C5"]

# 坐标轴颜色
COLOR_AXIS = "0.25"
COLOR_POINT = "C3"

# 字体：数学符号走 mathtext（Computer Modern，最接近教材）
# 不要把手写字体套到数学符号上——会破坏下划线和斜体
MATHTEXT_FONTSET = "cm"
MATHTEXT_DEFAULT = "it"
# 中文兜底用楷体，与手绘调性一致
CJK_FONT = "KaiTi"

# 自动取值范围：先在此区间采样，再按分位裁剪（避开 tan、1/x 的渐近线撑爆纵轴）
AUTO_X_RANGE = (-5.0, 5.0)
AUTO_SAMPLES = 400
AUTO_PAD = 0.15
AUTO_CLIP_PCT = (2, 98)

# 关于抖动是否可复现：抖动由 Agg 后端在 C++ 里执行（参数经 gc.set_sketch_params
# 传入），随机源不在 Python 层，np.random.seed() 影响不到它。实测同一份 spec
# 多次渲染字节完全一致，所以出图是稳定可复现的；但也因此无法用种子去换抖动形态，
# 故不提供 --seed 选项。若将来需要多种抖动形态，得在 Python 里自行实现位移。
