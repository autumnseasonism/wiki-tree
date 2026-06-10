#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_mcp_server.py — 把本知识库暴露为 MCP 工具，支持 MCP 的 agent（Claude Code / Codex …）
连上后会在工具列表里自动看到 kb_search / kb_topic / kb_document 并据描述判断何时调用。

依赖: pip install "mcp[cli]"  （未安装时给出提示并退出；CLI 方式 kb_query.py 不依赖本文件）
注册(Claude Code): 已随包生成 .mcp.json；或 `claude mcp add <id>-kb -- python /abs/path/kb_mcp_server.py`
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

try:
    import kb_query
except Exception as e:  # noqa: BLE001
    sys.stderr.write("无法导入 kb_query.py: %s\n" % e)
    sys.exit(1)

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # noqa: BLE001
    sys.stderr.write(
        "未安装 MCP SDK。请先 `pip install \"mcp[cli]\"` 再注册本服务。\n"
        "（无需 MCP 也能用：python kb_query.py \"你的问题\" --json）\n"
    )
    sys.exit(2)

KB = kb_query.KB
_KB_PATH = os.path.join(ROOT, "kb.json")


def _kb_key():
    try:
        st = os.stat(_KB_PATH)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


_KB_KEY = _kb_key()


def _refresh():
    """kb.json (mtime,size) 变化时重读并赋回 kb_query.KB / EXTRACTED——常驻进程对增量
    emit 重写后的库不再返回旧主题表（与 kb_hub_server 的失效策略对齐）。每个 tool 入口先调本函数。
    注：kb_search 的 description（含 use_when 文案）在装饰器求值时已冻结，刷新不覆盖它，可接受。"""
    global KB, _KB_KEY
    key = _kb_key()
    if key is None or key == _KB_KEY:
        return
    try:
        kb = kb_query._load_kb()
    except (SystemExit, ValueError, OSError):
        return  # 读失败（被删/写一半等）：保留旧 KB 继续服务，下次调用重试
    KB = kb_query.KB = kb
    kb_query.EXTRACTED = os.path.join(kb_query.ROOT, kb["entrypoints"]["extracted_dir"])
    _KB_KEY = key


_SCOPE = "、".join(KB.get("use_when", [])[:10])
mcp = FastMCP(KB.get("name", "knowledge-base"))


_SEARCH_DESC = (
    "搜索本地知识库「%s」（适用问题涉及：%s 等）。返回相关「主题摘要」路径 + 候选「文档」路径；"
    "level=detailed 时附逐文档详细摘要。拿到结果后：先读 topics[].summary_file，需要确切数字再读 documents[].path。"
    % (KB.get("name", ""), _SCOPE)
)


@mcp.tool(description=_SEARCH_DESC)
def kb_search(question: str, level: str = "short", top: int = 5) -> str:
    _refresh()
    return json.dumps(kb_query.search(question, top, level), ensure_ascii=False, indent=2)


@mcp.tool()
def kb_topic(name: str) -> str:
    """获取某个主题的完整 L1 摘要（已把该主题全部文档汇总好）。用 kb_list_topics 看可选主题。"""
    _refresh()
    return kb_query.get_topic(name)


@mcp.tool()
def kb_document(doc_id: str, level: str = "detailed") -> str:
    """读某篇文档：level=short(1-2句) | detailed(逐文档详细摘要) | full(L0 原文)。"""
    _refresh()
    return kb_query.get_doc(doc_id, level)


@mcp.tool()
def kb_list_topics() -> str:
    """列出全部主题及其文档数与一句话摘要。"""
    _refresh()
    return json.dumps([
        {"name": t["name"], "docs": t.get("docs", 0), "one_liner": t.get("one_liner", "")}
        for t in KB.get("topics", [])
    ], ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
