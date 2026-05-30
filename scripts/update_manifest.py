#!/usr/bin/env python3
"""
update_manifest.py — 维护增量处理 manifest（"哪些源文件已抽取完成"的唯一真相源）

manifest 路径：<vault>/.memory-wiki/manifest.json
结构：{"version": 1, "updated_at": ISO, "processed": {"<源文件绝对路径>": {...}}}
一个源文件出现在 processed 里 = 已完整处理（已抽取并落盘）。scan_folder.py 据此跳过它。

用法:
  # 仅初始化 .memory-wiki/ 底座（manifest + extracted/ 目录），不标记任何文件
  python update_manifest.py --vault /path/to/vault

  # 标记单个源文件为已完成（每篇文档抽取落盘后调用）
  python update_manifest.py --vault VAULT --mark /abs/源文件.docx \
         --doc-md documents/源文件.md [--doc-id 源文件] [--mtime ISO]

  # 批量标记：子 agent 处理完一批后一次性提交
  python update_manifest.py --vault VAULT --mark-from /path/batch-result.json

batch-result.json 格式（数组）:
  [{"source_path": "/abs/a.docx", "doc_md": "documents/a.md", "doc_id": "a", "mtime": "ISO(可选)"}, ...]

设计要点：
- 原子写（临时文件 + replace），中途崩溃不会损坏 manifest。
- mtime 默认自动 stat 源文件，与 scan_folder.py 记录的 modified_at 同源（ISO/UTC），
  这样下一轮扫描能正确判定 done（mtime 一致）/ modified（源被改过，需重抽取）。
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone


def _mw_dir(vault: Path) -> Path:
    return vault / ".memory-wiki"


def _manifest_path(vault: Path) -> Path:
    return _mw_dir(vault) / "manifest.json"


def ensure_substrate(vault: Path) -> Path:
    """确保 .memory-wiki/ 与 extracted/ 存在，manifest.json 存在。"""
    (_mw_dir(vault) / "extracted").mkdir(parents=True, exist_ok=True)
    mpath = _manifest_path(vault)
    if not mpath.exists():
        _write_manifest(vault, {"version": 1, "processed": {}})
    return mpath


def load_manifest(vault: Path) -> dict:
    mpath = _manifest_path(vault)
    if not mpath.exists():
        return {"version": 1, "processed": {}}
    try:
        with open(mpath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # 非破坏性：把损坏的 manifest 备份到带时间戳的副本（连续损坏不互相覆盖），再重置
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        backup = mpath.with_name(f"manifest.corrupt-{ts}.json")
        try:
            import shutil
            shutil.copy2(mpath, backup)
            print(f"警告: manifest 损坏，已备份到 {backup} 后重置（done 状态可从该备份恢复）", file=sys.stderr)
        except OSError:
            print(f"警告: manifest 损坏且备份失败，将重置为空（已处理状态会丢失）: {mpath}", file=sys.stderr)
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    if not isinstance(data.get("processed"), dict):
        data["processed"] = {}
    return data


def _write_manifest(vault: Path, data: dict) -> None:
    """原子写：先写临时文件再替换，避免中途崩溃损坏 manifest。"""
    mw = _mw_dir(vault)
    mw.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    mpath = _manifest_path(vault)
    tmp = mpath.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(mpath)


def _source_mtime(source_path: str):
    p = Path(source_path)
    if p.exists():
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
    return None


def mark_one(data: dict, source_path: str, doc_md: str = None,
             doc_id: str = None, mtime: str = None) -> None:
    if mtime is None:
        mtime = _source_mtime(source_path)
    if doc_id is None and doc_md:
        doc_id = Path(doc_md).stem
    # 归一化为解析后的绝对路径，与 scan_folder.py 扫描到的路径对齐（避免大小写/相对路径导致匹配不上）
    key = str(Path(source_path).resolve())
    data["processed"][key] = {
        "mtime": mtime,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "doc_md": doc_md,
        "doc_id": doc_id,
        "status": "done",
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="维护增量处理 manifest")
    parser.add_argument("--vault", required=True, help="Vault 根目录")
    parser.add_argument("--mark", help="标记为已完成的源文件路径")
    parser.add_argument("--doc-md", help="该源文件对应的 documents/*.md（建议相对 vault 的路径）")
    parser.add_argument("--doc-id", help="extracted/<doc-id>.json 用的 id（默认取 doc-md 的文件名）")
    parser.add_argument("--mtime", help="源文件 mtime（ISO）；默认自动 stat 源文件")
    parser.add_argument("--mark-from", help="批量标记：指向一个 JSON 数组文件")
    args = parser.parse_args()

    vault = Path(args.vault)
    ensure_substrate(vault)
    data = load_manifest(vault)

    marked = 0
    if args.mark_from:
        with open(args.mark_from, "r", encoding="utf-8") as f:
            items = json.load(f)
        for it in items:
            mark_one(data, it["source_path"], it.get("doc_md"),
                     it.get("doc_id"), it.get("mtime"))
            marked += 1
    if args.mark:
        mark_one(data, args.mark, args.doc_md, args.doc_id, args.mtime)
        marked += 1

    _write_manifest(vault, data)

    print(json.dumps({
        "vault": str(vault),
        "marked": marked,
        "total_processed": len(data["processed"]),
        "manifest": str(_manifest_path(vault)),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
