---
name: kb-query
description: Personal OKF-Wiki 面向写作与决策的精准召回规范。任何 AI Agent 均可调用，执行同义词扩写、双轨与 1 跳图谱检索，并编排固定五段式“写作弹药包（Writing Brief Package）”。
---

# `kb-query` — L1/L2 分级召回与写作弹药包编排规范

> 任何 AI Agent 均可无锁并发调用 `kb query` 接口执行知识库精准召回。

## 标准动作链

### 1. 关键词扩写与总目录扫读
拿到用户的写作主题或调研问题后：
1. 先将主查询词扩写为 **3–5 个同义词、关联概念或英文术语**（补足词法检索与跨表达联想）。
2. 快速扫读 `wiki/index.md`，识别标题或摘要语义相关但字面不同的潜在页面。

### 2. 执行双轨 + 1 跳图谱检索
对主词与扩展词分别调用（完全无锁只读，可并发执行）：
```bash
kb query "<主查询词>" --json
kb query "<扩展词1>" --json
```
- 合并并按 `rel_path` 去重所有 `l1_inventory`、`l2_full_docs`、`evidences_with_sources` 与 `graph_neighbors`。
- 若返回 `stats.truncated == true` 且需要更多广度结果，使用 `--offset 50 --l1-limit 50` 按需翻页补拉。
- 若需按类型精确聚焦，可加 `--type concept|case|evidence|insight|source`。

### 3. 输出固定五段式“写作弹药包（Writing Brief Package）”
严格按以下五段结构交付，每条引用均附可溯源的 `wiki/` 页面路径或 `raw/` 原文路径：

1. **核心认知与框架（Concepts）**
   - 提炼与主题最相关的底层概念、方法论与思维模型，标注 `wiki/concepts/...` 路径。
2. **我的原创想法与碎片（Insights）**
   - 汇总个人既有判断、经验反思与未展开灵感，标注 `wiki/insights/...` 路径。
3. **可直接调用的案例库（Cases）**
   - 列出“背景 → 动作 → 结果 → 可迁移启示”完整案例，标注 `wiki/cases/...` 路径。
4. **可引用的数据与金句（Evidences + 原文溯源路径）**
   - 列出精准数据、研究结论与原话金句，必须同时附 `wiki/evidences/...` 与对应 `raw/...` 原始出处。
5. **观点碰撞与外部检索建议（Gaps & Next Steps）**
   - 显式指出库内存在的对立观点或张力，并指出当前知识盲区与建议补充的外部资料方向。

文末附 **L1 其余命中页紧凑索引**（标题 + 相对路径 + 一句话摘要），供人类或下游写作 Skill 按需深挖。
