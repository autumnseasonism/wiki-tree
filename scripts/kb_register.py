#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_register.py — 把一个已生成接入包的 vault 注册到「全局 KB 注册中心」，
让任何项目、任何运行时里的 agent 都能发现并调用它（跨项目可发现性）。

做两件事：
  1) upsert  ~/.knowledge-bases/registry.json （新增/更新本 KB 条目；幂等，按 id）
  2) (可选) 安装「全局 hook」：在 ~/.claude/CLAUDE.md 写入一段 KB-HUB managed block，
     告诉 agent「本机有哪些 KB、命中其 use_when 时如何用 query_cli 检索」。
     —— 这一步会改你的全局指令文件，故默认不做，需显式 --install-hook；无论是否安装都会打印该 block。

用法:
  python kb_register.py --vault VAULT [--install-hook]
                        [--registry ~/.knowledge-bases/registry.json]
                        [--hook-file ~/.claude/CLAUDE.md]
"""
import os
import sys
import json
import argparse
from datetime import datetime, timezone

BEGIN = "<!-- KB-HUB:BEGIN auto-managed by local-memory-wiki -->"
END = "<!-- KB-HUB:END -->"


def _abs(p):
    return os.path.abspath(os.path.expanduser(p)).replace("\\", "/")


def load_registry(path):
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"version": "1.0", "knowledge_bases": []}


def build_entry(vault):
    kb = json.load(open(os.path.join(vault, "kb.json"), encoding="utf-8"))
    root = _abs(vault)
    return {
        "id": kb["id"],
        "name": kb["name"],
        "scope": kb.get("scope", ""),
        "use_when": kb.get("use_when", []),
        "root": root,
        "query_cli": 'python "%s/kb_query.py" "{question}" --json' % root,
        "query_levels": "short|detailed|full（四档下钻：主题摘要→逐文档详细摘要→原文）",
        "mcp_cmd": 'python "%s/kb_mcp_server.py"' % root,
        "stats": kb.get("stats", {}),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def render_block(reg):
    L = [
        BEGIN,
        "## 本机知识库（Knowledge Bases）",
        "已构建的本地知识库登记于 `~/.knowledge-bases/registry.json`。当用户问题落入某 KB 的 use_when / scope 时：",
        "- 若客户端有 MCP 工具 `kb_search` / `list_knowledge_bases`（kb-hub），**优先用 MCP 工具**；",
        "- 否则跑该 KB 的 `query_cli`（见下，四档下钻：L1 主题摘要 → `--level detailed` 逐文档详细摘要 → L0 原文）。",
        "据返回的 summaries/documents 路径读原文、**引用路径作答**；库内证据不足再用通用知识/联网。",
        "",
        "当前已登记：",
    ]
    for kb in reg.get("knowledge_bases", []):
        L.append("- **%s** — %s" % (kb["id"], kb.get("scope", kb.get("name", ""))))
        L.append("  - 触发词: %s" % "、".join(kb.get("use_when", [])[:8]))
        L.append("  - CLI: `%s`" % kb.get("query_cli", 'python "%s/kb_query.py" "{question}" --json' % kb.get("root", "")))
    L.append(END)
    return "\n".join(L)


def install_hook(hook_file, block):
    hook_file = os.path.expanduser(hook_file)
    os.makedirs(os.path.dirname(hook_file), exist_ok=True)
    old = open(hook_file, encoding="utf-8").read() if os.path.exists(hook_file) else ""
    if BEGIN in old and END in old:
        pre = old[:old.index(BEGIN)]
        post = old[old.index(END) + len(END):]
        new = pre + block + post
    else:
        sep = "" if old.endswith("\n\n") or old == "" else ("\n" if old.endswith("\n") else "\n\n")
        new = old + sep + block + "\n"
    open(hook_file, "w", encoding="utf-8").write(new)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True)
    ap.add_argument("--registry", default="~/.knowledge-bases/registry.json")
    ap.add_argument("--hook-file", default="~/.claude/CLAUDE.md")
    ap.add_argument("--install-hook", action="store_true",
                    help="把 KB-HUB block 写入全局指令文件（默认不写，仅打印）")
    a = ap.parse_args()

    reg_path = os.path.expanduser(a.registry)
    os.makedirs(os.path.dirname(reg_path), exist_ok=True)
    reg = load_registry(reg_path)
    entry = build_entry(os.path.abspath(a.vault))
    reg["knowledge_bases"] = [k for k in reg.get("knowledge_bases", []) if k.get("id") != entry["id"]]
    reg["knowledge_bases"].append(entry)
    reg["knowledge_bases"].sort(key=lambda k: k.get("id", ""))
    json.dump(reg, open(reg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("已登记到全局 registry: %s（共 %d 个 KB）" % (reg_path, len(reg["knowledge_bases"])))

    block = render_block(reg)
    if a.install_hook:
        install_hook(a.hook_file, block)
        print("已安装全局 hook 到: %s" % os.path.expanduser(a.hook_file))
    else:
        print("\n--- 全局 hook block（未安装；可手动粘贴到 %s，或加 --install-hook 自动安装）---\n"
              % a.hook_file)
        print(block)


if __name__ == "__main__":
    main()
