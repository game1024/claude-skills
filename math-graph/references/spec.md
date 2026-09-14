# spec 文件格式（math-graph）

> 本文件只讲 spec 文件怎么写。默认样式、调用步骤和自检见 `SKILL.md`。
> 尺寸、透明度、抖动等观感参数不在 spec 里调，改 `${CLAUDE_SKILL_DIR}/tools/mgconfig.py`。

spec 就是一个普通 Python 文件，用模块级变量描述画什么。五个变量全部可选：

| 变量 | 含义 |
|---|---|
| `curves` | 曲线列表，每项是一个 dict |
| `points` | 点列表，每项是 `dict(at=(x, y), label="$...$")` |
| `xlim` | 横轴范围，如 `(-3, 3)`；不给则自动定 |
| `ylim` | 纵轴范围；不给则自动定 |
| `aspect` | 填 `"equal"` 时两轴等比例。**圆、椭圆、参数曲线必须填**，否则形状会被拉伸 |

## aspect = "equal" 什么时候要填

坐标区是正方形的，只有两轴跨度相等时，一个单位在 x 和 y 上才一样长。填 `"equal"` 会把较短的一边**扩大**到与长边相同（只扩不缩，不会裁掉数据），从而保证形状不失真。

不填的话：`xlim=(-3.5,3.5)`、`ylim=(-2.5,2.5)` 画 `x=3cos(t), y=2sin(t)`，本该是 1.50 宽高比的椭圆会变成 1.07，几乎成了圆。

判断方法很简单：**画的是函数曲线（y=f(x)）就不用填；量的是几何形状（圆、椭圆、双曲线、参数曲线）就填。**

## curves 的三种形式

按 dict 里出现的键自动判别，**只能三选一**：

### 1. 显式函数 `y = f(x)`

```python
dict(expr="x**2")
```

`expr` 是用 `x` 表示的表达式字符串。

### 2. 隐函数 `F(x, y) = 0`

```python
dict(fxy="x**2 + y**2 - 4")
```

`fxy` 是用 `x`、`y` 表示的表达式，画它等于 0 的等值线。圆、椭圆、双曲线用这个，不用拆成上下两半。

### 3. 参数方程

```python
dict(x="2*cos(t)", y="2*sin(t)", t=(-3.1416, 3.1416))
```

三个键必须同时给：`x`、`y` 是用参数 `t` 表示的表达式，`t` 是取值范围。

### curves 的公共可选键

| 键 | 默认 | 说明 |
|---|---|---|
| `label` | 无 | 曲线名，写成 mathtext，如 `"$f(x)$"`、`"$C$"`。位置自动挑离坐标轴远的点 |
| `color` | 依次取 `C0`、`C1`… | matplotlib 颜色，如 `"C2"`、`"#c0504d"` |

## points

```python
dict(at=(x, y), label="$p(x_0, y_0)$")
```

- `at` 必填，数据坐标
- `label` 选填，**一律写成抽象符号，不要写具体数值**；渲染器会把标签放在点的右侧
- `color` 选填，默认 `C3`

## 表达式里可用的函数

`sin` `cos` `tan` `arcsin` `arccos` `arctan` `sinh` `cosh` `tanh` `exp` `log` `log2` `log10` `sqrt` `abs` `sign` `floor` `ceil` `power` `minimum` `maximum` `where`，常数 `pi` `e`。这些都按 numpy 数组运算，所以 `x**2 + 1`、`sin(2*pi*x)` 都是合法的。

## 自动取值范围

不给 `xlim` / `ylim` 时，渲染器在 `(-5, 5)` 上采样函数，对结果取 2%~98% 分位再留 15% 余量。这样 `tan`、`1/x` 这类无界函数不会把纵轴撑爆。窗口不合适就直接写 `xlim` / `ylim`。

## 可直接照抄的示例

### 抛物线 + 一个点

```python
# -*- coding: utf-8 -*-
curves = [
    dict(expr="x**2", label="$f(x)$", color="C0"),
]
points = [
    dict(at=(0, 0), label="$p(x_0, y_0)$"),
]
```

### 单位圆 + 圆上一点

```python
# -*- coding: utf-8 -*-
aspect = "equal"
xlim = (-2.5, 2.5)
ylim = (-2.5, 2.5)
curves = [
    dict(fxy="x**2 + y**2 - 1", label="$C$", color="C0"),
]
points = [
    dict(at=(0, 1), label="$p(x_0, y_0)$"),
]
```

### 两条曲线对比

```python
# -*- coding: utf-8 -*-
xlim = (-2, 2)
ylim = (-1, 5)
curves = [
    dict(expr="exp(x)", label="$f(x)=e^x$", color="C0"),
    dict(expr="1 + x + x**2/2", label="$g(x)$", color="C1"),
]
```

### 正弦曲线

```python
# -*- coding: utf-8 -*-
xlim = (-6.5, 6.5)
ylim = (-1.6, 1.6)
curves = [
    dict(expr="sin(x)", label="$f(x)$", color="C0"),
]
```

### 参数方程画椭圆（必须 aspect = "equal"）

```python
# -*- coding: utf-8 -*-
aspect = "equal"
xlim = (-3.5, 3.5)
ylim = (-2.5, 2.5)
curves = [
    dict(x="3*cos(t)", y="2*sin(t)", t=(-3.1416, 3.1416), label="$C$", color="C0"),
]
points = [
    dict(at=(3, 0), label="$p(x_0, y_0)$"),
]
```

### 只要点和标注

```python
# -*- coding: utf-8 -*-
xlim = (-3, 3)
ylim = (-3, 3)
points = [
    dict(at=(1, 2), label="$p(x_0, y_0)$"),
    dict(at=(-1, -1), label="$q$", color="C0"),
]
```
