---
name: kb-ingest
description: Personal OKF-Wiki 唯一全自动入库编译规范源。任何 AI Agent 均可调用，用于扫描投递箱、开启编译事务、英文进中文出拆解原子页、保护人类主权、毒丸隔离并完成安全提交。
---

# `kb-ingest` — 全自动知识编译入库规范

> 任何 AI Agent 均拥有调用本 Skill 与执行 `kb` 入库接口的完整权限。本文件是系统唯一的编译业务规则源。

## 标准动作链（严格按序执行）

### 0. 入库前零污染简易清洗（必做：先清洗、再归档进 `raw/`）
`raw/` 是供人类舒适直读的干净原文档案室，**严禁将带垃圾噪声或排版凌乱的脏数据直接灌入 `raw/`**。在运行 `kb scan` 或 `kb add` 之前，必须对 `inbox/` 或待录入文本执行**简易清洗**：
1. **剔除噪声污染**：清除网页广告、导航栏/页脚、播客赞助商口播广告、视频字幕时间戳（如 `0:00`、`1分钟4秒钟`）、`[Music]`/`[Laughter]` 标记、PDF 孤立页码与下载水印。
2. **Markdown 舒适排版**：将从书籍、PDF、网页或字幕粘贴导致的错乱硬换行、碎行合并为自然段落，梳理清晰的标题层级（`#`/`##`/`###`）与列表，确保人类打开 `raw/` 阅读体验舒适。
3. **英文原文忠实中文化**：若输入为英文原文，在进入 `raw/` 前将其忠实翻译为自然流畅、符合中文阅读习惯的 Markdown 文本。
4. **忠实原意红线（简易清洗，绝非深度改写）**：清洗与翻译必须与原文语义 **100% 匹配、不增不减**——严禁添油加醋、严禁加入个人发挥、严禁做摘要替代正文。提炼、拆解与观点关联留到第 4 步的 `wiki/` 编译阶段完成。

### 1. 扫描归档已清洗素材
确认 `inbox/` 中的文本已完成第 0 步清洗后，执行：
```bash
kb scan --json
```
- 若用户直接给出一段文本/灵感而未放入 `inbox/`，先按第 0 步完成清洗与中文排版，再执行：
  ```bash
  kb add --text "<已清洗文本>" --title "<可读中文标题>" --json
  ```

### 2. 开启编译事务会话
```bash
kb compile-begin --limit 10 --json
```
- 若返回 `error_code: "ACTIVE_SESSION_EXISTS"`：
  先告知用户或直接执行 `kb recover --json` 完成上次中断事务的对账/零丢失恢复，再重新调用 `kb compile-begin --json`。
- 若返回的 `items` 为空列表，直接调用 `kb compile-commit --session <session_id> --note "无待编译新素材" --json` 关闭空会话并结束。

### 3. 逐项读取原始档案（含多模态图片支持与毒丸隔离）
遍历 `items` 中的每一项：
- 若 `needs_vision == false`：读取 `raw_rel_path` 对应的原始 Markdown/文本文件。
- 若 `needs_vision == true`：直接用原生多模态视觉能力读取 `asset_rel_path` 对应的原图（同时参考 `raw_rel_path` 存根元数据），提取图中文字、数据、结构图关系与核心洞见，并在编译后的 `wiki/` 正文用相对路径（如 `![图名](../../raw/assets/xxx.png)`）嵌入原图以便 Obsidian 预览。
- **毒丸隔离**：若某个文件损坏、乱码或完全无法解析，**严禁阻塞整批编译**，立即调用：
  ```bash
  kb compile-fail --session <session_id> --hash <content_hash> --reason "<具体失败原因>" --json
  ```
  然后继续编译同批其余正常文件。

### 4. 结合总目录执行中文原子编译（英文进，中文出）
先阅读 `wiki/index.md` 掌握现有概念、案例、证据与洞察，避免重复造轮子：
- **拆解策略**：每份原始资料通常编译为 **1 篇 `source` 精读卡**（`wiki/sources/`）+ **N 篇原子页**（按内容归入 `wiki/concepts/`、`wiki/cases/`、`wiki/evidences/`、`wiki/insights/`）。
- **中文化与跨语言召回**：英文素材已在第 0 步忠实译为中文并排版舒适地存入 `raw/`；`wiki/` 中的文件名、标题、摘要、标签与正文全部使用自然中文，同时**必须将英文原标题与核心英文术语写入 `aliases` 字段**以支撑跨语言召回。
- **增量融合与观点碰撞**：若库内已有同名或同义概念，优先增量融合到现有页面，并在 `## 写作切入点与观点边界` 中使用 `> [!warning] 观点碰撞` 显式标注新旧观点的张力并给出相对路径链接。
- **五大原子页标准 Frontmatter**：
  ```yaml
  ---
  type: concept                         # source | concept | case | evidence | insight
  title: "自然流畅的中文标题"
  summary: "不超过 50 字的精炼摘要"
  status: stable                        # draft | stable | deprecated
  tags: [中文标签1, 中文标签2]
  aliases: ["English Term", "同义词"]
  domain: "personal-growth"
  visibility: private                   # private | team | public
  generated:
    by: "agent:kb-ingest"
    at: "YYYY-MM-DD"
  verified: []
  sources:
    - id: s1
      ref: "../../raw/YYYY-MM-DD-文件名.md"
  ---
  ```
- **绝对红线**：
  1. 仅使用标准相对路径 Markdown 链接（如 `[概念名](../concepts/概念名.md)`），禁止 `[[wikilinks]]`。
  2. 严禁修改任何页面中 `<!-- human:start -->` 至 `<!-- human:end -->` 之间的任何字符。
  3. 对 `verified` 非空的页面，**只允许保持不变或在文末纯追加**，禁止改动原有正文。
  4. 严禁手动编辑 `wiki/index.md`、`wiki/log.md` 或 `raw/`。
  5. 严禁删除已有 `wiki/` 页面；需要改名时使用 `kb refactor rename`。

### 5. 全量安全校验与精确提交
```bash
kb compile-commit --session <session_id> --note "<简明中文更新摘要>" --json
```
- 若返回校验失败错误码（`OKF_SCHEMA_ERROR`、`BROKEN_LINK`、`HUMAN_BLOCK_MODIFIED`、`VERIFIED_CONTENT_MODIFIED`、`UNEXPECTED_FILE_DELETED`、`CONTENT_SHRINKAGE`），根据返回的 `details.errors` 精准修复对应页面后，重新调用 `kb compile-commit`。
- 提交成功后，向用户汇报本次编译入库的页面清单、新增概念/案例/证据摘要以及（如有）被隔离的失败文件与 `kb retry` 建议。
