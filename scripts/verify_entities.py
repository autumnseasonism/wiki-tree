#!/usr/bin/env python3
"""
verify_entities.py — 抽取后的确定性「幻觉闸门」：只保留确实在原文出现的实体。

修正了纯 text.find 子串匹配的误放问题，按实体名分两种规则判断"是否在原文出现"：
  - 纯 ASCII 实体名 → **词边界匹配**（避免 "AI" 误命中 "WAIT"/"available" 这类子串）
  - 含非 ASCII（中文等）实体名 → **子串匹配**（CJK 无词边界概念，子串是务实选择）

用法:
  python verify_entities.py --doc documents/x.md --entities entities.json [-o filtered.json]

--entities 接受 {"entities": [{kind,text},...]} 或裸列表 [{kind,text},...]
--doc 为已转换的 .md（自动剥离 YAML front-matter，只对正文匹配）
输出: {"entities": [保留...], "dropped": [{text, reason}...], "kept_count", "dropped_count"}
"""

import re
import sys
import json
import argparse
from pathlib import Path


def strip_front_matter(text: str) -> str:
    """剥离开头的 YAML front-matter（--- ... ---），只留正文。"""
    if not text.startswith("---"):
        return text
    lines = text.splitlines(keepends=True)
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[i + 1:])
    return text  # 没有闭合 ---，整体当正文


def is_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in s)


def entity_present(text: str, content: str) -> bool:
    """实体名是否在正文出现：纯 ASCII 按词边界，含非 ASCII 按子串。"""
    text = (text or "").strip()
    if not text:
        return False
    if is_ascii(text):
        return re.search(r"\b" + re.escape(text) + r"\b", content, re.IGNORECASE) is not None
    return text in content


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="抽取后幻觉闸门：过滤未在原文出现的实体")
    parser.add_argument("--doc", required=True, help="已转换的 .md 文档路径")
    parser.add_argument("--entities", required=True, help="实体 JSON（{'entities':[...]} 或裸列表）")
    parser.add_argument("-o", "--output", help="输出 JSON（默认 stdout）")
    args = parser.parse_args()

    content = strip_front_matter(Path(args.doc).read_text(encoding="utf-8"))

    with open(args.entities, "r", encoding="utf-8") as f:
        data = json.load(f)
    ents = data.get("entities", data) if isinstance(data, dict) else data
    if not isinstance(ents, list):
        raise SystemExit("--entities 应为列表，或含 entities 字段的对象")

    kept, dropped = [], []
    for e in ents:
        text = e.get("text") if isinstance(e, dict) else str(e)
        if entity_present(text, content):
            kept.append(e)
        else:
            dropped.append({"text": text, "reason": "未在原文出现（按词边界/子串规则）"})

    result = {
        "entities": kept,
        "dropped": dropped,
        "kept_count": len(kept),
        "dropped_count": len(dropped),
    }
    out = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"幻觉闸门：保留 {len(kept)}，丢弃 {len(dropped)} → {args.output}")
    else:
        print(out)


if __name__ == "__main__":
    main()
