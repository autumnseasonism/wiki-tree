#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_hub_server.py — 全局 KB 中枢 MCP 服务。

读 ~/.knowledge-bases/registry.json，把本机**所有**已注册知识库暴露为 MCP 工具：
  list_knowledge_bases / kb_search / kb_topic / kb_document
支持 MCP 的客户端（Claude Code / Codex …）连上后会在工具列表里自动看到它们，
据描述判断何时调用——无需任何指令文件。新增 KB 只要重跑 kb_register.py
（registry 多一条），本服务动态读取、**无需改任何配置**。

依赖: pip install "mcp[cli]"
注册示例:
  Codex     ~/.codex/config.toml:  [mcp_servers.kb_hub]  command="python" args=["<此文件绝对路径>"]
  Claude Code:  claude mcp add -s user kb-hub -- python "<此文件绝对路径>"
"""
import os
import sys
import json
import subprocess

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


def _run(kb, extra):
    kbq = os.path.join(kb["root"], "kb_query.py")
    p = subprocess.run([sys.executable, kbq] + extra,
                       capture_output=True, text=True, encoding="utf-8")
    return p.stdout or p.stderr or "(无输出)"


def _catalog():
    kbs = _load().get("knowledge_bases", [])
    return "、".join("%s(%s)" % (k["id"], (k.get("scope") or "")[:20]) for k in kbs) or "(空)"


try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # noqa: BLE001
    sys.stderr.write('未安装 MCP SDK：请 `pip install "mcp[cli]"`。\n')
    sys.exit(2)

mcp = FastMCP("kb-hub")


@mcp.tool(description="列出本机所有本地知识库（id / 名称 / 范围 scope / 触发词 use_when / 规模）。"
                     "回答前先用它了解有哪些库、各自管什么。")
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
    return _run(kb, [question, "--json", "--level", level, "--top", str(top)])


@mcp.tool(description="取某库某主题的完整 L1 摘要（已汇总该主题全部文档）。")
def kb_topic(kb_id: str, name: str) -> str:
    kb = _kb(kb_id)
    return _run(kb, ["--topic", name]) if kb else "未知 kb_id: %s" % kb_id


@mcp.tool(description="读某库某文档：level=short(1-2句)|detailed(逐文档详细摘要)|full(原文)。")
def kb_document(kb_id: str, doc_id: str, level: str = "detailed") -> str:
    kb = _kb(kb_id)
    return _run(kb, ["--doc", doc_id, "--level", level]) if kb else "未知 kb_id: %s" % kb_id


if __name__ == "__main__":
    mcp.run()
