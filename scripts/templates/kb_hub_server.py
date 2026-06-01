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

REG = os.path.expanduser("~/.knowledge-bases/registry.json")


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


def _toks(s):
    return set(re.findall(r"[a-z0-9]+|[一-鿿]", (s or "").lower()))


def _kbjson(root):
    p = os.path.join(root, "kb.json")
    if not os.path.exists(p):
        raise FileNotFoundError(
            "未找到 kb.json（%s）；该库可能未生成接入包，请在该库目录运行 emit_access_bundle.py" % p)
    return json.load(open(p, encoding="utf-8"))


_IDX_CACHE = {}


def _index(root, kb):
    """加载预分词检索索引并按 root 缓存（常驻进程：首查加载、后续复用）；无则返回 None。"""
    if root in _IDX_CACHE:
        return _IDX_CACHE[root]
    rel = kb.get("entrypoints", {}).get("search_index", ".wiki-tree/search-index.json")
    p = os.path.join(root, rel)
    rows = None
    if os.path.exists(p):
        try:
            rows = [(set(r.get("tok", [])), r)
                    for r in json.load(open(p, encoding="utf-8")).get("docs", [])]
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            rows = None
    _IDX_CACHE[root] = rows
    return rows


def _search(root, q, top, level):
    qt = _toks(q)
    if not qt:
        return {"error": "empty query"}
    kb = _kbjson(root)
    scored = []
    for t in kb.get("topics", []):
        sc = len(qt & _toks(t["name"] + " " + t.get("one_liner", "")))
        if sc:
            scored.append((sc, t))
    scored.sort(key=lambda x: -x[0])
    docs = []
    rows = _index(root, kb)
    if rows is not None:
        for toks, r in rows:
            sc = len(qt & toks)
            if sc:
                it = {"doc_id": r.get("id", ""), "path": r.get("md", ""),
                      "importance": r.get("imp", 0) or 0, "short": r.get("short", "")}
                if level in ("detailed", "full"):
                    it["detailed"] = r.get("detailed", "")
                docs.append((sc * (0.5 + it["importance"]), it))
    else:
        ext = os.path.join(root, kb["entrypoints"]["extracted_dir"])
        for f in glob.glob(os.path.join(ext, "*.json")):
            try:
                d = json.load(open(f, encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            blob = (d.get("short_summary", "") + " " + d.get("detailed_summary", "") + " "
                    + " ".join((e.get("text") or "") for e in d.get("entities", [])))
            sc = len(qt & _toks(blob))
            if sc:
                did = d.get("doc_id", "")
                it = {"doc_id": did, "path": d.get("doc_md", "documents/%s.md" % did),
                      "importance": d.get("importance", 0) or 0, "short": d.get("short_summary", "")}
                if level in ("detailed", "full"):
                    it["detailed"] = d.get("detailed_summary", "")
                docs.append((sc * (0.5 + it["importance"]), it))
    docs.sort(key=lambda x: -x[0])
    return {
        "query": q, "level": level,
        "drilldown": "L1 主题摘要(topics.summary_file) → 逐文档详细摘要(--level detailed) → L0 全文(documents 路径)",
        "topics": [{"name": t["name"], "summary_file": t["summary_file"],
                    "one_liner": t.get("one_liner", "")} for _, t in scored[:3]],
        "documents": [it for _, it in docs[:top]],
        "cite_rule": "引用 documents/*.md 路径为据；库内不足再用通用知识/联网。",
    }


def _topic(root, name):
    if not (name or "").strip():
        return "请提供主题名"
    kb = _kbjson(root)
    for t in kb.get("topics", []):
        if t["name"] == name or _toks(name) <= _toks(t["name"]):
            p = os.path.join(root, t["summary_file"])
            if os.path.exists(p):
                return open(p, encoding="utf-8").read()
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


def _catalog():
    kbs = _load().get("knowledge_bases", [])
    return "、".join("%s(%s)" % (k["id"], (k.get("scope") or "")[:20]) for k in kbs) or "(空)"


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


@mcp.tool(description="在指定本地知识库里检索（四档下钻）。当前可用库：" + _catalog() +
                     "。用法：命中某库 use_when 的问题→用其 id 调本工具；不确定先 list_knowledge_bases。"
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
