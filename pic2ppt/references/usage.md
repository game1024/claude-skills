# pic2ppt

> 本文件是脚本的完整说明。在 skill 中，脚本位于
> `${CLAUDE_SKILL_DIR}/tools/pic2ppt.py`，依赖文件位于
> `${CLAUDE_SKILL_DIR}/tools/requirements.txt`；下文示例里的 `pic2ppt.py`
> 都可以替换为该路径。

把幻灯片截图 / 图片转换成**可编辑的 PowerPoint（.pptx）**。

两种优先级：

- `--profile editability`（默认）：可编辑优先，把 `raster-text-std` 提高到 50、
  `raster-text-mad` 提高到 18，让更多文字进入原生文本框；位于纯色/渐变色带上的
  文字会拟合**原生 freeform + 线性渐变底板**，并默认**烘焙进干净底图**，最终只有
  一张融合后的背景图 + 可编辑文字。代价是复杂背景/装饰线可能被擦除或出现轻微
  近似痕迹；
- `--profile balanced`：尽量保留视觉保真，复杂背景上的文字保留为像素，不启用
  渐变底板重建。

这个脚本参考了 `image-to-editable-ppt` skill 的核心原理，并用 OCR + 传统计算机视觉把原来由视觉 Agent 完成的判断自动化：

1. **背景优先**：先判断背景是纯色/简单设计，还是照片/复杂设计。
   - 简单背景 → 用 PPT 原生填充，或生成干净底板。
   - 复杂背景 → 保留为“干净底板”（clean base）：把可编辑文字和已分离的前景对象抹掉后的背景图。
2. **前景可选**：图标、徽章等非文字视觉对象默认保留在底图里；只有显式加
   `--icons` 时才通过连通域 + 颜色距离抠图分离为透明 PNG。默认关闭是为了避免
   把图表、泡泡、文字碎片误抠成图标。
3. **原生元素最后**：文字、扁平卡片、分隔线用真正的 PPT 对象重建。所有对象以源图像素坐标写进 `manifest.json`，再由确定性的 OOXML writer 生成 `.pptx`。

最终产物**不是**“整页截图 + 文字浮层”：底板里的可编辑文字已经被移除，文字、卡片、线条都是真实可编辑对象。

---

## 安装

需要 Python 3.10+。

```bash
pip install -r requirements.txt
```

国内网络访问官方 PyPI 较慢时，可临时改用中科大镜像（`-i` 只对本次安装生效）：

```bash
pip install -r requirements.txt -i https://mirrors.ustc.edu.cn/pypi/simple
```

或写入 pip 全局配置（会改变之后所有 pip 安装的默认源）：

```bash
pip config set global.index-url https://mirrors.ustc.edu.cn/pypi/simple
# 恢复官方源
pip config unset global.index-url
```

镜像只改变下载来源，包内容仍来自 PyPI；长期使用后如遇版本不一致，可临时用
`-i https://pypi.org/simple` 回到官方源。清华镜像
（`https://pypi.tuna.tsinghua.edu.cn/simple`）可作为备用。

`requirements.txt` 包含 `opencv-python-headless`，负责文字抹除、Poisson 融合和
局部修复；`scikit-image` 用于渐变背景上的 biharmonic 修复，让字形擦除更平滑。
缺少这两个库时脚本仍可运行，但背景修复会退化为 OpenCV Telea 或纯 numpy 扩散填充。

可选的本地 OCR：

```bash
# 完全本地 OCR（中英文）
pip install rapidocr onnxruntime

# 或者使用 Tesseract
pip install pytesseract
# 并安装 Tesseract 可执行文件与 chi_sim 语言包
```

## OCR 配置

配置 token 后，脚本默认使用 **PP-OCRv6 云端 OCR**：

1. **PP-OCRv6**（需要 token，逐行文本识别；`auto` 默认）
2. **RapidOCR**（本地，无需联网）
3. **Tesseract**
4. **none**（不 OCR；文字会保留在底图里，PPT 不是完全可编辑）

PP-OCRv6 与飞桨网页/API 使用的是同一模型系列，返回逐行 `rec_texts` / `rec_boxes`，
非常适合幻灯片、思维导图、密集小字和图标+文字混排的页面。脚本关闭了文档方向分类
和去畸变，避免干净截图被旋转或拉伸，返回坐标直接对应源图。实测心智图页原始识别
34 行（过滤图标碎片和低置信度噪声后保留 31 个文本框），页脚的
`CTBC BUSINESS SCHOOL` 等小字也能识别。

> PP-OCRv6 是一个模型系列（small / medium / server）。本地 RapidOCR 默认加载
> PP-OCRv6 **small** ONNX 模型，而飞桨 API 使用服务端模型和不同的预处理/后处理，
> 所以同一张图的结果可能不同；API 通常更完整。两者不是同一个“PP-OCRv6”权重。

PaddleOCR-VL 不再出现在 `auto` 顺序里；需要版面解析（表格、图表、段落块）时显式使用：

```bash
python pic2ppt.py slide.png --ocr paddleocr-vl
python pic2ppt.py slide.png --ocr paddleocr-vl --chart-recognition
```

没有 token、调用失败或返回 0 条文本时，`auto` 会回退到 RapidOCR。RapidOCR 是本地
ONNX 推理，默认用 PP-OCRv6 **small** 模型，速度和隐私更好，但小字/低对比度文字
可能漏识别。RapidOCR 对最长边小于 1200px 的图会先 2× 放大再识别。

需要完全离线时显式指定：

```bash
pip install rapidocr onnxruntime
python pic2ppt.py slide.png --ocr rapidocr
```

Paddle OCR token 的读取顺序：

1. `--paddle-token`
2. 环境变量 `PADDLE_OCR_TOKEN`
3. `~/.editppt/config.yaml` 里的 `PADDLE_OCR_TOKEN`
   （与 `editppt config --paddle-ocr-token` 保存的位置一致）

免费 token 申请：<https://aistudio.baidu.com/account/accessToken>

> 使用 PP-OCRv6 或 PaddleOCR-VL 时，图片会上传到百度 PaddleOCR 服务；如果素材涉密，
> 请改用 `--ocr rapidocr` 或 `--ocr none`。

---

## 用法

```bash
# 单张图片，输出到同目录 <name>_editable.pptx
python pic2ppt.py slide.png

# 多张图片合成一个 deck（每张一页）
python pic2ppt.py slide1.png slide2.png slide3.png -o deck.pptx

# 显式使用云端逐行 OCR（有 token 时 auto 的默认）
python pic2ppt.py slide.png --ocr ppocrv6

# 使用 PaddleOCR-VL 的版面解析（表格/图表块）
python pic2ppt.py slide.png --ocr paddleocr-vl

# 强制使用本地 OCR
python pic2ppt.py slide.png --ocr rapidocr

# 只重建文字，不把扁平卡片/线条原生化
python pic2ppt.py slide.png --no-shapes

# 显式启用前景图标分离（默认关闭）
python pic2ppt.py slide.png --icons

# 强制复杂背景模式（不尝试把扁平卡片原生化）
python pic2ppt.py slide.png --mode image

# 人工修改 manifest.json 后重新生成 PPTX
python pic2ppt.py --from-manifest run/pages/page_001/manifest.json -o refined.pptx
```

### 常用选项

| 选项 | 说明 |
| --- | --- |
| `-o, --output` | 输出 `.pptx` 路径 |
| `--workdir` | 中间产物目录，默认 `<输出名>_work` |
| `--ocr` | `auto` / `ppocrv6` / `paddleocr-vl` / `rapidocr` / `tesseract` / `none` |
| `--paddle-token` | Paddle OCR token（PP-OCRv6 与 PaddleOCR-VL 共用） |
| `--paddle-model` | PaddleOCR-VL 模型，默认 `PaddleOCR-VL-1.6`（仅 `--ocr paddleocr-vl`） |
| `--chart-recognition` | 让 PaddleOCR-VL 额外解析图表；图表标题/页脚等可能作为文本返回，图表内部数值仍按图表块处理 |
| `--profile` | `editability`（默认，可编辑优先）或 `balanced`（保真优先） |
| `--mode` | `auto` / `native` / `image`（背景与元素重建策略） |
| `--icons` | 启用前景图标分离（默认关闭；默认图标保留在底图） |
| `--no-icons` | 兼容旧参数，等价于默认行为 |
| `--no-shapes` | 不检测扁平卡片/分隔线 |
| `--gradient-backdrops` | 把文字背后的平滑色带/渐变重建为 freeform 渐变底板（editability 默认开启） |
| `--no-gradient-backdrops` | 不重建色带背景，无法安全擦除的文字保留为像素（balanced 默认） |
| `--bake-backdrops` | 把渐变底板直接融合进干净背景图（默认），PPT 里只有背景图 + 文字 |
| `--keep-backdrop-shapes` | 不烘焙，保留独立的原生渐变形状，便于在 PPT 中单独调整 |
| `--no-text-fit` | 关闭字号自动收缩 |
| `--raster-text-mad` | 文字框外围均匀度阈值；`editability` 默认 18，`balanced` 默认 15；调大可将更多文字原生化 |
| `--raster-text-std` | 文字框内非文字背景的颜色离散度阈值；`editability` 默认 50，`balanced` 默认 20 |
| `--font-cjk` | 中文字体，默认 `Microsoft YaHei` |
| `--font-latin` | 西文字体，默认 `Arial` |
| `--max-source-side` | 输入最长边缩放上限，默认 3000px |
| `--bg-max-width` | 干净底板最大宽度，默认 2400px |
| `--quiet` | 只输出最终结果 |

---

## 输出结构

```text
<output_stem>_work/
  deck_manifest.json
  validation.json
  pages/
    page_001/
      source.png              # 规范化后的源图
      text_hints.json         # 识别到的文字、像素框、字号
      text_hints.png          # 文字框可视化
      ocr_blocks.json         # 原始 OCR 块（便于调试）
      assets/
        base.png              # 干净底板（如果背景复杂）
        icon_001.png          # 启用 --icons 时分离出的前景图标（带 alpha）
      manifest.json           # 页面构建的唯一权威来源
      preview.png             # 根据 manifest 渲染的预览
      comparison.png          # 原图 vs 重建对比图
      page.pptx               # 单页 PPT
      validation.json
      page_result.json
```

最终 deck 由所有 `manifest.json` 按页序重新构建，`page.pptx` 只用于单页校验。

---

## 原理映射

| skill 中的步骤 | 本脚本的实现 |
| --- | --- |
| 页面清单 / 文字检测 | PP-OCRv6 / PaddleOCR-VL / RapidOCR / Tesseract |
| 文字行测量、字号分组 | Otsu 二值化 + ink band 测量（移植自 skill） |
| 背景判定与修复 | 颜色量化/边缘密度判定；纯色/渐变填充，复杂区域用扩散或 OpenCV inpainting |
| 前景素材分离 | 调色板距离 + 局部中值差分 + 连通域 + 颜色距离 alpha matte |
| 原生形状重建 | 扁平空卡片 → `rect`/`roundRect`；长直线 → `line` |
| 色带/渐变文字背景 | 饱和色带 → freeform + 线性渐变，默认烘焙进干净底图 |
| 原生表格/公式 | 未实现；保留在底图中并给出 warning |
| manifest → PPTX | 确定性 OOXML writer（无 `python-pptx` 依赖） |
| 校验 | zip/XML/关系/媒体/文字框结构检查 |

### 文字处理细节

- 每个 OCR 块会在源图上重新测量墨迹，得到实际字形高度和行数。
- 同一视觉层级的文字用同一个字号（按字形高度聚类，避免 23.7pt / 24.1pt 抖动）。
- 一个 OCR 块如果同时包含大标题和小正文（行高差异 > 18%），会拆成多个独立文本框。
- OCR 把两行合并成一行字符串时，会按测得的行宽比例自动重新断行。
- 文字颜色从墨迹像素估计；粗体用笔画宽度/字形高度比估计。
- 文本框使用源图像素坐标，构建时映射到 slide 的 `content_box`，不会拉伸。
- 每个文字框会做两项背景复杂度检查：外围颜色稳定性（MAD）和框内非文字像素的
  颜色离散度（std）。背景是照片、3D 物体或复杂插画时，文字**不会**被强行原生化，
  而是保留为底图像素，避免抠字后出现鬼影或 X 形修复痕迹；这些文本记录在
  `manifest.json` 的 `raster_text` 中。扁平卡片、干净的渐变背景上的普通文字仍会
  正常重建为原生文本框。
- 当文字位于**饱和色带/渐变**（丝带、端帽、徽章）上、普通擦除会留下明显色块时，
  editability profile 会分割文字周围的色带区域，拟合线性渐变，并生成原生
  `freeform` 形状覆盖旧文字；文字仍然使用可编辑文本框。`freeform` 按饱和色带的
  轮廓裁剪并略微向内收缩；轮廓用 Chaikin 平滑，渐变改为沿主方向采样 **3 个色标**
  （暗部/中间/亮部），比单一两段渐变更接近 3D 明暗。默认不额外柔化或降透明度；
  独立形状模式（`--keep-backdrop-shapes`）会再加 **1px 边缘羽化**，只用于隐藏
  残留接缝，不会明显发虚。默认烘焙模式会用 **OpenCV Poisson `seamlessClone`** 把
  底板与背景按边界颜色融合，再写入干净背景图，PPT 中不再有单独的色块形状。
  底板中的旧文字用紧贴字形的 inpainting 清除。拟合不可靠（照片、复杂插画、
  色带覆盖不足）时仍保留为 `raster_text`。
- 纯数字短标签（如 `01`/`02`/`03`）位于 3D 圆盘上时，不叠加渐变形状，直接紧贴
  字形修复并原生化，避免数字背后出现矩形色块。
- 普通文字位于非平坦卡片/渐变上时，不再整框平涂，而是按测得的字形 mask 做
  biharmonic/Telea 修复；这样不会把字体颜色或背景色带进填充，也不会留下矩形补丁。
- 彩色标题前的小竖条、同色装饰线等非字形像素会和文字颜色一起识别，不再被误判为
  “复杂背景”。外围是平坦卡片时，这些装饰会随文字一起从底图清除，然后重建为原生
  文本框；如果装饰需要保留，可在 `manifest.json` 中手工补一个形状。
- OCR 框如果包含了 3D 圆盘/丝带的高光细带，测量的行数会被高光误判为两行。脚本会
  丢弃贴边且明显矮于主文字带的细带，按真实文字高度计算字号，避免同一组数字
  （如 `01`/`02`/`03`）字号不一致。

### 背景处理细节

- 文字框外围颜色稳定 → 直接填充该颜色（含渐变时用四角双线性插值）。
- 外围是照片/纹理 → 扩散修复；安装了 OpenCV 时优先用 Telea inpainting。
- 底板与页面背景色几乎一致时，不输出底板图片，直接用原生纯色背景。
- 空白扁平卡片会从底板中抹除，再画成原生 `rect` / `roundRect`。
- 细长且与背景有明显对比的线条会画成原生 `line`，并从底板抹除。
- 大块浅色面板（图表/画布）内检测到大量小碎片时，会保留整个面板为底图，
  避免把柱状图、坐标轴刻度、图例色块拆成互不相干的“图标”。

---

## 已知限制

- **OCR 漏字**：极小、低对比度、艺术字可能识别不到，会留在底图中（仍是像素文字）。
  PP-OCRv6 云端识别通常最完整；本地 RapidOCR 使用 small 模型，更容易漏掉页脚、小字。
  当前页的 `ocr_blocks.json` / `text_hints.png` 可用于检查。
- **复杂背景上的文字**：照片、3D 物体、手写艺术字上的文字会保留为底图像素（记录在
  `raster_text` 中），以保证视觉保真；这意味着这部分文字不可直接编辑。
- **渐变底板的近似**：`freeform` + 线性渐变只能近似色带，无法完全复刻 3D 高光、
  阴影和纹理；放大后可能看到轻微差异。默认烘焙后底板是背景图像素，不能再单独
  编辑形状，但文字仍是原生文本框；需要保留形状对象时加 `--keep-backdrop-shapes`。
  需要严格保真时加 `--no-gradient-backdrops`（balanced profile 默认关闭）。
- **可编辑优先的代价**：`--profile editability` 会提高文字原生化阈值，复杂背景上
  可能出现修复痕迹，文字框内的装饰线（如下划线）也可能被擦除，原生字体也会替换
  掉像素/艺术字体。需要保真时使用 `--profile balanced`。
- **旋转 / 手写文字**：即使被原生化，也按水平文本框重建，会丢失旋转角度和字体风格。
- **表格 / 公式**：不会重建为原生表格/公式，保留在底图中；skill 中这一步由 Agent 用原生表格和 LaTeX 渲染完成。
- **图表内部**：使用默认的 PP-OCRv6 时，坐标轴、刻度、图例等会被逐行识别；在干净背景上它们可能变成独立文本框，柱体/折线仍保留在底图中。这提高了文字可编辑性，但不会重建真正的原生图表。若希望图表内部整体保留为一张 raster 块，使用 `--ocr paddleocr-vl`；图表标题/页脚可加 `--chart-recognition`。要真正可编辑图表，仍需原生柱状图/饼图重建，而不是在图表图片上叠文本框。
- **图标分离默认关闭**：图标、徽章等默认保留在底图中，避免把图表、泡泡、文字碎片误抠成独立对象；需要单独移动图标时显式加 `--icons`。
- **背景修复痕迹**：即使使用 OpenCV，文字抹除后仍可能有轻微模糊或色块。
- **不是图像生成模型**：脚本不会“重绘”背景。skill 中由图像生成模型完成的干净底图/素材表，在这里由 CV 修复替代。

如果需要 skill 级别的语义判断（复杂背景重绘、表格原生重建、公式 LaTeX、图标素材表），可以在这个脚本产出的 `manifest.json` 基础上继续用 `image-to-editable-ppt` 的 `editppt` 运行时精修。
