#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_query.py — 通用知识树查询器（运行时无关，无需 LLM / 网络）。

四档下钻（由粗到细）：
  L2 全局摘要  →  L1 主题摘要  →  逐文档·详细摘要  →  L0 全文
  --global        topics(下方)    --level detailed     documents/*.md

它只读「同目录的 kb.json + vault 文件」，因此同一份代码放进任何用
wiki-tree 构建的 vault 都能用。任何能跑 shell 的 agent
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
import math
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


# CJK 单字停用字（与 kb_ingest.py 同源）：只滤单字 token，bigram 不滤
CJK_STOP = set("的了和是在与及或该本为也都把从这那个等对之其由以并而且但即则若如因故被让给向自"
               "至于各已未可须应要会能将就只更最不无有")


def _toks(s):
    """英文 [a-z0-9]+ 按词；CJK 连续段以重叠 bigram 为主、单字为辅（单字滤停用字）。
    必须与 emit_access_bundle.py 建索引的分词严格一致，改动需三处同步（emit/本文件/kb_hub_server）。"""
    out = set()
    for seg in re.findall(r"[a-z0-9]+|[一-鿿]+", (s or "").lower()):
        if seg.isascii():
            out.add(seg)
        else:
            out.update(a + b for a, b in zip(seg, seg[1:]))
            out.update(c for c in seg if c not in CJK_STOP)
    return out


def _idf_score(qt, toks, df, n):
    """交集 token 的 IDF 加权和 sum(log((N+1)/(df+1))+1)；
    无 df（v1 旧索引 / 回退扫 extracted）时退化为每 token 权重 1.0。"""
    hit = qt & toks
    if not hit:
        return 0.0
    if not df:
        return float(len(hit))
    return sum(math.log((n + 1) / (df.get(t, 0) + 1)) + 1.0 for t in hit)


def _iter_extracted():
    for f in glob.glob(os.path.join(EXTRACTED, "*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                yield json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue


def _load_index():
    """读预分词检索索引（一次只读 1 个文件）；无或比 extracted/ 旧（过期）则返回 None → 回退全扫。
    返回 (docs, df, n)；df 为 token→文档频率，v1 旧索引无此字段时为 None（每 token 权重 1.0）。"""
    rel = KB.get("entrypoints", {}).get("search_index", ".wiki-tree/search-index.json")
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        return None
    try:
        newest = 0
        for f in glob.glob(os.path.join(EXTRACTED, "*.json")):
            m = os.path.getmtime(f)
            if m > newest:
                newest = m
        if os.path.getmtime(p) < newest:
            sys.stderr.write("[kb_query] 索引过期（extracted/ 有更新），本次回退全扫；"
                             "请重跑 emit_access_bundle.py 刷新索引。\n")
            return None
    except OSError:
        pass
    try:
        data = json.load(open(p, encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    docs = data.get("docs")
    if docs is None:
        return None
    return docs, data.get("df") or None, len(docs)


def search(q, top=5, level="short"):
    """分层检索：返回相关主题(L1)摘要路径 + 候选文档；level=detailed 时附逐文档详细摘要。"""
    qt = _toks(q)
    if not qt:
        return {"error": "empty query"}
    idx = _load_index()
    rows, df, n = idx if idx is not None else (None, None, 0)
    topics = []
    for t in KB["topics"]:
        sc = _idf_score(qt, _toks(t["name"] + " " + t.get("one_liner", "")), df, n)
        if sc:
            topics.append((sc, t))
    topics.sort(key=lambda x: -x[0])
    docs = []
    if rows is not None:
        for r in rows:
            sc = _idf_score(qt, set(r.get("tok", [])), df, n)
            if sc:
                item = {"doc_id": r.get("id", ""), "path": r.get("md", ""),
                        "importance": r.get("imp", 0) or 0, "short": r.get("short", "")}
                if level in ("detailed", "full"):
                    item["detailed"] = r.get("detailed", "")
                # importance 作加权小项而非乘子：避免高 imp 泛化文档碾压低 imp 相关文档
                docs.append((sc + 0.3 * item["importance"], item))
    else:
        for d in _iter_extracted():
            did = d.get("doc_id", "")
            blob = (d.get("short_summary", "") + " " + d.get("detailed_summary", "") + " "
                    + did.replace("-", " ") + " "
                    + " ".join(t or "" for t in d.get("topics", []) or []) + " "
                    + " ".join((e.get("text") or "") for e in d.get("entities", []) or []))
            sc = _idf_score(qt, _toks(blob), None, 0)
            if sc:
                item = {
                    "doc_id": did,
                    "path": d.get("doc_md", "documents/%s.md" % did),
                    "importance": d.get("importance", 0) or 0,
                    "short": d.get("short_summary", ""),
                }
                if level in ("detailed", "full"):
                    item["detailed"] = d.get("detailed_summary", "")
                docs.append((sc + 0.3 * item["importance"], item))
    docs.sort(key=lambda x: -x[0])
    return {
        "query": q,
        "level": level,
        "drilldown": "L1 主题摘要(topics.summary_file) → 逐文档详细摘要(--level detailed) → L0 全文(documents 路径)",
        "topics": [{"name": t["name"], "summary_file": t["summary_file"],
                    "one_liner": "(摘要尚未生成)" if t.get("summary_missing")
                    else t.get("one_liner", "")} for _, t in topics[:3]],
        "documents": [it for _, it in docs[:top]],
        "cite_rule": "引用 documents/*.md 路径为据；库内不足再用通用知识/联网。",
    }


def get_topic(name):
    if not (name or "").strip():
        return "请提供主题名（用 --list-topics 查看全部）"
    q = name.strip()
    qn = re.sub(r"\s+", "", q)
    qt = _toks(q)
    # 先精确名、再去空白归一相等，最后才用词集子集判断（bigram 防「管理」误命中含管+理二字的任意主题）
    hits = ([t for t in KB["topics"] if t["name"] == q]
            or [t for t in KB["topics"] if re.sub(r"\s+", "", t["name"]) == qn]
            or [t for t in KB["topics"] if qt and qt <= _toks(t["name"])])
    for t in hits:
        p = os.path.join(ROOT, t["summary_file"])
        if os.path.exists(p):
            return open(p, encoding="utf-8").read()
    if hits:
        return ("主题「%s」的摘要尚未生成（缺 %s）。请直接读 documents/ 下相关原文，"
                "或补写主题摘要后重跑 finalize / emit_access_bundle。"
                % (hits[0]["name"], hits[0]["summary_file"]))
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
    # safe 部分须 glob.escape：实体名可含 [ ] 等 glob 元字符（如「[草稿]方案」）
    hits = glob.glob(os.path.join(ROOT, "entities", "*-" + glob.escape(safe) + ".md"))
    if hits:
        return open(hits[0], encoding="utf-8").read()
    return "未找到实体卡片: %s" % name


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")  # 警告含中文，避免 GBK 控制台/管道乱码
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
        rel = KB["entrypoints"]["global_summary"]
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            print("未找到全局摘要: %s（L2 全局摘要尚未生成；补写后重跑 emit_access_bundle 刷新）" % rel)
            return
        print(open(p, encoding="utf-8").read())
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
