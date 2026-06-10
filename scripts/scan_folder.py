#!/usr/bin/env python3
"""
scan_folder.py — 扫描本地文件夹，分类统计文档
用法: python scan_folder.py /path/to/folder [--output /path/to/output]
"""

import os
import sys
import json
import argparse
import fnmatch
from pathlib import Path
from datetime import datetime, timezone

SUPPORTED_EXTENSIONS = {
    # Word
    '.docx': 'word',
    # PDF
    '.pdf': 'pdf',
    # Markdown
    '.md': 'markdown',
    '.markdown': 'markdown',
    '.mdx': 'markdown',
    # JSON
    '.json': 'json',
    # Text
    '.txt': 'text',
    '.text': 'text',
    '.log': 'text',
    # CSV（表格感知，转 Markdown 表格）
    '.csv': 'csv',
}

# 内置剪枝目录集：依赖/构建产物目录通常不含源文档，默认不深入。
# 非隐藏名（build/dist/target 等）可能恰好被用户用来放文档，
# 命中时记录到报告 builtin_excluded_dirs，保证「被吞」可被发现。
BUILTIN_PRUNE_DIRS = {
    'node_modules', '__pycache__', '.git', 'venv', '.venv',
    'target', 'build', 'dist', '.obsidian',
}


def load_manifest(vault):
    """读取增量 manifest 的 processed 段；vault 为空或文件不存在/损坏时返回 {}。"""
    if not vault:
        return {}
    mpath = Path(vault) / ".wiki-tree" / "manifest.json"
    if not mpath.exists():
        return {}
    try:
        with open(mpath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        print(f"警告: manifest 损坏或不可读，本次按全新处理（不跳过任何文件）: {mpath}", file=sys.stderr)
        return {}
    if isinstance(data, dict) and isinstance(data.get("processed"), dict):
        return data["processed"]
    return {}


def load_ignore_file(target: Path):
    """读取目标根目录下的 .mwignore（每行一个 glob，# 注释，空行忽略）。"""
    patterns = []
    ig = target / ".mwignore"
    if ig.exists():
        try:
            for line in ig.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
        except (OSError, UnicodeDecodeError):
            print(f"警告: .mwignore 读取失败，已忽略: {ig}", file=sys.stderr)
    return patterns


def _is_excluded(rel_posix: str, name: str, patterns) -> bool:
    """rel_posix=相对 target 的 posix 路径；name=basename。任一 glob 命中即排除。
    支持目录式 'foo/'（匹配该目录本身及其下全部）与普通 glob（*.md、sub/*.csv 等）。"""
    for pat in patterns:
        p = pat.rstrip("/")
        if (fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(rel_posix, p)
                or fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(name, p)
                or fnmatch.fnmatch(rel_posix, p + "/*")):
            return True
    return False


def scan_folder(target_path, vault=None, exclude=None):
    """递归扫描文件夹，返回分类统计。

    vault 非空时进入增量模式：读取 vault/.wiki-tree/manifest.json，为每个文件
    标记 new/modified/done，处理计划只覆盖待处理（new+modified）集，已完成的跳过。
    被 >100 推迟的文件不会被写入 manifest，因此下一轮仍是 new，会被自动补上。

    exclude：glob 列表，命中的文件/目录跳过；另会读取 target/.mwignore 合并进排除集。
    """
    target = Path(target_path).resolve()
    if not target.exists():
        return {"error": f"路径不存在: {target_path}"}
    if not target.is_dir():
        return {"error": f"不是目录: {target_path}"}

    patterns = list(exclude or []) + load_ignore_file(target)
    manifest = load_manifest(vault)
    # 归一化路径（大小写/分隔符）以可靠匹配 manifest 键与扫描到的文件
    manifest_norm = {os.path.normcase(k): v for k, v in manifest.items()}
    # 归一化键 → manifest 原始键（孤儿检测要报告原始路径）
    manifest_keys = {os.path.normcase(k): k for k in manifest}

    # vault 严格位于扫描目标之内时必须剪掉：否则上一轮产物
    # （documents/entities/summaries 等非隐藏 .md）会被当作源文件回灌，逐轮膨胀
    vault_inside = None
    if vault:
        v = Path(vault).resolve()
        if v != target:
            try:
                v.relative_to(target)
                vault_inside = v
            except ValueError:
                pass

    results = {
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "target_path": str(target),
        "total_files": 0,
        "supported_files": 0,
        "unsupported_files": 0,
        "categories": {},
        "files": [],
        "unsupported_extensions": set(),
        "excluded_count": 0,
        "exclude_patterns": patterns,
        "vault_excluded": vault_inside is not None,
        "builtin_excluded_dirs": [],
    }

    for category in set(SUPPORTED_EXTENSIONS.values()):
        results["categories"][category] = {
            "count": 0,
            "total_size_bytes": 0,
            "files": []
        }

    scanned_norm = set()

    for root, dirs, files in os.walk(target):
        rootp = Path(root)
        # vault 位于扫描目标之内：剪掉 vault 目录本身（resolve 比较，吸收符号链接/相对路径差异）
        if vault_inside is not None:
            dirs[:] = [d for d in dirs if (rootp / d).resolve() != vault_inside]
        # 跳过隐藏目录和常见的非文档目录；命中内置集的非隐藏目录记录相对路径
        kept = []
        for d in dirs:
            if d.startswith('.'):
                continue
            if d in BUILTIN_PRUNE_DIRS:
                results["builtin_excluded_dirs"].append(
                    (rootp / d).relative_to(target).as_posix())
                continue
            kept.append(d)
        dirs[:] = kept
        # 用户 exclude / .mwignore：命中的目录剪枝（不再深入遍历）
        if patterns:
            dirs[:] = [d for d in dirs if not _is_excluded(
                (rootp / d).relative_to(target).as_posix(), d, patterns)]

        for fname in files:
            fpath = rootp / fname
            # 用户 exclude / .mwignore：命中的文件直接跳过（不计入任何计数）
            if patterns and _is_excluded(fpath.relative_to(target).as_posix(), fname, patterns):
                results["excluded_count"] += 1
                continue
            results["total_files"] += 1
            ext = fpath.suffix.lower()

            if ext in SUPPORTED_EXTENSIONS:
                category = SUPPORTED_EXTENSIONS[ext]
                # 单次 stat 且容错：避免双重 stat，且存在但 stat 失败(权限等)不致整体崩溃
                try:
                    st = fpath.stat()
                    size = st.st_size
                    modified_at = datetime.fromtimestamp(
                        st.st_mtime, tz=timezone.utc).isoformat()
                except OSError:
                    size = 0
                    modified_at = None

                file_info = {
                    "path": str(fpath),
                    "name": fname,
                    "extension": ext,
                    "category": category,
                    "size_bytes": size,
                    "modified_at": modified_at,
                }

                # 增量状态：与 manifest 比对（manifest 只记录已抽取完成 = done 的源）
                norm_path = os.path.normcase(str(fpath))
                scanned_norm.add(norm_path)
                rec = manifest_norm.get(norm_path)
                if rec is None:
                    file_info["status"] = "new"
                elif rec.get("mtime") != file_info["modified_at"]:
                    file_info["status"] = "modified"
                else:
                    file_info["status"] = "done"

                results["files"].append(file_info)
                results["supported_files"] += 1
                results["categories"][category]["count"] += 1
                results["categories"][category]["total_size_bytes"] += size
                results["categories"][category]["files"].append(file_info)
            else:
                results["unsupported_files"] += 1
                if ext:
                    results["unsupported_extensions"].add(ext)

    results["unsupported_extensions"] = sorted(list(results["unsupported_extensions"]))
    results["builtin_excluded_dirs"].sort()

    if results["vault_excluded"]:
        if vault_inside.is_dir():
            print(f"提示: Vault 位于扫描目标之内，已从扫描范围排除: {vault_inside}", file=sys.stderr)
        else:
            print(f"提示: 检测到 Vault 计划位于扫描目标之内（目录尚不存在），"
                  f"后续轮次将自动剪枝；仍建议把输出目录移出扫描目标: {vault_inside}", file=sys.stderr)
    if results["builtin_excluded_dirs"]:
        shown = results["builtin_excluded_dirs"][:10]
        more = len(results["builtin_excluded_dirs"]) - len(shown)
        tail = f" 等（其余 {more} 个见报告 builtin_excluded_dirs）" if more else ""
        print(f"提示: 已跳过 {len(results['builtin_excluded_dirs'])} 个内置排除目录"
              f"（依赖/构建产物目录默认不扫描）: {', '.join(shown)}{tail}", file=sys.stderr)

    # 增量模式：拆出待处理集（new + modified），处理计划只覆盖它，已完成的跳过
    if vault:
        pending = [f for f in results["files"] if f.get("status") != "done"]
        pending.sort(key=lambda f: f.get("modified_at", "") or "", reverse=True)
        results["incremental"] = True
        results["total_supported"] = results["supported_files"]
        results["done_count"] = results["supported_files"] - len(pending)
        results["pending_count"] = len(pending)
        results["files"] = pending  # 下游（转换/抽取）只面向待处理集
        plan_total = len(pending)
        # 孤儿源：manifest 已登记、本轮未扫到且盘上已不存在（仍存在的只是被
        # exclude/缩小扫描范围，不算）。只可见化不删除，清理决策留给 agent/用户。
        orphaned = sorted(
            orig for norm, orig in manifest_keys.items()
            if norm not in scanned_norm and not os.path.isfile(orig)
        )
        results["orphaned"] = orphaned
        results["orphaned_count"] = len(orphaned)
        if orphaned:
            print(f"提示: manifest 中 {len(orphaned)} 个已登记源文件已不存在（孤儿），"
                  f"其产物仍留在 vault 中，见报告 orphaned 字段", file=sys.stderr)
    else:
        results["incremental"] = False
        plan_total = results["supported_files"]

    # 生成处理计划（基于待处理总数 plan_total）
    total = plan_total
    if total <= 20:
        results["plan"] = {
            "strategy": "single_batch",
            "description": "一次性处理全部文档",
            "batches": 1,
            "batch_size": total,
        }
    elif total <= 100:
        batch_size = 20
        batches = (total + batch_size - 1) // batch_size
        results["plan"] = {
            "strategy": "multi_batch",
            "description": f"分 {batches} 批处理，每批 {batch_size} 个",
            "batches": batches,
            "batch_size": batch_size,
        }
    else:
        results["plan"] = {
            "strategy": "priority_batch",
            "description": f"优先处理最近修改的 100 个，其余 {total - 100} 个标记为待处理",
            "batches": 5,
            "batch_size": 20,
            "deferred_count": total - 100,
        }
        # 按修改时间排序、最新在前，并**截断到 100**（强制执行上限）：本轮只处理这 100 个；
        # 其余 deferred_count 个不进入 files → 不会被转换/抽取/登记 → 下一轮 --vault 增量扫描时
        # 仍是 new，会被自动补上。不再依赖调用方传 --limit。
        results["files"].sort(
            key=lambda f: f.get("modified_at", "") or "",
            reverse=True
        )
        results["files"] = results["files"][:100]

    return results


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="扫描本地文件夹，分类统计文档")
    parser.add_argument("path", help="目标文件夹路径")
    parser.add_argument("--output", "-o", help="输出 JSON 报告路径（默认 stdout）")
    parser.add_argument("--vault", help="已有 Vault 路径；提供则进入增量模式"
                                        "（读取 .wiki-tree/manifest.json，跳过已处理文档）")
    parser.add_argument("--exclude", action="append", default=[],
                        help="排除匹配该 glob 的文件/目录（可多次指定）；"
                             "目标根目录的 .mwignore 也会被读取并合并")
    args = parser.parse_args()

    results = scan_folder(args.path, vault=args.vault, exclude=args.exclude)

    if "error" in results:
        print(f"错误: {results['error']}", file=sys.stderr)
        sys.exit(1)

    # 输出 JSON
    output = json.dumps(results, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"扫描报告已写入: {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
