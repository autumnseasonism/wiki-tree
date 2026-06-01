#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_ingest.py — 入库路由器：给一个/一批散落的新文件，自动判定它该进哪个本地知识库。

读 ~/.knowledge-bases/registry.json 里**所有** KB 的“签名”做 IDF 加权 token 匹配，
按策略（confirm/auto）与用户确认后，把文件暂存进目标 vault 的 inbox 并交接增量重建。

设计要点
  - 路由信号：每个 KB 的“签名” = use_when + scope + name +（其 kb.json 的主题名/一句话）分词；
    IDF 跨 KB 加权——越是只属于某库的词区分度越高；文件 token = 文件名(权重更高) + 内容切片。
  - 决策三态：高置信(s1≥MIN 且 s1≥R×s2) / 歧义 / 无匹配(待归类)。
  - 显式 --kb：跳过打分，直奔写入（最高优先级——显式指定 = 最高置信意图）。
  - 写库前确认：mode=confirm 必确认；mode=auto 高置信直接写、**歧义/无匹配仍需决策（绝不瞎写）**。
  - 抽取/摘要是 LLM 步：本脚本只做确定性环节——暂存 + scan + convert（--build），以及抽取后的
    centrality+assemble+emit+register（--finalize）；抽取与主题摘要交 agent 按 SKILL.md 完成。

用法
  路由+确认+暂存（默认）:
    python kb_ingest.py FILE [FILE...] [--kb ID] [--mode confirm|auto] [--yes] [--json]
  只看路由计划、不写:
    python kb_ingest.py FILE...  --plan         # --json 亦默认只出计划，除非加 --yes
  暂存后直接跑确定性 scan+convert:
    python kb_ingest.py FILE... --kb ID --yes --build
  抽取完成后收尾（确定性 reduce + 重建索引 + 更新 registry）:
    python kb_ingest.py --finalize --kb ID
  回滚一篇暂存（给 inbox 文件名）:
    python kb_ingest.py --rollback FILENAME --kb ID
"""
import os
import re
import sys
import json
import math
import shutil
import hashlib
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone

HOME = Path.home()
KB_DIR = HOME / ".knowledge-bases"
REGISTRY = KB_DIR / "registry.json"
POLICY = KB_DIR / "ingest-policy.json"
LOG = KB_DIR / "ingest-log.jsonl"

DEFAULT_POLICY = {
    "mode": "confirm",        # confirm=写库前必确认；auto=高置信直接写、歧义/无匹配仍需决策
    "min_score": 2.0,         # 最低绝对分（低于则判“无匹配/待归类”）
    "ratio": 1.5,             # 第一名需 ≥ 第二名的多少倍才算“高置信”
    "per_kb_override": {},    # {kb_id: {"min_score": .., "ratio": ..}}
    "skill_scripts_dir": "",  # memory-wiki/scripts 目录；留空自动探测
}

SUPPORTED = {".docx", ".pdf", ".md", ".markdown", ".mdx",
             ".json", ".txt", ".text", ".log", ".csv"}
TEXT_LIKE = {".md", ".markdown", ".mdx", ".txt", ".text", ".log", ".csv", ".json"}


def _utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


# 路由专用停用词：英文功能词 + 中文常见虚词。仅用于「判库」匹配，不影响 kb_query 的检索分词。
# 单 KB 时 IDF 无法区分（所有 token df=1→idf=1），停用词会污染打分，必须显式滤除。
EN_STOP = {
    "a", "an", "the", "and", "or", "but", "if", "of", "to", "in", "on", "at", "for", "by",
    "with", "from", "as", "is", "are", "was", "were", "be", "been", "being", "it", "its",
    "this", "that", "these", "those", "i", "you", "he", "she", "we", "they", "him", "her",
    "them", "my", "your", "his", "our", "their", "me", "us", "do", "does", "did", "done",
    "has", "have", "had", "will", "would", "can", "could", "should", "may", "might", "must",
    "shall", "not", "no", "yes", "so", "than", "then", "there", "here", "what", "which",
    "who", "whom", "whose", "how", "when", "where", "why", "all", "any", "some", "each",
    "every", "both", "few", "more", "most", "other", "into", "over", "under", "about", "up",
    "down", "out", "off", "again", "further", "once", "only", "own", "same", "such", "too",
    "very", "just", "also", "via", "per", "etc", "ie", "eg", "vs", "let", "get", "got",
}
CJK_STOP = set("的了和是在与及或该本为也都把从这那个等对之其由以并而且但即则若如因故被让给向自"
               "至于各已未可须应要会能将就只更最不无有")


def _toks(s):
    """英文按词，CJK(含扩展A)/谚文/假名按字；滤除停用词、单字母、纯数字（路由匹配用，越准越好）。
    字符类比 kb_query 检索分词更宽以支持多语种库；二者独立、互不影响。"""
    out = set()
    for t in re.findall(r"[a-z0-9]+|[㐀-鿿가-힣぀-ヿ]", (s or "").lower()):
        if t in CJK_STOP:
            continue
        if t.isascii():
            if t.isdigit():
                continue  # 纯数字（页码/年份等）不作判库信号
            if t.isalpha() and (len(t) <= 1 or t in EN_STOP):
                continue
        out.add(t)
    return out


# ---------------- 配置加载 ----------------
def load_registry():
    if not REGISTRY.exists():
        return []
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8")).get("knowledge_bases", [])
    except (OSError, json.JSONDecodeError):
        return []


def load_policy():
    pol = dict(DEFAULT_POLICY)
    if POLICY.exists():
        try:
            data = json.loads(POLICY.read_text(encoding="utf-8"))
            pol.update({k: v for k, v in data.items() if k in DEFAULT_POLICY})
        except (OSError, json.JSONDecodeError):
            pass
    else:
        try:  # 首次运行落一份默认策略，方便用户改 mode
            KB_DIR.mkdir(parents=True, exist_ok=True)
            POLICY.write_text(json.dumps(DEFAULT_POLICY, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        except OSError:
            pass
    return pol


def policy_for(pol, kb_id):
    o = (pol.get("per_kb_override") or {}).get(kb_id, {})
    return (float(o.get("min_score", pol["min_score"])),
            float(o.get("ratio", pol["ratio"])))


# ---------------- KB 签名 + IDF ----------------
def kb_signature(kb):
    parts = list(kb.get("use_when", [])) + [kb.get("scope", ""), kb.get("name", "")]
    kbj = Path(kb.get("root", "")) / "kb.json"
    if kbj.exists():
        try:
            data = json.loads(kbj.read_text(encoding="utf-8"))
            for t in data.get("topics", []) or []:
                parts.append(t.get("name", ""))
                parts.append(t.get("one_liner", ""))
        except (OSError, json.JSONDecodeError):
            pass
    return _toks(" ".join(parts))


def build_idf(sigs):
    n = len(sigs)
    df = {}
    for s in sigs:
        for t in s:
            df[t] = df.get(t, 0) + 1
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}


# ---------------- 文件 token ----------------
def _read_slice(p, max_chars=8000):
    ext = p.suffix.lower()
    try:
        if ext in TEXT_LIKE:
            return p.read_text(encoding="utf-8", errors="ignore")[:max_chars]
        if ext == ".docx":
            try:
                from docx import Document
                buf, n = [], 0
                for para in Document(str(p)).paragraphs:
                    t = para.text or ""
                    buf.append(t)
                    n += len(t)
                    if n > max_chars:
                        break
                return " ".join(buf)[:max_chars]
            except Exception:
                return ""
        if ext == ".pdf":
            try:
                import fitz
                d = fitz.open(str(p))
                buf, n = [], 0
                for page in d:
                    t = page.get_text("text")
                    buf.append(t)
                    n += len(t)
                    if n > max_chars:
                        break
                d.close()
                return " ".join(buf)[:max_chars]
            except Exception:
                return ""
    except OSError:
        return ""
    return ""


def file_tokens(p):
    """返回 {token: weight}；文件名 token 权重更高（强信号）。"""
    w = {}
    for t in _toks(_read_slice(p)):
        w[t] = 1.0
    for t in _toks(p.stem):
        w[t] = max(w.get(t, 0.0), 2.0)
    return w


# ---------------- 打分 + 决策 ----------------
def score_kb(fw, sig, idf):
    s, hits = 0.0, []
    for t, wt in fw.items():
        if t in sig:
            v = idf.get(t, 1.0) * wt
            s += v
            hits.append((t, v))
    hits.sort(key=lambda x: -x[1])
    return s, [t for t, _ in hits]


def decide(scored, min_score, ratio):
    """scored: [(kb_id, score, hits)] 降序。返回 (state, payload)。"""
    if not scored or scored[0][1] <= 0:
        return "none", scored[:3]
    s1 = scored[0][1]
    s2 = scored[1][1] if len(scored) > 1 else 0.0
    if s1 >= min_score and (s2 <= 1e-9 or s1 >= ratio * s2):
        return "assign", scored[0]
    if s1 >= min_score:
        cands = [x for x in scored if x[1] >= min_score and x[1] * ratio >= s1]
        return "ambiguous", (cands or scored[:2])
    return "none", scored[:3]


def route_file(p, kbs, sigs, idf, pol, explicit=None):
    if explicit:
        return {"file": str(p), "name": p.name, "state": "explicit",
                "target": explicit, "hits": [], "scored": []}
    fw = file_tokens(p)
    scored = []
    for kb, sig in zip(kbs, sigs):
        s, hits = score_kb(fw, sig, idf)
        scored.append((kb["id"], s, hits))
    scored.sort(key=lambda x: -x[1])
    min_score, ratio = policy_for(pol, scored[0][0]) if scored else (pol["min_score"], pol["ratio"])
    state, payload = decide(scored, min_score, ratio)
    out = {"file": str(p), "name": p.name, "state": state,
           "scored": [{"kb": k, "score": round(s, 3), "hits": h[:8]} for k, s, h in scored[:5]]}
    if state == "assign":
        out["target"], out["score"], out["hits"] = payload[0], round(payload[1], 3), payload[2][:8]
    elif state == "ambiguous":
        out["candidates"] = [x[0] for x in payload]
    return out


# ---------------- 文件收集 ----------------
def collect(paths, recursive=True):
    files, skipped = [], []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            (files if p.suffix.lower() in SUPPORTED else skipped).append(p)
        elif p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for f in it:
                if (f.is_file() and f.suffix.lower() in SUPPORTED
                        and not any(part.startswith(".") for part in f.relative_to(p).parts)):
                    files.append(f)
        else:
            skipped.append(p)
    seen, uniq = set(), []
    for f in files:
        k = str(f.resolve())
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq, skipped


# ---------------- 暂存 / 审计 ----------------
def _file_hash(p):
    h = hashlib.md5()
    try:
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(65536), b""):
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def stage(src, vault):
    inbox = Path(vault) / ".memory-wiki" / "_ingest"
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / src.name
    if dest.exists():
        sh, dh = _file_hash(src), _file_hash(dest)
        if not (sh and dh and sh == dh):  # 同名但内容不同/无法比对 → 按内容哈希加后缀，绝不覆盖
            sfx = (sh or hashlib.md5(str(src.resolve()).encode("utf-8")).hexdigest())[:8]
            dest = inbox / ("%s-%s%s" % (src.stem, sfx, src.suffix))
    shutil.copy2(src, dest)
    return dest


def audit(rec):
    try:
        KB_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def do_writes(do, regmap, mode):
    by_kb, staged = {}, []
    now = datetime.now(timezone.utc).isoformat()
    for p in do:
        kid = p["target"]
        vault = (regmap.get(kid) or {}).get("root", "")
        if not vault or not Path(vault).exists():
            print("跳过 %s：KB '%s' 的 root 无效/不存在(%r)" % (
                Path(p["file"]).name, kid, vault), file=sys.stderr)
            continue
        dest = stage(Path(p["file"]), vault)
        by_kb[kid] = vault
        rec = {"time": now, "file": p["file"], "staged": str(dest), "target_kb": kid,
               "state": p["state"], "score": p.get("score"), "hits": p.get("hits", []),
               "mode": mode}
        audit(rec)
        staged.append(rec)
    return by_kb, staged


# ---------------- 确定性管线（调 skill 脚本）----------------
def resolve_scripts_dir(pol):
    cands = [pol.get("skill_scripts_dir") or "",
             str(HOME / ".claude" / "skills" / "memory-wiki" / "scripts"),
             str(HOME / ".codex" / "skills" / "memory-wiki" / "scripts")]
    for c in cands:
        if c and (Path(c) / "scan_folder.py").exists():
            return c
    return None


def _run(cmd):
    print("   $ " + " ".join('"%s"' % c if (" " in c) else c for c in cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError("命令失败(rc=%d): %s" % (r.returncode, " ".join(cmd)))


def stage_build(vault, scripts):
    """确定性：增量 scan + convert（抽取前可做的全部）。"""
    py = sys.executable
    mw = Path(vault) / ".memory-wiki"
    inbox, scan_out = str(mw / "_ingest"), str(mw / "_ingest_scan.json")
    _run([py, str(Path(scripts) / "scan_folder.py"), inbox, "--vault", vault, "-o", scan_out])
    _run([py, str(Path(scripts) / "convert_documents.py"),
          "--scan-report", scan_out, "--output", vault])


def finalize(vault, kb, scripts):
    """确定性 reduce + 重建索引 + 更新 registry（应在 agent 完成抽取后调用）。"""
    py = sys.executable
    mw = Path(vault) / ".memory-wiki"
    cc = [py, str(Path(scripts) / "compute_centrality.py"), "--vault", vault]
    if (mw / "_dedup-map.json").exists():
        cc += ["--dedup-map", str(mw / "_dedup-map.json")]
    cc += ["-o", str(mw / "centrality.json")]
    _run(cc)
    _run([py, str(Path(scripts) / "assemble_vault.py"), "--vault", vault])
    emit = [py, str(Path(scripts) / "emit_access_bundle.py"), "--vault", vault,
            "--id", kb["id"], "--name", kb["name"], "--scope", kb.get("scope", "")]
    if kb.get("use_when"):
        emit += ["--extra-use-when", ",".join(kb["use_when"])]
    _run(emit)
    _run([py, str(Path(scripts) / "kb_register.py"), "--vault", vault])


def rollback(vault, basename):
    mw = Path(vault) / ".memory-wiki"
    removed = []
    staged = mw / "_ingest" / basename
    if staged.exists():
        try:
            staged.unlink()
            removed.append(str(staged))
        except OSError:
            pass
    # best-effort：从转换报告映射 inbox 源 → 产物 doc-md
    doc_md = None
    rep = Path(vault) / "_conversion_report.json"
    if rep.exists():
        try:
            data = json.loads(rep.read_text(encoding="utf-8"))
            entries = data if isinstance(data, list) else (
                data.get("results") or data.get("converted") or [])
            for e in entries or []:
                src = e.get("source") or e.get("source_path") or e.get("path") or ""
                if src and (Path(src).name == basename or src.endswith(basename)):
                    doc_md = e.get("output") or e.get("doc_md") or e.get("md") or ""
                    break
        except (OSError, json.JSONDecodeError):
            pass
    stem = Path(doc_md).stem if doc_md else Path(basename).stem
    for cand in [Path(vault) / "documents" / (stem + ".md"),
                 mw / "extracted" / (stem + ".json")]:
        if cand.exists():
            try:
                cand.unlink()
                removed.append(str(cand))
            except OSError:
                pass
    mf = mw / "manifest.json"
    if mf.exists():
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            proc = data.get("processed", {})
            keys = [k for k in proc if Path(k).name == basename or k.endswith(basename)]
            for k in keys:
                proc.pop(k, None)
            if keys:
                mf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                removed.append("manifest:%d 条" % len(keys))
        except (OSError, json.JSONDecodeError):
            pass
    for r in removed:
        print("  - 删除 %s" % r)
    if not removed:
        print("  （未找到可回滚的产物——可能尚未 build/extract）")


# ---------------- 输出 ----------------
def print_plan_human(plan, pol):
    print("路由计划（mode=%s, min_score=%.1f, ratio=%.1f）:" % (
        pol["mode"], pol["min_score"], pol["ratio"]))
    for p in plan:
        if p["state"] == "explicit":
            print("  • %s → [%s]（显式指定，跳过匹配）" % (p["name"], p["target"]))
        elif p["state"] == "assign":
            print("  • %s → [%s]  分=%.2f  命中: %s" % (
                p["name"], p["target"], p["score"], "/".join(p["hits"][:6])))
        elif p["state"] == "ambiguous":
            tops = "; ".join("%s(%.2f)" % (s["kb"], s["score"]) for s in p["scored"][:3])
            print("  • %s → 歧义: %s" % (p["name"], tops))
        else:
            top = p["scored"][0] if p.get("scored") else None
            tail = ("（最高 %s=%.2f，低于 min_score）" % (top["kb"], top["score"])) if top else ""
            print("  • %s → 无匹配·待归类%s" % (p["name"], tail))


def _ask_pick(p):
    cs = p.get("candidates", [])
    if not cs:
        return None
    print("\n歧义: %s" % p["name"])
    for i, s in enumerate(p["scored"][:len(cs)]):
        print("  [%d] %s  分=%.2f  命中: %s" % (i + 1, s["kb"], s["score"], "/".join(s["hits"][:6])))
    ans = input("选编号写入，或回车跳过: ").strip()
    if ans.isdigit() and 1 <= int(ans) <= len(cs):
        return p["scored"][int(ans) - 1]["kb"]
    return None


def print_handoff(by_kb, scripts, built):
    sd = scripts or "<memory-wiki/scripts>"
    print("\n=== 后续：抽取 + reduce（抽取/摘要是 LLM 步，由 agent 按 SKILL.md 完成）===")
    for kid, vault in by_kb.items():
        print("\n[%s] vault: %s" % (kid, vault))
        if not built:
            print("  1) 增量扫描+转换（确定性；本脚本可代跑：加 --build）:")
            print('     python "%s/scan_folder.py" "%s/.memory-wiki/_ingest" --vault "%s" -o "%s/.memory-wiki/_ingest_scan.json"'
                  % (sd, vault, vault, vault))
            print('     python "%s/convert_documents.py" --scan-report "%s/.memory-wiki/_ingest_scan.json" --output "%s"'
                  % (sd, vault, vault))
        print("  2) 抽取（LLM·agent）: 读 documents/ 新增 .md，按 references/subagent-batch-extraction.md")
        print("     写 .memory-wiki/extracted/<doc-id>.json；再登记 manifest:")
        print('     python "%s/update_manifest.py" --vault "%s" --from-conversion-report "%s/_conversion_report.json"'
              % (sd, vault, vault))
        print("  3) 收尾（确定性；本脚本可代跑：python kb_ingest.py --finalize --kb %s）:" % kid)
        print("     compute_centrality → assemble_vault → emit_access_bundle → kb_register")
        print("  4) 摘要（LLM·agent，可选）: 新增内容若影响某主题，重写 summaries/topic-<主题>.md（及全局摘要）")


# ---------------- 主流程 ----------------
def is_tty():
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def main():
    _utf8()
    ap = argparse.ArgumentParser(
        description="入库路由器：判定新文件归属哪个本地知识库并按策略写入")
    ap.add_argument("paths", nargs="*", help="待入库的文件或目录")
    ap.add_argument("--kb", help="显式指定目标库 id（跳过匹配，直奔写入）")
    ap.add_argument("--mode", choices=["confirm", "auto"], help="覆盖策略模式")
    ap.add_argument("--min-score", type=float, help="覆盖最低绝对分")
    ap.add_argument("--ratio", type=float, help="覆盖高置信倍率")
    ap.add_argument("--plan", action="store_true", help="只输出路由计划，不写入")
    ap.add_argument("--json", action="store_true",
                    help="JSON 输出（默认只出计划不写，除非加 --yes）")
    ap.add_argument("-y", "--yes", action="store_true", help="非交互直接写入计划内的可写项")
    ap.add_argument("--build", action="store_true", help="暂存后立即跑确定性 scan+convert")
    ap.add_argument("--no-recursive", action="store_true", help="目录不递归")
    ap.add_argument("--finalize", action="store_true",
                    help="对 --kb 的 vault 跑确定性 reduce+emit+register（抽取后收尾）")
    ap.add_argument("--rollback", help="回滚：给暂存文件名（inbox basename），需配 --kb")
    a = ap.parse_args()

    reg = load_registry()
    regmap = {k["id"]: k for k in reg}
    pol = load_policy()
    if a.mode:
        pol["mode"] = a.mode
    if a.min_score is not None:
        pol["min_score"] = a.min_score
    if a.ratio is not None:
        pol["ratio"] = a.ratio
    scripts = resolve_scripts_dir(pol)

    # --- finalize ---
    if a.finalize:
        if not a.kb or a.kb not in regmap:
            print("错误：--finalize 需要有效 --kb（已注册: %s）" % (", ".join(regmap) or "无"),
                  file=sys.stderr)
            sys.exit(2)
        root = regmap[a.kb].get("root", "")
        if not root or not Path(root).exists():
            print("错误：KB '%s' 的 root 无效/不存在: %r" % (a.kb, root), file=sys.stderr)
            sys.exit(2)
        if not scripts:
            print("错误：找不到 memory-wiki/scripts；请在 ingest-policy.json 设 skill_scripts_dir",
                  file=sys.stderr)
            sys.exit(2)
        try:
            finalize(root, regmap[a.kb], scripts)
        except Exception as e:
            print("收尾失败（vault 可能处于半重建态，检查后重跑 --finalize）: %s" % e, file=sys.stderr)
            sys.exit(1)
        print("收尾完成: %s" % a.kb)
        return

    # --- rollback ---
    if a.rollback:
        if not a.kb or a.kb not in regmap:
            print("错误：--rollback 需要有效 --kb", file=sys.stderr)
            sys.exit(2)
        root = regmap[a.kb].get("root", "")
        if not root or not Path(root).exists():
            print("错误：KB '%s' 的 root 无效/不存在: %r" % (a.kb, root), file=sys.stderr)
            sys.exit(2)
        rollback(root, a.rollback)
        print("已回滚暂存: %s（如需重建，跑 --finalize --kb %s）" % (a.rollback, a.kb))
        return

    if not a.paths:
        print("错误：未提供待入库文件/目录", file=sys.stderr)
        sys.exit(2)
    if not reg:
        print("错误：registry 为空（~/.knowledge-bases/registry.json）。先用 kb_register.py 登记至少一个 KB。",
              file=sys.stderr)
        sys.exit(2)

    files, skipped = collect(a.paths, recursive=not a.no_recursive)
    for s in skipped:
        print("跳过（不支持/不存在）: %s" % s, file=sys.stderr)
    if not files:
        print("错误：没有可入库的受支持文件", file=sys.stderr)
        sys.exit(2)

    explicit = None
    if a.kb:
        if a.kb not in regmap:
            print("错误：未知 --kb '%s'（已注册: %s）" % (a.kb, ", ".join(regmap)), file=sys.stderr)
            sys.exit(2)
        explicit = a.kb

    sigs = [kb_signature(k) for k in reg] if not explicit else []
    idf = build_idf(sigs) if sigs else {}
    plan = [route_file(f, reg, sigs, idf, pol, explicit=explicit) for f in files]

    # --- JSON 模式（非交互；写入仅当 --yes）---
    if a.json:
        result = {"mode": pol["mode"], "plan": plan}
        if a.yes and not a.plan:
            do = [p for p in plan if p["state"] in ("explicit", "assign")]
            by_kb, staged = do_writes(do, regmap, pol["mode"])
            result["staged"] = staged
            result["skipped"] = {
                "ambiguous": [p["name"] for p in plan if p["state"] == "ambiguous"],
                "none": [p["name"] for p in plan if p["state"] == "none"]}
            if a.build and scripts and by_kb:
                errs = {}
                for kid, vault in by_kb.items():
                    try:
                        stage_build(vault, scripts)
                    except Exception as e:
                        errs[kid] = str(e)
                result["built"] = (not errs)
                if errs:
                    result["build_errors"] = errs
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # --- 人类可读模式 ---
    print_plan_human(plan, pol)
    if a.plan:
        return

    writable = [p for p in plan if p["state"] in ("explicit", "assign")]
    ambiguous = [p for p in plan if p["state"] == "ambiguous"]
    nones = [p for p in plan if p["state"] == "none"]

    if a.yes:
        do = writable
    elif pol["mode"] == "auto":
        do = writable  # auto：高置信/显式直接写；歧义/无匹配不动
    else:  # confirm
        if is_tty():
            resolved = []
            for p in ambiguous:
                pick = _ask_pick(p)
                if pick:
                    p["state"], p["target"] = "explicit", pick
                    writable.append(p)
                    resolved.append(p)
            ambiguous = [x for x in ambiguous if all(x is not r for r in resolved)]
            ans = input("\n写入以上 %d 个文件? [y/N] " % len(writable)).strip().lower()
            do = writable if ans.startswith("y") else []
        else:
            print("\nconfirm 模式且非交互：未写入。确认后加 --yes；或 --kb 指定库；或改 --mode auto。")
            return

    if not do:
        print("\n未写入任何文件。")
        if ambiguous:
            print("歧义待决: %s" % ", ".join(p["name"] for p in ambiguous))
        if nones:
            print("无匹配·待归类: %s" % ", ".join(p["name"] for p in nones))
        return

    by_kb, staged = do_writes(do, regmap, pol["mode"])
    for rec in staged:
        print("✓ 暂存 %s → [%s] %s" % (Path(rec["file"]).name, rec["target_kb"], rec["staged"]))

    built = False
    if a.build:
        if not scripts:
            print("警告：--build 需要 scripts 目录但未找到；请设 skill_scripts_dir 或手动跑。",
                  file=sys.stderr)
        else:
            ok = True
            for kid, vault in by_kb.items():
                print("\n=== 确定性 build: %s ===" % kid)
                try:
                    stage_build(vault, scripts)
                except Exception as e:
                    ok = False
                    print("build 失败 [%s]: %s（其余 KB 继续）" % (kid, e), file=sys.stderr)
            built = ok

    if ambiguous:
        print("\n歧义未写（指定 --kb 或交互选择）: %s" % ", ".join(p["name"] for p in ambiguous))
    if nones:
        print("无匹配·待归类（未写）: %s" % ", ".join(p["name"] for p in nones))

    print_handoff(by_kb, scripts, built)


if __name__ == "__main__":
    main()
