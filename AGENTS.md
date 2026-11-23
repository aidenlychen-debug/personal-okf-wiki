# Personal OKF-Wiki — AI Agent 顶层协作宪法（AGENTS.md）

本项目是基于 **Karpathy LLM Wiki 工作流 × Google OKF v0.3** 构建的个人知识库引擎。所有接入本仓库的 AI Coding Agent、对话助手与自动化工作流均须遵循本宪法。

## 0. 多 Agent 开放权限声明（Open Multi-Agent Access）

- **全员录入权与接口调用权**：**任何 AI Agent**（包括但不限于 Claude Code、Cursor、Gemini、Codex、本地自动化脚本及第三方 Agent）均拥有本知识库的**完整录入权**与**全量 `kb` CLI 接口调用权限**，无需额外审批或白名单配置。
- **并发协调机制**：所有写操作（`add`、`scan`、`compile-begin`、`compile-fail`、`compile-commit`、`recover`、`retry`、`reindex`、`rebuild`、`refactor rename`）均由 `.kb/write.lock` 全局排他写锁与 SQLite WAL 事务自动串行保护；所有只读查询（`query`、`status`、`lint`、`eval`）支持任意多 Agent 完全无锁并发执行。
- **溯源标识**：任意 Agent 编译或写入 `wiki/` 页面时，在 YAML Frontmatter 的 `generated.by` 字段记录 `agent:kb-ingest`（或 `agent:<agent-name>`），便于历史审计。

## 1. 八大不可违背的工程铁律

1. **人机物理隔离（阅读区零哈希）**
   - `inbox/`、`raw/`、`wiki/` 的目录名、文件名与正文标题中严禁出现哈希值或机器 ID。
   - 机器状态、指纹、索引、锁文件与日志全部收口至 `.kb/`，禁止 Agent 手动篡改 `.kb/kb.db`。
2. **OKF v0.3 × Obsidian 双兼容**
   - 仅根目录 `wiki/index.md` 声明 `okf_version: "0.3"`；子页面严禁写入 `okf_version`。
   - 所有内部链接必须采用**标准相对路径 Markdown 链接**（例如 `[知识复利效应](../concepts/知识复利效应.md)`），**严禁使用 `[[wikilinks]]`**。
   - 外链（`http://`、`https://`、`mailto:`）与纯页内 `#锚点` 可自由使用，不会被误判为断链。
3. **确定性归 Python，智能归 Agent**
   - **严禁手工改写 `wiki/index.md` 或 `wiki/log.md`**：两者分别由 `kb compile-commit` / `kb reindex` 确定性重生成与顶部追加。
   - **严禁直接修改或删除 `raw/` 中的原始档案**：`raw/` 为不可变档案室。
4. **人类主权不可侵犯**
   - **严禁修改 `<!-- human:start -->` 与 `<!-- human:end -->` 之间的任何字符**（哪怕 1 个空格，触发 `HUMAN_BLOCK_MODIFIED` 阻断）。
   - **已验证页面（`verified` 非空）只允许保持不变或在文末纯追加**，严禁删改既有正文（触发 `VERIFIED_CONTENT_MODIFIED` 阻断）。
5. **防误删与防缩水**
   - **严禁擅自删除已有 `wiki/` 页面**（触发 `UNEXPECTED_FILE_DELETED`），亦不可将已有页面正文有效字数缩减超过 30%（触发 `CONTENT_SHRINKAGE`）。
   - 如需重命名或移动页面，**必须调用 `kb refactor rename <old> <new> [--title "..."]`**，严禁手工 `mv` 造成断链。
6. **事务闭环与毒丸隔离**
   - 所有编译入库必须严格走 `kb compile-begin --json` → 编写/更新 `wiki/` → `kb compile-commit --session <id> --note "<摘要>" --json` 事务闭环。
   - 遇到损坏或无法解析的单个输入文件时，立即调用 `kb compile-fail --session <id> --hash <hash> --reason "<原因>" --json` 隔离该毒丸，继续完成同批其余正常文件。
   - 若遇到 `ACTIVE_SESSION_EXISTS` 或中断现场，先调用 `kb recover --json` 执行跨系统对账或零丢失备份恢复。
7. **Raw 零污染预清洗与中文可读呈现（忠实原意，不添油加醋）**
   - **入库前简易清洗（先清洗、再进 `raw/`）**：素材在进入不可变的 `raw/` 之前，必须先剔除网页广告、导航栏、播客赞助商口播、字幕时间戳（如 `0:00` / `1分钟4秒钟`）、`[Music]`/`[Laughter]` 噪声、PDF 孤立页码与水印；将书本/网页粘贴过来的凌乱断行与碎段落梳理为符合人类阅读习惯的清晰 Markdown 排版（标题层级、流畅段落、列表）。
   - **英文进，中文出（含 `raw/` 层）**：英文原文在录入 `raw/` 前应忠实翻译为自然流畅、排版舒适的中文 Markdown；编译至 `wiki/` 时同步将英文原标题与核心英文术语写入 `aliases` 字段以支撑跨语言召回。
   - **忠实原意底线（简易清洗，非深度改写）**：`raw/` 清洗与翻译必须严格保持与原文语义 100% 匹配，**严禁添油加醋、严禁夹带主观评论、严禁删减或概括替代正文原意**；深度提炼与拆解留给 `wiki/` 原子编译阶段。
8. **纯净 JSON 接口调用**
   - 推荐所有 Agent 统一附加 `--json` 参数调用 `kb` 命令；所有命令均遵守 `{"ok": true, ...}` 或 `{"ok": false, "error_code": "...", "message": "...", "details": {}}` 统一契约。

## 2. 标准 Skills 路由

- **入库编译**：阅读并执行 `skills/kb-ingest/SKILL.md`
- **精准召回 / 写作弹药包**：阅读并执行 `skills/kb-query/SKILL.md`
- **知识库巡检与治理**：阅读并执行 `skills/kb-lint/SKILL.md`
- **自适应架构自迭代与能力上限评估**：阅读并执行 `skills/kb-evolve/SKILL.md`
