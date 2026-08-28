---
name: math-subtitle-translate
description: Use when the user asks to translate English .srt subtitle files for math videos into Chinese (数学视频英文字幕翻译成中文，如微积分、线性代数、概率统计等课程). Splits long subtitles into small parts before translating to stay within context limits, then merges them back. Preserves numbering and timestamps; writes a new <原名>.zh.srt file (.en.srt → .zh.srt, dropping the .en).
---

# SRT 数学视频英文字幕 → 中文翻译

## 输入
- 单个 `.srt` 文件路径，或一个目录（批量翻译其中所有未翻译的 .srt）

## 预处理：修断行 + 分片（翻译前先执行）
数学视频时长可能达 40 分钟 ~ 1 小时（字幕约 600~1200 条），一次性翻译会超出上下文长度，**必须先分片**。自动生成字幕常把一句拆成多行（条内不必要的换行），**先修复再分片**：

1. 运行本技能目录下的脚本合并条目内不必要的断行（只合并句子中间的分行，句号/问号等句末符处保留换行；`--out` 可另存新文件，默认原地修改）：
   在本技能目录下执行：`python split_srt.py join "<字幕路径>"`
2. 运行拆分脚本（`python` 不可用时改试 `py`）：
   `python split_srt.py split "<字幕路径>" --per 200`
3. 脚本按条目拆分，**保留原序号和时间轴**，输出到 `<字幕同目录>/_split_parts/`，命名为 `<原名>.part01.srt`、`<原名>.part02.srt`……
4. 若只有 1 片（总条数 ≤ 200），说明文件不长，后续按单文件流程处理即可

## 翻译规则
1. 只翻译文本行。序号、时间轴（`HH:MM:SS,mmm --> HH:MM:SS,mmm`）和空行结构原样保留
2. 一个条目的文本跨多行时整体翻译，**译文条目内尽量写成一行**，仅在句子结束处（句号/问号/感叹号/省略号等）才可断行；不要照搬英文原文的分行。不跨条目合并或拆分语义
3. 保留 `<i>`、`<font>`、`{\an8}` 等字幕标签和 `♪`、`[...]` 等符号，只译其中文字
4. **公式与数学符号原样保留**：
   - 字幕里的公式、LaTeX（`\frac{a}{b}`、`x^2 + 1`、`\sum_{i=1}^{n}`）和数学符号（`≤ ≥ ≠ ∈ ∑ ∫ √ π ∞`）不动，只翻译公式前后的文字
   - 变量名（x、y、n、f(x)、ε、δ）原样保留，不替换
   - 自动生成字幕常把口述公式记成文字（如 "x squared"、"the square root of x"、"pi over two"），翻译时按中文数学读法还原：x 的平方（或 x²）、x 的平方根（或 √x）、π/2，能写成符号时优先写符号
5. 数学术语翻译**以词表（glossary.md）为准**，首次出现用「中文（English）」格式，之后只用中文。例：derivative → 导数（derivative），之后 → 导数
6. 定理名、数学家姓名用通行中文译名（Euler → 欧拉、Pythagorean theorem → 毕达哥拉斯定理），不确定的保留英文并在其后用括号注明译文
7. 翻译符合中文口语习惯，避免直译腔；结合前后条目的语境理解再译。注意数学课堂常用语，如 "let's say" → 比如说、"let x be ..." → 令 x 为……、"suppose that" → 假设、QED → 证毕

## 词表
- 翻译开始前先阅读 `glossary.md`（数学术语英中对照），译法以词表为准
- 词表未收录的术语按规则 4、5 处理

## 分片翻译
- **一次只处理一个分片**：Read 一个 `partNN.srt` → 按翻译规则完整翻译 → Write 为 `_split_parts/<原名>.partNN.zh.srt`，处理完再做下一片，避免多个分片同时进入上下文
- 每个分片独立应用全部翻译规则；不跨分片合并或拆分条目

## 合并（全部分片翻译完成后执行）
1. 运行合并脚本：
   在本技能目录下执行：`python split_srt.py merge "<_split_parts 目录>" "<原目录>/<原名>.zh.srt" --zh`
2. 合并 = 按分片顺序拼接，序号连续、时间轴不变
3. 对合并后的中文文件再执行一次 join，兜底修复条目内残留的不必要断行：
   `python split_srt.py join "<原目录>/<原名>.zh.srt"`
4. 删除临时目录 `_split_parts`
5. 执行完成自检（见下）

## 输出
- 最终产物 `<原名>.zh.srt`（如 `video.srt` → `video.zh.srt`），不覆盖原文件
- 原文件带 `.en` 后缀时去掉它再添加：`video.en.srt` → `video.zh.srt`，不要生成 `video.en.zh.srt`
- UTF-8 编码；若播放器出现乱码，提示用户可改用带 BOM 的编码重新保存

## 批量模式
1. 用 Glob 搜索 `**/*.srt`，排除 `*.zh.srt`
2. 已存在对应 `.zh.srt` 的文件默认跳过（`.en.srt` 对应的是去掉 `.en` 后的 `<原名>.zh.srt`）
3. 每个文件都按「分片 → 分片翻译 → 合并」流程处理
4. 全部完成后报告：成功 N 个 / 跳过 M 个 / 失败 K 个

## 完成自检
- `.zh.srt` 条目数量与原文件一致
- 所有时间轴与原文完全相同
- 公式、LaTeX、变量名与数学符号与原文一致
