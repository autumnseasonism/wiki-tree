#!/usr/bin/env python3
"""
assemble_vault.py — 确定性地把 extracted/*.json 汇总成 Vault 产物（reduce 阶段自动化）

reduce 阶段里大量工作其实是**确定性聚合**，数据都现成在 extracted/*.json（+ 可选的
scan.json / _conversion_report.json）里。本脚本把这些自动化，agent 只需补叙述性散文：
  1. 回填 _index.md：统计（文档/实体/关系数）、中心度 Top-N、主题概览、最近处理
  2. 生成 relations/_knowledge-graph.md：按关系类型分组的完整清单 + 类型统计
  3. 生成 _processing-report.md：扫描/转换/抽取/中心度统计（消费 importance）
  4. 生成 entities/<kind>-<text>.md 卡片：front-matter + 受管区段（统计/关联/来源）

**零悬空保证**：卡片之间、索引里的 wikilink 只指向"已建卡的实体"，未建卡的实体一律
以普通文本呈现 —— 因此无论建多少卡，都不会产生悬空链接。

**卡片受管区段**：自动生成的统计/关联/来源包在 <!-- wiki-tree:auto:start/end --> 标记
内；重跑只重写 front-matter + 标记区，标记外的 agent 散文原样保留。无标记的旧版卡片
保持现状跳过（兼容）；--force-cards 整卡重建。

中心度/kind/degree 复用 compute_centrality.py 的聚合逻辑（单一真相源，避免漂移）。

用法:
  python assemble_vault.py --vault VAULT [--dedup-map MAP] [--top 10]
        [--cards-all | --card-top N | --cards-min-degree D] [--force-cards] [--no-index]

默认：卡片建给 degree>=1 的实体（连通图节点）；index/graph/report 直接覆写。
"""

import re
import sys
import json
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compute_centrality import compute as _centrality_rows, load_dedup_map, canon  # noqa: E402

_INVALID = '\\/:*?"<>|\n\r\t'
# 文件名片段上限按 UTF-8 字节数计：Linux ext4 等限制单个文件名 255 字节（CJK 每字 3 字节），
# 按字符数截断在 Linux 上仍会超限（80 个 CJK 字符=240 字节，加 kind 前缀/哈希/.md 即越界）。
_MAX_NAME_BYTES = 120
_AUTO_START = "<!-- wiki-tree:auto:start -->"
_AUTO_END = "<!-- wiki-tree:auto:end -->"


def _safe(name: str) -> str:
    """实体名 → 安全文件名片段（仅替换文件系统非法字符；wikilink 也用此形式以保持一致）。"""
    raw = (name or "").strip()
    out = "".join("-" if c in _INVALID else c for c in raw)
    out = out.strip() or "未命名"
    if len(out.encode("utf-8")) > _MAX_NAME_BYTES:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        out = (out.encode("utf-8")[:_MAX_NAME_BYTES].decode("utf-8", errors="ignore").rstrip()
               + "-" + digest)
    return out


def _num(x, default=0.0):
    """LLM 可能把数值字段写成字符串/null：尽力转 float，失败回 default 而非抛异常。"""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_extracted(vault: Path):
    d = vault / ".wiki-tree" / "extracted"
    docs = []
    if d.is_dir():
        for fp in sorted(d.glob("*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    obj = json.load(f)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
                print(f"警告: 跳过损坏 extracted {fp.name}: {e}", file=sys.stderr)
                continue
            # doc_id 是 LLM 写的，可能与实际文档文件名漂移（doclink 会悬空）：
            # 优先从 doc_md 推导；doc_md 缺失才信 JSON 内 doc_id，再缺用文件名 stem
            claimed = str(obj.get("doc_id") or "").strip()
            doc_md = obj.get("doc_md")
            if doc_md:
                derived = Path(str(doc_md).replace("\\", "/")).stem
            else:
                derived = claimed or fp.stem
            if claimed and claimed != derived:
                print(f"警告: {fp.name} 内 doc_id={claimed!r} 与 doc_md 推导值 {derived!r} 不一致，采用后者",
                      file=sys.stderr)
            obj["doc_id"] = derived
            docs.append(obj)
    return docs


def _load_json(p: Path):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def aggregate(vault: Path, docs, dmap):
    """汇总关系（去重）、实体→文档、文档元信息、主题分组。中心度行另由 compute 提供。

    LLM 产物的 confidence/importance/short_summary/topics 可能是字符串/null 等坏类型：
    数值经 _num 净化、文本/列表兜空，无法解析的值计入 bad_values 返回，不中断聚合。
    """
    rels = {}  # (s,p,o) -> {subject,predicate,object,evidence,confidence,sources:set}
    entity_docs = defaultdict(set)
    doc_meta = {}
    topics = defaultdict(list)
    bad_values = 0

    def _numw(x, default=0.0):
        nonlocal bad_values
        if x is None:
            return default
        v = _num(x, None)
        if v is None:
            bad_values += 1
            return default
        return v

    for d in docs:
        did = d["doc_id"]
        raw_topics = d.get("topics") or []
        if not isinstance(raw_topics, list):
            bad_values += 1
            raw_topics = []
        doc_meta[did] = {
            "doc_id": did,
            "doc_md": d.get("doc_md") or f"documents/{did}.md",
            "short_summary": str(d.get("short_summary") or ""),
            "importance": _numw(d.get("importance")),
            "topics": [str(t) for t in raw_topics if t],
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
            p = str(rel.get("predicate") or "RELATED_TO").strip()
            if not s or not o:
                continue
            entity_docs[s].add(did)
            entity_docs[o].add(did)
            conf = _numw(rel.get("confidence"))
            key = (s, p, o)
            rec = rels.setdefault(key, {
                "subject": s, "predicate": p, "object": o,
                "evidence": rel.get("evidence") or "",
                "confidence": conf,
                "sources": set(),
            })
            rec["sources"].add(did)
            if conf > rec["confidence"]:
                rec["confidence"] = conf
                rec["evidence"] = rel.get("evidence") or rec["evidence"]
    return rels, entity_docs, doc_meta, topics, bad_values


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="确定性汇总 extracted/*.json 为 Vault 产物")
    ap.add_argument("--vault", required=True, help="Vault 根目录")
    ap.add_argument("--dedup-map", help="变体→规范名 JSON；默认自动读取 .wiki-tree/_dedup-map.json")
    ap.add_argument("--top", type=int, default=10, help="_index.md 核心实体数（默认 10）")
    ap.add_argument("--cards-all", action="store_true", help="为所有实体建卡")
    ap.add_argument("--card-top", type=int, default=0, help="只为中心度前 N 实体建卡（0=不启用）")
    ap.add_argument("--cards-min-degree", type=int, default=1,
                    help="为 degree>=该值的实体建卡（默认 1=连通图节点；设 0 含孤立实体）")
    ap.add_argument("--force-cards", action="store_true",
                    help="整卡重建已存在卡片（默认只刷新受管标记区+front-matter，标记外散文保留）")
    ap.add_argument("--no-index", action="store_true", help="不生成/覆写 _index.md")
    args = ap.parse_args()

    vault = Path(args.vault)
    if not (vault / ".wiki-tree" / "extracted").is_dir():
        print(f"错误: 未找到 {vault}\\.wiki-tree\\extracted（先跑抽取阶段）", file=sys.stderr)
        sys.exit(1)

    if args.dedup_map:
        # 用户显式指定的映射文件不存在 → 报错退出（静默忽略会让去重悄悄失效）
        dmap_path = Path(args.dedup_map)
        if not dmap_path.exists():
            print(f"错误: --dedup-map 指定的文件不存在: {dmap_path}", file=sys.stderr)
            sys.exit(1)
        dmap = load_dedup_map(str(dmap_path))
    else:
        # 默认路径属"可选自动发现"，缺失是常态，静默回空映射
        dmap_path = vault / ".wiki-tree" / "_dedup-map.json"
        dmap = load_dedup_map(str(dmap_path)) if dmap_path.exists() else {}

    docs = load_extracted(vault)
    rows, _skipped = _centrality_rows(vault, dmap)
    rels, entity_docs, doc_meta, topics, bad_values = aggregate(vault, docs, dmap)

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

    # 卡片文件名预解析：不同实体经 _safe 清洗/截断后可能同名（含大小写不敏感文件系统），
    # 本轮内后到者追加 -2/-3 序号。wikilink 与写盘共用该映射，保证零悬空且不指错卡。
    card_file = {}
    used_names = set()
    collisions = []
    for e in carded:
        base = f"{kind_of.get(e, 'concept')}-{_safe(e)}"
        fname, n = base, 2
        while fname.casefold() in used_names:
            fname = f"{base}-{n}"
            n += 1
        used_names.add(fname.casefold())
        card_file[e] = fname
        if fname != base:
            collisions.append({"entity": e, "file": f"{fname}.md", "base": f"{base}.md"})

    def link(e):
        """已建卡 → wikilink（带别名显示原名）；否则普通文本。保证零悬空。"""
        if e in carded_set:
            return f"[[{card_file[e]}|{e}]]"
        return e

    def doclink(did):
        return f"[[{_safe(did)}]]"

    def _front_matter(kind, row, created_at):
        return (
            f"---\nkind: entity\nentity_type: {kind}\ntags:\n  - {kind}\n  - source/local-files\n"
            f"created_at: {created_at}\ncentrality_rank: {row['rank']}\ncentrality_degree: {row['degree']}\n---\n"
        )

    written = {"cards_created": 0, "cards_updated": 0, "cards_skipped": 0, "files": []}
    errors = []
    ent_dir = vault / "entities"
    ent_dir.mkdir(parents=True, exist_ok=True)
    deg_of = {r["entity"]: r for r in rows}

    # ---- 实体卡片 ----
    for e in carded:
        k = kind_of.get(e, "concept")
        path = ent_dir / f"{card_file[e]}.md"
        row = deg_of[e]
        rel_lines = []
        for rec in rels.values():
            if rec["subject"] == e:
                rel_lines.append(f"- **{rec['predicate']}** → {link(rec['object'])}")
            elif rec["object"] == e:
                rel_lines.append(f"- {link(rec['subject'])} → **{rec['predicate']}**")
        src_lines = [f"- {doclink(d)}" for d in sorted(entity_docs.get(e, ()))]
        auto = (
            _AUTO_START + "\n"
            f"**类型**：{k}｜中心度 #{row['rank']}（degree={row['degree']}, "
            f"relations={row['relation_count']}, docs={row['doc_count']}）\n\n"
            "## 关联\n" + ("\n".join(rel_lines) if rel_lines else "（无显式关系）") + "\n\n"
            "## 来源\n" + ("\n".join(src_lines) if src_lines else "（无）") + "\n"
            + _AUTO_END
        )
        try:
            if path.exists() and not args.force_cards:
                old = path.read_text(encoding="utf-8")
                if _AUTO_START not in old or old.find(_AUTO_END) <= old.find(_AUTO_START):
                    # 旧版无标记卡：无法区分手工内容与自动内容，保持现状不动
                    written["cards_skipped"] += 1
                    continue
                # 受管刷新：front-matter（中心度统计随重跑变化）+ 标记区重写；
                # 标记区外（agent 补写的散文）原样保留，created_at 沿用旧值
                created = _now()
                fm_m = re.match(r"^---\n.*?\n---\n", old, re.DOTALL)
                body = old
                if fm_m:
                    cm = re.search(r"^created_at:\s*(.+)$", fm_m.group(0), re.MULTILINE)
                    if cm:
                        created = cm.group(1).strip()
                    body = _front_matter(k, row, created) + old[fm_m.end():]
                si, ei = body.find(_AUTO_START), body.find(_AUTO_END)
                if si == -1 or ei <= si:
                    written["cards_skipped"] += 1
                    continue
                body = body[:si] + auto + body[ei + len(_AUTO_END):]
                path.write_text(body, encoding="utf-8")
                written["cards_updated"] += 1
                continue
            body = (
                _front_matter(k, row, _now()) + "\n"
                f"# {e}\n\n"
                f"<!-- 说明：可由 Agent 补充该实体的定义/作用（1-2 句）；标记区内由 assemble_vault.py 维护、"
                f"重跑会刷新，散文请写在标记区外。 -->\n\n"
                + auto + "\n"
            )
            path.write_text(body, encoding="utf-8")
            written["cards_created"] += 1
        except OSError as exc:
            # 个别实体名仍可能踩中文件系统限制：记入 errors 后继续，单卡失败不毁整次装配
            errors.append({"entity": e, "file": path.name, "error": str(exc)})

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
            "> 本知识库由 Wiki Tree 自动生成（索引统计由 assemble_vault.py 回填）\n",
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
        idx.append("\n---\n\n*本索引由 [Wiki Tree]"
                   "(https://github.com/autumnseasonism/wiki-tree) Skill 自动生成*\n")
        (vault / "_index.md").write_text("\n".join(idx), encoding="utf-8")
        written["files"].append("_index.md")

    # ---- _processing-report.md ----
    scan = _load_json(vault / ".wiki-tree" / "scan.json") or {}
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
        "cards_updated": written["cards_updated"],
        "cards_skipped": written["cards_skipped"],
        "collisions": collisions,
        "errors": errors,
        "bad_values": bad_values,
        "generated": written["files"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
