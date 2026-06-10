#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_hub_server.py — 全局 KB 中枢 MCP 服务（进程内直读，无子进程）。

读 ~/.knowledge-bases/registry.json，把本机**所有**已注册知识库暴露为 MCP 工具：
  list_knowledge_bases / kb_search / kb_topic / kb_document
支持 MCP 的客户端连上后工具列表自动出现；新增 KB 重跑 kb_register.py 即动态生效。

实现说明：检索逻辑**在本进程内直接读各库文件**（按 registry 的 root 定位），不 spawn
子进程——避免"在 async stdio MCP 服务里跑阻塞 subprocess"在 Windows 上的管道句柄死锁，
并省掉每次冷启动。逻辑与各库的 kb_query.py 等价（四档下钻 short|detailed|full）。

依赖: pip install "mcp[cli]"
"""
import os
import re
import sys
import json
import glob
import math

REG = os.path.expanduser("~/.knowledge-bases/registry.json")

try:
    sys.stderr.reconfigure(encoding="utf-8")  # 警告/报错含中文；stdout 是 MCP stdio 通道，不动
except (AttributeError, ValueError):
    pass


def _load():
    try:
        return json.load(open(REG, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": "1.0", "knowledge_bases": []}


def _kb(kid):
    for k in _load().get("knowledge_bases", []):
        if k.get("id") == kid:
            return k
    return None


# CJK 单字停用字（与 kb_ingest.py 同源）：只滤单字 token，bigram 不滤
CJK_STOP = set("的了和是在与及或该本为也都把从这那个等对之其由以并而且但即则若如因故被让给向自"
               "至于各已未可须应要会能将就只更最不无有")


def _toks(s):
    """英文 [a-z0-9]+ 按词；CJK 连续段以重叠 bigram 为主、单字为辅（单字滤停用字）。
    必须与 emit_access_bundle.py 建索引的分词严格一致，改动需三处同步（emit/kb_query/本文件）。"""
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


def _kbjson(root):
    p = os.path.join(root, "kb.json")
    if not os.path.exists(p):
        raise FileNotFoundError(
            "未找到 kb.json（%s）；该库可能未生成接入包，请在该库目录运行 emit_access_bundle.py" % p)
    return json.load(open(p, encoding="utf-8"))


_IDX_CACHE = {}  # root -> ((mtime, size), (rows, df, n))


def _index(root, kb):
    """加载预分词检索索引并按 root 缓存；缓存键记录索引文件 (mtime, size)，每次 stat 比对，
    变了即重载（常驻进程对增量重建后的库不再返回旧结果）。索引缺失、读失败、比 extracted/
    更旧、或文档数与 extracted/ 不符（删除方向）时返回 None → 调用方回退全扫；读失败不缓存，下次重试。
    返回 (rows, df, n)；df 为 token→文档频率，v1 旧索引无此字段时为 None。"""
    ep = kb.get("entrypoints", {})
    p = os.path.join(root, ep.get("search_index", ".wiki-tree/search-index.json"))
    try:
        st = os.stat(p)
    except OSError:
        _IDX_CACHE.pop(root, None)
        return None
    ext = os.path.join(root, ep.get("extracted_dir", ".wiki-tree/extracted/"))
    files = glob.glob(os.path.join(ext, "*.json"))
    newest = 0
    for f in files:
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if m > newest:
            newest = m
    if st.st_mtime < newest:
        sys.stderr.write("[kb-hub] %s 索引过期（extracted/ 有更新），本次回退全扫；"
                         "请重跑 emit_access_bundle.py 刷新索引。\n" % root)
        _IDX_CACHE.pop(root, None)
        return None
    key = (st.st_mtime, st.st_size)
    hit = _IDX_CACHE.get(root)
    if hit and hit[0] == key:
        val = hit[1]
    else:
        try:
            data = json.load(open(p, encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            _IDX_CACHE.pop(root, None)
            return None
        rows = [(set(r.get("tok", [])), r) for r in data.get("docs", []) or []]
        val = (rows, data.get("df") or None, len(rows))
        _IDX_CACHE[root] = (key, val)
    # 过期的「删除」方向：回滚等删 extracted 文件不会推高最大 mtime，按文档数不符兜底
    if len(files) != val[2]:
        sys.stderr.write("[kb-hub] %s 索引过期（extracted/ 文档数与索引不符），本次回退全扫；"
                         "请重跑 emit_access_bundle.py 刷新索引。\n" % root)
        _IDX_CACHE.pop(root, None)
        return None
    return val


def _fmt_topic(root, t):
    """summary_missing 是 emit 时刻快照：摘要可能在 emit 后已补写，输出前实查一次文件，
    已存在则照常输出（与 _topic 的动态检查口径一致；仅对 scored[:3] 调用，开销可忽略）。"""
    missing = t.get("summary_missing") and not os.path.exists(os.path.join(root, t["summary_file"]))
    return {"name": t["name"], "summary_file": t["summary_file"],
            "one_liner": "(摘要尚未生成)" if missing else t.get("one_liner", "")}


def _search(root, q, top, level):
    qt = _toks(q)
    if not qt:
        return {"error": "empty query"}
    kb = _kbjson(root)
    res = _index(root, kb)
    rows, df, n = res if res is not None else (None, None, 0)
    scored = []
    for t in kb.get("topics", []):
        sc = _idf_score(qt, _toks(t["name"] + " " + t.get("one_liner", "")), df, n)
        if sc:
            scored.append((sc, t))
    scored.sort(key=lambda x: -x[0])
    docs = []
    if rows is not None:
        for toks, r in rows:
            sc = _idf_score(qt, toks, df, n)
            if sc:
                it = {"doc_id": r.get("id", ""), "path": r.get("md", ""),
                      "importance": r.get("imp", 0) or 0, "short": r.get("short", "")}
                if level in ("detailed", "full"):
                    it["detailed"] = r.get("detailed", "")
                # importance 作加权小项而非乘子：避免高 imp 泛化文档碾压低 imp 相关文档
                docs.append((sc + 0.3 * it["importance"], it))
    else:
        ext = os.path.join(root, kb["entrypoints"]["extracted_dir"])
        for f in glob.glob(os.path.join(ext, "*.json")):
            try:
                d = json.load(open(f, encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            did = d.get("doc_id", "")
            blob = (d.get("short_summary", "") + " " + d.get("detailed_summary", "") + " "
                    + did.replace("-", " ") + " "
                    + " ".join(t or "" for t in d.get("topics", []) or []) + " "
                    + " ".join((e.get("text") or "") for e in d.get("entities", []) or []))
            sc = _idf_score(qt, _toks(blob), None, 0)
            if sc:
                it = {"doc_id": did, "path": d.get("doc_md", "documents/%s.md" % did),
                      "importance": d.get("importance", 0) or 0, "short": d.get("short_summary", "")}
                if level in ("detailed", "full"):
                    it["detailed"] = d.get("detailed_summary", "")
                docs.append((sc + 0.3 * it["importance"], it))
    docs.sort(key=lambda x: -x[0])
    return {
        "query": q, "level": level,
        "drilldown": "L1 主题摘要(topics.summary_file) → 逐文档详细摘要(--level detailed) → L0 全文(documents 路径)",
        "topics": [_fmt_topic(root, t) for _, t in scored[:3]],
        "documents": [it for _, it in docs[:top]],
        "cite_rule": "引用 documents/*.md 路径为据；库内不足再用通用知识/联网。",
    }


def _topic(root, name):
    if not (name or "").strip():
        return "请提供主题名"
    kb = _kbjson(root)
    q = name.strip()
    qn = re.sub(r"\s+", "", q)
    qt = _toks(q)
    topics = kb.get("topics", [])
    # 先精确名、再去空白归一相等，最后才用词集子集判断（bigram 防「管理」误命中含管+理二字的任意主题）
    hits = ([t for t in topics if t["name"] == q]
            or [t for t in topics if re.sub(r"\s+", "", t["name"]) == qn]
            or [t for t in topics if qt and qt <= _toks(t["name"])])
    for t in hits:
        p = os.path.join(root, t["summary_file"])
        if os.path.exists(p):
            return open(p, encoding="utf-8").read()
    if hits:
        return ("主题「%s」的摘要尚未生成（缺 %s）。请直接读 documents/ 下相关原文，"
                "或补写主题摘要后重跑 finalize / emit_access_bundle。"
                % (hits[0]["name"], hits[0]["summary_file"]))
    return "未找到主题: %s" % name


def _doc(root, did, level):
    did = os.path.basename(did[:-3] if did.endswith(".md") else did)
    if level == "full":
        p = os.path.join(root, "documents", did + ".md")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else "未找到原文: " + did
    kb = _kbjson(root)
    p = os.path.join(root, kb["entrypoints"]["extracted_dir"], did + ".json")
    if not os.path.exists(p):
        return "未找到抽取记录: " + did
    d = json.load(open(p, encoding="utf-8"))
    return d.get("detailed_summary", "") if level == "detailed" else d.get("short_summary", "")


try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # noqa: BLE001
    sys.stderr.write('未安装 MCP SDK：请 `pip install "mcp[cli]"`。\n')
    sys.exit(2)

mcp = FastMCP("kb-hub")


@mcp.tool(description="列出本机所有本地知识库（id / 名称 / 范围 scope / 触发词 use_when / 规模）。回答前先用它了解有哪些库、各自管什么。")
def list_knowledge_bases() -> str:
    return json.dumps(
        [{f: kb.get(f) for f in ("id", "name", "scope", "use_when", "stats")}
         for kb in _load().get("knowledge_bases", [])],
        ensure_ascii=False, indent=2)


@mcp.tool(description="在指定本地知识库里检索（四档下钻）。"
                     "先调 list_knowledge_bases 获取当前库列表与各自 use_when，命中某库的问题→用其 id 调本工具。"
                     "返回相关主题摘要路径+候选文档；level=short|detailed|full（detailed 附逐文档详细摘要）。"
                     "拿到后先读 topics[].summary_file，要确切数字再读 documents[].path 并引用。")
def kb_search(kb_id: str, question: str, level: str = "short", top: int = 5) -> str:
    kb = _kb(kb_id)
    if not kb:
        return "未知 kb_id: %s（用 list_knowledge_bases 查看可用库）" % kb_id
    try:
        return json.dumps(_search(kb["root"], question, top, level), ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        return "检索失败: %s" % e


@mcp.tool(description="取某库某主题的完整 L1 摘要（已汇总该主题全部文档）。")
def kb_topic(kb_id: str, name: str) -> str:
    kb = _kb(kb_id)
    if not kb:
        return "未知 kb_id: %s" % kb_id
    try:
        return _topic(kb["root"], name)
    except Exception as e:  # noqa: BLE001
        return "读取失败: %s" % e


@mcp.tool(description="读某库某文档：level=short(1-2句)|detailed(逐文档详细摘要)|full(原文)。")
def kb_document(kb_id: str, doc_id: str, level: str = "detailed") -> str:
    kb = _kb(kb_id)
    if not kb:
        return "未知 kb_id: %s" % kb_id
    try:
        return _doc(kb["root"], doc_id, level)
    except Exception as e:  # noqa: BLE001
        return "读取失败: %s" % e


if __name__ == "__main__":
    mcp.run()
