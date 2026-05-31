#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""emit_access_bundle.py — 为一个已建好的 vault 生成「接入包」的自描述部分：
   kb.json（单一真相源）+ AGENTS.md（自描述指令）+ .mcp.json（Claude Code 项目 MCP 注册）。
kb_query.py / kb_mcp_server.py 是通用代码模板，单独放在 vault 根（不由本脚本生成）。

只依赖 reduce 阶段的标准产物：`.memory-wiki/extracted/*.json`、`.memory-wiki/centrality.json`、
`summaries/topic-*.md`（Phase 6 产出），可选 `.memory-wiki/_dedup-map.json`。**不依赖任何临时
中间文件**——主题列表/一句话直接从 summaries 与 extracted 派生。

用法:
  python emit_access_bundle.py --vault VAULT --id ID --name NAME [--scope "..."]
        [--extra-use-when "词1,词2"]
"""
import os
import re
import sys
import json
import glob
import argparse


def _safe(s):
    return "".join("-" if c in '\\/:*?"<>|' else c for c in (s or "").strip())


def _read_one_liner(md_path):
    """从 summaries/topic-*.md 解析 `**一句话**：...` 行；没有则返回空串。"""
    try:
        with open(md_path, encoding="utf-8") as f:
            for line in f:
                m = re.search(r"\*\*一句话\*\*[：:]\s*(.+)", line)
                if m:
                    return m.group(1).strip()
    except OSError:
        pass
    return ""


def build_kb(vault, kid, name, scope, extra_use_when=None):
    mw = os.path.join(vault, ".memory-wiki")
    ext = os.path.join(mw, "extracted")
    cent = json.load(open(os.path.join(mw, "centrality.json"), encoding="utf-8"))
    try:
        dmap = json.load(open(os.path.join(mw, "_dedup-map.json"), encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        dmap = {}

    def canon(t):
        return dmap.get(t, t)

    # 关系去重 + 按主题计文档数（主题取 extracted 的 topics，safe 后作为 key）
    docs = glob.glob(os.path.join(ext, "*.json"))
    rels = set()
    topic_count = {}
    for f in docs:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        for r in d.get("relations", []) or []:
            s = canon((r.get("subject") or "").strip())
            o = canon((r.get("object") or "").strip())
            p = (r.get("predicate") or "RELATED_TO").strip()
            if s and o:
                rels.add((s, p, o))
        for t in d.get("topics", []) or []:
            k = _safe((t or "").strip())
            if k:
                topic_count[k] = topic_count.get(k, 0) + 1

    # 主题以 summaries/topic-*.md 为准（Phase 6 保证产出）：name/链接用文件名 → 零悬空；
    # 一句话从文件解析；文档数按 safe 后的 topic 匹配。
    topics = []
    seen = set()
    for p in sorted(glob.glob(os.path.join(vault, "summaries", "topic-*.md"))):
        base = os.path.basename(p)
        safe_name = base[len("topic-"):-3]
        if not safe_name:
            continue
        seen.add(safe_name)
        topics.append({
            "name": safe_name,
            "docs": topic_count.get(safe_name, 0),
            "summary_file": "summaries/" + base,
            "one_liner": _read_one_liner(p),
        })
    # 兜底：extracted 里出现、但没有摘要文件的主题（标准流程一般不会发生）
    for k, c in topic_count.items():
        if k and k not in seen:
            topics.append({"name": k, "docs": c,
                           "summary_file": "summaries/topic-%s.md" % k, "one_liner": ""})
    topics.sort(key=lambda t: -t["docs"])

    # use_when：通用派生（中心度 Top 实体 + 主要主题名）+ 可选领域补充词；去重保序
    top_entities = [r["entity"] for r in cent.get("top", [])[:15]]
    raw = top_entities + [t["name"] for t in topics[:6]] + list(extra_use_when or [])
    seen2, use_when = set(), []
    for x in raw:
        if x and x not in seen2:
            seen2.add(x)
            use_when.append(x)

    return {
        "kb_version": "1.0",
        "id": kid,
        "name": name,
        "scope": scope,
        "use_when": use_when,
        "stats": {
            "documents": len(docs),
            "entities": cent.get("entity_count", len(cent.get("top", []))),
            "relations": len(rels),
            "topics": len(topics),
        },
        "entrypoints": {
            "index": "_index.md",
            "global_summary": "summaries/_global-summary.md",
            "topics_dir": "summaries/",
            "documents_dir": "documents/",
            "entities_dir": "entities/",
            "knowledge_graph": "relations/_knowledge-graph.md",
            "extracted_dir": ".memory-wiki/extracted/",
        },
        "query": {
            "cli": "python kb_query.py \"{question}\" [--level short|detailed|full] [--json]",
            "mcp": "python kb_mcp_server.py",
        },
        "levels": [
            "L2 global_summary (summaries/_global-summary.md)",
            "L1 topic summary (summaries/topic-*.md)",
            "doc detailed_summary (.memory-wiki/extracted/*.json)",
            "L0 full document (documents/*.md)",
        ],
        "topics": topics,
    }


def render_agents(kb):
    s = kb["stats"]
    L = [
        "# Knowledge Base: %s" % kb["name"],
        "<!-- 由 local-memory-wiki 自动生成；改 kb.json 后用 emit_access_bundle.py 重生成，勿手改 -->",
        "",
        "**何时用我**：%s。" % kb["scope"],
        "命中关键词（任一）即应优先查本库：%s 等。" % "、".join(kb["use_when"][:12]),
        "",
        "**规模**：%(documents)d 文档 · %(topics)d 主题 · %(entities)d 实体 · %(relations)d 关系。" % s,
        "",
        "**怎么查我（四档下钻，由粗到细，够用就停）**：",
        "1. `python kb_query.py \"你的问题\" --json` → 返回相关「主题摘要」路径 + 候选「文档」路径。",
        "2. 读 `summaries/topic-*.md`（L1，已汇总该主题全部要点）——多数问题到此即可作答。",
        "3. 要更聚焦单篇：`python kb_query.py \"问题\" --level detailed` → 逐文档「详细摘要」。",
        "4. 要确切数字/边角规则：读候选 `documents/*.md`（L0 原文），或 `python kb_query.py --doc <id> --level full`。",
        "- 其他：`--topic \"名\"` 取主题摘要 | `--entity \"名\"` 取实体卡 | `--global` 取全局摘要 | `--list-topics`。",
        "- 支持 MCP 的客户端：本目录 `.mcp.json` 已注册 `kb_mcp_server.py`，工具 kb_search/kb_topic/kb_document 会自动出现。",
        "",
        "**引用规则**：作答须引用所依据的 `documents/*.md` 路径；库内证据不足再用通用知识/联网。",
        "",
        "**主题一览**：",
    ]
    for t in sorted(kb["topics"], key=lambda x: -x["docs"]):
        L.append("- %s（%d 篇）：%s" % (t["name"], t["docs"], (t["one_liner"] or "")[:50]))
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True)
    ap.add_argument("--id", default="kb")
    ap.add_argument("--name", default="本地知识库")
    ap.add_argument("--scope", default="由 local-memory-wiki 构建的本地知识库")
    ap.add_argument("--extra-use-when", default="",
                    help="额外触发词（逗号分隔），追加到自动派生的 use_when（领域词可在此补充）")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    vault = os.path.abspath(a.vault)
    extra = [x.strip() for x in (a.extra_use_when or "").split(",") if x.strip()]
    kb = build_kb(vault, a.id, a.name, a.scope, extra)
    json.dump(kb, open(os.path.join(vault, "kb.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    open(os.path.join(vault, "AGENTS.md"), "w", encoding="utf-8").write(render_agents(kb))
    mcp = {"mcpServers": {"%s-kb" % a.id: {
        "command": "python", "args": ["kb_mcp_server.py"], "cwd": vault.replace("\\", "/")}}}
    json.dump(mcp, open(os.path.join(vault, ".mcp.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("接入包生成完成: kb.json / AGENTS.md / .mcp.json")
    print(json.dumps(kb["stats"], ensure_ascii=False))


if __name__ == "__main__":
    main()
