#!/usr/bin/env python3
"""
assemble_vault.py — 确定性地把 extracted/*.json 汇总成 Vault 产物（reduce 阶段自动化）

reduce 阶段里大量工作其实是**确定性聚合**，数据都现成在 extracted/*.json（+ 可选的
scan.json / _conversion_report.json）里。本脚本把这些自动化，agent 只需补叙述性散文：
  1. 回填 _index.md：统计（文档/实体/关系数）、中心度 Top-N、主题概览、最近处理
  2. 生成 relations/_knowledge-graph.md：按关系类型分组的完整清单 + 类型统计
  3. 生成 _processing-report.md：扫描/转换/抽取/中心度统计（消费 importance）
  4. 生成 entities/<kind>-<text>.md 卡片骨架：front-matter + 关联/来源区段

**零悬空保证**：卡片之间、索引里的 wikilink 只指向"已建卡的实体"，未建卡的实体一律
以普通文本呈现 —— 因此无论建多少卡，都不会产生悬空链接。

中心度/kind/degree 复用 compute_centrality.py 的聚合逻辑（单一真相源，避免漂移）。

用法:
  python assemble_vault.py --vault VAULT [--dedup-map MAP] [--top 10]
        [--cards-all | --card-top N | --cards-min-degree D] [--force-cards] [--no-index]

默认：卡片建给 degree>=1 的实体（连通图节点）；index/graph/report 直接覆写；
卡片 create-if-missing（不覆盖 agent 已补的内容），重置加 --force-cards。
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compute_centrality import compute as _centrality_rows, load_dedup_map, canon  # noqa: E402

_INVALID = '\\/:*?"<>|\n\r\t'


def _safe(name: str) -> str:
    """实体名 → 安全文件名片段（仅替换文件系统非法字符；wikilink 也用此形式以保持一致）。"""
    out = "".join("-" if c in _INVALID else c for c in (name or "").strip())
    return out.strip() or "未命名"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_extracted(vault: Path):
    d = vault / ".memory-wiki" / "extracted"
    docs = []
    if d.is_dir():
        for fp in sorted(d.glob("*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                obj.setdefault("doc_id", fp.stem)
                docs.append(obj)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
                print(f"警告: 跳过损坏 extracted {fp.name}: {e}", file=sys.stderr)
    return docs


def _load_json(p: Path):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def aggregate(vault: Path, docs, dmap):
    """汇总关系（去重）、实体→文档、文档元信息、主题分组。中心度行另由 compute 提供。"""
    rels = {}  # (s,p,o) -> {subject,predicate,object,evidence,confidence,sources:set}
    entity_docs = defaultdict(set)
    doc_meta = {}
    topics = defaultdict(list)

    for d in docs:
        did = d["doc_id"]
        doc_meta[did] = {
            "doc_id": did,
            "doc_md": d.get("doc_md") or f"documents/{did}.md",
            "short_summary": d.get("short_summary", ""),
            "importance": d.get("importance", 0) or 0,
            "topics": d.get("topics", []) or [],
        }
        for t in doc_meta[did]["topics"]:
            topics[t].append(did)
        for ent in d.get("entities", []) or []:
            if isinstance(ent, dict):
                e = canon(ent.get("text"), dmap)
                if e:
                    entity_docs[e].add(did)
        for rel in d.get("relations", []) or []:
            if not isinstance(rel, dict):
                continue
            s = canon(rel.get("subject"), dmap)
            o = canon(rel.get("object"), dmap)
            p = (rel.get("predicate") or "RELATED_TO").strip()
            if not s or not o:
                continue
            entity_docs[s].add(did)
            entity_docs[o].add(did)
            key = (s, p, o)
            rec = rels.setdefault(key, {
                "subject": s, "predicate": p, "object": o,
                "evidence": rel.get("evidence", ""),
                "confidence": rel.get("confidence", 0) or 0,
                "sources": set(),
            })
            rec["sources"].add(did)
            if (rel.get("confidence") or 0) > rec["confidence"]:
                rec["confidence"] = rel.get("confidence")
                rec["evidence"] = rel.get("evidence", rec["evidence"])
    return rels, entity_docs, doc_meta, topics


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="确定性汇总 extracted/*.json 为 Vault 产物")
    ap.add_argument("--vault", required=True, help="Vault 根目录")
    ap.add_argument("--dedup-map", help="变体→规范名 JSON；默认自动读取 .memory-wiki/_dedup-map.json")
    ap.add_argument("--top", type=int, default=10, help="_index.md 核心实体数（默认 10）")
    ap.add_argument("--cards-all", action="store_true", help="为所有实体建卡")
    ap.add_argument("--card-top", type=int, default=0, help="只为中心度前 N 实体建卡（0=不启用）")
    ap.add_argument("--cards-min-degree", type=int, default=1,
                    help="为 degree>=该值的实体建卡（默认 1=连通图节点；设 0 含孤立实体）")
    ap.add_argument("--force-cards", action="store_true", help="覆写已存在卡片（默认跳过，保护 agent 已补内容）")
    ap.add_argument("--no-index", action="store_true", help="不生成/覆写 _index.md")
    args = ap.parse_args()

    vault = Path(args.vault)
    if not (vault / ".memory-wiki" / "extracted").is_dir():
        print(f"错误: 未找到 {vault}\\.memory-wiki\\extracted（先跑抽取阶段）", file=sys.stderr)
        sys.exit(1)

    dmap_path = args.dedup_map or (vault / ".memory-wiki" / "_dedup-map.json")
    dmap = load_dedup_map(str(dmap_path)) if Path(dmap_path).exists() else {}

    docs = load_extracted(vault)
    rows, _skipped = _centrality_rows(vault, dmap)
    rels, entity_docs, doc_meta, topics = aggregate(vault, docs, dmap)

    kind_of = {r["entity"]: (r["kind"] or "concept") for r in rows}
    rank_of = {r["entity"]: r["rank"] for r in rows}

    # 决定建卡集合
    if args.cards_all:
        carded = [r["entity"] for r in rows]
    elif args.card_top and args.card_top > 0:
        carded = [r["entity"] for r in rows[:args.card_top]]
    else:
        carded = [r["entity"] for r in rows if r["degree"] >= args.cards_min_degree]
    carded_set = set(carded)

    def link(e):
        """已建卡 → wikilink（带别名显示原名）；否则普通文本。保证零悬空。"""
        if e in carded_set:
            return f"[[{kind_of.get(e, 'concept')}-{_safe(e)}|{e}]]"
        return e

    def doclink(did):
        return f"[[{_safe(did)}]]"

    written = {"cards_created": 0, "cards_skipped": 0, "files": []}
    ent_dir = vault / "entities"
    ent_dir.mkdir(parents=True, exist_ok=True)
    deg_of = {r["entity"]: r for r in rows}

    # ---- 实体卡片骨架 ----
    for e in carded:
        k = kind_of.get(e, "concept")
        path = ent_dir / f"{k}-{_safe(e)}.md"
        if path.exists() and not args.force_cards:
            written["cards_skipped"] += 1
            continue
        row = deg_of[e]
        rel_lines = []
        for rec in rels.values():
            if rec["subject"] == e:
                rel_lines.append(f"- **{rec['predicate']}** → {link(rec['object'])}")
            elif rec["object"] == e:
                rel_lines.append(f"- {link(rec['subject'])} → **{rec['predicate']}**")
        src_lines = [f"- {doclink(d)}" for d in sorted(entity_docs.get(e, ()))]
        body = (
            f"---\nkind: entity\nentity_type: {k}\ntags:\n  - {k}\n  - source/local-files\n"
            f"created_at: {_now()}\ncentrality_rank: {row['rank']}\ncentrality_degree: {row['degree']}\n---\n\n"
            f"# {e}\n\n"
            f"**类型**：{k}｜中心度 #{row['rank']}（degree={row['degree']}, "
            f"relations={row['relation_count']}, docs={row['doc_count']}）\n\n"
            f"<!-- 说明：可由 Agent 补充该实体的定义/作用（1-2 句）。本卡片骨架由 assemble_vault.py 生成。 -->\n\n"
            f"## 关联\n" + ("\n".join(rel_lines) if rel_lines else "（无显式关系）") + "\n\n"
            f"## 来源\n" + ("\n".join(src_lines) if src_lines else "（无）") + "\n"
        )
        path.write_text(body, encoding="utf-8")
        written["cards_created"] += 1

    # ---- relations/_knowledge-graph.md ----
    by_pred = defaultdict(list)
    for rec in rels.values():
        by_pred[rec["predicate"]].append(rec)
    kg = [
        "---\nkind: relation\ncreated_at: " + _now() + "\ntags:\n  - relations\n  - source/local-files\n---\n",
        "# 🕸️ 知识关系图谱\n",
        f"> 由 assemble_vault.py 自动生成 · 共 {len(rels)} 条关系"
        "（未单独建卡的实体以普通文本表示）\n",
        "## 实体关系列表\n",
    ]
    for pred in sorted(by_pred):
        kg.append(f"### {pred}（{len(by_pred[pred])}）")
        for rec in sorted(by_pred[pred], key=lambda r: (-(r["confidence"] or 0), r["subject"])):
            srcs = "、".join(doclink(d) for d in sorted(rec["sources"]))
            ev = f" — {rec['evidence']}" if rec["evidence"] else ""
            kg.append(f"- {link(rec['subject'])} → **{pred}** → {link(rec['object'])}"
                      f"（置信度 {rec['confidence']}）{ev}  ｜来源：{srcs}")
        kg.append("")
    kg.append("## 关系类型统计\n")
    kg.append("| 关系类型 | 数量 |")
    kg.append("|----------|------|")
    for pred in sorted(by_pred, key=lambda p: -len(by_pred[p])):
        kg.append(f"| {pred} | {len(by_pred[pred])} |")
    kg.append("\n---\n\n*由 assemble_vault.py 生成*\n")
    (vault / "relations").mkdir(parents=True, exist_ok=True)
    (vault / "relations" / "_knowledge-graph.md").write_text("\n".join(kg), encoding="utf-8")
    written["files"].append("relations/_knowledge-graph.md")

    # ---- _index.md ----
    if not args.no_index:
        top_rows = rows[:args.top] if args.top and args.top > 0 else rows
        recent = sorted(doc_meta.values(), key=lambda m: (-m["importance"], m["doc_id"]))
        idx = [
            "---\nkind: index\ncreated_at: " + _now() + "\ntags:\n  - index\n  - source/local-files\n---\n",
            "# 📚 个人知识库索引\n",
            "> 本知识库由 Local Memory Wiki 自动生成（索引统计由 assemble_vault.py 回填）\n",
            "## 📊 统计\n",
            f"- **文档总数**：{len(docs)}",
            f"- **实体总数**：{len(rows)}",
            f"- **关系总数**：{len(rels)}",
            f"- **主题数**：{len(topics)}",
            f"- **最后更新**：{_now()}\n",
            "## 🗂️ 主题概览\n",
        ]
        for t in sorted(topics, key=lambda x: -len(topics[x])):
            dids = topics[t]
            core = [r["entity"] for r in rows if any(r["entity"] in entity_docs and d in entity_docs[r["entity"]] for d in dids)][:4]
            idx.append(f"### {t}")
            idx.append(f"- 文档数：{len(dids)}")
            idx.append(f"- 核心实体：{'、'.join(link(e) for e in core) if core else '（待补）'}")
            idx.append("- 摘要：<!-- 一句话摘要可由 Agent 补充 -->\n")
        idx.append("## 🔑 核心实体（中心度 Top）\n")
        for r in top_rows:
            idx.append(f"{r['rank']}. {link(r['entity'])} — {r['kind'] or '?'}"
                       f"（degree={r['degree']}）")
        idx.append("\n## 📝 最近处理\n")
        for m in recent:
            ss = f" — {m['short_summary']}" if m["short_summary"] else ""
            idx.append(f"- {doclink(m['doc_id'])}{ss}")
        idx.append("\n---\n\n*本索引由 [Local Memory Wiki]"
                   "(https://github.com/autumnseasonism/local-memory-wiki) Skill 自动生成*\n")
        (vault / "_index.md").write_text("\n".join(idx), encoding="utf-8")
        written["files"].append("_index.md")

    # ---- _processing-report.md ----
    scan = _load_json(vault / ".memory-wiki" / "scan.json") or {}
    conv = _load_json(vault / "_conversion_report.json") or {}
    n_rel = sum(len(d.get("relations", []) or []) for d in docs)
    rep = [
        "# 处理报告\n",
        f"> 由 assemble_vault.py 生成于 {_now()}\n",
        "## 概览\n",
        f"- 扫描文件总数：{scan.get('total_files', '—')}",
        f"- 受支持文档：{scan.get('supported_files', '—')}",
        f"- 不支持跳过：{scan.get('unsupported_files', '—')}"
        + (f"（{ '、'.join(scan.get('unsupported_extensions', [])) }）" if scan.get('unsupported_extensions') else ""),
        f"- 被 exclude/.mwignore 排除：{scan.get('excluded_count', 0)}",
        f"- 转换成功：{conv.get('success', '—')} ｜ 内容去重跳过：{conv.get('skipped', '—')} ｜ 错误：{conv.get('error', '—')}",
        f"- 抽取文档：{len(docs)} ｜ 实体（去重后）：{len(rows)} ｜ 关系（去重后）：{len(rels)}（原始 {n_rel}）\n",
        "## 中心度 Top 10\n",
        "| 排名 | 实体 | 类型 | degree | relation_count | doc_count |",
        "|------|------|------|--------|----------------|-----------|",
    ]
    for r in rows[:10]:
        rep.append(f"| {r['rank']} | {r['entity']} | {r['kind'] or '?'} | "
                   f"{r['degree']} | {r['relation_count']} | {r['doc_count']} |")
    rep.append("\n## 文档（按 importance 排序）\n")
    rep.append("| 文档 | importance | 主题 | 短摘要 |")
    rep.append("|------|-----------|------|--------|")
    for m in sorted(doc_meta.values(), key=lambda x: (-x["importance"], x["doc_id"])):
        ss = (m["short_summary"][:40] + "…") if len(m["short_summary"]) > 40 else m["short_summary"]
        rep.append(f"| {m['doc_id']} | {m['importance']} | {'、'.join(m['topics'])} | {ss} |")
    rep.append("\n---\n\n*由 assemble_vault.py 生成；叙述性摘要见 summaries/。*\n")
    (vault / "_processing-report.md").write_text("\n".join(rep), encoding="utf-8")
    written["files"].append("_processing-report.md")

    print(json.dumps({
        "vault": str(vault),
        "docs": len(docs),
        "entities": len(rows),
        "relations": len(rels),
        "topics": len(topics),
        "cards_created": written["cards_created"],
        "cards_skipped": written["cards_skipped"],
        "generated": written["files"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
