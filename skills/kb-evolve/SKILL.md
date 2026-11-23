---
name: kb-evolve
description: Personal OKF-Wiki 有理有据的自适应架构自迭代规范。分析近期入库与召回遥测数据，在收益递减或触及模型/词法能力上限时主动刹车休眠，待数据量继续增长或出现新痛点后再唤醒微调；默认只进化未来规则，不强迫翻修历史旧页面。
---

# `kb-evolve` — 自适应架构自迭代与能力上限判断规范

> 核心哲学：**有理有据才迭代，绝不为了迭代而迭代；触及能力上限主动刹车，待量变积累后再唤醒微调；只进化未来动作，默认不动历史页面。**

## 触发时机

1. **自动建议触发**：当 `kb compile-commit` 或 `kb status` 返回 `evolution.evolve_recommended == true` 时（新编译资料达到当前自适应间隔，如 `5 → 10 → 20 → 40` 篇，或真实查询零命中率异常飙升）。
2. **用户随时手动触发**：当用户要求“做一次架构自查 / 评估要不要迭代”时。

## 标准动作链

### 1. 读取客观诊断数据（先看证据，不拍脑袋）
```bash
kb evolve-report --json
```
重点核查以下 4 组硬数据：
- `evolution_status`：距上次自省新增了几篇 `raw`（`since_last_evolve`）、当前处于 `active` 还是 `ceiling_paused`（能力上限休眠态）、唤醒原因（`volume_milestone` 或 `pain_signal_spike`）。
- `query_diagnostics`：零命中查询列表（`zero_hit_queries`）、被迫触发 OR 兜底的弱命中查询（`fallback_queries`）、学员模式 `LOW_CONFIDENCE` 列表。
- `ingest_diagnostics`：近期入库页面的 `aliases` / `mechanisms` / `layers` / `experience_type` 覆盖率、平均字数与近期新增页面清单（可抽读最近 3–5 篇新页面检查拆解质量）。
- `delta_vs_last_checkpoint`：对比上一轮自省后的真实指标变化（`zero_hit_rate_delta`、`fallback_rate_delta`、`orphan_delta`）。

### 2. 三选一理性决策（核心智能门禁）

严格按客观证据落入以下 3 种结论之一：

1. **有明确痛点且规则/架构可解 → `evolved`（提出精准微调）**
   - 仅当存在**可验证的真实问题**（例如：多个高频口语查询零命中、某类文档拆解粒度持续过碎/过粗、`mechanisms` 缺失导致跨领域联想失败、权重配置失衡）时，才提出具体改动方案。
   - 方案必须分三栏呈现，由用户拍板：
     1. **未来入库动作微调**（修改 `skills/kb-ingest/SKILL.md` / `AGENTS.md`，仅作用于后续新入库）；
     2. **未来召回与引擎微调**（修改 `skills/kb-query/SKILL.md` / `config.yaml` / 新增 `eval` 回归用例 / 引擎代码微调）；
     3. **旧内容可选优化（默认不改）**：默认声明“历史已编译页面保持不动”；仅当某项改动能显著提升核心高频页质量时，单列 1–3 条可选回溯项供用户自选。

2. **无显著痛点 → `no_gain`（不为了迭代而迭代）**
   - 近期入库与召回指标健康，没有值得动规则的真实问题。直接告知用户当前运行良好、无需改动，并登记 `no_gain` 自动拉长下次自检间隔。

3. **连续优化后无明显提升，已触及当前架构/模型上限 → `model_ceiling`（主动刹车休眠）**
   - 如果对比 `delta_vs_last_checkpoint` 发现上一轮规则微调后，剩余痛点受限于当前纯词法检索边界（例如纯隐喻/无字面重合检索）或当前模型推理上限，继续改 Prompt/规则已无边际收益：
   - **必须明确告诉用户**：“当前已达到现有架构/模型的能力上限，继续微调规则收益极低，建议暂时停止迭代”（若瓶颈在纯词法检索，可客观提示未来可按需开启 `config.yaml` 中预埋的云端 Embedding + Rerank）。
   - 执行 `kb evolve-mark --outcome model_ceiling`，引擎会自动进入 `ceiling_paused` 状态并拉长退避间隔（`5 → 10 → 20 → 40` 篇）；直到后续再积累 10 篇/20 篇新资料产生新的数据量变（或查询痛点飙升），再自动唤醒评估。

### 3. 落地回归与标记水位线
- 若用户批准了 `evolved` 方案：落地修改 → 运行 `.venv/bin/pytest` 与 `kb eval` 确认无退化 → Git 提交。
- 最后必须调用 `kb evolve-mark` 记录本次结论与最新指标基线，驱动自适应间隔更新：
  ```bash
  kb evolve-mark --outcome evolved --summary "<简明中文结论>" --json
  # 或无须迭代：
  kb evolve-mark --outcome no_gain --summary "指标稳定，无明确迭代痛点，拉长下次自检间隔" --json
  # 或达到能力上限暂停迭代：
  kb evolve-mark --outcome model_ceiling --summary "触及纯词法/当前模型能力上限，暂停迭代，待数据量增至下一阈值再唤醒" --json
  ```
