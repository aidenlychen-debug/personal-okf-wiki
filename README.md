# Personal OKF-Wiki

> **Local-first, self-evolving AI personal knowledge base** built on **Karpathy LLM Wiki × Google OKF v0.3 × Obsidian**.

一套零外部 API 即可跑通 95+ 分检索精度、支持多 AI Agent 无锁并发协作、内置科学爆款拆解与自适应自迭代闭环的个人知识库引擎。

## 核心特性

- **人机物理隔离（阅读区零哈希）**：`inbox/`、`raw/`、`wiki/` 全程保持人类可读的纯净中文 Markdown 与相对路径链接（Obsidian 原生双链预览），所有索引、指纹、遥测与写锁全部收口在 `.kb/`。
- **事务闭环与四把安全锁**：Git + SQLite WAL 跨系统对账，内置 `<!-- human:start -->` 人类禁改字节锁、`verified` 已核验页纯追加锁、防误删锁、防缩水锁与毒丸隔离机制。
- **双模式精准召回（V3.0 P0+P1）**：
  - `--mode creator`：面向个人创作，全库发散检索 + 段落级/页面级 AND 优先 FTS5 + `mechanisms` 底层机制词索引 + Top-K 1 跳图谱（分离 `confirmed_relations` 与 `candidate_relations`）+ 一手经验温和加权。
  - `--mode student`：面向学员问答，严格限定 `visibility: public` + `status: stable`，加权 `verified` 页面，未命中触发 `LOW_CONFIDENCE` 拒答保护。
  - **可插拔云端 98+ 升级通道**：`config.yaml` 预埋标准云端 Embedding（OpenAI 兼容 / Gemini）与 Rerank 接口，默认关闭，按需填入 API Key 后执行 `kb rebuild` 即可原地开启混合检索。
- **科学爆款文案拆解（`kb-viral-ingest`）**：先剥离账号/热点等非文本混淆变量，再基于行为经济学、认知心理学、社会心理学与叙事学经典理论做分指标因果归因、反事实检验、负样本最小差异对照，并沉淀去题材填空骨架。
- **有理有据的自适应自迭代（`kb-evolve`）**：静默记录真实查询遥测，按 `5 → 10 → 20 → 40` 篇自适应退避节奏触发诊断；触及当前架构/模型能力上限时主动刹车休眠（`model_ceiling`），待后续数据量积累或零命中痛点飙升时再唤醒微调，默认只进化未来规则不翻修旧页。
- **确定性治理工具**：内置 `kb refactor rename`、`kb refactor merge`、`kb audit`、`kb lint`、`kb rebuild`，外加原生 macOS PDFKit 解析与零污染预清洗器。

## 快速开始

```bash
# 1. 克隆仓库并安装本地引擎
git clone <your-repo-url> personal-okf-wiki
cd personal-okf-wiki
python3 -m venv .venv
.venv/bin/pip install -e .

# 2. 幂等初始化目录树、数据库与 Git 环境
.venv/bin/kb init

# 3. 检查状态
.venv/bin/kb status
```

## 作为统一 Skill 接入 AI Agent

本仓库根目录已提供标准 [`SKILL.md`](./SKILL.md) 与顶层协作规范 [`AGENTS.md`](./AGENTS.md)，可直接挂载至 Claude Code、Cursor、Gemini、Codex 等 AI Agent 使用：

- `SKILL.md`：统一入口与 `kb` CLI 速查
- `skills/kb-ingest/SKILL.md`：通用资料零污染清洗与中文原子编译
- `skills/kb-viral-ingest/SKILL.md`：爆款文案与负样本对照的科学归因拆解
- `skills/kb-query/SKILL.md`：双模式精准召回与五段式写作弹药包编排
- `skills/kb-lint/SKILL.md`：OKF v0.3 合规巡检与知识库治理
- `skills/kb-evolve/SKILL.md`：自适应架构自迭代与能力上限判断

## 目录结构

```text
personal-okf-wiki/
├── SKILL.md                  # 统一 Skill 顶层入口
├── AGENTS.md                 # 多 Agent 协作宪法与工程铁律
├── config.yaml               # 检索权重、安全锁、适配器、云端 API 与自迭代配置
├── pyproject.toml            # Python 包与 `kb` CLI 入口定义
├── src/kb_engine/            # 确定性核心引擎（SQLite FTS5 / 图谱 / 事务 / 自迭代）
├── skills/                   # 5 套模块化业务 Skill 规范
├── scripts/                  # 基线重置脚本（reset-to-baseline.sh）
├── docs/                     # Obsidian 配合使用说明
├── inbox/                    # 素材投递箱（原始文件入口）
├── raw/                      # 不可变、已清洗的干净原文档案室
└── wiki/                     # 编译后的 OKF v0.3 原子知识库（sources/concepts/cases/evidences/insights）
```

## 常用维护命令

```bash
.venv/bin/kb lint             # 校验 Frontmatter、相对链接与安全锁
.venv/bin/kb audit            # 治理审计（学员就绪度/废弃/孤儿/重复候选/V3 覆盖）
.venv/bin/kb evolve-report    # 查看入库与召回遥测诊断报告
.venv/bin/kb rebuild          # 从 raw/ 与 wiki/ 无损重建 .kb/kb.db
```
