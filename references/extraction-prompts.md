# 记忆抽取提示词模板

以下是 Agent 在执行记忆抽取时使用的提示词模板。Agent 应根据这些模板调用 LLM。

> **本文件是实体类型、关系类型与各步骤输出 JSON 契约的唯一权威来源（single source of truth）。** SKILL.md 等其他文件只引用、不复制这些枚举；新增或修改类型时只改本文件，避免多处定义漂移。

---

## 1. 实体提取提示词

### System Prompt
```
你是一个精确的实体提取器。从给定的文档内容中提取所有命名实体。

严格返回 JSON 格式，不要返回任何其他内容。

实体类型定义：
- person：人名（包括昵称、代称）
- organization：组织、公司、部门、团队
- project：项目名、产品名、代号
- concept：专业术语、方法论、理论框架
- tool：工具、系统、平台、软件
- date：重要日期、截止日期、里程碑
- location：地点、办公区、城市
- event：会议、活动、发布

重要性评分标准：
- 0.9+：包含可执行决策、关键信息、明确承诺
- 0.6+：实质性讨论、事实内容、命名实体密集
- 0.3+：背景上下文、低密度散文
- < 0.3：表情、确认、琐碎信息
```

### User Prompt Template
```
请从以下文档中提取实体：

标题：{title}
内容：
{content}

返回格式（严格 JSON）：
{
  "entities": [
    {"kind": "person", "text": "张三"},
    {"kind": "project", "text": "Phoenix"}
  ],
  "topics": ["主题1", "主题2"],
  "importance": 0.7,
  "importance_reason": "一句话说明重要性评分理由"
}
```

---

## 2. 摘要生成提示词

### System Prompt
```
你是一个精确的文档摘要生成器。为给定的文档生成两层摘要。

要求：
1. 短摘要：1-2 句话概括文档的核心主题和关键信息
2. 详细摘要：保留具体事实、决策、人名、日期、数据，不要编造信息
3. 使用中文
4. 保持客观，不添加评价
```

### User Prompt Template
```
请为以下文档生成摘要：

标题：{title}
来源：{source_path}
实体：{entities_list}

内容：
{content}

返回格式（严格 JSON）：
{
  "short_summary": "1-2句话的短摘要",
  "detailed_summary": "一段话的详细摘要，保留关键事实",
  "key_decisions": ["决策1", "决策2"],
  "key_dates": ["2026-05-19：项目启动"],
  "key_people": ["张三：负责产品设计"]
}
```

---

## 3. 关系抽取提示词

### System Prompt
```
你是一个精确的关系抽取器。基于文档内容和已提取的实体，推断实体间的关系。

关系类型：
- WORKS_ON：工作于/负责
- BELONGS_TO：属于/隶属于
- USES：使用/依赖
- DECIDES：决定/决策
- MENTIONS_IN：在…中提及
- RELATED_TO：相关/关联
- REPORTS_TO：汇报于
- COLLABORATES：协作/合作

只提取文档中有明确证据的关系，不要推测。
```

### User Prompt Template
```
基于以下文档内容和已提取的实体，推断实体间关系：

实体列表：{entities}

内容：
{content}

返回格式（严格 JSON）：
{
  "relations": [
    {
      "subject": "张三",
      "predicate": "WORKS_ON",
      "object": "Phoenix",
      "evidence": "张三负责 Phoenix 项目的产品设计",
      "confidence": 0.9
    }
  ]
}
```

---

## 4. 主题摘要合并提示词

### System Prompt
```
你是一个知识整合专家。将同一主题下的多篇文档摘要合并为一份主题摘要。

要求：
1. 去重：相同信息只保留一次
2. 保留时间线：按时间顺序组织
3. 突出决策和结论
4. 标注信息来源
5. 使用中文
```

### User Prompt Template
```
请将以下"{topic}"主题的文档摘要合并为一份主题摘要：

{summaries_with_sources}

返回格式（严格 JSON）：
{
  "topic_summary": "主题的综合摘要",
  "timeline": [
    {"date": "2026-05-01", "event": "事件描述", "source": "文档名"}
  ],
  "key_entities": ["实体1", "实体2"],
  "key_decisions": ["决策1"],
  "open_questions": ["待解决问题1"]
}
```

---

## 5. 全局摘要压缩提示词

### System Prompt
```
你是一个高级知识整合专家。将所有主题摘要压缩为一份全局摘要。

要求：
1. 提炼最重要的 10 个要点
2. 识别跨主题的模式和联系
3. 标注核心实体和关键决策
4. 使用中文
5. 控制在 500 字以内
```

### User Prompt Template
```
请将以下所有主题摘要压缩为一份全局摘要：

{topic_summaries}

返回格式（严格 JSON）：
{
  "global_summary": "全局摘要文本（≤500字）",
  "top_10_points": [
    "要点1：具体描述",
    "要点2：具体描述"
  ],
  "core_entities": ["核心实体1", "核心实体2"],
  "cross_topic_patterns": ["模式1：描述"],
  "action_items": ["待办1：描述"]
}
```
