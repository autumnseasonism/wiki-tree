#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""emit_doc_summaries.py — 可选：把每篇文档的「短摘要 + 详细摘要」落成可见的
   summaries/doc-<id>.md。默认不做——逐文档详细摘要本就存于 .wiki-tree/extracted/，
   kb_query.py --level detailed 即可取用，无需文件。仅当语料是「长文档」且需要在
   Obsidian 里逐篇浏览/双链摘要时才用（短文档语料会让文件数翻倍、收益小）。

用法: python emit_doc_summaries.py --vault VAULT
"""
import os
import sys
import json
import glob
import argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True)
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    vault = os.path.abspath(a.vault)
    ext = os.path.join(vault, ".wiki-tree", "extracted")
    out = os.path.join(vault, "summaries")
    os.makedirs(out, exist_ok=True)
    n = 0
    for f in glob.glob(os.path.join(ext, "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        did = d.get("doc_id", os.path.basename(f)[:-5])
        body = (
            "---\nkind: summary\ndoc_id: %s\ntags:\n  - summary\n  - source/local-files\n---\n\n"
            "# 摘要：%s\n\n**短摘要**：%s\n\n## 详细摘要\n%s\n\n## 原文\n- [[%s]]\n"
            % (did, did, d.get("short_summary", ""), d.get("detailed_summary", ""), did)
        )
        open(os.path.join(out, "doc-%s.md" % did), "w", encoding="utf-8").write(body)
        n += 1
    print("生成逐文档摘要文件: %d 篇 -> summaries/doc-*.md" % n)


if __name__ == "__main__":
    main()
