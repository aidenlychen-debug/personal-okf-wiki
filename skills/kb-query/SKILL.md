---
name: kb-query
description: Personal OKF-Wiki 面向写作与决策的精准召回规范。任何 AI Agent 均可调用，执行同义词扩写、双轨与 1 跳图谱检索，并编排固定五段式“写作弹药包（Writing Brief Package）”。
---

# `kb-query` — L1/L2 分级召回与写作弹药包编排规范

> 任何 AI Agent 均可无锁并发调用 `kb query` 接口执行知识库精准召回。

## 标准动作链

### 1. 关键词扩写（含口语词 → 抽象概念改写）与总目录扫读
拿到用户的写作主题或调研问题后：
1. 先将主查询词扩写为 **3–5 个同义词、关联概念或英文术语**；若用户提问包含日常口语/场景词（如“打工”、“做自媒体”、“内卷”、“副业”），**必须额外拆解为对应的抽象概念词**（例如：`打工` → `出售时间` / `线性收入` / `地位游戏` / `无杠杆`；`自媒体` → `媒体杠杆` / `零边际成本` / `真实性` / `产品化自己`），补足纯词法检索的语义跨度。
2. 支持空格分隔多词精准交集查询（如 `kb query "无限游戏 杠杆" --json`），引擎默认优先执行 `AND` 严格匹配，仅在命中不足时才自动降级到 `OR` 兜底并剪枝长尾噪声。
3. 快速扫读 `wiki/index.md`，识别标题或摘要语义相关但字面不同的潜在页面。

### 2. 选择召回模式并执行双轨/混合 + Top-K 1 跳图谱检索
根据使用场景切换模式（完全无锁只读，可并发执行）：
- **个人创作模式（默认）**：`kb query "<查询词>" --mode creator --json`（搜索 `private + public`、`draft + stable`，默认排除 `deprecated` 过时页；如需回溯历史认知可加 `--include-deprecated`）。创作模式下引擎会对 `experience_type: firsthand` 的非证据页做温和加权，优先浮现个人亲历洞察，同时不抬高事实性 `evidence` 的客观权重。
- **学员问答模式**：`kb query "<查询词>" --mode student --json`（强制仅检索 `visibility: public` 且 `status: stable` 的页面，优先加权 `verified` 核验页；若返回 `confidence_warning == "LOW_CONFIDENCE"`，必须如实说明知识库暂未覆盖该问题，严禁编造）。
- **V3.0 精准切片过滤**：按需组合 `--layer L1..L6`（按认知层级筛选）、`--experience-type firsthand|observation|others`（按经验来源筛选）、`--reading-status read|partial|unread`（按阅读深度筛选）；`mechanisms` 底层机制词已直接进入全文检索索引，可直接用机制词命中跨领域概念。
- **已确认关系 vs 候选关系分离**：结果中的 `confirmed_relations`（与 `graph_neighbors` 一致）为已确认的 1 跳关联，可直接作为论证支撑；`candidate_relations` 为 `status: candidate` 的 AI 候选联想，仅作创作灵感提示，不可直接当作既定结论引用（`rejected` 关系已自动排除）。
- **跨领域“知识涌现”桥接法（如把日常小事“吃饭”与《时间的秩序》相连）**：不要直接拿日常表象词硬搜，而是先将日常现象**向上抽离 1 层底层机制词**（例如：`吃饭` → `吸收低熵 / 热量传递 / 不可逆过程 / 维持秩序`），再扫读 `wiki/index.md` 跨学科概念并并发调用 `kb query` 碰撞出高维切入点。
- 合并并按 `rel_path` 去重所有 `l1_inventory`、`l2_full_docs`、`evidences_with_sources` 与 `graph_neighbors`。
- 优先信任 `match_tier` 为 `hybrid+strict` / `exact+strict` / `strict` / `exact` 的高置信命中；若 `stats.fallback_used == true`，对 `fallback_or` 页面结合摘要复核。
- 优先阅读 `l2_full_docs[].matched_sections`（按查询词相关性排序的段落切片）快速提取论据与金句，仅在需要完整上下文时再通读 `body`。
- 若返回 `stats.truncated == true` 且需要更多广度结果，使用 `--offset 50 --l1-limit 50` 按需翻页补拉；按类型聚焦可加 `--type concept|case|evidence|insight|source`。
- **云端双模型连接状态检查与主动提醒（必做）**：每次读取 `kb query --json` 返回后，必须检查 `stats.vector_used`、`stats.reranker_used` 与 `stats.degraded_reasons`：
  - 若 `config.yaml` 已开启云端 Embedding / Rerank，但 `stats.degraded_reasons` 非空（如 `missing_env:KB_EMBEDDING_API_KEY`、`missing_env:KB_RERANK_API_KEY`、`embedding_api_error`、`reranker_api_error`）或 `vector_used` / `reranker_used` 为 `false`，**必须在回复开头或结尾显式提醒用户**：“当前云端向量/精排 API 未连接（已自动降级为纯本地检索）”，并提示用户检查 `.kb/.env`（长期本地密钥文件，不入 Git）或 `~/.zshrc` 中是否已配置 `KB_EMBEDDING_API_KEY` 与 `KB_RERANK_API_KEY`（未申请可前往硅基流动 `https://www.siliconflow.cn/` 免费获取），确认连上后需在首次启用时执行一次 `.venv/bin/kb rebuild`。

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
