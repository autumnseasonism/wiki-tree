#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_deps.py — 报告 wiki-tree 的可选依赖状态（纯标准库，自身零依赖）。

核心流程——扫描 / Markdown·Text·JSON·CSV 转换 / kb_query 查询 / kb_ingest 路由 /
中心度 / 装配 / 本测试套件——全用 Python 标准库，0 安装即可用。下列依赖为可选，
各启用一项功能；本脚本只**报告**在/缺并给出安装命令，**不安装**任何东西。

用法: python scripts/check_deps.py   （退出码恒为 0；纯信息性）
"""
import importlib.util
import sys

# (pip 包名, 导入名, 启用的功能, 安装命令)
OPTIONAL = [
    ("python-docx", "docx", ".docx (Word) 文档转换", "pip install python-docx"),
    ("PyMuPDF", "fitz", ".pdf 文档转换", "pip install PyMuPDF"),
    ("mcp[cli]", "mcp",
     "MCP 服务器 kb_hub_server / kb_mcp_server（CLI 查询 kb_query.py 不需要）",
     'pip install "mcp[cli]"'),
]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    print("wiki-tree 依赖自检 — 核心为纯标准库，0 安装即可用")
    print("Python %s @ %s\n" % (sys.version.split()[0], sys.executable))
    missing = []
    for pkg, mod, feature, cmd in OPTIONAL:
        present = importlib.util.find_spec(mod) is not None
        if present:
            print("  [OK]      %-12s 已安装" % pkg)
        else:
            print("  [MISSING] %-12s 未安装 → %s" % (pkg, feature))
            missing.append(cmd)
    if missing:
        print("\n缺以下可选依赖（用到对应功能才需要）：")
        for cmd in missing:
            print("  " + cmd)
        print("\n或一键装全：pip install -r requirements.txt")
    else:
        print("\n全部可选依赖已就绪 — 全功能可用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
