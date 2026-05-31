#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_query.py — 通用知识树查询器（运行时无关，无需 LLM / 网络）。

四档下钻（由粗到细）：
  L2 全局摘要  →  L1 主题摘要  →  逐文档·详细摘要  →  L0 全文
  --global        topics(下方)    --level detailed     documents/*.md

它只读「同目录的 kb.json + vault 文件」，因此同一份代码放进任何用
local-memory-wiki 构建的 vault 都能用。任何能跑 shell 的 agent
（Claude Code / Codex / OpenClaw / Hermes …）都可直接调用。

用法:
  python kb_query.py "你的问题" [--json] [--top N] [--level short|detailed|full]
  python kb_query.py --topic "主题名"                # 打印 L1 主题摘要
  python kb_query.py --doc <doc_id> --level detailed  # 逐文档·详细摘要(或 short/full)
  python kb_query.py --entity "实体名"                # 打印实体卡片
  python kb_query.py --global                         # 打印 L2 全局摘要
  python kb_query.py --list-topics
"""
import sys
import os
import re
import json
import glob
import argparse

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_kb():
    p = os.path.join(ROOT, "kb.json")
    if not os.path.exists(p):
        sys.stderr.write(
            "[kb_query] 未找到 kb.json（应与本脚本同目录）。"
            "请先在该知识库目录运行 emit_access_bundle.py 生成接入包。\n")
        sys.exit(2)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


KB = _load_kb()
EXTRACTED = os.path.join(ROOT, KB["entrypoints"]["extracted_dir"])


def _toks(s):
    """英文按词、中文按字切分，用于无依赖的关键词重合度打分。"""
    return set(re.findall(r"[a-z0-9]+|[一-鿿]", (s or "").lower()))


def _iter_extracted():
    for f in glob.glob(os.path.join(EXTRACTED, "*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                yield json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue


def search(q, top=5, level="short"):
    """分层检索：返回相关主题(L1)摘要路径 + 候选文档；level=detailed 时附逐文档详细摘要。"""
    qt = _toks(q)
    if not qt:
        return {"error": "empty query"}
    topics = []
    for t in KB["topics"]:
        sc = len(qt & _toks(t["name"] + " " + t.get("one_liner", "")))
        if sc:
            topics.append((sc, t))
    topics.sort(key=lambda x: -x[0])
    docs = []
    for d in _iter_extracted():
        blob = (d.get("short_summary", "") + " " + d.get("detailed_summary", "") + " "
                + " ".join((e.get("text") or "") for e in d.get("entities", [])))
        sc = len(qt & _toks(blob))
        if sc:
            did = d.get("doc_id", "")
            item = {
                "doc_id": did,
                "path": d.get("doc_md", "documents/%s.md" % did),
                "importance": d.get("importance", 0) or 0,
                "short": d.get("short_summary", ""),
            }
            if level in ("detailed", "full"):
                item["detailed"] = d.get("detailed_summary", "")
            docs.append((sc * (0.5 + item["importance"]), item))
    docs.sort(key=lambda x: -x[0])
    return {
        "query": q,
        "level": level,
        "drilldown": "L1 主题摘要(topics.summary_file) → 逐文档详细摘要(--level detailed) → L0 全文(documents 路径)",
        "topics": [{"name": t["name"], "summary_file": t["summary_file"],
                    "one_liner": t.get("one_liner", "")} for _, t in topics[:3]],
        "documents": [it for _, it in docs[:top]],
        "cite_rule": "引用 documents/*.md 路径为据；库内不足再用通用知识/联网。",
    }


def get_topic(name):
    for t in KB["topics"]:
        if t["name"] == name or _toks(name) <= _toks(t["name"]):
            p = os.path.join(ROOT, t["summary_file"])
            if os.path.exists(p):
                return open(p, encoding="utf-8").read()
    return "未找到主题: %s（用 --list-topics 查看全部）" % name


def get_doc(doc, level="detailed"):
    did = doc[:-3] if doc.endswith(".md") else doc
    did = os.path.basename(did)
    if level == "full":
        p = os.path.join(ROOT, "documents", did + ".md")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else "未找到原文: " + did
    p = os.path.join(EXTRACTED, did + ".json")
    if not os.path.exists(p):
        return "未找到抽取记录: " + did
    d = json.load(open(p, encoding="utf-8"))
    return d.get("detailed_summary", "") if level == "detailed" else d.get("short_summary", "")


def get_entity(name):
    safe = "".join("-" if c in '\\/:*?"<>|' else c for c in (name or "").strip())
    hits = glob.glob(os.path.join(ROOT, "entities", "*-" + safe + ".md"))
    if hits:
        return open(hits[0], encoding="utf-8").read()
    return "未找到实体卡片: %s" % name


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="通用知识树查询器（四档下钻）")
    ap.add_argument("question", nargs="?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--level", choices=["short", "detailed", "full"], default="short")
    ap.add_argument("--topic")
    ap.add_argument("--doc")
    ap.add_argument("--entity")
    ap.add_argument("--global", dest="globl", action="store_true")
    ap.add_argument("--list-topics", action="store_true")
    a = ap.parse_args()

    if a.list_topics:
        for t in sorted(KB["topics"], key=lambda x: -x.get("docs", 0)):
            print("%4d  %s" % (t.get("docs", 0), t["name"]))
        return
    if a.globl:
        print(open(os.path.join(ROOT, KB["entrypoints"]["global_summary"]), encoding="utf-8").read())
        return
    if a.topic:
        print(get_topic(a.topic))
        return
    if a.doc:
        print(get_doc(a.doc, a.level))
        return
    if a.entity:
        print(get_entity(a.entity))
        return
    if not a.question:
        ap.print_help()
        return

    r = search(a.question, a.top, a.level)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    print("【先读·主题摘要 L1】")
    for t in r["topics"]:
        print("  - %s  (%s)" % (t["summary_file"], (t["one_liner"] or "")[:50]))
    print("\n【按需·候选文档】(--level detailed 看逐篇详细摘要; --level full / 读路径看原文)")
    for d in r["documents"]:
        print("  - %s  imp=%.2f  %s" % (d["path"], d["importance"], (d["short"] or "")[:46]))
        if a.level == "detailed" and d.get("detailed"):
            print("      详: " + d["detailed"][:140] + "…")
    print("\n【引用规则】" + r["cite_rule"])


if __name__ == "__main__":
    main()
