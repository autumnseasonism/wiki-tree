#!/usr/bin/env python3
"""
compute_centrality.py — 从 extracted/*.json 确定性地计算实体中心度

读取 <vault>/.memory-wiki/extracted/*.json，对每个实体统计三项信号：
  - degree        ：与它直接相连的**不同实体数**（标准图度数，跳过自环）—— 图谱枢纽程度
  - relation_count：连在它身上的**关系边条数**（同一对实体跨文档重复也累加）—— 被引用强度
  - doc_count     ：它出现过的不同文档数（entities 列表 ∪ 关系端点）—— 覆盖面

默认按 (degree, relation_count, doc_count) 降序排名 —— **degree（不同邻居数）为主**，最贴合"知识图谱枢纽 = 核心实体"。

「去重是语义活、交 LLM；计数是确定性活、交脚本」：
- 默认按**原始实体名**统计（"张三"与"Zhang San"分开计）。
- 传 --dedup-map 时，先把变体折叠成规范名，再统计（合并、票数相加）。

用法:
  python compute_centrality.py --vault VAULT [--dedup-map MAP.json] [--top N] [-o OUT.json]

--dedup-map 格式: {"变体名": "规范名", ...}（变体 → 规范名；不在表里的原样保留）
"""

import sys
import json
import argparse
from pathlib import Path
from collections import defaultdict


def load_dedup_map(path):
    """读取 变体→规范名 映射；未提供则返回 {}（即默认原始频次模式）。"""
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"--dedup-map 应为 JSON 对象 {{变体: 规范}}，实际为 {type(data).__name__}")
    return {str(k).strip(): str(v).strip() for k, v in data.items() if str(k).strip()}


def canon(name, dmap):
    """把实体名折叠成规范名（不在映射里就原样返回）。"""
    name = (name or "").strip()
    return dmap.get(name, name)


def compute(vault: Path, dmap: dict):
    extracted_dir = vault / ".memory-wiki" / "extracted"
    files = sorted(extracted_dir.glob("*.json")) if extracted_dir.is_dir() else []

    relation_count = defaultdict(int)  # 实体 → 关系边条数（含跨文档重复）
    neighbors = defaultdict(set)       # 实体 → 直接相连的不同实体（其数量 = 标准图度 degree）
    docs = defaultdict(set)            # 实体 → 出现过的 doc_id 集合
    kind_votes = defaultdict(lambda: defaultdict(int))  # 实体 → {kind: 票数}

    skipped = []
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            skipped.append({"file": fp.name, "reason": str(e)})
            continue
        doc_id = data.get("doc_id") or fp.stem

        # 实体列表 → 贡献 doc_count + kind 投票
        for ent in data.get("entities", []) or []:
            if not isinstance(ent, dict):
                continue
            text = canon(ent.get("text"), dmap)
            if not text:
                continue
            docs[text].add(doc_id)
            kind = (ent.get("kind") or "").strip()
            if kind:
                kind_votes[text][kind] += 1

        # 关系 → 贡献 degree + neighbors + doc_count
        for rel in data.get("relations", []) or []:
            if not isinstance(rel, dict):
                continue
            s = canon(rel.get("subject"), dmap)
            o = canon(rel.get("object"), dmap)
            if not s or not o:
                continue
            docs[s].add(doc_id)
            docs[o].add(doc_id)
            if s == o:
                continue  # 跳过自环，不计入度数/邻居
            relation_count[s] += 1
            relation_count[o] += 1
            neighbors[s].add(o)
            neighbors[o].add(s)

    # 实体全集 = 出现在 entities 列表或关系端点中的所有规范名
    all_entities = set(docs) | set(relation_count) | set(neighbors)

    rows = []
    for ent in all_entities:
        kinds = kind_votes.get(ent, {})
        kind = max(kinds, key=kinds.get) if kinds else None
        rows.append({
            "entity": ent,
            "kind": kind,
            "degree": len(neighbors.get(ent, ())),        # 标准图度 = 不同邻居数
            "relation_count": relation_count.get(ent, 0),  # 关系边条数（含跨文档重复）
            "doc_count": len(docs.get(ent, ())),
        })

    # degree（不同邻居数）为主，relation_count 次之，doc_count 兜底；按名字稳定排序保证可复现
    rows.sort(key=lambda r: (-r["degree"], -r["relation_count"], -r["doc_count"], r["entity"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows, skipped


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="从 extracted/*.json 确定性计算实体中心度")
    parser.add_argument("--vault", required=True, help="Vault 根目录")
    parser.add_argument("--dedup-map", help="可选：变体→规范名 的 JSON 映射；传了就先折叠再统计")
    parser.add_argument("--top", type=int, default=0, help="只输出前 N 个（0=全部）")
    parser.add_argument("-o", "--output", help="输出 JSON 路径（默认 stdout）")
    args = parser.parse_args()

    vault = Path(args.vault)
    dmap = load_dedup_map(args.dedup_map)
    rows, skipped = compute(vault, dmap)

    top = rows[:args.top] if args.top and args.top > 0 else rows
    result = {
        "vault": str(vault),
        "dedup_applied": bool(dmap),
        "entity_count": len(rows),
        "returned": len(top),
        "sort": "degree(distinct neighbors) desc, relation_count desc, doc_count desc",
        "top": top,
    }
    if skipped:
        result["skipped_files"] = skipped

    out = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"中心度已写入: {args.output}（共 {len(rows)} 个实体，输出 {len(top)} 个）")
    else:
        print(out)


if __name__ == "__main__":
    main()
