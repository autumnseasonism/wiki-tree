#!/usr/bin/env python3
"""
suggest_dedup.py — 从 extracted/*.json 里挖"疑似同一实体的不同写法"候选对，辅助生成 _dedup-map.json

去重是语义活、最终交 LLM 判断；本脚本只做**确定性的候选挖掘**（不下结论），把值得人/LLM
复核的实体对列出来，避免在几十上百个实体里肉眼找变体。命中规则（任一）：
  - 归一化后相等：忽略大小写/空格/._-·后一致（如 "Chart.js" vs "chartjs"、"Zhang San" vs "zhangsan"）
  - 一个是另一个的子串（长度接近，仅限非平凡长度，避免 "AI" 命中一切）
  - 编辑距离 ≤1（短拼写差异/typo，长度≥4 才判，降低误报）

输出候选 JSON，并按出现文档数提示更可能的规范名（freq 高者）。**agent 复核后**自行写
最终的 _dedup-map.json（格式：{变体: 规范名}）。跨语言译名（如 "张三"/"Zhang San"）这类
无字面相似的变体本脚本发现不了，仍需 LLM 依据语义补充。

用法:
  python suggest_dedup.py --vault VAULT [-o candidates.json] [--max-pairs N]
"""

import sys
import json
import argparse
from pathlib import Path
from collections import defaultdict

_STRIP = set(" ._-·/\\\t·　")


def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c not in _STRIP)


def _edit_distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 1:
        return 2  # 提前剪枝：差超过 1 不可能 ≤1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def collect_entities(vault: Path):
    """返回 {实体名: 出现的不同文档数}。"""
    freq = defaultdict(set)
    d = vault / ".wiki-tree" / "extracted"
    for fp in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        did = obj.get("doc_id", fp.stem)
        for ent in obj.get("entities", []) or []:
            if isinstance(ent, dict) and (ent.get("text") or "").strip():
                freq[ent["text"].strip()].add(did)
        for rel in obj.get("relations", []) or []:
            if isinstance(rel, dict):
                for endp in (rel.get("subject"), rel.get("object")):
                    if endp and endp.strip():
                        freq[endp.strip()].add(did)
    return {k: len(v) for k, v in freq.items()}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="挖掘 _dedup-map 候选变体对（不下结论，交 LLM 复核）")
    ap.add_argument("--vault", required=True, help="Vault 根目录")
    ap.add_argument("-o", "--output", help="输出 JSON（默认 stdout）")
    ap.add_argument("--max-pairs", type=int, default=200, help="候选对上限（默认 200）")
    args = ap.parse_args()

    vault = Path(args.vault)
    freq = collect_entities(vault)
    ents = sorted(freq)
    seen = set()
    cands = []

    for i in range(len(ents)):
        for j in range(i + 1, len(ents)):
            a, b = ents[i], ents[j]
            reason = None
            na, nb = _norm(a), _norm(b)
            if na and na == nb:
                reason = "归一化后相等"
            elif len(min(a, b, key=len)) >= 3 and (
                    (a in b or b in a) and a != b):
                reason = "子串包含"
            elif min(len(a), len(b)) >= 4 and a.isascii() and b.isascii() \
                    and _edit_distance(a.lower(), b.lower()) <= 1:
                reason = "编辑距离≤1（疑似拼写差异）"
            if reason:
                key = tuple(sorted((a, b)))
                if key in seen:
                    continue
                seen.add(key)
                # freq 高者更可能是规范名；并列则取较短
                canonical = max((a, b), key=lambda x: (freq[x], -len(x)))
                variant = b if canonical == a else a
                cands.append({
                    "variants": [a, b],
                    "reason": reason,
                    "suggest_canonical": canonical,
                    "suggest_map": {variant: canonical},
                    "freq": {a: freq[a], b: freq[b]},
                })
                if len(cands) >= args.max_pairs:
                    break
        if len(cands) >= args.max_pairs:
            break

    result = {
        "vault": str(vault),
        "entity_count": len(ents),
        "candidate_pairs": len(cands),
        "note": "确定性候选，需 agent/LLM 复核后写入 _dedup-map.json；跨语言译名等无字面相似的变体本脚本无法发现。",
        "candidates": cands,
    }
    out = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"候选已写入: {args.output}（{len(ents)} 实体 → {len(cands)} 候选对）")
    else:
        print(out)


if __name__ == "__main__":
    main()
