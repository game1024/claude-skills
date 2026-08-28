---
name: blender-subtitle-translate
description: Use when the user asks to translate English .srt subtitle files into Chinese (英文字幕翻译成中文). Preserves numbering and timestamps; writes a new <原名>.zh.srt file (.en.srt → .zh.srt, dropping the .en). After translation, copies the .mp4/.en.srt/.zh.srt to the output folder with a user-supplied Part prefix (e.g. Part1-1).
---

# SRT 英文字幕 → 中文翻译

## 输入
- 单个 `.srt` 文件路径，或一个目录（批量翻译其中所有未翻译的 .srt）
- 超长文件（约 2000 行以上）用 Read 的 offset/limit 分段读取，按顺序处理

## 翻译规则
1. 只翻译文本行。序号、时间轴（`HH:MM:SS,mmm --> HH:MM:SS,mmm`）和空行结构原样保留
2. 一个条目的文本跨多行时整体翻译，行数可以变化；不跨条目合并或拆分语义
3. 保留 `<i>`、`<font>`、`{\an8}` 等字幕标签和 `♪`、`[...]` 等符号，只译其中文字
4. 技术内容（如 Blender 教程）：软件界面名称、菜单、快捷键保留英文；专业术语首次出现用「中文（English）」格式，之后只用中文。**术语译法以词表（glossary.md）为准**，如 Viewport → 视图（不要译成「视口」）
5. 翻译符合中文口语习惯，避免直译腔；结合前后条目的语境理解再译
6. 不确定的专有名词或谐音梗，保留英文原文并在其后用括号注明译文

## 词表
- 翻译开始前先阅读 `glossary.md`（Blender 建模术语英中对照），译法以词表为准
- 词表未收录的术语按规则 4、5 处理

## 输出
- 写入新文件 `<原名>.zh.srt`（如 `video.srt` → `video.zh.srt`），不覆盖原文件
- 原文件带 `.en` 后缀时去掉它再添加：`video.en.srt` → `video.zh.srt`，不要生成 `video.en.zh.srt`
- UTF-8 编码；若播放器出现乱码，提示用户可改用带 BOM 的编码重新保存

## 批量模式
1. 用 Glob 搜索 `**/*.srt`，排除 `*.zh.srt`
2. 已存在对应 `.zh.srt` 的文件默认跳过（`.en.srt` 对应的是去掉 `.en` 后的 `<原名>.zh.srt`）
3. 逐个翻译，最后报告：成功 N 个 / 跳过 M 个 / 失败 K 个

## 完成自检
- `.zh.srt` 条目数量与原文件一致
- 所有时间轴与原文完全相同

## 输出归档（翻译结束后执行）
见工作区 `CLAUDE.md`（输出归档与重命名规则已抽取至该文件）。
