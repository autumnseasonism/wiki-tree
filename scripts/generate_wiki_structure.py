#!/usr/bin/env python3
"""
generate_wiki_structure.py — 生成 Obsidian Wiki 目录结构和模板文件
用法: python generate_wiki_structure.py --output /path/to/vault

注意：此脚本只生成目录结构和配置文件。
实际的记忆抽取（实体提取、摘要生成）由 Agent 通过 LLM 调用完成。
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone


def create_obsidian_config(vault_path: Path):
    """创建 Obsidian 配置文件"""
    obsidian_dir = vault_path / ".obsidian"
    obsidian_dir.mkdir(parents=True, exist_ok=True)

    # 图谱颜色配置
    graph_config = {
        "color-groups": [
            {"query": "tag:#source/local-files", "color": {"a": 1, "rgb": 3066993}},
            {"query": "tag:#person", "color": {"a": 1, "rgb": 10494192}},
            {"query": "tag:#project", "color": {"a": 1, "rgb": 15158332}},
            {"query": "tag:#concept", "color": {"a": 1, "rgb": 3447003}},
            {"query": "tag:#tool", "color": {"a": 1, "rgb": 16776960}},
            {"query": "tag:#organization", "color": {"a": 1, "rgb": 16744448}},
            {"query": "tag:#summary", "color": {"a": 1, "rgb": 8421504}},
        ]
    }
    (obsidian_dir / "graph.json").write_text(
        json.dumps(graph_config, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    # 类型提示配置
    types_config = {
        "types": [
            {"name": "Document", "tag": "source/local-files", "color": "#3498db"},
            {"name": "Person", "tag": "person", "color": "#e74c3c"},
            {"name": "Project", "tag": "project", "color": "#2ecc71"},
            {"name": "Concept", "tag": "concept", "color": "#9b59b6"},
            {"name": "Summary", "tag": "summary", "color": "#95a5a6"},
        ]
    }
    (obsidian_dir / "types.json").write_text(
        json.dumps(types_config, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def create_wiki_structure(vault_path: Path):
    """创建 Wiki 目录结构"""
    dirs = [
        "documents",
        "summaries",
        "entities",
        "relations",
    ]
    for d in dirs:
        (vault_path / d).mkdir(parents=True, exist_ok=True)


def create_index_template(vault_path: Path, force: bool = False):
    """创建全局索引模板（已存在且非 force 时跳过，避免覆盖 Agent 已填内容）"""
    if (vault_path / "_index.md").exists() and not force:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    index_content = f"""---
kind: index
created_at: {now}
tags:
  - index
  - source/local-files
---

# 📚 个人知识库索引

> 本知识库由 Local Memory Wiki 自动生成

## 📊 统计

- **文档总数**：待填充
- **实体总数**：待填充
- **主题数**：待填充
- **最后更新**：{now}

## 🗂️ 主题概览

<!-- 由 Agent 填充，格式：
### [[summaries/topic-主题名|主题名]]
- 文档数：N
- 核心实体：[[<kind>-实体1]]、[[<kind>-实体2]]  （格式 [[<kind>-<实体名>]]，与 entities/ 卡片文件名一致，如 [[concept-RAG]]）
- 摘要：一句话概括
-->

## 🔑 核心实体

<!-- 由 Agent 填充 Top 10 核心实体 -->

## 📝 最近处理

<!-- 由 Agent 填充最近处理的文档列表 -->

---

*本索引由 [Local Memory Wiki](https://github.com) Skill 自动生成*
"""
    (vault_path / "_index.md").write_text(index_content, encoding="utf-8")


def create_global_summary_template(vault_path: Path, force: bool = False):
    """创建全局摘要模板（已存在且非 force 时跳过）"""
    if (vault_path / "summaries" / "_global-summary.md").exists() and not force:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = f"""---
kind: summary
summary_level: global
created_at: {now}
tags:
  - summary
  - source/local-files
---

# 全局摘要

> 本摘要由所有文档的主题摘要压缩生成

## 核心要点

<!-- 由 Agent 填充，格式：
1. **要点一**：具体描述
2. **要点二**：具体描述
...最多 10 条
-->

## 时间线

<!-- 由 Agent 填充关键时间点和事件 -->

## 知识图谱

<!-- 由 Agent 填充核心实体间的关系概览 -->

---

*自动生成于 {now}*
"""
    (vault_path / "summaries" / "_global-summary.md").write_text(
        content, encoding="utf-8"
    )


def create_knowledge_graph_template(vault_path: Path, force: bool = False):
    """创建知识关系总览模板（已存在且非 force 时跳过）"""
    if (vault_path / "relations" / "_knowledge-graph.md").exists() and not force:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = f"""---
kind: relation
created_at: {now}
tags:
  - relations
  - source/local-files
---

# 🕸️ 知识关系图谱

> 本文档记录所有实体间的关系

## 实体关系列表

<!-- 由 Agent 填充，格式：
### [[<kind>-实体A|实体A]] → 关系 → [[<kind>-实体B|实体B]]  （<kind>=person/concept/…，与 entities/ 卡片文件名一致）
- 来源：[[documents/doc1|文档1]]、[[documents/doc2|文档2]]
- 置信度：高/中/低
-->

## 关系类型统计

<!-- 由 Agent 按实际出现的关系类型填充；关系类型定义以 references/extraction-prompts.md 的「3. 关系抽取提示词」为唯一权威来源，此处不预置固定类型表，避免与权威来源漂移。
| 关系类型 | 数量 | 示例 |
|----------|------|------|
| <类型> | <数量> | <示例> |
-->

---

*自动生成于 {now}*
"""
    (vault_path / "relations" / "_knowledge-graph.md").write_text(
        content, encoding="utf-8"
    )


def create_memory_substrate(vault_path: Path):
    """创建增量/并行共享的状态底座：.memory-wiki/ + extracted/ + 空 manifest。

    - manifest.json：「哪些源文件已抽取完成」的唯一真相源（增量判断依据）
    - extracted/：每篇文档的抽取结果 JSON（持久缓存，reduce 阶段全量重建图谱）
    目录名以 . 开头，扫描时会被 scan_folder 自动排除，不会污染源扫描。
    """
    mw = vault_path / ".memory-wiki"
    (mw / "extracted").mkdir(parents=True, exist_ok=True)
    manifest = mw / "manifest.json"
    if not manifest.exists():
        manifest.write_text(
            json.dumps({"version": 1, "processed": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="生成 Obsidian Wiki 目录结构")
    parser.add_argument("--output", "-o", required=True, help="Vault 输出目录")
    parser.add_argument("--force", action="store_true",
                        help="强制覆写 _index.md/_global-summary.md/_knowledge-graph.md"
                             "（默认缺失才创建，避免覆盖 Agent 已填内容）")
    args = parser.parse_args()

    vault_path = Path(args.output)
    vault_path.mkdir(parents=True, exist_ok=True)

    create_obsidian_config(vault_path)
    create_wiki_structure(vault_path)
    create_memory_substrate(vault_path)
    create_index_template(vault_path, force=args.force)
    create_global_summary_template(vault_path, force=args.force)
    create_knowledge_graph_template(vault_path, force=args.force)

    print(f"Wiki 结构已创建: {vault_path}")
    print(f"  .obsidian/     — Obsidian 配置")
    print(f"  documents/     — 标准化文档")
    print(f"  summaries/     — 主题/全局摘要")
    print(f"  entities/      — 实体卡片")
    print(f"  relations/     — 知识关系")
    print(f"  _index.md      — 全局索引")


if __name__ == "__main__":
    main()
