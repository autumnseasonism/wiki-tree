#!/usr/bin/env python3
"""
convert_documents.py — 将多种格式文档统一转换为 Markdown
用法: python convert_documents.py --scan-report /path/to/scan.json --output /path/to/output

依赖: pip install python-docx PyMuPDF
"""

import os
import sys
import json
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, timezone


def convert_word(file_path: str) -> str:
    """Word (.docx) → Markdown"""
    try:
        from docx import Document
    except ImportError:
        raise RuntimeError("缺少依赖 python-docx，无法转换 .docx：pip install python-docx")

    doc = Document(file_path)
    lines = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            lines.append("")
            continue

        style_name = para.style.name.lower() if para.style else ""

        if "heading 1" in style_name or "标题 1" in style_name:
            lines.append(f"# {text}")
        elif "heading 2" in style_name or "标题 2" in style_name:
            lines.append(f"## {text}")
        elif "heading 3" in style_name or "标题 3" in style_name:
            lines.append(f"### {text}")
        elif "heading 4" in style_name or "标题 4" in style_name:
            lines.append(f"#### {text}")
        elif "list" in style_name:
            lines.append(f"- {text}")
        else:
            lines.append(text)

    # 表格：追加在段落之后（防御性——失败不影响段落提取）
    try:
        for ti, table in enumerate(doc.tables):
            trows = [[(c.text or "").strip().replace("|", "\\|") for c in row.cells]
                     for row in table.rows]
            trows = [r for r in trows if any(r)]
            if not trows:
                continue
            w = max(len(r) for r in trows)
            pad = lambda r: r + [""] * (w - len(r))
            lines.append("")
            lines.append(f"<!-- 表格 {ti + 1} -->")
            lines.append("| " + " | ".join(pad(trows[0])) + " |")
            lines.append("| " + " | ".join(["---"] * w) + " |")
            for r in trows[1:]:
                lines.append("| " + " | ".join(pad(r)) + " |")
    except Exception:
        pass

    return "\n".join(lines)


def convert_pdf(file_path: str) -> str:
    """PDF → Markdown"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise RuntimeError("缺少依赖 PyMuPDF，无法转换 .pdf：pip install PyMuPDF")

    doc = fitz.open(file_path)
    pages = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text")
        if text.strip():
            pages.append(f"<!-- 第 {page_num + 1} 页 -->\n{text.strip()}")

    doc.close()
    return "\n\n".join(pages)


def convert_json(file_path: str) -> str:
    """JSON → Markdown"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RuntimeError(f"JSON 解析失败: {e}") from e

    return json_to_markdown(data, level=2)


def json_to_markdown(obj, level=2, prefix="") -> str:
    """递归将 JSON 转为 Markdown"""
    lines = []
    heading = "#" * level

    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{heading} {prefix}{key}")
                lines.append(json_to_markdown(value, level + 1, f"{prefix}{key}."))
            else:
                lines.append(f"**{key}**: {value}")
        lines.append("")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, (dict, list)):
                lines.append(f"{heading} [{i}]")
                lines.append(json_to_markdown(item, level + 1, f"{prefix}[{i}]."))
            else:
                lines.append(f"- {item}")
        lines.append("")
    else:
        lines.append(str(obj))

    return "\n".join(lines)


def convert_text(file_path: str) -> str:
    """纯文本 → Markdown"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        try:
            with open(file_path, "r", encoding="gbk") as f:
                content = f.read()
        except UnicodeDecodeError:
            raise RuntimeError("无法解码文件（尝试了 UTF-8 和 GBK）")

    # 简单的段落检测：连续非空行合并为段落
    paragraphs = []
    current = []
    for line in content.split("\n"):
        if line.strip():
            current.append(line.strip())
        else:
            if current:
                paragraphs.append(" ".join(current))
                current = []
    if current:
        paragraphs.append(" ".join(current))

    return "\n\n".join(paragraphs)


CSV_ROW_CAP = 1000  # CSV 数据行上限；超出则截断并注明（避免超大表撑爆下游抽取）


def _md_cell(value) -> str:
    """转义单元格内容，使其安全嵌入 Markdown 表格。"""
    return (str(value).replace("\\", "\\\\").replace("|", "\\|")
            .replace("\r", " ").replace("\n", " ").strip())


def convert_csv(file_path: str) -> str:
    """CSV → Markdown 表格（首行作表头；超大表截断并注明）。"""
    import csv
    rows = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(file_path, "r", encoding=enc, newline="") as f:
                rows = list(csv.reader(f))
            break
        except UnicodeDecodeError:
            continue
    if rows is None:
        raise RuntimeError("无法解码 CSV（尝试了 UTF-8 和 GBK）")
    rows = [r for r in rows if any((c or "").strip() for c in r)]  # 丢弃全空行
    if not rows:
        return ""
    total_data = len(rows) - 1
    note = ""
    if total_data > CSV_ROW_CAP:
        rows = [rows[0]] + rows[1:CSV_ROW_CAP + 1]
        note = f"\n\n<!-- 表格过大：共 {total_data} 行数据，仅转换前 {CSV_ROW_CAP} 行 -->"
    width = max(len(r) for r in rows)
    pad = lambda r: list(r) + [""] * (width - len(r))
    lines = ["| " + " | ".join(_md_cell(c) for c in pad(rows[0])) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    for r in rows[1:]:
        lines.append("| " + " | ".join(_md_cell(c) for c in pad(r)) + " |")
    return "\n".join(lines) + note


CONVERTERS = {
    "word": convert_word,
    "pdf": convert_pdf,
    "markdown": None,  # 直接读取
    "json": convert_json,
    "text": convert_text,
    "csv": convert_csv,
}


def _existing_is_same_source(md_path: Path, source_path: str) -> bool:
    """同名 .md 是否就是本源文件（front-matter 里的 source_path 一致）。

    增量重跑/上次中断遗留时用于判断：是 → 覆写既有文件，而不是再生成 -1 重复副本。
    不同源文件恰好同名时（如两个目录各有 report.docx）仍走 -1 区分，互不影响。
    """
    try:
        head = md_path.read_text(encoding="utf-8")[:800]
    except (OSError, UnicodeDecodeError):
        return False
    return f"source_path: {source_path}" in head


def process_file(file_info: dict, output_dir: Path, content_hashes: dict = None) -> dict:
    """处理单个文件，返回结果信息。

    content_hashes 非 None 时启用文档级内容去重：内容完全相同的文件只保留第一份。
    """
    source_path = file_info["path"]
    category = file_info["category"]
    name = Path(source_path).stem

    # 输出文件名：去重 + 清理
    safe_name = name.replace(" ", "-").replace("/", "-").replace("\\", "-")
    output_path = output_dir / f"{safe_name}.md"

    # 避免重名；但若同名文件正是本源文件（增量重跑/上次中断遗留）→ 覆写，不再生成 -1 副本
    counter = 1
    while output_path.exists():
        if _existing_is_same_source(output_path, source_path):
            break
        output_path = output_dir / f"{safe_name}-{counter}.md"
        counter += 1

    try:
        if category == "markdown":
            try:
                content = Path(source_path).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = Path(source_path).read_text(encoding="gbk")
        else:
            converter = CONVERTERS.get(category)
            if not converter:
                return {
                    "status": "skipped",
                    "reason": f"不支持的类型: {category}",
                    "source": source_path,
                }
            content = converter(source_path)

        # 文档级内容去重：内容完全相同的文件只转换第一份（空内容不参与去重）
        if content_hashes is not None and content.strip():
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if digest in content_hashes:
                return {
                    "status": "skipped",
                    "reason": f"内容重复，等同于 {content_hashes[digest]}",
                    "source": source_path,
                    "duplicate_of": content_hashes[digest],
                }
            content_hashes[digest] = str(output_path)

        # 添加 front-matter
        now = datetime.now(timezone.utc).isoformat()
        fm = f"""---
source_type: local_file
source_path: {source_path}
source_format: {file_info.get('extension', 'unknown').lstrip('.')}
converted_at: {now}
file_size_bytes: {file_info.get('size_bytes', 0)}
---

"""
        output_path.write_text(fm + content, encoding="utf-8")

        return {
            "status": "success",
            "source": source_path,
            "output": str(output_path),
            "size": len(content),
        }

    except Exception as e:
        return {
            "status": "error",
            "reason": str(e),
            "source": source_path,
        }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="批量转换文档为 Markdown")
    parser.add_argument("--scan-report", required=True, help="scan_folder.py 生成的 JSON 报告")
    parser.add_argument("--output", "-o", required=True, help="输出目录")
    parser.add_argument("--limit", type=int, default=0, help="最多处理文件数（0=全部）")
    parser.add_argument("--no-dedup-content", action="store_true",
                        help="关闭文档级内容去重（默认开启：内容完全相同的文件只转换第一份）")
    args = parser.parse_args()

    # 读取扫描报告
    with open(args.scan_report, "r", encoding="utf-8") as f:
        report = json.load(f)

    files = report.get("files", [])
    if args.limit > 0:
        files = files[:args.limit]

    output_dir = Path(args.output) / "documents"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 文档级内容去重（默认开启；--no-dedup-content 关闭）
    content_hashes = None if args.no_dedup_content else {}

    results = {
        "total": len(files),
        "success": 0,
        "error": 0,
        "skipped": 0,
        "details": [],
    }

    for i, file_info in enumerate(files):
        result = process_file(file_info, output_dir, content_hashes)
        results["details"].append(result)

        if result["status"] == "success":
            results["success"] += 1
        elif result["status"] == "error":
            results["error"] += 1
        else:
            results["skipped"] += 1

        # 进度输出
        if (i + 1) % 10 == 0 or i + 1 == len(files):
            print(f"进度: {i + 1}/{len(files)} "
                  f"(成功: {results['success']}, "
                  f"错误: {results['error']}, "
                  f"跳过: {results['skipped']})")

    # 写入转换报告
    report_path = Path(args.output) / "_conversion_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n转换完成！报告: {report_path}")
    print(f"  成功: {results['success']}")
    print(f"  错误: {results['error']}")
    print(f"  跳过: {results['skipped']}")


if __name__ == "__main__":
    main()
