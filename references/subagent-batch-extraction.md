# 子 Agent 批处理任务模板（并行抽取 worker）

本文件是**并行 fan-out 时分发给每个 worker 子 agent 的任务契约**。

由主 agent（orchestrator）在 Phase 4 决定并行时按需加载：把待处理文档切成若干批，每批 spawn 一个子 agent，并把下面这份契约 + 该批的文件清单交给它。实体/关系/摘要的**具体提示词与类型枚举不在此复制**，以 [`extraction-prompts.md`](extraction-prompts.md) 为唯一权威来源。

---

## 角色

你是一个**抽取 worker**。你只负责一批已转换好的 Markdown 文档的记忆抽取，把每篇结果写到 `extracted/`，然后返回一份极简报告（计数 + 完成清单）。你**不做**图谱合并、去重、主题/全局摘要，也**不写 manifest**——这些都由主 agent 统一做。

## 输入（由主 agent 提供）

- `vault`：Vault 根目录绝对路径。
- `doc_paths`：本批要处理的 `documents/*.md` 路径列表（已带 front-matter，其中 `source_path:` 是原始文件路径）。
- `skill_root`：**必填**，本 skill 安装目录的绝对路径。你是冷启动会话，下文所有 skill 内资源（提示词文件、校验脚本）都用它定位；缺失时先向主 agent 要，不要凭记忆编提示词或跳过闸门。
- 提示词来源：`{skill_root}/references/extraction-prompts.md`（实体提取 = 第 1 节、摘要 = 第 2 节、关系 = 第 3 节）。

## 上下文纪律（关键，别踩坑）

- **逐篇流式处理**：一次只把**一篇**文档读进上下文，抽完、落盘、再读下一篇。**绝不要**一次性把整批文档全读进来——否则"一批大文档"会撑爆你自己的上下文，正是并行要避免的事。
- 处理完一篇就丢弃其正文，只在内存里保留累加的计数。

## 每篇文档的处理步骤

对 `doc_paths` 里的**每一个** `doc_md`，依次：

1. **读取**该 `.md`，从 front-matter 解析出 `source_path`；正文记为 `content`。注意 `source_path` 的值是 **JSON/YAML 双引号标量**（Windows 路径反斜杠成对转义，如 `"D:\\docs\\a.docx"`），按 YAML 规则还原为真实路径后再写入 `done` 清单。
2. **实体提取**：按 `{skill_root}/references/extraction-prompts.md` 第 1 节调用 LLM，得到 `entities / topics / importance / importance_reason`。
3. **幻觉闸门（确定性脚本）**：先把第 2 步的实体 JSON 写入 `<vault>/.wiki-tree/tmp/<doc-id>.entities.json`，再用 `python {skill_root}/scripts/verify_entities.py --doc <doc_md> --entities <vault>/.wiki-tree/tmp/<doc-id>.entities.json` 过滤（`--entities` **只接受文件路径**，不接受内联 JSON 字符串）——纯 ASCII 实体按**词边界**、含非 ASCII 按**子串**判断是否在原文出现，未出现的丢弃。（跨语言/译名变体的合并不在这里做，留给 reduce 阶段。）
4. **关系抽取**：按第 3 节，基于过闸后的实体推断 `relations`（只取有明确证据的）。**确定性兜底**：丢弃 subject/object 任一端不在过闸实体集中的关系（set 过滤）。
5. **摘要**：按第 2 节生成 `short_summary / detailed_summary / key_decisions / key_dates / key_people`。
6. **落盘**：把结果写成 `<vault>/.wiki-tree/extracted/<doc-id>.json`（schema 见下）。`<doc-id>` 取 `doc_md` 的文件名（去扩展名）。
7. **不在此写 manifest**：把本篇的 `{source_path, doc_md, doc_id[, mtime]}` 累加到本批的 `done` 清单（见下方返回契约）。**登记 manifest 由主 agent 在收齐所有 worker 后串行完成，子 agent 绝不直接调 `update_manifest`。**

> **为什么子 agent 不自己写 manifest**：manifest 是单个共享 JSON，多个 worker 并发"读-改-写"会丢更新（原子写只防文件损坏，不防丢更新）。所以分工是：每篇的 `extracted/<doc-id>.json` **各写各的**（文件名唯一、无竞态，可逐篇落盘以抗崩溃）；而对 **manifest 的写入收归主 agent 单一写入者**。即使某篇暂时没被登记，它的 `extracted/<doc-id>.json` 仍在磁盘上、reduce 照样会纳入——最坏只是下一轮多处理它一次，不会重复、不会丢数据。

## `extracted/<doc-id>.json` 产物 schema

> 字段是 `extraction-prompts.md` 第 1/2/3 节输出的并集；新增字段时以那边为准。

```json
{
  "source_path": "/abs/原始文件.docx",
  "doc_md": "documents/原始文件.md",
  "doc_id": "原始文件",
  "entities": [
    {"kind": "person", "text": "张三"},
    {"kind": "project", "text": "Phoenix"}
  ],
  "topics": ["主题1", "主题2"],
  "importance": 0.7,
  "importance_reason": "一句话理由",
  "short_summary": "1-2 句话",
  "detailed_summary": "一段话，保留关键事实/决策/人名/日期",
  "key_decisions": ["决策1"],
  "key_dates": ["2026-05-19：项目启动"],
  "key_people": ["张三：负责产品设计"],
  "relations": [
    {"subject": "张三", "predicate": "WORKS_ON", "object": "Phoenix",
     "evidence": "原文证据句", "confidence": 0.9}
  ]
}
```

## 返回契约（给主 agent 的，不是给人看的）

处理完整批后，**只返回一段极简 JSON 计数**，不要回传任何文档正文或抽取明细（明细已在磁盘上）：

```json
{
  "batch_done": true,
  "docs_processed": 50,
  "docs_failed": 0,
  "entities_total": 412,
  "relations_total": 173,
  "done": [
    {"source_path": "/abs/原始文件.docx", "doc_md": "documents/原始文件.md", "doc_id": "原始文件",
     "mtime": "2026-05-19T06:00:00+00:00"}
  ],
  "failures": [{"doc_md": "...", "reason": "..."}]
}
```

其中 `done` 是本 worker 成功处理的文档清单，**主 agent 用它来批量登记 manifest**（见下）。

`mtime` 为可选字段：取扫描报告（`<vault>/.wiki-tree/scan.json`）中该 `source_path` 的 `modified_at`（主 agent 分发清单时可一并带上，或 worker 自行从 scan.json 读取）。带上它，fan-in 登记时 `--mark-from` 会按扫描时刻的 mtime 写入 manifest 而非登记时刻重新 stat——消除"抽取期间源文件被改、却被登记成新 mtime → 下一轮误判 done"的时间窗。

主 agent 收齐所有 worker 的返回后：

1. **登记 manifest（单一写入者，无竞态）**：把各 worker 的 `done` 清单合并成一个 JSON 数组，写到 `{vault}/.wiki-tree/_marks.json`，调一次：
   ```
   python scripts/update_manifest.py --vault {vault} --mark-from {vault}/.wiki-tree/_marks.json
   ```
   `--mark-from` 一次性读改写整份 manifest，由主 agent 串行调用，彻底避开并发写丢更新。
2. **reduce**：扫 `extracted/*.json` 全集 → 实体去重/中心度（Phase 5）→ L1 主题摘要 / L2 全局摘要（Phase 6）→ 回填 Vault 模板。
