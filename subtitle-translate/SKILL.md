---
name: subtitle-translate
description: Use when the user asks to translate English .srt subtitle files into Chinese (英文字幕翻译成中文)，适用于 Blender/CG Fast Track 教程与数学课程（MIT 6.042J、18.06SC 等）两类字幕。保留序号与时间轴，输出 <原名>.zh.srt（.en.srt 去掉 .en）；超过 200 条的长字幕自动走分片翻译后合并；按输入路径选用对应领域词表与规则（references/blender 或 references/math）。
---

# SRT 英文字幕 → 中文翻译（Blender/CG 与数学课程统一技能）

## 领域判定（先判定，再翻译）
按输入路径判断课程类型，**先读取并遵循对应领域的规则文件与词表**：

- **Blender / CG Fast Track 教程**（路径含 `CG Fast Track` 等）→ 读取 `${CLAUDE_SKILL_DIR}/references/blender/workflow.md` 与 `${CLAUDE_SKILL_DIR}/references/blender/glossary.md`
- **数学课程**（如 `MIT 6.042J Mathematics for Computer Science, Spring 2015`、`MIT 18.06SC Linear Algebra, Fall 2011` 等目录）→ 读取 `${CLAUDE_SKILL_DIR}/references/math/workflow.md` 与 `${CLAUDE_SKILL_DIR}/references/math/glossary.md`
- 无法从路径判断时，按内容特征（建模/软件术语 vs 数学公式）判定领域，并在最终报告中说明所采用的领域规则

## 公共翻译规则（所有领域通用）
1. 只翻译文本行。序号、时间轴（`HH:MM:SS,mmm --> HH:MM:SS,mmm`）和空行结构原样保留
2. 一个条目的文本跨多行时整体翻译，译文条目内尽量写成一行，仅在句子结束处（句号/问号/感叹号/省略号）断行；不跨条目合并或拆分语义
3. 保留 `<i>`、`<font>`、`{\an8}` 等字幕标签和 `♪`、`[...]` 等符号，只译其中文字
4. 术语首次出现用「中文（English）」格式，之后只用中文；具体译法以命中领域的词表（glossary.md）为准
5. 翻译符合中文口语习惯，避免直译腔；结合前后条目的语境理解再译
6. 不确定的专有名词或谐音梗，保留英文原文并在其后用括号注明译文
7. 输出写入新文件 `<原名>.zh.srt`（`video.en.srt` → `video.zh.srt`，去掉 `.en`，不要生成 `video.en.zh.srt`），不覆盖原文件；UTF-8 编码（若播放器乱码，提示用户可改用带 BOM 的编码重新保存）

## 批量模式
1. 用 Glob 搜索 `**/*.srt`，排除 `*.zh.srt`
2. 已存在对应 `.zh.srt` 的文件默认跳过（`.en.srt` 对应去掉 `.en` 后的 `<原名>.zh.srt`）
3. 逐个文件按「单片 / 分片」流程处理，最后报告：成功 N 个 / 跳过 M 个 / 失败 K 个

## 分片规则（文件较长时使用，两个领域一致）
- **判据**：条目数 > 200（或文件约 2000 行以上）→ 必须先分片翻译，不可一次性硬译

1. **join**（预处理：合并自动字幕拆在句子中间的断行，只在句末符号处保留换行；默认原地修改，`--out` 可另存新文件）：
   `python "${CLAUDE_SKILL_DIR}/tools/split_srt.py" join "<字幕路径>"`
2. **split**（按条目拆分，保留原序号和时间轴，输出到字幕同目录 `_split_parts/<原名>.partNN.srt`）：
   `python "${CLAUDE_SKILL_DIR}/tools/split_srt.py" split "<字幕路径>" --per 200`
3. 若只有 1 片（总条数 ≤ 200），说明文件不长，按单片直接翻译即可
4. **一次只处理一个分片**：Read `partNN.srt` → 按规则完整翻译 → 写出 `_split_parts/<原名>.partNN.zh.srt`，处理完再做下一片（不要多个分片同时进入上下文）
5. 全部分片翻译完成后 **merge**（按分片顺序拼接，序号连续、时间轴不变）：
   `python "${CLAUDE_SKILL_DIR}/tools/split_srt.py" merge "<_split_parts 目录>" "<原目录>/<原名>.zh.srt" --zh`
   - merge 只收集该视频自己的 zh 分片；**多个视频的分片同处一个 `_split_parts` 时，逐个视频完成「merge → 自检 → 清理该视频分片」后再放入下一个视频的分片**
6. 对合并后的中文文件再执行一次 **join**，兜底修复条目内残留的不必要断行
7. 删除临时目录 `_split_parts`

## 完成自检（每个文件都要做）
- `.zh.srt` 条目数量与原文件一致
- 所有时间轴与原文完全相同（diff 为空）
- 公式、字幕标签、变量与原文一致
- 分片文件额外核对：空 cue 集合与原文一致（cue 块只有序号+时间轴两行、无文本行）

## 归档
本技能**不定义归档规则**：翻译完成并自检后，如需把成果复制到归档目录，一律按工作区 `CLAUDE.md` 及对应课程的实际约定执行（不同课程的命名与目标目录不同，翻译阶段不处理归档）。
