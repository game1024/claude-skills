---
name: pic2ppt
description: 把幻灯片截图或图片转换成可编辑 PowerPoint（.pptx）的技能。当用户提到图片转PPT、截图转PPT、图片做成PPT、可编辑PPT、image to PPT、slide screenshot to PPT、把这张幻灯片重做成PPT等时使用。流程：用 tools/pic2ppt.py 自动 OCR（PP-OCRv6 / PaddleOCR-VL / RapidOCR / Tesseract），清理背景、把文字/扁平卡片/分隔线重建为 PPT 原生对象（图标分离默认关闭，需要时加 --icons），输出 .pptx 以及 manifest、预览图和原图对比图；支持修改 manifest 后用 --from-manifest 重建。默认采用可编辑性优先（--profile editability）。
---

# 图片 → 可编辑 PPT（pic2ppt）

## 依赖（首次使用前确认）

```bash
python -m pip install -r "${CLAUDE_SKILL_DIR}/tools/requirements.txt"
```

国内网络建议加中科大 PyPI 镜像加速（仅本次安装生效，不改全局配置）：

```bash
python -m pip install -r "${CLAUDE_SKILL_DIR}/tools/requirements.txt" \
  -i https://mirrors.ustc.edu.cn/pypi/simple
```

需要长期使用时执行 `pip config set global.index-url https://mirrors.ustc.edu.cn/pypi/simple`；
恢复官方源用 `pip config unset global.index-url`。

必需：Pillow、numpy、scipy、requests。`requirements.txt` 里的 `opencv-python-headless`
负责文字擦除、Poisson `seamlessClone` 融合和局部修复；`scikit-image` 负责渐变背景上的
biharmonic 修复，建议保留。缺失时脚本会退化为 Telea 或纯 numpy 扩散填充。

可选 OCR：PP-OCRv6（云端，需 token，默认）、PaddleOCR-VL（云端，版面解析）、
RapidOCR（本地）或 Tesseract。有 token 时 `auto` 优先 PP-OCRv6；没有 token 时
自动回退到本地 OCR。

## 核心原则（不要把整页截图直接贴进 PPT）

1. **背景优先**：判断纯色/简单背景还是照片/复杂背景；文字和已分离对象从底图中移除，
   得到干净底板。
2. **前景可选**：图标等非文字对象默认保留在底图；只有显式加 `--icons` 才分离为
   透明 PNG。默认关闭是为了避免把图表、泡泡、文字碎片误抠成图标。
3. **原生元素最后**：文字、扁平卡片、分隔线用真实 PPT 对象重建；文字背后的
   平滑色带/渐变会重建为 freeform 渐变底板，并默认烘焙进干净背景图
   （editability 默认开启；需要保留形状对象时加 `--keep-backdrop-shapes`）。
4. 所有对象以源图像素坐标写入 `manifest.json`，再由确定性 OOXML writer 生成 `.pptx`。

## 执行流程

### 1. 生成

```bash
python "${CLAUDE_SKILL_DIR}/tools/pic2ppt.py" "<图片1>" ["<图片2>" ...] -o "<输出.pptx>" --profile editability
```

**本 skill 默认采用可编辑性优先（`--profile editability`，脚本默认值也是它）**：
提高文字原生化阈值（`raster-text-mad` 18、`raster-text-std` 50），让更多文字成为
可编辑文本框；文字背后的饱和色带/渐变会拟合成 `freeform` + 线性渐变底板，并默认
**烘焙进干净背景图**（PPT 里只有背景图 + 可编辑文字），尽量让丝带、端帽、徽章上的
文字也可编辑。代价是复杂背景上的文字修复痕迹、
标题下划线等装饰可能被一起擦除，带连接线的标注也可能与连线重叠，渐变底板放大后
可能有轻微矢量近似痕迹。用户明确要求“视觉保真优先”时，改用 `--profile balanced`。

默认 OCR 顺序：PP-OCRv6（有 token 时，逐行识别）→ RapidOCR → Tesseract → none。
PP-OCRv6 与飞桨网页/API 使用同一模型系列，返回逐行文本，适合思维导图、密集小字、
页脚等场景；脚本关闭文档方向分类和去畸变，返回坐标直接对应源图。实测心智图页原始
识别 34 行（过滤图标碎片和低置信度噪声后 31 个文本框），包括 `CTBC BUSINESS SCHOOL`
等页脚小字。调用失败或返回 0 条文本时，脚本自动回退到 RapidOCR；RapidOCR 是本地
small 模型，结果可能不同。

需要版面解析（表格、图表块、段落分组）时，显式使用 PaddleOCR-VL；图表标题/页脚
可加 `--chart-recognition`：

```bash
python "${CLAUDE_SKILL_DIR}/tools/pic2ppt.py" "<图片>" -o "<输出.pptx>" --ocr paddleocr-vl --profile editability
```

涉密、离线或用户明确要求本地处理时，显式指定本地后端：

```bash
python "${CLAUDE_SKILL_DIR}/tools/pic2ppt.py" "<图片>" -o "<输出.pptx>" --ocr rapidocr --profile editability
python "${CLAUDE_SKILL_DIR}/tools/pic2ppt.py" "<图片>" -o "<输出.pptx>" --ocr none --profile editability
```

`--ocr none` 仍会生成 PPT，但文字保留在底图中，不是完全可编辑；必须在结果里说明。

### 2. 检查结果

1. 确认命令退出码为 0，输出文件存在。
2. 读取 `<workdir>/validation.json`，检查顶层 `passed` 和 `warnings`。
3. 用 Read 查看 `<page_dir>/comparison.png`（左：原图，右：重建），逐项核对：
   - 标题、正文、卡片文字的位置和字号是否接近原图；
   - 是否有明显鬼影、色块、错位、文字溢出；
   - 若加了 `--icons`：图标是否完整、没有把文字误抠成图标；
   - 若使用了渐变底板：丝带/端帽文字周围有没有明显的矩形补丁或色带断裂；
   - 复杂背景上的文字是否按预期保留为像素（见 `manifest.json.raster_text`）。
4. 如验证失败，读取 `errors` 并如实报告；不要伪造成功。

### 3. 报告

最终回复至少包含：

- 输出 `.pptx` 的绝对路径；
- 使用的 profile（editability / balanced）；
- validation 结果；
- 原生文本框 / 原生形状 / 图片数量；如使用了渐变底板，说明它是烘焙进背景图还是保留为独立形状；
- `raster_text` 中的文字（这些是不可编辑的保留项）；
- 使用的 OCR 后端，以及是否发生了联网调用；
- 产物目录（manifest、preview、comparison、text_hints）。

## 常用选项

| 选项 | 说明 |
| --- | --- |
| `-o, --output` | 输出 `.pptx` 路径 |
| `--workdir` | 中间产物目录，默认 `<输出名>_work` |
| `--ocr` | `auto` / `ppocrv6` / `paddleocr-vl` / `rapidocr` / `tesseract` / `none` |
| `--paddle-token` | Paddle OCR token（PP-OCRv6 与 PaddleOCR-VL 共用；优先环境变量/config） |
| `--paddle-model` | PaddleOCR-VL 模型，默认 `PaddleOCR-VL-1.6`（仅 `--ocr paddleocr-vl`） |
| `--chart-recognition` | 让 PaddleOCR-VL 额外解析图表；图表标题/页脚可能变成文本，图表内部仍保留底图 |
| `--mode` | `auto` / `native` / `image`（背景与元素重建策略） |
| `--icons` | 启用前景图标分离（默认关闭；默认图标保留在底图） |
| `--no-icons` | 兼容旧参数，等价于默认行为 |
| `--no-shapes` | 不把扁平卡片/分隔线原生化 |
| `--gradient-backdrops` | 把平滑色带背景重建为 freeform 渐变底板（editability 默认） |
| `--no-gradient-backdrops` | 不重建色带背景，文字保留为像素（balanced 默认） |
| `--bake-backdrops` | 把渐变底板融合进干净背景图（默认），PPT 中只有背景图 + 文字 |
| `--keep-backdrop-shapes` | 保留独立的原生渐变形状，不烘焙进背景 |
| `--no-text-fit` | 关闭字号自动收缩 |
| `--profile` | `editability`（默认，可编辑优先）或 `balanced`（保真优先） |
| `--raster-text-mad` | 文字框外围均匀度阈值；`editability` 默认 18，`balanced` 默认 15；调大可让更多文字原生化 |
| `--raster-text-std` | 文字框内非文字背景的颜色离散度阈值；editability 默认 50，balanced 默认 20 |
| `--font-cjk` / `--font-latin` | 中/西文字体名 |
| `--from-manifest` | 从修改后的 `manifest.json` 重建 deck |

完整说明见 `${CLAUDE_SKILL_DIR}/references/usage.md`。

## 关键产物

```text
<output>_work/
  pages/page_001/
    source.png
    manifest.json        # 页面构建的唯一权威来源（可手工精修）
    preview.png          # 根据 manifest 渲染的预览
    comparison.png       # 原图 vs 重建
    text_hints.png       # 识别到的文字框可视化
    assets/base.png      # 干净底板（复杂背景时）
    assets/icon_*.png    # 仅在 --icons 时存在的分离图标
    validation.json
    page.pptx
```

精修流程：编辑 `manifest.json` 的 `text_boxes` / `shapes` / `images`（坐标均为
源图像素，字段说明见 manifest 本身与 `references/usage.md`），然后：

```bash
python "${CLAUDE_SKILL_DIR}/tools/pic2ppt.py" \
  --from-manifest "<page_dir>/manifest.json" -o "<新的输出.pptx>"
```

## 隐私与凭据（必须遵守）

- 脚本**不会把 token 写入任何产物**；它只从以下位置读取：
  1. `--paddle-token` 参数
  2. 环境变量 `PADDLE_OCR_TOKEN`
  3. `~/.editppt/config.yaml` 中的 `PADDLE_OCR_TOKEN`
- 不要在命令、回复、日志、manifest、PPT 或任何文件中回显 token；不要 `cat`/读取并打印
  `~/.editppt/config.yaml` 的内容。
- 优先使用环境变量或已有 config，避免把 token 放进 shell 历史；如必须传
  `--paddle-token`，不要在最终回复里重复它。
- `--ocr auto` 只要发现 token 就会优先走 PP-OCRv6（PaddleOCR-VL 仅在显式指定时使用），
  会上传待转换的页面图片到百度 PaddleOCR 服务。用户要求离线、涉密或本地处理时，
  必须显式使用 `--ocr rapidocr` 或 `--ocr none`。
- 除 PP-OCRv6 / PaddleOCR-VL 外，脚本运行时没有其他显式联网调用；RapidOCR 的某些版本
  首次运行可能自行下载模型文件，需向用户说明。

## 限制

- 表格和公式不会重建为原生对象，保留在底图中。
- 照片、3D 物体、手写艺术字、复杂插画上的文字会保留为底图像素（记录在
  `raster_text`），以保证视觉保真；这部分文字不可直接编辑。判定会同时检查
  文字框外围均匀度和框内非文字背景的颜色离散度。
- OCR 可能漏掉极小/低对比度文字；检查 `text_hints.png`。
- 旋转文字按水平文本框重建，可能丢失旋转角度和字体风格。
- 不支持的原生形状（例如六边形）会保留为底图；带连接线的标注文字如果连接线进入
  文字框，也会保留为像素，以免破坏连线。
- 可编辑优先（`--profile editability`）会把更多文字原生化，但文字框内的装饰线
  （例如标题下划线）可能被一起擦除，原生字体也会替换掉像素/艺术字体；带连接线的
  标注文字还可能与连接线重叠。如果用户更重视视觉保真，改用 `--profile balanced`。
- 色带/端帽上的渐变底板是 `freeform` + 线性渐变的近似，无法完全复刻 3D 高光、
  阴影和纹理；`freeform` 按饱和色带轮廓裁剪并略微内缩，轮廓用 Chaikin 平滑，
  渐变沿主方向采样 3 个色标（暗部/中间/亮部）。默认不额外柔化；独立形状模式
  （`--keep-backdrop-shapes`）会额外加 1px 边缘羽化来隐藏残留接缝。默认烘焙模式
  用 OpenCV Poisson `seamlessClone` 按边界颜色融合底板，再写入背景图；底板成为
  背景像素，不能再单独编辑形状，但文字仍是原生文本框。需要严格保真时使用
  `--no-gradient-backdrops` 或 `--profile balanced`。
- 纯数字短标签（如 `01`/`02`/`03`）位于 3D 圆盘上时不会叠加渐变底板，而是直接
  紧贴字形修复，避免数字背后出现矩形色块。
- 普通文字位于非平坦卡片/渐变上时只修复字形 mask（biharmonic/Telea），不做整框
  平涂；避免把字体颜色或周围色带混进背景填充。
- 彩色标题前的小竖条、同色装饰线可能随文字一起从底图清除，以保证文字可编辑；
  需要保留时可在 `manifest.json` 中手工补回。
- 图表面板会自动保留为底图：大块浅色面板里检测到多个小碎片时，不会把柱状图、
  坐标轴刻度或图例色块拆成独立图标。
- 图表内部（坐标轴、刻度、图例）在默认的 PP-OCRv6 下会被逐行识别；干净背景上的
  标签可能变成独立文本框，柱体/折线仍保留在底图中。这不会重建真正的原生图表；
  若希望图表内部整体保留为 raster 块，改用 `--ocr paddleocr-vl`，图表标题/页脚可
  加 `--chart-recognition`。需要真正可编辑的图表时，应重建原生柱状图/饼图，而不是
  在图表图片上叠文本框。
- 这是 CV 修复，不是图像生成模型：不会“重绘”背景，复杂背景可能有轻微修复痕迹。
- 需要 AI 级干净底图、原生表格或公式时，改用 `image-to-editable-ppt` skill 的
  `editppt` 运行时继续精修。
