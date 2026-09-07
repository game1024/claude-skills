#!/usr/bin/env python3
"""SRT 字幕分片工具：按条目数量拆分（保留原序号/时间轴），翻译完成后合并。

用法：
  python split_srt.py split <input.srt> [--per 200] [--out DIR]
  python split_srt.py merge <parts_dir> <output.srt> [--zh]
  python split_srt.py join <input.srt> [--out FILE]
"""
import argparse
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def read_srt(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法解码文件：{path}")


def blocks(text: str):
    """按空行把字幕文本拆成条目块列表（每个块 = 序号行 + 时间轴行 + 文本行）。

    剥离行尾 \\r：Windows 上 Path.write_text 的文本模式会把 \\n 转成 \\r\\n，
    若行尾残留 \\r 会写出双重回车 \\r\\r\\n，播放器会判定字幕损坏。
    """
    parts = re.split(r"\r?\n\s*\r?\n", text.strip("\n"))
    return [p.strip("\n").replace("\r", "") for p in parts if p.strip()]


def split_cmd(args):
    src = Path(args.input)
    text = read_srt(src)
    bs = blocks(text)
    if not bs:
        print(f"未解析到字幕条目：{src}", file=sys.stderr)
        return 1
    out_dir = Path(args.out) if args.out else src.parent / "_split_parts"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = src.stem
    n_parts = (len(bs) + args.per - 1) // args.per
    for i in range(n_parts):
        chunk = bs[i * args.per:(i + 1) * args.per]
        out = out_dir / f"{stem}.part{i + 1:02d}.srt"
        out.write_text("\n\n".join(chunk) + "\n", encoding="utf-8")
    print(f"共 {len(bs)} 条字幕，拆成 {n_parts} 片（每片 ≤ {args.per} 条），输出到 {out_dir}")
    for i in range(n_parts):
        cnt = min(args.per, len(bs) - i * args.per)
        print(f"  {stem}.part{i + 1:02d}.srt : {cnt} 条")
    return 0


def merge_cmd(args):
    parts_dir = Path(args.parts_dir)
    pattern = "*.zh.srt" if args.zh else "*.srt"
    files = sorted(parts_dir.glob(pattern))
    if not files:
        print(f"在 {parts_dir} 找不到匹配 {pattern} 的分片文件", file=sys.stderr)
        return 1
    merged = []
    for f in files:
        merged.extend(blocks(read_srt(f)))
    out = Path(args.output)
    out.write_text("\n\n".join(merged) + "\n", encoding="utf-8")
    print(f"合并 {len(files)} 个分片 → {out}（共 {len(merged)} 条字幕）")
    return 0


_CJK_RE = re.compile(r"[一-鿿　-〿＀-￯]")
_EN_SENT_END = ".!?…:;"
_ZH_SENT_END = "。！？…：；"


def join_cmd(args):
    """合并条目内不必要的断行：一句被拆成多行时拼回一行，仅在句子结束处保留换行。

    语言自动识别：条目含 CJK 字符按中文处理（拼接不加空格，句末符 。！？…：；），
    否则按英文处理（拼接加空格，句末符 .!?…:;）。只改文本行，序号、时间轴、空行结构不变。
    """
    src = Path(args.input)
    bs = blocks(read_srt(src))
    if not bs:
        print(f"未解析到字幕条目：{src}", file=sys.stderr)
        return 1
    out_blocks = []
    changed = 0
    for b in bs:
        lines = b.split("\n")
        if len(lines) < 3:  # 序号行 + 时间轴行 之后没有文本行
            out_blocks.append(b)
            continue
        texts = lines[2:]
        if not any(t.strip() for t in texts):
            out_blocks.append(b)
            continue
        is_zh = any(_CJK_RE.search(t) for t in texts)
        ends = _ZH_SENT_END if is_zh else _EN_SENT_END
        sep = "" if is_zh else " "
        new_texts = []
        cur = ""
        for i, t in enumerate(texts):
            s = t.strip()
            if not s:
                continue
            cur = s if not cur else cur + sep + s
            if i < len(texts) - 1 and s[-1] in ends:
                new_texts.append(cur)
                cur = ""
        if cur:
            new_texts.append(cur)
        if new_texts != texts:
            changed += 1
        out_blocks.append("\n".join(lines[:2] + new_texts))
    out = Path(args.out) if args.out else src
    out.write_text("\n\n".join(out_blocks) + "\n", encoding="utf-8")
    print(f"join：{src} → {out}（{len(out_blocks)} 条字幕，其中 {changed} 条合并了断行）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="SRT 字幕分片/合并工具")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("split", help="按条目数拆分成小分片")
    sp.add_argument("input", help="输入 .srt 路径")
    sp.add_argument("--per", type=int, default=200, help="每片最多条目数（默认 200）")
    sp.add_argument("--out", help="分片输出目录（默认 <输入同目录>/_split_parts）")
    sp.set_defaults(fn=split_cmd)
    mp = sub.add_parser("merge", help="合并分片（默认合并 *.srt，--zh 合并 *.zh.srt 译文）")
    mp.add_argument("parts_dir", help="分片所在目录")
    mp.add_argument("output", help="合并输出 .srt 路径")
    mp.add_argument("--zh", action="store_true", help="合并译文分片（*.zh.srt）")
    mp.set_defaults(fn=merge_cmd)
    jp = sub.add_parser("join", help="合并条目内不必要的断行（默认原地修改，--out 另存新文件）")
    jp.add_argument("input", help="输入 .srt 路径")
    jp.add_argument("--out", help="输出 .srt 路径（默认覆盖输入文件）")
    jp.set_defaults(fn=join_cmd)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
