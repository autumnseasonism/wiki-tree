---
name: local-memory-wiki
description: "Use when user wants to build a personal knowledge wiki from local documents. Scans a folder for Word/PDF/Markdown/JSON/text files, converts to Markdown, extracts memories (entities, relationships, topics), and outputs an Obsidian-compatible wiki with hierarchical summaries. Inspired by OpenHuman's memory tree architecture."
version: 1.0.0
author: AI-Writing
license: MIT
metadata:
  hermes:
    tags: [memory, obsidian, wiki, knowledge-management, documents, local-files]
    related_skills: [flow-any]
---

# Local Memory Wiki — 本地文档记忆抽取 → Obsidian Wiki

将任意本地文件夹中的文档转化为结构化的 Obsidian 知识库——记忆抽取、实体关联、层级摘要、wikilink 图谱，一键生成。

灵感来自 OpenHuman 的 Memory Tree 架构，但完全本地化，支持中文文档。

---

## 触发条件

当用户的请求涉及以下意图时使用：
- 「把我的资料整理成知识库」「帮我建一个 Obsidian Wiki」
- 「扫描这个文件夹，提取里面的知识」
- 「把文档变成可检索的记忆」
- 指定了一个本地路径，并期望 Agent 自动处理其中的文档
- 任何涉及本地文档批量处理 + 知识提取 + Obsidian 输出的场景

---

## 支持的文档格式

| 格式 | 扩展名 | 处理方式 |
|------|--------|----------|
| **Word** | `.docx` | python-docx 提取段落文本 |
| **PDF** | `.pdf` | PyMuPDF (fitz) 提取文本 |
| **Markdown** | `.md` | 直接读取 |
| **JSON** | `.json` | 递归展开为可读文本 |
| **CSV** | `.csv` | 解析为 Markdown 表格（首行表头；超大表截断并注明） |
| **Text** | `.txt`, `.text`, `.log` | 直接读取 |

不支持的格式会跳过并记录在报告中。

---

## 核心工作流

### Phase 1：环境准备

先看扫描结果里的格式，**按需安装**（纯 Markdown / JSON / Text / CSV 无需任何第三方库）：

```bash
pip install python-docx   # 仅当目标文件夹含 .docx
pip install PyMuPDF        # 仅当目标文件夹含 .pdf
```

若不含 Word/PDF，可跳过本步。

### Phase 2：扫描与评估

使用 Python 脚本（不是 bash find）扫描目标路径：

**入口脚本**：`scripts/scan_folder.py`

功能：
1. 递归遍历目标路径下所有文件
2. 按扩展名分类（Word / PDF / Markdown / JSON / Text / 不支持）
3. 统计每类文件的数量和大小
4. 输出结构化的扫描报告

**根据文档规模制定处理计划：**

| 文档总数 | 策略 |
|----------|------|
| ≤ 20 | 一次性处理，单次 run 完成 |
| 21-100 | 分批处理，每批 20 个，每批完成后输出进度 |
| > 100 | 分批 + 优先处理最近修改的 100 个，其余标记为"待处理"，**下一轮自动补处理（见下方增量模式）** |

**增量模式（已有 Vault 时复跑）**：若目标 Vault 已存在，给 `scan_folder.py` 传 `--vault {vault}`，它会读取 `{vault}/.memory-wiki/manifest.json`，为每个文件标记 `new / modified / done`：

```bash
python scripts/scan_folder.py {目标路径} --vault {vault} -o {vault}/.memory-wiki/scan.json
```

- 输出的 `files` 只含待处理集（`new` + `modified`），`done` 的直接跳过；并附 `pending_count / done_count`。
- 被 >100 推迟的文件**不写入 manifest**，因此下一轮仍是 `new`、会被自动补上——彻底消除"被推迟的老文档永远不被处理"。
- manifest 是"是否已处理"的**唯一真相源**（取代不可靠的时间戳过滤）。

### Phase 3：文档标准化（转 Markdown）

对每个文档，按类型执行转换：

**Word (.docx)**：
```
提取段落文本 → 保留标题层级 → 保留列表格式 → 输出 Markdown
```

**PDF (.pdf)**：
```
逐页提取文本 → 每页加 `<!-- 第 N 页 -->` 标注 → 输出 Markdown（注：暂不做段落合并/页眉页脚剥离；扫描版图片型 PDF 需 OCR，见 FAQ）
```

**Markdown (.md)**：
```
直接读取 → 保留原始格式
```

**JSON (.json)**：
```
递归展开 → key 作为标题 → value 作为内容 → 输出 Markdown
```

**Text (.txt / .text / .log)**：
```
读取文本 → 检测段落边界 → 输出 Markdown
```

**每个文件转换后**：
- 写入 `{输出目录}/documents/{原文件名}.md`
- 添加 YAML front-matter：
  ```yaml
  ---
  source_type: local_file
  source_path: /原始/路径/文件名.docx
  source_format: docx
  converted_at: 2026-05-19T14:30:00Z
  file_size_bytes: 12345
  ---
  ```

**增量与脚手架的幂等性**：
- 转换脚本对"同名且 front-matter 的 `source_path` 一致"的既有 `.md` 会**覆写**而非生成 `-1` 副本，重跑/补处理不产生重复文档；不同源恰好同名时仍以 `-1` 区分。
- `generate_wiki_structure.py` 默认**缺失才创建**模板（`_index.md`/`_global-summary.md`/`_knowledge-graph.md`），重跑不覆盖 Agent 已填内容；需重置版式时显式加 `--force`。它还会创建 `.memory-wiki/`（manifest + `extracted/`）这一增量/并行共享底座。

### Phase 4：记忆抽取（核心）

对每篇标准化后的 Markdown 文档，执行三步记忆抽取：

#### Step 4.1：实体提取

加载 `references/extraction-prompts.md` 的「1. 实体提取提示词」，按其 System / User 模板调用 LLM。**实体类型定义、输出 JSON 字段契约与重要性评分标准均以该文件为唯一权威来源，本文件不再复制类型表**（杜绝两处枚举漂移）。

**幻觉防护（确定性闸门）**：抽取后用 `python scripts/verify_entities.py --doc {documents/x.md} --entities {实体JSON}` 过滤，只保留确实在原文出现的实体。匹配规则修正了纯子串匹配的误放：**纯 ASCII 实体按词边界匹配**（避免 "AI" 误命中 "WAIT"/"available"），**含中文等非 ASCII 的按子串匹配**。

#### Step 4.2：关系抽取

加载 `references/extraction-prompts.md` 的「3. 关系抽取提示词」，按其模板基于已提取实体推断关系。**关系类型定义同样以该文件为唯一权威来源**，仅提取文档中有明确证据的关系，不要推测。

#### Step 4.3：摘要生成

对每篇文档生成两层摘要——**短摘要**（1-2 句话：这篇在讲什么）与**详细摘要**（一段话：保留关键事实、决策、结论）。完整 System / User 提示词见 `references/extraction-prompts.md` 的「2. 摘要生成提示词」。

#### Step 4.4：落盘与登记（增量的关键）

每篇文档抽取完成后，把实体/关系/摘要写入 `{vault}/.memory-wiki/extracted/{doc-id}.json`（schema 见 `references/subagent-batch-extraction.md`）。`extracted/` 是持久缓存，reduce 永远从其全集重建图谱（幂等）。

**登记 manifest（标记 `done`）按模式不同：**
- **顺序模式**：主 agent 每抽完一篇就登记一次 `python scripts/update_manifest.py --vault {vault} --mark "{source_path}" --doc-md "documents/{name}.md"`（只有一个写入者，安全；逐篇登记还能抗崩溃）。
- **并行模式**：子 agent **不写 manifest**，只回传 `done` 清单，由主 agent 在 fan-in 后**单一写入者串行登记**（见 4.5）——避免多个 worker 并发"读-改-写"同一份 manifest 丢更新。

下一轮 `scan_folder.py --vault` 据此跳过已登记的；未登记的仍是 `new`/`modified`，下轮重做（其 `extracted/` 已在磁盘，最坏只是多跑一次，不重复、不丢数据）。

> **内容去重的副本也必须登记**：转换报告 `_conversion_report.json` 里 `status=skipped` 且带 `duplicate_of` 的源，要一并用 `update_manifest --mark` 标记 `done`、且 `doc_md` 指向其 `duplicate_of` 对应的文档。否则下一轮这些副本会被判为 `new` 重新进 convert——而内容去重只在**单次运行内**生效（哈希表每次新建），跨运行不生效，会把副本当成新文档转换+抽取、污染图谱。

#### Step 4.5：并行 fan-out（可选——能力探测 + 优雅降级）

抽取是逐文档独立、又最贵的环节，可并行：

- **宿主 agent 具备子 agent / Task 能力时**：把待处理文档按计划的 `batch_size` 切批，每批 spawn 一个子 agent，交给它 `references/subagent-batch-extraction.md` 的任务契约 + 该批文件清单。子 agent **逐篇流式**处理（一次只读一篇，避免撑爆自身上下文），各自写 `extracted/`，**不写 manifest**，只回传"计数 + `done` 清单"。
- **fan-in（主 agent 独揽登记）**：收齐所有 worker 的 `done` 清单 → 合并成一个 JSON 数组写到 `{vault}/.memory-wiki/_marks.json` → 调一次 `python scripts/update_manifest.py --vault {vault} --mark-from {vault}/.memory-wiki/_marks.json` 串行登记。**单一写入者 → 无并发写竞态。**
- **不具备能力时**：主 agent 顺序按批处理（默认行为，按 4.4 顺序模式逐篇登记），产物与接口完全一致。
- 两种模式下 **reduce（Phase 5/6）始终在主 agent 进行**，读 `extracted/*.json` 全集；它吃紧凑 JSON 而非全文，因此文档再多也塞得进上下文——这正是并行能突破"单上下文塞不下上百篇全文"这一 100 上限根因的关键。

### Phase 5：知识图谱构建

**输入 = `{vault}/.memory-wiki/extracted/*.json` 全集**（本轮 + 历轮累积，增量复跑据此重建，幂等）。汇总所有文档的实体和关系，构建知识图谱：

1. **实体去重（语义，交 LLM）**：合并同一实体的不同写法（如"张三/Zhang San"）。把合并结果写成一份"变体→规范名"映射 `{vault}/.memory-wiki/_dedup-map.json`，供下一步确定性计算复用。
2. **关系聚合**：同一实体对的多条关系合并。
3. **计算中心度（确定性，交脚本）**：运行
   ```
   python scripts/compute_centrality.py --vault {vault} --dedup-map {vault}/.memory-wiki/_dedup-map.json --top 10 -o {vault}/.memory-wiki/centrality.json
   ```
   它读 `extracted/*.json` 全集，按 **degree（关系度数）为主、neighbors / doc_count 兜底** 给实体排名，输出 `degree / neighbors / doc_count` 三项信号。**去重是语义活交 LLM、计数是确定性活交脚本**；若跳过第 1 步去重，则省略 `--dedup-map`，按原始实体名统计。
4. **生成实体卡片**：为中心度 Top 的核心实体各出一张 Markdown 卡片（核心实体清单也回填 `_index.md`）。

### Phase 6：层级摘要（借鉴 Bucket-Seal）

参考 OpenHuman 的记忆树，但简化为两级：

**L0 = 原始文档**（每篇一个 .md 文件）

**L1 = 主题摘要**：
- 将文档按 `topics` 分组
- 同一主题下的所有文档摘要，合并为一份主题摘要
- 主题摘要保留：关键事实、核心决策、重要人物、时间线

**L2 = 全局摘要**：
- 所有主题摘要压缩为一份全局摘要
- 回答："这批资料整体在讲什么？最重要的 10 件事是什么？"

> L1 主题合并、L2 全局压缩的完整提示词见 `references/extraction-prompts.md` 的「4. 主题摘要合并提示词」与「5. 全局摘要压缩提示词」。

### Phase 7：Obsidian Wiki 输出

> **术语说明**："Vault（仓库）"是 Obsidian 对"知识库文件夹"的叫法，**指 `{输出目录}` 这个文件夹本身**，而不是某个名为 `Vault` 的子文件夹。本 Skill 不会、也不需要创建叫 `Vault` 的目录——用 Obsidian「打开文件夹作为仓库 / Open folder as vault」选中 `{输出目录}` 即可。

将所有产出写入 `{输出目录}/` 的 Obsidian Vault 结构：

```
{输出目录}/
├── .obsidian/
│   └── graph.json          # 图谱颜色配置
├── _index.md               # 全局索引（全局摘要 + 主题列表）
├── documents/
│   ├── doc1.md             # 标准化后的原始文档
│   └── doc2.md
├── summaries/
│   ├── topic-AI工程.md     # 主题摘要
│   ├── topic-项目管理.md
│   └── _global-summary.md  # 全局摘要
├── entities/
│   ├── person-张三.md      # 实体卡片
│   ├── project-Phoenix.md
│   └── concept-RAG.md
└── relations/
    └── _knowledge-graph.md # 知识关系总览
```

**每个文件的 YAML front-matter**：

```yaml
---
kind: document | summary | entity | index
source_type: local_file
source_path: /原始/路径/
tags:
  - source/local-files
  - person/张三
  - project/Phoenix
created_at: 2026-05-19T14:30:00Z
---
```

**Obsidian graph.json 配置**：

```json
{
  "color-groups": [
    {"query": "tag:#source/local-files", "color": {"a": 1, "rgb": 3066993}},
    {"query": "tag:#person", "color": {"a": 1, "rgb": 10494192}},
    {"query": "tag:#project", "color": {"a": 1, "rgb": 15158332}},
    {"query": "tag:#concept", "color": {"a": 1, "rgb": 3447003}}
  ]
}
```

**Wikilink 格式**：实体引用使用 **`[[<kind>-<实体名>]]`** 格式（必须与 `entities/` 下的卡片文件名一致），例如 `[[person-张三]]`、`[[concept-RAG]]`、`[[project-Phoenix]]`。`<kind>` 取自实体类型（person/organization/project/concept/tool/date/location/event，以 `references/extraction-prompts.md` 为准）。
> ⚠️ **不要用 `[[entity-张三]]` 这种统一前缀**——卡片文件名带的是真实类型前缀（如 `person-张三.md`），用 `entity-` 会解析不到、产生悬空链接。

### Phase 8：交接文档输出

处理完成后，生成 `_processing-report.md`：

```markdown
# 处理报告

## 概览
- 扫描文件总数：156
- 成功处理：142
- 跳过（不支持格式）：14
- 输出目录：/path/to/output

## 文档分类
| 类型 | 数量 | 大小 |
|------|------|------|
| Word | 45 | 12.3 MB |
| PDF | 67 | 89.1 MB |
| Markdown | 23 | 1.2 MB |
| JSON | 5 | 0.3 MB |
| Text | 2 | 0.1 MB |

## 实体提取统计
- 人物实体：34
- 项目实体：12
- 概念实体：56
- 核心实体（中心度 Top 10）：...

## 主题分布
| 主题 | 文档数 | 核心实体 |
|------|--------|----------|
| AI工程 | 23 | 张三、RAG、提示词 |
| 项目管理 | 15 | Phoenix、Scrum |

## 处理耗时
- 总耗时：12m 34s
- 文档转换：3m 12s
- 记忆抽取：7m 45s
- Wiki 生成：1m 37s
```

---

## 增量更新与定时任务

### 增量处理逻辑（manifest 为唯一真相源）

复跑时**不再依赖时间戳过滤**——旧设计"只处理晚于上次时间戳的文件"会把被 >100 推迟的老文档永久漏掉（它们的 mtime 早于时间戳）。改为基于 manifest：

1. `scan_folder.py --vault {vault}` 读取 `.memory-wiki/manifest.json`，逐文件判定：
   - 不在 manifest → `new`（待处理）
   - 在 manifest 但源 mtime 变了 → `modified`（需重抽取）
   - 在 manifest 且 mtime 一致 → `done`（跳过）
2. 只处理 `new + modified`；每篇完成后用 `update_manifest.py --mark` 登记。
3. 被 >100 推迟的文件不登记 → 下一轮仍是 `new` → **保证最终被补上**。

> 时间戳（如保留）仅作为加速扫描的可选优化；"是否已处理"一律以 manifest 为准。

### 定时任务集成（环境自适应）

可与定时调度（Hermes cronjob，或宿主提供的任何 cron 能力）集成，实现"半夜自动增量"：

1. 首次运行指定目标路径与输出 Vault。
2. 若宿主提供建任务能力则创建定时任务；否则跳过此步、由用户手动复跑——**功能不依赖特定调度器**。
3. 每次唤醒：`scan_folder.py --vault` 取增量 → 转换 → 抽取（有子 agent 能力则并行 fan-out）→ reduce → 写 `_processing-report.md` 交接文档。
4. manifest + `extracted/` 即跨次运行的记忆，下一个 Agent 据此不重复、不遗漏。

---

## 常见问题

### Q: 文件太多怎么办？

A: Skill 会自动分批处理并输出进度。超过 100 个时优先处理最近修改的 100 个，其余标记为"待处理"；下一轮增量复跑（`--vault`）会据 manifest 把推迟的自动补上，不会遗漏。文档量很大且宿主支持子 agent 时，可并行 fan-out 加速（见 Phase 4.5）。

### Q: PDF 提取的文本质量很差怎么办？

A: 对于扫描版 PDF（图片型），需要 OCR 支持。1.0 版本暂不支持 OCR，会在报告中标记为"需要 OCR"。

### Q: 处理后的 Wiki 可以用 Obsidian 直接打开吗？

A: 是的。**输出目录这个文件夹本身就是一个完整的 Obsidian Vault**，用 Obsidian「打开文件夹作为仓库 / Open folder as vault」选中它即可。注意："Vault（仓库）"是 Obsidian 的术语、不是文件夹名——**不存在也不需要一个叫 `Vault` 的子文件夹**，找不到是正常的。没装 Obsidian 时它就是个普通文件夹，里面的 `.md` 用任何编辑器都能看，只是没有图谱/双链可视化效果。

### Q: 会不会丢失原始文件？

A: 不会。Skill 只在输出目录创建新文件，不修改或删除原始文件。

### Q: LLM 调用会不会很多？

A: 每篇文档需要 2-3 次 LLM 调用（实体提取 + 摘要生成 + 可选关系抽取）。100 篇文档大约需要 200-300 次调用。建议使用成本较低的模型。

---

## 验证清单

- [ ] 确认 python-docx 和 PyMuPDF 已安装
- [ ] 目标路径存在且可读
- [ ] 首次运行输出目录为空；增量复跑则传 `--vault` 指向既有 Vault（不覆盖已填内容）
- [ ] 扫描报告确认文档数量在预期范围内（增量模式核对 `pending_count / done_count`）
- [ ] 处理完成后检查 `_processing-report.md`
- [ ] 用 Obsidian 打开输出目录，验证图谱和链接正常
- [ ] （增量）确认被 >100 推迟的文件在下一轮被补上；（并行）确认子 agent 都写入了 `.memory-wiki/extracted/` 且 manifest 已登记
