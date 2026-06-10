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
import shutil
import argparse
from datetime import datetime, timezone

BEGIN = "<!-- KB-HUB:BEGIN auto-managed by wiki-tree -->"
END = "<!-- KB-HUB:END -->"
# 幂等替换只认稳定的起始标签（描述文字可随技能改名而变，匹配不受影响 → 永不产生重复块）
_BEGIN_TAG = "<!-- KB-HUB:BEGIN"


def _abs(p):
    return os.path.abspath(os.path.expanduser(p)).replace("\\", "/")


def load_registry(path):
    # registry 是全机所有 KB 的唯一登记处：已存在但读不出来时绝不能静默重置为空表再覆盖
    # （那会一次清空全部登记），必须中止交人工处置；文件不存在才是正常的首次初始化。
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except json.JSONDecodeError as e:
            print("错误：registry 已存在但不是合法 JSON：%s\n  解析错误：%s\n"
                  "  请手工修复（或确认无需保留后删除该文件）再重跑，本次不做任何写入。"
                  % (path, e), file=sys.stderr)
            sys.exit(1)
        except OSError as e:
            print("错误：registry 存在但无法读取：%s（%s）。本次不做任何写入。"
                  % (path, e), file=sys.stderr)
            sys.exit(1)
    return {"version": "1.0", "knowledge_bases": []}


def _atomic_write(path, text):
    """临时文件 + os.replace：写一半被打断也不会留下半截文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


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
    if _BEGIN_TAG in old:
        bi = old.index(_BEGIN_TAG)
        ei = old.find(END, bi)
        # 孤立 BEGIN 两种排列都必须拒绝改写：
        #   ① 其后无任何 END（ei<0）；
        #   ② BEGIN 与找到的 END 之间又出现一个 BEGIN——说明该 END 配对的是后面那个
        #      完整块，当前 BEGIN 实为孤立（旧版在孤立 BEGIN 文件上追加过一次就会留下
        #      这种「孤立BEGIN…用户内容…BEGIN…END」存量），整段替换会吞掉中间的用户内容。
        if ei < 0 or old.find(_BEGIN_TAG, bi + len(_BEGIN_TAG), ei) >= 0:
            print("错误：%s 中存在孤立的 KB-HUB BEGIN 标记（其后找不到配对的 %s）。\n"
                  "  为避免后续替换吞掉标记之间的内容，本次未改写该文件；"
                  "请手动删除孤立标记（或补上配对 END）后重跑 --install-hook。"
                  % (hook_file, END), file=sys.stderr)
            sys.exit(1)
        new = old[:bi] + block + old[ei + len(END):]
    else:
        sep = "" if old.endswith("\n\n") or old == "" else ("\n" if old.endswith("\n") else "\n\n")
        new = old + sep + block + "\n"
    if new == old:
        return  # 内容未变（幂等重跑）：不写盘也不产生备份
    # 改写的是用户的全局指令文件：先留备份，再原子替换
    if os.path.exists(hook_file):
        base = hook_file + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        # 秒级时间戳同一秒内会碰撞（如脚本循环对多个 vault 连跑 --install-hook）：
        # 已存在则追加 -2/-3 序号，绝不覆盖更早的快照——最早一份恰是最有价值的。
        bak, n = base, 2
        while os.path.exists(bak):
            bak = "%s-%d" % (base, n)
            n += 1
        shutil.copy2(hook_file, bak)
    _atomic_write(hook_file, new)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
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
    _atomic_write(reg_path, json.dumps(reg, ensure_ascii=False, indent=2))
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
