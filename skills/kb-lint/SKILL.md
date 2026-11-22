---
name: kb-lint
description: Personal OKF-Wiki 语义级巡检与治理规范。任何 AI Agent 均可调用，用于运行结构校验、排查孤儿页与失败队列、发现重合概念合并机会并识别知识盲区。
---

# `kb-lint` — 知识库语义级巡检与治理规范

> 任何 AI Agent 均可无锁调用 `kb lint --json` 与 `kb status --json` 执行健康检查。

## 标准动作链

### 1. 运行确定性检查与健康度面板
```bash
kb lint --json
kb status --json
```

### 2. 逐项诊断与可操作建议
结合 `kb lint`、`kb status` 与 `wiki/index.md` 输出结构化巡检报告：

1. **硬性错误修复（若有）**
   - 列出所有 `OKF_SCHEMA_ERROR`、`BROKEN_LINK` 等报错及对应文件，给出精确修复方案。
2. **失败毒丸队列检查（`raw_documents.failed > 0`）**
   - 提醒用户存在编译失败文件，说明失败原因并建议在确认修复后执行 `kb retry --all-failed --json`（或指定 `--hash`）重新入队。
3. **活跃/中断事务检查（`active_session != null`）**
   - 提醒存在未关闭的编译会话，建议执行 `kb recover --json` 对账或安全恢复。
4. **孤儿页关联建议（`ORPHAN_PAGE` 警告）**
   - 为无入链的孤儿页推荐 1–3 个最适合建立双向相对链接的父概念或关联案例页。
5. **概念重合与重构建议**
   - 审视 `wiki/index.md` 中语义高度重合或命名不统一的页面，建议调用确定性重命名工具：
     ```bash
     kb refactor rename concepts/旧名.md concepts/新名.md --title "新标题" --json
     ```
6. **过期复审（`STALE_PAGE`）与知识盲区识别**
   - 标出超过 `stale_after` 的页面，并基于当前主题分布指出值得补充外部原始资料的薄弱环节。
