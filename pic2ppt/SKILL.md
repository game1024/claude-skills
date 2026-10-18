---
name: pic2ppt
description: 把幻灯片截图或图片转换成可编辑 PowerPoint（.pptx）的技能。当用户提到图片转PPT、截图转PPT、图片做成PPT、可编辑PPT、image to PPT、slide screenshot to PPT、把这张幻灯片重做成PPT等时使用。流程：用 tools/pic2ppt.py 自动 OCR（PaddleOCR-VL / RapidOCR / Tesseract），清理背景、分离图标、把文字/扁平卡片/分隔线重建为 PPT 原生对象，输出 .pptx 以及 manifest、预览图和原图对比图；支持修改 manifest 后用 --from-manifest 重建。
---

# 图片 → 可编辑 PPT（pic2ppt）

## 依赖（首次使用前确认）

```bash
python -m pip install -r "${CLAUDE_SKILL_DIR}/tools/requirements.txt"
```

必需：Pillow、numpy、scipy、requests。`requirements.txt` 里的 `opencv-python-headless`
负责背景文字擦除的 Telea inpainting，建议保留；缺失时脚本会退化为纯 numpy 扩散填充。

可选 OCR：RapidOCR（本地）或 Tesseract。没有本地 OCR 时，脚本会自动读取
PaddleOCR-VL token（若已配置）。

## 核心原则（不要把整页截图直接贴进 PPT）

1. **背景优先**：判断纯色/简单背景还是照片/复杂背景；文字和已分离对象从底图中移除，
   得到干净底板。
2. **前景其次**：图标等非文字对象分离为透明 PNG。
3. **原生元素最后**：文字、扁平卡片、分隔线用真实 PPT 对象重建。
4. 所有对象以源图像素坐标写入 `manifest.json`，再由确定性 OOXML writer 生成 `.pptx`。

## 执行流程

### 1. 生成

```bash
python "${CLAUDE_SKILL_DIR}/tools/pic2ppt.py" "<图片1>" ["<图片2>" ...] -o "<输出.pptx>"
```

默认 OCR 顺序：PaddleOCR-VL（有 token 时）→ RapidOCR → Tesseract → none。
涉密、离线或用户明确要求本地处理时，显式指定本地后端：

```bash
python "${CLAUDE_SKILL_DIR}/tools/pic2ppt.py" "<图片>" -o "<输出.pptx>" --ocr rapidocr
python "${CLAUDE_SKILL_DIR}/tools/pic2ppt.py" "<图片>" -o "<输出.pptx>" --ocr none
```

`--ocr none` 仍会生成 PPT，但文字保留在底图中，不是完全可编辑；必须在结果里说明。

### 2. 检查结果

1. 确认命令退出码为 0，输出文件存在。
2. 读取 `<workdir>/validation.json`，检查顶层 `passed` 和 `warnings`。
3. 用 Read 查看 `<page_dir>/comparison.png`（左：原图，右：重建），逐项核对：
   - 标题、正文、卡片文字的位置和字号是否接近原图；
   - 是否有明显鬼影、色块、错位、文字溢出；
   - 图标是否完整、没有把文字误抠成图标；
   - 复杂背景上的文字是否按预期保留为像素（见 `manifest.json.raster_text`）。
4. 如验证失败，读取 `errors` 并如实报告；不要伪造成功。

### 3. 报告

最终回复至少包含：

- 输出 `.pptx` 的绝对路径；
- validation 结果；
- 原生文本框 / 原生形状 / 图片数量；
- `raster_text` 中的文字（这些是不可编辑的保留项）；
- 使用的 OCR 后端，以及是否发生了联网调用；
- 产物目录（manifest、preview、comparison、text_hints）。

## 常用选项

| 选项 | 说明 |
| --- | --- |
| `-o, --output` | 输出 `.pptx` 路径 |
| `--workdir` | 中间产物目录，默认 `<输出名>_work` |
| `--ocr` | `auto` / `paddleocr-vl` / `rapidocr` / `tesseract` / `none` |
| `--paddle-token` | PaddleOCR-VL token（一般不要用，优先环境变量/config） |
| `--mode` | `auto` / `native` / `image`（背景与元素重建策略） |
| `--no-icons` | 不分离前景图标（复杂背景抠图效果差时使用） |
| `--no-shapes` | 不把扁平卡片/分隔线原生化 |
| `--no-text-fit` | 关闭字号自动收缩 |
| `--raster-text-mad` | 背景复杂度阈值，默认 15；调大可让更多文字原生化 |
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
    assets/icon_*.png    # 分离出的透明图标
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
- `--ocr auto` 只要发现 token 就会优先走 PaddleOCR-VL，会上传待转换的页面图片到
  百度 PaddleOCR 服务。用户要求离线、涉密或本地处理时，必须显式使用
  `--ocr rapidocr` 或 `--ocr none`。
- 除 PaddleOCR-VL 外，脚本运行时没有其他显式联网调用；RapidOCR 的某些版本首次运行
  可能自行下载模型文件，需向用户说明。

## 限制

- 表格和公式不会重建为原生对象，保留在底图中。
- 照片、3D 物体、手写艺术字上的文字会保留为底图像素（记录在 `raster_text`），
  以保证视觉保真；这部分文字不可直接编辑。
- OCR 可能漏掉极小/低对比度文字；检查 `text_hints.png`。
- 旋转文字按水平文本框重建，可能丢失旋转角度和字体风格。
- 这是 CV 修复，不是图像生成模型：不会“重绘”背景，复杂背景可能有轻微修复痕迹。
- 需要 AI 级干净底图、原生表格或公式时，改用 `image-to-editable-ppt` skill 的
  `editppt` 运行时继续精修。
