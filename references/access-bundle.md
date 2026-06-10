# 接入包可选基建（Phase 9 进阶）

本文件承接 SKILL.md Phase 9：核心三步（拷模板 → `emit_access_bundle.py` → `kb_register.py`）完成后，按需启用以下可选能力。除逐文档摘要外，均为"装一次、覆盖本机所有库"的全局基建。

---

## 1. 逐文档摘要落文件（`emit_doc_summaries.py`，默认关）

长文档语料若需在 Obsidian 逐篇浏览详细摘要：

```bash
python scripts/emit_doc_summaries.py --vault {vault}
```

生成 `summaries/doc-*.md`。短文档语料不建议（文件数翻倍、收益小，详细摘要 `kb_query.py --level detailed` 已可取）。

---

## 2. 全局 MCP 中枢（`kb_hub_server.py`，真·自动发现，覆盖所有 KB）

读 `~/.knowledge-bases/registry.json`，把**本机所有** KB 暴露为 MCP 工具（`list_knowledge_bases` / `kb_search` / `kb_topic` / `kb_document`）。装一次、所有库共享，新增库无需改配置：

```bash
cp scripts/templates/kb_hub_server.py ~/.knowledge-bases/kb_hub_server.py
# Codex:  在 ~/.codex/config.toml 加 [mcp_servers.kb_hub] command="<python>" args=["~/.knowledge-bases/kb_hub_server.py"]
# Claude Code:  claude mcp add -s user kb-hub -- <python> ~/.knowledge-bases/kb_hub_server.py
```

软指针（KB-HUB block）措辞为"**优先 MCP 工具 `kb_search`，否则跑 CLI**"——MCP 端走硬发现、其余端走 CLI，二义性消解、互为冗余。

---

## 3. 入库路由（`kb_ingest.py`，多库时给新文件自动判库）

散落的新文件不确定进哪个库时用它。读 registry 里所有 KB 的「签名」（use_when + scope + 主题名/一句话）做 IDF 加权 token 匹配，按策略与你确认后暂存进目标 vault 并交接增量重建。装一次、覆盖所有库：

```bash
cp scripts/templates/kb_ingest.py ~/.knowledge-bases/kb_ingest.py
python ~/.knowledge-bases/kb_ingest.py 新文件.pdf 某目录/     # 路由+确认+暂存（默认 confirm）
python ~/.knowledge-bases/kb_ingest.py 文件 --kb {id}         # 显式指定库 → 跳过匹配直接写
```

- **决策三态**：高置信(s1≥min_score 且 s1≥ratio×s2)→写 / 歧义→列候选待选 / 无匹配→「待归类」不写。停用词/单字母在路由分词时滤除（单库时 IDF 无区分度，否则虚词会污染打分）。
- **策略** `~/.knowledge-bases/ingest-policy.json`：`mode` 默认 `confirm`（写前必确认）；`auto`=高置信直接写、**歧义/无匹配仍不写**（auto≠瞎写）；支持 `per_kb_override` 与 CLI `--mode/--min-score/--ratio` 覆盖。
- **分工**：脚本只做确定性环节——暂存、`--build`(scan+convert)、抽取后 `--finalize`(centrality+assemble+emit+register，重建 kb.json/索引/registry)；**抽取与主题摘要是 LLM 步，交 agent 按 SKILL.md 增量流程完成后再跑 `--finalize`**（否则新主题的 `summary_file` 会悬空）。审计 `~/.knowledge-bases/ingest-log.jsonl`；回滚 `--rollback <暂存名> --kb {id}`。

---

## 4. 各运行时如何发现

| 运行时 | 机制 |
|--------|------|
| Claude Code | 全局 `~/.claude/CLAUDE.md` 的 KB-HUB block（跨项目）+ 库根 `.mcp.json`（在库目录时自动挂 MCP）|
| Codex | 库根 `AGENTS.md`（原生）+ MCP |
| OpenClaw / Hermes / 其他 | 能跑 shell 即可调 `kb_query.py`；在其指令/记忆位放同样指针 |

> `kb_query.py`（CLI）是**通用底座**，不依赖任何协议；`kb_mcp_server.py` + `.mcp.json`（MCP）是**自动发现增强**，覆盖支持 MCP 的端。两者叠加 = 通用性最大。
