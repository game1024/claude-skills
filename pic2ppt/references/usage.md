# pic2ppt

> 本文件是脚本的完整说明。在 skill 中，脚本位于
> `${CLAUDE_SKILL_DIR}/tools/pic2ppt.py`，依赖文件位于
> `${CLAUDE_SKILL_DIR}/tools/requirements.txt`；下文示例里的 `pic2ppt.py`
> 都可以替换为该路径。

把幻灯片截图 / 图片转换成**可编辑的 PowerPoint（.pptx）**。

这个脚本参考了 `image-to-editable-ppt` skill 的核心原理，并用 OCR + 传统计算机视觉把原来由视觉 Agent 完成的判断自动化：

1. **背景优先**：先判断背景是纯色/简单设计，还是照片/复杂设计。
   - 简单背景 → 用 PPT 原生填充，或生成干净底板。
   - 复杂背景 → 保留为“干净底板”（clean base）：把可编辑文字和已分离的前景对象抹掉后的背景图。
2. **前景其次**：图标、徽章等非文字视觉对象通过连通域 + 颜色距离抠图分离为透明 PNG，而不是继续焊死在底板里。
3. **原生元素最后**：文字、扁平卡片、分隔线用真正的 PPT 对象重建。所有对象以源图像素坐标写进 `manifest.json`，再由确定性的 OOXML writer 生成 `.pptx`。

最终产物**不是**“整页截图 + 文字浮层”：底板里的可编辑文字已经被移除，文字、卡片、线条都是真实可编辑对象。

---

## 安装

需要 Python 3.10+。

```bash
pip install -r requirements.txt
```

`requirements.txt` 包含 `opencv-python-headless`，它负责文字抹除时的 Telea inpainting，
对照片/渐变背景的修复质量影响很大。没有 OpenCV 时脚本仍可运行，但背景修复会退化为
纯 numpy 扩散填充。

可选的本地 OCR：

```bash
# 完全本地 OCR（中英文）
pip install rapidocr onnxruntime

# 或者使用 Tesseract
pip install pytesseract
# 并安装 Tesseract 可执行文件与 chi_sim 语言包
```

## OCR 配置

脚本按以下顺序自动选择 OCR：

1. **PaddleOCR-VL**（需要 token，效果最好）
2. **RapidOCR**（本地，无需联网）
3. **Tesseract**
4. **none**（不 OCR；文字会保留在底图里，PPT 不是完全可编辑）

PaddleOCR-VL token 的读取顺序：

1. `--paddle-token`
2. 环境变量 `PADDLE_OCR_TOKEN`
3. `~/.editppt/config.yaml` 里的 `PADDLE_OCR_TOKEN`
   （与 `editppt config --paddle-ocr-token` 保存的位置一致）

免费 token 申请：<https://aistudio.baidu.com/account/accessToken>

> 使用 PaddleOCR-VL 时，图片会上传到百度 PaddleOCR 服务；如果素材涉密，请改用
> `--ocr rapidocr` 或 `--ocr none`。

---

## 用法

```bash
# 单张图片，输出到同目录 <name>_editable.pptx
python pic2ppt.py slide.png

# 多张图片合成一个 deck（每张一页）
python pic2ppt.py slide1.png slide2.png slide3.png -o deck.pptx

# 强制使用本地 OCR
python pic2ppt.py slide.png --ocr rapidocr

# 只重建文字和图标，不把扁平卡片/线条原生化
python pic2ppt.py slide.png --no-shapes

# 不分离前景图标（复杂背景上抠图效果不好时使用）
python pic2ppt.py slide.png --no-icons

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
| `--ocr` | `auto` / `paddleocr-vl` / `rapidocr` / `tesseract` / `none` |
| `--paddle-token` | PaddleOCR-VL token |
| `--paddle-model` | 默认 `PaddleOCR-VL-1.6` |
| `--mode` | `auto` / `native` / `image`（背景与元素重建策略） |
| `--no-icons` | 不分离前景图标 |
| `--no-shapes` | 不检测扁平卡片/分隔线 |
| `--no-text-fit` | 关闭字号自动收缩 |
| `--raster-text-mad` | 背景复杂度阈值，默认 15；调大可将更多文字原生化，调小则更保守 |
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
        icon_001.png          # 分离出的前景图标（带 alpha）
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
| 页面清单 / 文字检测 | PaddleOCR-VL / RapidOCR / Tesseract |
| 文字行测量、字号分组 | Otsu 二值化 + ink band 测量（移植自 skill） |
| 背景判定与修复 | 颜色量化/边缘密度判定；纯色/渐变填充，复杂区域用扩散或 OpenCV inpainting |
| 前景素材分离 | 调色板距离 + 局部中值差分 + 连通域 + 颜色距离 alpha matte |
| 原生形状重建 | 扁平空卡片 → `rect`/`roundRect`；长直线 → `line` |
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
- 每个文字框外围的背景复杂度会被测量（median absolute deviation）。背景是照片、
  3D 物体或复杂插画时，文字**不会**被强行原生化，而是保留为底图像素，避免抠字后
  出现鬼影或 X 形修复痕迹；这些文本记录在 `manifest.json` 的 `raster_text` 中。
  扁平卡片、渐变背景上的普通文字仍会正常重建为原生文本框。

### 背景处理细节

- 文字框外围颜色稳定 → 直接填充该颜色（含渐变时用四角双线性插值）。
- 外围是照片/纹理 → 扩散修复；安装了 OpenCV 时优先用 Telea inpainting。
- 底板与页面背景色几乎一致时，不输出底板图片，直接用原生纯色背景。
- 空白扁平卡片会从底板中抹除，再画成原生 `rect` / `roundRect`。
- 细长且与背景有明显对比的线条会画成原生 `line`，并从底板抹除。

---

## 已知限制

- **OCR 漏字**：极小、低对比度、艺术字可能识别不到，会留在底图中（仍是像素文字）。
  当前页的 `ocr_blocks.json` / `text_hints.png` 可用于检查。
- **复杂背景上的文字**：照片、3D 物体、手写艺术字上的文字会保留为底图像素（记录在
  `raster_text` 中），以保证视觉保真；这意味着这部分文字不可直接编辑。
- **旋转 / 手写文字**：即使被原生化，也按水平文本框重建，会丢失旋转角度和字体风格。
- **表格 / 公式**：不会重建为原生表格/公式，保留在底图中；skill 中这一步由 Agent 用原生表格和 LaTeX 渲染完成。
- **复杂背景上的图标**：为避免抠图出现光晕，会保留在底图中；可用 `--no-icons` 关闭全部抠图。
- **背景修复痕迹**：即使使用 OpenCV，文字抹除后仍可能有轻微模糊或色块。
- **不是图像生成模型**：脚本不会“重绘”背景。skill 中由图像生成模型完成的干净底图/素材表，在这里由 CV 修复替代。

如果需要 skill 级别的语义判断（复杂背景重绘、表格原生重建、公式 LaTeX、图标素材表），可以在这个脚本产出的 `manifest.json` 基础上继续用 `image-to-editable-ppt` 的 `editppt` 运行时精修。
