---
name: wiki-tree
description: "Turn a folder of mixed local documents (Word/PDF/Markdown/JSON/CSV/text) into a structured knowledge base — an Obsidian-compatible vault that humans browse and any AI agent can query. Use whenever the user wants to 把资料/文档整理成知识库、建 Obsidian wiki/vault、扫描文件夹提取知识、把一堆文档/笔记做成可浏览可检索的知识库、给文档建索引、做成 agent 能查询的本地知识库/第二大脑 — or points at a local folder of mixed documents and wants it organized, summarized, searchable or queryable, even without saying 'Obsidian' or 'wiki'; also when re-running to incrementally fold in newly added documents. Not for editing one file, web scraping, or building a website."
license: MIT
metadata:
  version: 1.0.1
  author: AI-Writing
  # 仅 Hermes 生态读取；其他运行时忽略。
  hermes:
    tags: [memory, obsidian, wiki, knowledge-management, documents, local-files]
    related_skills: [flow-any]
---

# Wiki Tree — 本地文档记忆抽取 → Obsidian Wiki

把本地文件夹中的文档转化为结构化 Obsidian 知识库，并产出任意 agent 可检索的接入包。

## 支持的文档格式

| 格式（扩展名） | 处理要点（已内置于 `convert_documents.py`） |
|------|------|
| Word `.docx` | python-docx 提取段落，保留标题层级与列表 |
| PDF `.pdf` | PyMuPDF 逐页提取并加页码标注；扫描版需 OCR（暂不支持） |
| Markdown `.md/.markdown/.mdx` | 直读；源自带 front-matter 时降级为正文引用块 |
| JSON `.json` | 递归展开：key 作标题、value 作内容 |
| CSV `.csv` | 转 Markdown 表格（首行表头；超大表截断并注明） |
| Text `.txt/.text/.log` | 直读，检测段落边界 |

不支持的格式跳过并计入报告。

## 核心工作流

### Phase 1：环境准备

核心流程**纯标准库，0 安装即可用**；按扫描到的格式**按需装**可选依赖：

```bash
pip install python-docx     # 仅当含 .docx
pip install PyMuPDF          # 仅当含 .pdf
pip install "mcp[cli]"       # 仅当要挂 MCP 服务；CLI 查询 kb_query.py 不需要
# 一键装全：pip install -r requirements.txt
```

不确定缺什么先跑 `python scripts/check_deps.py` 自检。不含 Word/PDF 且不挂 MCP 可跳过本步。

> ⚠️ **输出目录（Vault）不要放在扫描目标内**——脚本已对 vault 防御性剪枝，但仍建议分离。

### Phase 2：扫描与评估

用脚本（不是 bash find）扫描：

```bash
python scripts/scan_folder.py {目标路径} -o {vault}/.wiki-tree/scan.json
```

报告含分类统计 + 处理计划（≤20 一次性；21-100 每批 20 个并输出进度；>100 优先最近修改的 100 个，其余下一轮补上）。

**增量模式（已有 Vault 时复跑）**：加 `--vault {vault}`，读 `.wiki-tree/manifest.json` 逐文件判定 `new`（不在 manifest）/ `modified`（源 mtime 变了，需重抽取）/ `done`（跳过）：

```bash
python scripts/scan_folder.py {目标路径} --vault {vault} -o {vault}/.wiki-tree/scan.json
```

- 输出的 `files` 只含待处理集（`new`+`modified`），附 `pending_count / done_count`。
- 被推迟的文件**不写入 manifest** → 下一轮仍是 `new`、自动补上，不会永久遗漏。
- manifest 是"是否已处理"的**唯一真相源**（不依赖时间戳）；登记见 Phase 4.4。
- 报告另含 `orphaned`（已登记但盘上已删除的源）：其产物仍留在 vault，**不自动删**；与用户确认后手动清理对应 `documents/{name}.md`、`.wiki-tree/extracted/{doc-id}.json` 与 manifest 条目。

**排除无关文件**：`--exclude <glob>`（可多次，如 `--exclude "nested-project/"`）或目标根放 `.mwignore`（每行一个 glob）；隐藏目录与 `node_modules/.git/dist` 等默认剪枝，被排除计入 `excluded_count`。

### Phase 3：文档标准化（转 Markdown）

首次运行先建 Vault 骨架（`.obsidian/` 配置、模板、`.wiki-tree/` 底座；缺失才创建、重跑不覆盖，重置加 `--force`）：

```bash
python scripts/generate_wiki_structure.py --output {vault}
```

再一条命令完成全部转换：

```bash
python scripts/convert_documents.py --scan-report {vault}/.wiki-tree/scan.json --output {vault}
```

**不要手写转换代码**——各格式处理细节已内置于脚本（见上方格式表），含内容去重、同源覆写防 `-1` 副本、front-matter 降级等幂等机制；手写会旁路全部机制，且不产生 Phase 4.4 依赖的 `_conversion_report.json`。

产物：`{vault}/documents/{原文件名}.md`，自动注入 front-matter（`source_type / source_path / source_format / converted_at / file_size_bytes`）；不同源恰好同名以 `-1` 区分。

### Phase 4：记忆抽取（核心）

对每篇标准化后的文档：

#### Step 4.1：实体提取

按 `references/extraction-prompts.md`「1. 实体提取提示词」调用 LLM。**实体类型、JSON 契约与重要性评分以该文件为唯一权威来源。**

**幻觉防护（确定性闸门）**：把过闸前的实体 JSON 写入 `{vault}/.wiki-tree/tmp/{doc-id}.entities.json`，再过滤：

```bash
python scripts/verify_entities.py --doc {vault}/documents/{name}.md --entities {vault}/.wiki-tree/tmp/{doc-id}.entities.json
```

`--entities` **只接受文件路径**，不接受内联 JSON。纯 ASCII 实体按词边界、含非 ASCII 按子串匹配，只保留确实在原文出现的实体。

#### Step 4.2：关系抽取

按「3. 关系抽取提示词」基于**过闸后**实体推断关系（类型枚举同以该文件为准），只取有明确证据的。**确定性兜底**：丢弃 subject/object 任一端不在过闸实体集中的关系（set 过滤）。

#### Step 4.3：摘要生成

按「2. 摘要生成提示词」生成**短摘要**（1-2 句）与**详细摘要**（保留关键事实/决策/结论）。

#### Step 4.4：落盘与登记（增量的关键）

每篇抽取完写入 `{vault}/.wiki-tree/extracted/{doc-id}.json`（schema 见 `references/subagent-batch-extraction.md`）；它是持久缓存，reduce 永远从其全集重建（幂等）。

**登记 manifest（标记 `done`，即 Phase 2 增量判定的数据源）：**

- **顺序模式**：每抽完一篇登记一次（逐篇登记抗崩溃）：
  ```bash
  python scripts/update_manifest.py --vault {vault} --mark "{source_path}" --doc-md "documents/{name}.md"
  ```
- **并行模式**：子 agent **不写 manifest**，只回传 `done` 清单，主 agent fan-in 后串行登记（见 4.5）。
- **去重副本也必须登记**（否则下一轮被判 `new` 重转）。抽取完后一次性登记已转换源+去重副本（副本指向 canonical，`error` 不登记）：
  ```bash
  python scripts/update_manifest.py --vault {vault} --from-conversion-report {vault}/_conversion_report.json
  ```

未登记的下轮重做；其 `extracted/` 已在磁盘，最坏多跑一次，不重复、不丢数据。

#### Step 4.5：并行 fan-out（可选，能力探测+优雅降级）

抽取逐文档独立、又最贵，可并行：

- **有子 agent / Task 能力**：按 `batch_size` 切批，每批 spawn 一个子 agent，交给它 `references/subagent-batch-extraction.md` 的契约 + 该批清单。**任务描述必须写入 `skill_root`（skill 安装目录绝对路径）**——worker 是冷启动会话，靠它定位 `{skill_root}/references/extraction-prompts.md` 与 `{skill_root}/scripts/verify_entities.py`。
- 子 agent **逐篇流式**处理（一次只读一篇），各写 `extracted/`，只回传"计数 + `done` 清单"。
- **fan-in（主 agent 独揽登记）**：合并所有 `done` 清单成一个 JSON 数组写 `{vault}/.wiki-tree/_marks.json`，调一次：
  ```bash
  python scripts/update_manifest.py --vault {vault} --mark-from {vault}/.wiki-tree/_marks.json
  ```
- **无能力时**：顺序按批处理（4.4 顺序模式），产物完全一致。
- **reduce（Phase 5/6）始终在主 agent**——读 `extracted/*.json` 全集（紧凑 JSON），文档再多也塞得下。

### Phase 5：知识图谱构建

**输入 = `{vault}/.wiki-tree/extracted/*.json` 全集**（本轮+历轮累积，增量复跑据此幂等重建）：

1. **实体去重（语义，交 LLM）**：合并同一实体的不同写法（如"张三/Zhang San"）。先拿确定性候选作线索：
   ```bash
   python scripts/suggest_dedup.py --vault {vault}
   ```
   再按语义确认，写"变体→规范名"映射到 `{vault}/.wiki-tree/_dedup-map.json`（脚本只给线索；跨语言译名凭语义补充）。
2. **中心度（确定性，交脚本）**：
   ```bash
   python scripts/compute_centrality.py --vault {vault} --dedup-map {vault}/.wiki-tree/_dedup-map.json -o {vault}/.wiki-tree/centrality.json
   ```
   按 **degree 为主、relation_count/doc_count 兜底**排名；跳过去重则省略 `--dedup-map`，不传 `--top` 输出全量供建卡。
3. **确定性汇总（交脚本）→ 补散文（交 LLM）**：
   ```bash
   python scripts/assemble_vault.py --vault {vault}
   ```
   确定性完成：①回填 `_index.md` 统计 ②生成 `relations/_knowledge-graph.md`（关系按 `(subject,predicate,object)` 去重）③生成 `_processing-report.md` ④为连通实体（默认 `degree≥1`，或 `--cards-all` / `--card-top N`）建 entities/ 卡片骨架。**wikilink 只指向已建卡实体、其余普通文本 → 天然零悬空**。卡片重跑时只刷新 front-matter 与 `<!-- wiki-tree:auto:start/end -->` 标记区（统计/关联/来源），**标记外你补写的散文原样保留**；无标记的旧卡跳过；`--force-cards` 整卡重建（会丢散文）。随后只需在标记区外给重要实体卡补 1-2 句散文，再做 Phase 6。

### Phase 6：层级摘要

**L0 = 原始文档**（`documents/` 每篇一个 .md）。

**L1 = 主题摘要**（`summaries/topic-{主题}.md`）：

1. **主题归一（先做）**：汇总全量 `topics` 做语义聚类合并变体（同实体 `_dedup-map` 模式，如「AI 工程/AI工程/人工智能工程」归一），"变体→规范主题"映射应用后再分组。
2. 同主题的所有文档摘要按 `references/extraction-prompts.md`「4. 主题摘要合并提示词」合并。
3. 落盘 `summaries/topic-{规范主题名}.md`，文件名清洗：`\ / : * ? " < > |` 与换行/制表符替为 `-`（与脚本 `_safe` 一致）。**正文第一行必须是 `**一句话**：{主题概括}`**——`emit_access_bundle.py` 据此提取 `one_liner`，检索与入库路由都用它。完整落盘骨架见 `references/extraction-prompts.md` 第 4 节。

**L2 = 全局摘要**（`summaries/_global-summary.md`）：所有主题摘要按「5. 全局摘要压缩提示词」压缩为一份。

### Phase 7：Obsidian Wiki 输出

> "Vault（仓库）"= `{输出目录}` 本身，不存在叫 `Vault` 的子文件夹；Obsidian「Open folder as vault」选中它即可。

```
{输出目录}/
├── .obsidian/graph.json        # 图谱配色（脚本生成）
├── _index.md                   # 全局索引
├── documents/                  # L0 标准化文档
├── summaries/                  # L1 topic-*.md + L2 _global-summary.md
├── entities/                   # 实体卡片（person-张三.md …）
└── relations/_knowledge-graph.md
```

**每个文件的 YAML front-matter**：

```yaml
---
kind: document | summary | entity | index
source_type: local_file
source_path: "D:\\原始\\路径\\文件.docx"   # JSON 双引号标量（脚本自动转义，路径含 #/: 不破坏 YAML）
tags:
  - source/local-files
  - person/张三
created_at: 2026-05-19T14:30:00Z
---
```

`graph.json` 配色由脚本生成 10 组（覆盖全部实体类型），**勿手写**。

**Wikilink 格式**：实体引用用 **`[[<kind>-<实体名>]]`**（与 `entities/` 卡片文件名一致），如 `[[person-张三]]`；`<kind>` 取实体类型（枚举见 `references/extraction-prompts.md`）。
> ⚠️ 不要用 `[[entity-张三]]` 统一前缀——卡片文件名带真实类型前缀，`entity-` 会悬空。

### Phase 8：交接文档输出

`_processing-report.md` 由 Phase 5 的 `assemble_vault.py` 自动生成，含扫描/转换/抽取/中心度统计 + 按 `importance` 排序的文档表，不必手写；如需自定义在其上补充。

### Phase 9：接入包 + 全局注册（让 agent 跨运行时调用）

> **一次构建，处处可调**：CLI 兜底（能跑 shell 即可用），MCP 为自动发现增强。reduce 完成后三步：

1. **拷通用模板**到 vault 根（只读 `kb.json`，同一份通吃任何库）：
   ```bash
   cp scripts/templates/kb_query.py      {vault}/kb_query.py
   cp scripts/templates/kb_mcp_server.py {vault}/kb_mcp_server.py
   ```
2. **生成自描述**（`kb.json` 为单一真相源，派生 `AGENTS.md`/`.mcp.json`/`search-index.json`）：
   ```bash
   python scripts/emit_access_bundle.py --vault {vault} \
     --id {kebab-id} --name "{库名}" --scope "{一句话：这库是什么、覆盖什么}"
   ```
   `use_when` 触发词自动派生自中心度 Top 实体+主题名（`--extra-use-when "词1,词2"` 补领域词）。
3. **全局注册**（跨项目可发现）：
   ```bash
   python scripts/kb_register.py --vault {vault} [--install-hook]
   ```
   upsert `~/.knowledge-bases/registry.json`（按 `id` 幂等）；`--install-hook` 往 `~/.claude/CLAUDE.md` 写 KB-HUB managed block（幂等替换）。**因改全局指令文件，默认不装、仅打印 block 供手贴。**

**四档下钻**（`kb_query.py` 内置）：`--global`(L2) → 主题摘要(L1，检索结果或 `--topic`) → `--level detailed`(逐文档详细摘要) → `--level full`(L0 原文)；默认 short，**够用就停**。

可选基建（`kb_hub_server` 全局 MCP 中枢 / `kb_ingest` 入库路由 / `emit_doc_summaries` / 运行时发现矩阵）见 `references/access-bundle.md`。

## 增量更新与定时任务

增量判定以 manifest 为唯一真相源（判定见 Phase 2，登记见 Phase 4.4）。可与定时调度（Hermes cronjob 或宿主 cron 能力）集成：

1. 首次运行指定目标路径与输出 Vault。
2. 宿主有建任务能力则建定时任务；否则用户手动复跑——**功能不依赖特定调度器**。
3. 每次唤醒：`scan_folder.py --vault` 取增量 → 转换 → 抽取（有子 agent 能力则并行 fan-out）→ reduce → 写 `_processing-report.md` **→ 重跑 `emit_access_bundle.py` 刷新 `search-index.json` 与 `kb.json`**（否则新文档对 `kb_query`/`kb_search` 检索永远不可见；`--id/--name/--scope` 可从既有 `kb.json` 回读）。
4. manifest + `extracted/` 即跨次运行的记忆，不重复、不遗漏。

## 验证清单

- [ ] 已跑 `python scripts/check_deps.py`；扫描含 `.docx`/`.pdf` 或需挂 MCP 时，对应可选依赖已就绪
- [ ] 目标路径可读；**输出 Vault 不在扫描目标内**；增量复跑传 `--vault`（不覆盖已填内容）
- [ ] 扫描报告文档数在预期范围（增量核对 `pending_count/done_count`）
- [ ] 跑过 `assemble_vault.py`：索引/知识图谱/处理报告已回填、实体卡已生成
- [ ] 用 Obsidian 打开输出目录，图谱与链接正常
- [ ] （增量）被推迟文件下一轮补上；复跑后已重跑 `emit_access_bundle.py`，`kb_query.py` 检索得到新增文档
- [ ] （并行）子 agent 都写入 `.wiki-tree/extracted/` 且 manifest 已登记
- [ ] （接入）已拷模板并跑 `emit_access_bundle.py`，`python kb_query.py "样例问题"` 验证四档可用
- [ ] （可发现）已 `kb_register.py` 登记；跨项目自动调用加 `--install-hook`（或手贴 KB-HUB block）
- [ ] （可选·多库）按 `references/access-bundle.md` 部署 `kb_ingest.py`/`kb_hub_server.py`
