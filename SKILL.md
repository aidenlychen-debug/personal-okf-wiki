---
name: personal-okf-wiki
description: Personal OKF-Wiki 统一知识库 Skill（Karpathy LLM Wiki × Google OKF v0.3 × Obsidian）。支持通用资料零污染入库编译、爆款文案科学归因拆解、创作者/学员双模式精准召回、确定性重构与治理审计，以及带能力上限识别的自适应架构自迭代。
---

# `personal-okf-wiki` — 个人知识库统一 Skill 入口

> 本文件是 `personal-okf-wiki` 的顶层统一 Skill 入口。任何 AI Agent（Claude Code、Cursor、Gemini、Codex 等）挂载本仓库后，只需按下方路由加载对应子规范，即可完整操作本知识库。

## 1. 环境初始化（首次使用）

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/kb init
```

所有 CLI 命令均建议附加 `--json` 参数，遵守统一契约 `{"ok": true, ...}` 或 `{"ok": false, "error_code": "...", "message": "...", "details": {}}`。

## 2. 五大核心工作流路由（按场景加载子规范）

| 使用场景 | 加载子规范 | 核心命令 |
| :--- | :--- | :--- |
| **通用资料入库编译**（书籍、文章、PDF、图片、灵感） | `skills/kb-ingest/SKILL.md` | `kb scan` / `kb add` → `kb compile-begin` → `kb compile-commit` |
| **爆款文案科学拆解入库**（正样本爆款 / 负样本对照） | `skills/kb-viral-ingest/SKILL.md` | `kb query`（前置对照）→ 科学归因 + 填空骨架 → `kb compile-commit` |
| **精准召回与写作弹药包**（个人创作 / 学员问答） | `skills/kb-query/SKILL.md` | `kb query "<词>" --mode creator\|student --json` |
| **健康巡检、治理审计与重构**（改名 / 合并重复概念） | `skills/kb-lint/SKILL.md` | `kb lint` / `kb audit` / `kb refactor rename\|merge` |
| **自适应架构自迭代**（满 5/10/20 篇或痛点飙升时） | `skills/kb-evolve/SKILL.md` | `kb evolve-report` → 理性决策 → `kb evolve-mark` |

完整工程铁律（人机物理隔离、四把安全锁、毒丸隔离与跨系统恢复）见根目录 `AGENTS.md`。

## 3. 高频 `kb` CLI 速查

### 录入与编译事务
```bash
kb scan --json                                           # 扫描 inbox/（支持文本/字幕/HTML/PDF/Word/PPT/EPUB/CSV/图片/HEIC/网页书签/音视频）
kb add --text "<已清洗文本>" --title "<标题>" --json       # 直接录入灵感或短文本
kb add --url "https://..." --title "<标题>" --json        # 直接抓取网页正文转 Markdown 入库
kb adapters --json                                       # 检查全套输入适配器与本机解码器（PDFKit/sips/ffmpeg/转写 Key）状态
kb compile-begin --limit 10 --json                       # 隔离人类改动并锁定待编译批次
kb compile-fail --session <id> --hash <h> --reason "..." --json  # 毒丸隔离单个损坏文件
kb compile-commit --session <id> --note "<摘要>" --json   # 全量安全校验、重建索引并精确提交
kb recover --json                                        # 中断会话跨系统对账或零丢失恢复
```

### 双模式精准召回（无锁并发只读）
```bash
kb query "关键词" --mode creator --json                  # 个人创作模式（全库发散 + 一手经验温和加权）
kb query "关键词" --mode student --json                  # 学员问答模式（仅 public+stable，加权 verified）
kb query "关键词" --layer L2 --experience-type firsthand --reading-status read --json
```

### 治理、重构与自适应自迭代
```bash
kb status --json                                         # 健康度面板（含自迭代触发提醒）
kb lint --json                                           # OKF v0.3 合规与四把安全锁校验
kb audit --json                                          # 治理审计（学员就绪度/废弃/孤儿/重复候选/V3覆盖）
kb refactor rename concepts/旧名.md concepts/新名.md --json
kb refactor merge concepts/重复概念.md concepts/主概念.md --json
kb evolve-report --json                                  # 输出客观入库/召回遥测与上轮指标差值
kb evolve-mark --outcome evolved|no_gain|model_ceiling|skipped --summary "..." --json
kb rebuild --json                                        # 从 raw/ 与 wiki/ 无损重建 .kb/kb.db
```

## 4. 不可违背的四条底线

1. **禁止手动编辑 `wiki/index.md`、`wiki/log.md`、`.kb/kb.db` 或 `raw/` 既有档案**。
2. **禁止使用 `[[wikilinks]]`**，所有内部链接必须使用标准相对路径 Markdown 链接。
3. **禁止修改 `<!-- human:start -->` 至 `<!-- human:end -->` 之间的任何字符**；`verified` 非空页面仅允许文末纯追加。
4. **自迭代必须有理有据**：触及当前架构/模型能力上限时主动标记 `model_ceiling` 暂停迭代，默认只进化未来规则，不强迫翻修历史页面。

## 5. 许可协议

个人非商业使用免费，任何商业用途须事先获得作者书面授权，详见根目录 [`LICENSE`](./LICENSE)。
