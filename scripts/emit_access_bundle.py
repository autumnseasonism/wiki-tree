#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""emit_access_bundle.py — 为一个已建好的 vault 生成「接入包」的自描述部分：
   kb.json（单一真相源）+ AGENTS.md（自描述指令）+ .mcp.json（Claude Code 项目 MCP 注册）。
kb_query.py / kb_mcp_server.py 是通用代码模板，单独放在 vault 根（不由本脚本生成）。

只依赖 reduce 阶段的标准产物：`.wiki-tree/extracted/*.json`、`.wiki-tree/centrality.json`、
`summaries/topic-*.md`（Phase 6 产出），可选 `.wiki-tree/_dedup-map.json`。**不依赖任何临时
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
    """从 summaries/topic-*.md 解析 `**一句话**：...` 行；
    没有则回退取去 front-matter 后首个非标题非空段落的第一句（中英句号切）。"""
    try:
        text = open(md_path, encoding="utf-8").read()
    except OSError:
        return ""
    m = re.search(r"\*\*一句话\*\*[：:]\s*(.+)", text)
    if m:
        return m.group(1).strip()
    body = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    for para in re.split(r"\n\s*\n", body):
        para = " ".join(para.split())
        if not para or para.startswith("#"):
            continue
        # 英文句号只在词尾断句，避免切断 "3.5" 这类小数
        sent = re.split(r"。|\.(?=\s|$)", para, 1)[0].strip()
        if sent:
            return sent
    return ""


# CJK 单字停用字（与 kb_ingest.py 同源）：只滤单字 token，bigram 不滤
CJK_STOP = set("的了和是在与及或该本为也都把从这那个等对之其由以并而且但即则若如因故被让给向自"
               "至于各已未可须应要会能将就只更最不无有")


def _toks(s):
    """英文 [a-z0-9]+ 按词；CJK 连续段以重叠 bigram 为主、单字为辅（单字滤停用字）。
    检索端（kb_query.py / kb_hub_server.py）分词必须与此严格一致，改动需三处同步。"""
    out = set()
    for seg in re.findall(r"[a-z0-9]+|[一-鿿]+", (s or "").lower()):
        if seg.isascii():
            out.add(seg)
        else:
            out.update(a + b for a, b in zip(seg, seg[1:]))
            out.update(c for c in seg if c not in CJK_STOP)
    return out


def build_kb(vault, kid, name, scope, extra_use_when=None):
    mw = os.path.join(vault, ".wiki-tree")
    ext = os.path.join(mw, "extracted")
    cent = json.load(open(os.path.join(mw, "centrality.json"), encoding="utf-8"))
    try:
        dmap = json.load(open(os.path.join(mw, "_dedup-map.json"), encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        dmap = {}

    def canon(t):
        return dmap.get(t, t)

    # 单遍扫描 extracted：关系去重 + 按主题计文档数 + 构建预分词检索索引
    docs = glob.glob(os.path.join(ext, "*.json"))
    rels = set()
    topic_count = {}
    index_docs = []
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
        did = d.get("doc_id", os.path.basename(f)[:-5])
        short = d.get("short_summary", "")
        detailed = d.get("detailed_summary", "")
        # blob 纳入 doc_id（连字符还原为空格）与 topics，支持按文件名/主题词检索
        blob = (short + " " + detailed + " " + did.replace("-", " ") + " "
                + " ".join(t or "" for t in d.get("topics", []) or []) + " "
                + " ".join((e.get("text") or "") for e in d.get("entities", []) or []))
        index_docs.append({
            "id": did,
            "md": d.get("doc_md", "documents/%s.md" % did),
            "imp": d.get("importance", 0) or 0,
            "short": short,
            "detailed": detailed,
            "tok": sorted(_toks(blob)),
        })

    # 全库 token 文档频率（df）：检索端据此做 IDF 加权（罕见 token 权重高）
    df = {}
    for row in index_docs:
        for t in row["tok"]:
            df[t] = df.get(t, 0) + 1

    # 写预分词检索索引（v2 = docs + df）：kb_query / kb_hub_server 优先用它
    # （读 1 个文件而非扫 963 个）；缺失或 v1 旧索引（无 df）时它们自动降级。
    json.dump({"version": 2, "df": df, "docs": index_docs},
              open(os.path.join(mw, "search-index.json"), "w", encoding="utf-8"),
              ensure_ascii=False)

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
    # 兜底：extracted 里出现、但没有摘要文件的主题（增量新主题在补写摘要前会短暂处于此态）；
    # 标记 summary_missing，检索端据此提示「摘要尚未生成」而非给出悬空路径
    for k, c in topic_count.items():
        if k and k not in seen:
            sf = "summaries/topic-%s.md" % k
            t = {"name": k, "docs": c, "summary_file": sf, "one_liner": ""}
            if not os.path.exists(os.path.join(vault, sf)):
                t["summary_missing"] = True
            topics.append(t)
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
        # 原始补充词单独留档：finalize 重跑只回灌它，不回灌派生主体（防触发面逐轮膨胀）
        "extra_use_when": list(extra_use_when or []),
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
            "extracted_dir": ".wiki-tree/extracted/",
            "search_index": ".wiki-tree/search-index.json",
        },
        "query": {
            "cli": "python kb_query.py \"{question}\" [--level short|detailed|full] [--json]",
            "mcp": "python kb_mcp_server.py",
        },
        "levels": [
            "L2 global_summary (summaries/_global-summary.md)",
            "L1 topic summary (summaries/topic-*.md)",
            "doc detailed_summary (.wiki-tree/extracted/*.json)",
            "L0 full document (documents/*.md)",
        ],
        "topics": topics,
    }


def render_agents(kb):
    s = kb["stats"]
    L = [
        "# Knowledge Base: %s" % kb["name"],
        "<!-- 由 wiki-tree 自动生成；改 kb.json 后用 emit_access_bundle.py 重生成，勿手改 -->",
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
    ap.add_argument("--scope", default="由 wiki-tree 构建的本地知识库")
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
    # command 写当前解释器绝对路径（裸 "python" 在 MCP 宿主的 PATH 下未必可解析）；
    # 接入包本就是本机产物（cwd 也是本机绝对路径），换机器重跑 emit 即可刷新
    mcp = {"mcpServers": {"%s-kb" % a.id: {
        "command": sys.executable.replace("\\", "/"),
        "args": ["kb_mcp_server.py"], "cwd": vault.replace("\\", "/")}}}
    json.dump(mcp, open(os.path.join(vault, ".mcp.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("接入包生成完成: kb.json / AGENTS.md / .mcp.json")
    print(json.dumps(kb["stats"], ensure_ascii=False))


if __name__ == "__main__":
    main()
