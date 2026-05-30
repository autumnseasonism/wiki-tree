#!/usr/bin/env python3
"""
local-memory-wiki 回归测试套件。
运行: python tests/test_skill.py   （建议先 set PYTHONUTF8=1 / export PYTHONUTF8=1）
覆盖 6 个脚本的核心行为 + 历次修复：
  增量(manifest)/并行登记/内容去重/CSV 表格/幻觉闸门/确定性中心度,
  以及 M1(转换失败→error)、M2(>100 截断)、L2(md GBK 回退)、L6(符号实体词边界)、L7(图谱配色)、F3(UTF-8 stdout)。
全程用临时目录，不触碰真实数据；任一断言失败则退出码 1。
"""
import os, re, sys, json, subprocess, tempfile, shutil
from pathlib import Path

SC = Path(__file__).resolve().parent.parent / "scripts"
PY = sys.executable
fails = []

def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

def run(*a, env=None):
    r = subprocess.run([PY, *map(str, a)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    if r.returncode != 0 and env is None:
        print("    ! cmd nonzero:", [str(x) for x in a], "\n   ", (r.stdout or "")[:200], (r.stderr or "")[:200])
    return r

def report(files):
    return {"files": [{"path": p, "name": Path(p).name, "extension": Path(p).suffix,
                       "category": c, "size_bytes": 0} for p, c in files]}

def jload(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def main():
    W = Path(tempfile.mkdtemp(prefix="lmw_test_"))
    try:
        # ---------- 1. scan: 分类 + 计划档位 + M2 截断 ----------
        print("[1] scan_folder")
        s1 = W / "s1"; s1.mkdir()
        (s1 / "a.md").write_text("# A", encoding="utf-8")
        (s1 / "b.txt").write_text("文本", encoding="utf-8")
        (s1 / "c.json").write_text('{"k":"v"}', encoding="utf-8")
        (s1 / "d.csv").write_text("h1,h2\n1,2", encoding="utf-8")
        (s1 / "e.png").write_text("x", encoding="utf-8")
        run(SC / "scan_folder.py", s1, "-o", W / "s1.json")
        d = jload(W / "s1.json")
        check("supported=4", d["supported_files"] == 4, d.get("supported_files"))
        check("unsupported=1", d["unsupported_files"] == 1)
        check("csv 归类 csv", d["categories"]["csv"]["count"] == 1)
        check("plan single_batch(<=20)", d["plan"]["strategy"] == "single_batch")

        s2 = W / "s2"; s2.mkdir()
        for i in range(25):
            (s2 / f"f{i}.md").write_text(f"d{i}", encoding="utf-8")
        run(SC / "scan_folder.py", s2, "-o", W / "s2.json")
        check("plan multi_batch(21-100)", jload(W / "s2.json")["plan"]["strategy"] == "multi_batch")

        s3 = W / "s3"; s3.mkdir()
        for i in range(105):
            (s3 / f"f{i}.md").write_text(f"d{i}", encoding="utf-8")
        run(SC / "scan_folder.py", s3, "-o", W / "s3.json")
        p3 = jload(W / "s3.json")
        check("plan priority(>100)", p3["plan"]["strategy"] == "priority_batch")
        check("deferred_count=5", p3["plan"].get("deferred_count") == 5, p3["plan"].get("deferred_count"))
        check("M2: files 截断到 100", len(p3["files"]) == 100, len(p3["files"]))

        # ---------- 2. generate: 目录 + 底座 + create-if-missing + L7 配色 ----------
        print("[2] generate_wiki_structure")
        v = W / "vault"; run(SC / "generate_wiki_structure.py", "--output", v)
        for dd in ("documents", "summaries", "entities", "relations", ".obsidian", ".memory-wiki/extracted"):
            check(f"目录 {dd}", (v / dd).is_dir())
        check("空 manifest", jload(v / ".memory-wiki/manifest.json")["processed"] == {})
        idx = v / "_index.md"; idx.write_text(idx.read_text(encoding="utf-8") + "\nKEEP\n", encoding="utf-8")
        run(SC / "generate_wiki_structure.py", "--output", v)
        check("create-if-missing 不覆盖", "KEEP" in idx.read_text(encoding="utf-8"))
        run(SC / "generate_wiki_structure.py", "--output", v, "--force")
        check("--force 覆盖", "KEEP" not in idx.read_text(encoding="utf-8"))
        check("F2: 模板用 [[<kind>- 而非 [[entity-",
              "[[entity-" not in (v / "relations/_knowledge-graph.md").read_text(encoding="utf-8"))
        gj = jload(v / ".obsidian/graph.json")
        qs = {g["query"] for g in gj["color-groups"]}
        check("L7: 图谱含 date/location/event 配色",
              {"tag:#date", "tag:#location", "tag:#event"} <= qs)

        # ---------- 3. convert: 格式 + CSV + 去重 + M1 + L2 ----------
        print("[3] convert_documents")
        cs = W / "cs"; cs.mkdir()
        (cs / "m.md").write_text("# M\n内容", encoding="utf-8")
        (cs / "t.csv").write_text("姓名,备注\n张三,负责 a|b\n", encoding="utf-8")
        (cs / "dupA.txt").write_text("相同内容", encoding="utf-8")
        (cs / "dupB.txt").write_text("相同内容", encoding="utf-8")
        (cs / "gbk.md").write_bytes("# 标题\nGBK中文内容".encode("gbk"))   # L2
        (cs / "fake.docx").write_text("not a real docx", encoding="utf-8")  # M1
        rep = W / "cs.json"
        rep.write_text(json.dumps(report([
            (str(cs / "m.md"), "markdown"), (str(cs / "t.csv"), "csv"),
            (str(cs / "dupA.txt"), "text"), (str(cs / "dupB.txt"), "text"),
            (str(cs / "gbk.md"), "markdown"), (str(cs / "fake.docx"), "word"),
        ]), ensure_ascii=False), encoding="utf-8")
        cv = W / "cv"; run(SC / "convert_documents.py", "--scan-report", rep, "--output", cv)
        cr = jload(cv / "_conversion_report.json")
        det = {Path(x["source"]).name: x for x in cr["details"]}
        check("csv→表格+竖线转义",
              "| 姓名 | 备注 |" in (cv / "documents/t.md").read_text(encoding="utf-8")
              and "a\\|b" in (cv / "documents/t.md").read_text(encoding="utf-8"))
        check("dupB 内容重复被跳过", det["dupB.txt"]["status"] == "skipped")
        check("L2: GBK 编码 .md 转换成功", det["gbk.md"]["status"] == "success"
              and "GBK中文内容" in (cv / "documents/gbk.md").read_text(encoding="utf-8"))
        check("M1: 缺依赖/坏 .docx → error（非 success）", det["fake.docx"]["status"] == "error", det["fake.docx"]["status"])
        check("M1: 失败文档未写出 .md", not (cv / "documents/fake.md").exists())
        # 同源重转不产生 -1
        run(SC / "convert_documents.py", "--scan-report", rep, "--output", cv)
        check("同源重转无 -1 重复", not list((cv / "documents").glob("*-1.md")))

        # ---------- 3b. 真实 .docx：L8 表格提取 + M1 有效文档成功路径 ----------
        print("[3b] convert 真实 docx (L8)")
        try:
            import docx as _docx  # python-docx
            _have_docx = True
        except ImportError:
            _have_docx = False
        if not _have_docx:
            print("  SKIP 真实 docx 测试（python-docx 未安装）")
        else:
            dd = W / "dd"; dd.mkdir()
            docp = dd / "real.docx"
            _d = _docx.Document()
            _d.add_heading("报告标题", level=1)
            _d.add_paragraph("正文段落内容。")
            _t = _d.add_table(rows=2, cols=2)
            _t.rows[0].cells[0].text = "姓名"; _t.rows[0].cells[1].text = "年龄"
            _t.rows[1].cells[0].text = "张三"; _t.rows[1].cells[1].text = "30"
            _d.add_table(rows=2, cols=2)  # 全空表 → 应被跳过且不占序号
            _t3 = _d.add_table(rows=2, cols=2)
            _t3.rows[0].cells[0].text = "项目"; _t3.rows[0].cells[1].text = "值"
            _t3.rows[1].cells[0].text = "备注"; _t3.rows[1].cells[1].text = "x|y"
            _d.save(str(docp))
            repd = W / "dd.json"
            repd.write_text(json.dumps(report([(str(docp), "word")]), ensure_ascii=False), encoding="utf-8")
            dv = W / "dv"; run(SC / "convert_documents.py", "--scan-report", repd, "--output", dv)
            crd = jload(dv / "_conversion_report.json")
            st = crd["details"][0]["status"]
            md = (dv / "documents/real.md").read_text(encoding="utf-8") if (dv / "documents/real.md").exists() else ""
            check("M1: 有效 docx 转换成功", st == "success", st)
            check("docx 段落保留", "正文段落内容" in md)
            check("L8: 表格渲染为 Markdown 表格", "| 姓名 | 年龄 |" in md and "| 张三 | 30 |" in md, md[:300])
            check("L8: 单元格竖线转义", "x\\|y" in md, md[:300])
            check("L8: 空表跳过 + 序号连续", "<!-- 表格 1 -->" in md and "<!-- 表格 2 -->" in md and "<!-- 表格 3 -->" not in md, md[:400])

        # ---------- 4. update_manifest ----------
        print("[4] update_manifest")
        mv = W / "mv"; run(SC / "generate_wiki_structure.py", "--output", mv)
        mf = W / "mf.json"
        mf.write_text(json.dumps([
            {"source_path": str(cs / "m.md"), "doc_md": "documents/m.md", "doc_id": "m"},
            {"source_path": str(cs / "t.csv"), "doc_md": "documents/t.md", "doc_id": "t"},
        ], ensure_ascii=False), encoding="utf-8")
        run(SC / "update_manifest.py", "--vault", mv, "--mark-from", mf)
        man = jload(mv / ".memory-wiki/manifest.json")
        check("mark-from 登记 2 且 status=done",
              len(man["processed"]) == 2 and all(x["status"] == "done" for x in man["processed"].values()))

        # L4: 损坏 manifest → 非破坏性（带时间戳备份，连续损坏不互相覆盖）
        bad = W / "badv"; (bad / ".memory-wiki").mkdir(parents=True)
        (bad / ".memory-wiki/manifest.json").write_text("{ 这不是合法 json", encoding="utf-8")
        run(SC / "update_manifest.py", "--vault", bad, "--mark", str(cs / "m.md"), "--doc-md", "documents/m.md")
        bk = list((bad / ".memory-wiki").glob("manifest.corrupt-*.json"))
        check("L4: 损坏 manifest 已备份(非破坏)", len(bk) == 1, [b.name for b in bk])
        check("L4: 备份保留损坏内容", bool(bk) and "这不是合法 json" in bk[0].read_text(encoding="utf-8"))
        check("L4: 重置后新 mark 写入成功", len(jload(bad / ".memory-wiki/manifest.json")["processed"]) == 1)
        (bad / ".memory-wiki/manifest.json").write_text("{ 再次损坏", encoding="utf-8")
        run(SC / "update_manifest.py", "--vault", bad, "--mark", str(cs / "dupA.txt"), "--doc-md", "documents/dupA.md")
        check("L4: 连续损坏产生 2 个独立备份",
              len(list((bad / ".memory-wiki").glob("manifest.corrupt-*.json"))) == 2)

        # ---------- 5. verify_entities: L6 词边界 ----------
        print("[5] verify_entities")
        doc = W / "ve.md"
        doc.write_text("---\nx: 1\n---\n\n项目用 C++ 实现。我们 WAIT 数据 available。上下文工程很重要。", encoding="utf-8")
        ents = W / "ve.json"
        ents.write_text(json.dumps({"entities": [
            {"kind": "concept", "text": "C++"},      # 符号实体、确实出现 → 应保留(L6)
            {"kind": "concept", "text": "AI"},       # 仅 WAIT/available 子串 → 应丢
            {"kind": "concept", "text": "上下文"},    # 中文子串 → 保留
        ]}, ensure_ascii=False), encoding="utf-8")
        res = jload_str(run(SC / "verify_entities.py", "--doc", doc, "--entities", ents).stdout)
        kept = {e["text"] for e in res["entities"]}
        check("L6: C++ 符号实体被保留", "C++" in kept, str(kept))
        check("AI 子串误放被丢", "AI" not in kept)
        check("上下文 中文子串保留", "上下文" in kept)
        # L6 已知取舍（有意行为，固化以防回归）：粘连更多字符时不命中
        doc2 = W / "ve2.md"; doc2.write_text("用 C++11 和 ASP.NET 开发。", encoding="utf-8")
        ents2 = W / "ve2.json"
        ents2.write_text(json.dumps({"entities": [{"kind": "concept", "text": "C++"},
                                                   {"kind": "concept", "text": ".NET"}]}, ensure_ascii=False), encoding="utf-8")
        kept2 = {e["text"] for e in jload_str(run(SC / "verify_entities.py", "--doc", doc2, "--entities", ents2).stdout)["entities"]}
        check("L6 取舍(已知): C++ 不命中 C++11、.NET 不命中 ASP.NET", "C++" not in kept2 and ".NET" not in kept2, str(kept2))
        # L6 中文场景局限（固化既有行为，旧 \b 亦同）：ASCII 实体紧贴 CJK 时不命中
        doc3 = W / "ve3.md"; doc3.write_text("AI的应用很广泛，基于RAG的系统已部署。", encoding="utf-8")
        ents3 = W / "ve3.json"
        ents3.write_text(json.dumps({"entities": [{"kind": "concept", "text": "AI"},
                                                   {"kind": "concept", "text": "RAG"}]}, ensure_ascii=False), encoding="utf-8")
        kept3 = {e["text"] for e in jload_str(run(SC / "verify_entities.py", "--doc", doc3, "--entities", ents3).stdout)["entities"]}
        check("L6 中文局限(已知): AI/RAG 紧贴汉字时不命中（旧\\b 同行为）",
              "AI" not in kept3 and "RAG" not in kept3, str(kept3))

        # ---------- 6. compute_centrality: F4 degree=不同邻居数 ----------
        print("[6] compute_centrality")
        cev = W / "cev"; (cev / ".memory-wiki/extracted").mkdir(parents=True)
        (cev / ".memory-wiki/extracted/d1.json").write_text(json.dumps({"doc_id": "d1",
            "entities": [{"kind": "concept", "text": t} for t in ("A", "B", "C")],
            "relations": [{"subject": "A", "predicate": "USES", "object": "B"},
                          {"subject": "A", "predicate": "USES", "object": "C"}]}, ensure_ascii=False), encoding="utf-8")
        (cev / ".memory-wiki/extracted/d2.json").write_text(json.dumps({"doc_id": "d2",
            "entities": [{"kind": "concept", "text": t} for t in ("A", "B")],
            "relations": [{"subject": "A", "predicate": "USES", "object": "B"}]}, ensure_ascii=False), encoding="utf-8")
        co = jload_str(run(SC / "compute_centrality.py", "--vault", cev, "-o", W / "cen.json").stdout and (W / "cen.json").read_text(encoding="utf-8"))
        A = next(r for r in co["top"] if r["entity"] == "A")
        check("F4: A.degree=2(不同邻居数)", A["degree"] == 2, A["degree"])
        check("F4: A.relation_count=3(边条数)", A["relation_count"] == 3, A["relation_count"])
        check("degree != relation_count", A["degree"] != A["relation_count"])
        check("A 排第一", co["top"][0]["entity"] == "A")

        # ---------- 7. F1: 内容副本跨运行不重复 ----------
        print("[7] F1 跨运行去重")
        f1 = W / "f1"; (f1 / "sub").mkdir(parents=True)
        (f1 / "doc.md").write_text("唯一X", encoding="utf-8")
        (f1 / "sub" / "copy.md").write_text("唯一X", encoding="utf-8")
        (f1 / "uniq.md").write_text("内容Y", encoding="utf-8")
        fv = W / "fv"; run(SC / "generate_wiki_structure.py", "--output", fv)
        run(SC / "scan_folder.py", f1, "-o", fv / ".memory-wiki/scan.json")
        run(SC / "convert_documents.py", "--scan-report", fv / ".memory-wiki/scan.json", "--output", fv)
        rc = jload(fv / "_conversion_report.json")
        dup = next((x for x in rc["details"] if x["status"] == "skipped"), None)
        check("copy.md 被判内容副本", dup is not None)
        canon = "documents/" + Path(dup["duplicate_of"]).name
        marks = [{"source_path": str(f1 / "doc.md"), "doc_md": "documents/doc.md", "doc_id": "doc"},
                 {"source_path": str(f1 / "uniq.md"), "doc_md": "documents/uniq.md", "doc_id": "uniq"},
                 {"source_path": dup["source"], "doc_md": canon, "doc_id": Path(canon).stem}]
        (fv / ".memory-wiki/_marks.json").write_text(json.dumps(marks, ensure_ascii=False), encoding="utf-8")
        run(SC / "update_manifest.py", "--vault", fv, "--mark-from", fv / ".memory-wiki/_marks.json")
        run(SC / "scan_folder.py", f1, "--vault", fv, "-o", fv / ".memory-wiki/scan2.json")
        check("F1: 副本登记后第二轮 pending=0", jload(fv / ".memory-wiki/scan2.json")["pending_count"] == 0)

        # ---------- 8. F3: 无 PYTHONUTF8 时中文 stdout 不乱 ----------
        print("[8] F3 UTF-8 stdout")
        clean = {k: vv for k, vv in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        cn = W / "cn"; cn.mkdir(); (cn / "照明数据.md").write_text("x", encoding="utf-8")
        r = run(SC / "scan_folder.py", cn, env=clean)
        check("scan stdout 中文文件名无乱码", "照明数据" in r.stdout and r.returncode == 0, (r.stdout or "")[:60])

        # ---------- 9. 补充覆盖：convert_json / CSV 截断 / convert_pdf / 增量 modified / dedup-map ----------
        print("[9] 补充覆盖")
        # 9a convert_json 递归渲染
        cj = W / "cj"; cj.mkdir()
        (cj / "data.json").write_text(json.dumps({"name": "张三", "meta": {"role": "工程师"}}, ensure_ascii=False), encoding="utf-8")
        repj = W / "cj.json"
        repj.write_text(json.dumps(report([(str(cj / "data.json"), "json")]), ensure_ascii=False), encoding="utf-8")
        jv = W / "jv"; run(SC / "convert_documents.py", "--scan-report", repj, "--output", jv)
        jmd = (jv / "documents/data.md").read_text(encoding="utf-8")
        check("convert_json: 键值递归渲染", "**name**: 张三" in jmd and "role" in jmd, jmd[:200])

        # 9b CSV 超大表截断 (CSV_ROW_CAP=1000)
        big = W / "big"; big.mkdir()
        (big / "big.csv").write_text("\n".join(["col"] + [f"ROW{i:04d}" for i in range(1, 1101)]), encoding="utf-8")
        repb = W / "big.json"
        repb.write_text(json.dumps(report([(str(big / "big.csv"), "csv")]), ensure_ascii=False), encoding="utf-8")
        bv = W / "bv"; run(SC / "convert_documents.py", "--scan-report", repb, "--output", bv)
        bmd = (bv / "documents/big.md").read_text(encoding="utf-8")
        check("CSV 截断: 含过大提示(共1100)", "表格过大" in bmd and "1100" in bmd)
        check("CSV 截断: 保留 ROW0001、丢弃 ROW1100", "ROW0001" in bmd and "ROW1100" not in bmd)

        # 9c convert_pdf（PyMuPDF 可用时实测，否则跳过）
        try:
            import fitz as _fitz
            _have_pdf = True
        except ImportError:
            _have_pdf = False
        if not _have_pdf:
            print("  SKIP convert_pdf 测试（PyMuPDF 未安装）")
        else:
            pd = W / "pd"; pd.mkdir(); pdfp = pd / "doc.pdf"
            _pdoc = _fitz.open()
            _pdoc.new_page().insert_text((72, 72), "PDF page one text")
            _pdoc.save(str(pdfp)); _pdoc.close()
            repp = W / "pd.json"
            repp.write_text(json.dumps(report([(str(pdfp), "pdf")]), ensure_ascii=False), encoding="utf-8")
            pv = W / "pv"; run(SC / "convert_documents.py", "--scan-report", repp, "--output", pv)
            pmd = (pv / "documents/doc.md").read_text(encoding="utf-8") if (pv / "documents/doc.md").exists() else ""
            check("convert_pdf: 页码标注 + 文本", "<!-- 第 1 页 -->" in pmd and "PDF page one text" in pmd, pmd[:200])

        # 9d 增量 modified：源 mtime 变化 → 重新待处理
        ms = W / "ms"; ms.mkdir(); (ms / "x.md").write_text("内容X", encoding="utf-8")
        mvt = W / "mvt"; run(SC / "generate_wiki_structure.py", "--output", mvt)
        run(SC / "scan_folder.py", ms, "-o", mvt / ".memory-wiki/scan.json")
        run(SC / "update_manifest.py", "--vault", mvt, "--mark", str(ms / "x.md"), "--doc-md", "documents/x.md")
        run(SC / "scan_folder.py", ms, "--vault", mvt, "-o", mvt / ".memory-wiki/s1.json")
        check("增量: 未改动 → done(pending 0)", jload(mvt / ".memory-wiki/s1.json")["pending_count"] == 0)
        os.utime(ms / "x.md", (1_700_000_000, 1_700_000_000))  # 强制改 mtime
        run(SC / "scan_folder.py", ms, "--vault", mvt, "-o", mvt / ".memory-wiki/s2.json")
        s2 = jload(mvt / ".memory-wiki/s2.json")
        st = {Path(f["path"]).name: f["status"] for f in s2["files"]}
        check("增量: mtime 变化 → modified(重新 pending)", st.get("x.md") == "modified" and s2["pending_count"] == 1, str(st))

        # 9e compute_centrality --dedup-map：变体合并
        dmp = W / "dmap.json"; dmp.write_text(json.dumps({"C": "B"}, ensure_ascii=False), encoding="utf-8")
        co_dm = jload_str(run(SC / "compute_centrality.py", "--vault", cev, "--dedup-map", dmp, "-o", W / "cen_dm.json").stdout
                          and (W / "cen_dm.json").read_text(encoding="utf-8"))
        ents_dm = {r["entity"] for r in co_dm["top"]}
        check("dedup-map: C 合并入 B（C 消失、dedup_applied）", "C" not in ents_dm and co_dm["dedup_applied"] is True, str(ents_dm))

        print("\n" + "=" * 40)
        print("ALL PASS [OK]" if not fails else f"FAILED ({len(fails)}): {fails}")
        return 1 if fails else 0
    finally:
        shutil.rmtree(W, ignore_errors=True)


def jload_str(s):
    return json.loads(s)


if __name__ == "__main__":
    sys.exit(main())
