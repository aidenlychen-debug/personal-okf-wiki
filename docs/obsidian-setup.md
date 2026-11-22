# Obsidian 推荐配置指南（零干扰 × OKF v0.3 完美兼容）

用 Obsidian 打开本知识库根目录（包含 `inbox/`、`raw/`、`wiki/` 的目录）即可无缝阅读与批注。为确保与 `kb` 引擎及 OKF v0.3 规范 100% 兼容，请按以下三步完成设置：

## 1. 关闭 Wikilinks，强制标准相对路径 Markdown 链接
打开 **Settings（设置） → Files & Links（文件与链接）**：
- **Use `[[Wikilinks]]`**：**关闭（OFF）**
- **New link format（新建链接格式）**：选择 **Relative path to file（相对于文件的路径）**
- **Default location for new attachments（新附件默认存放位置）**：建议保持在 `raw/assets` 或手动将新素材拖入 `inbox/` 交由 `kb watch` 自动归档。

## 2. 零干扰目录视图
- Obsidian 默认隐藏所有以 `.` 开头的目录，因此 `.kb/`（SQLite 数据库、锁文件、守护日志）、`.git/`、`.venv/` 与 `inbox/.processed/` 均不会出现在左侧文件树中。
- 日常阅读与写作聚焦 `wiki/`；随时可将文本、Markdown、截图拖入 `inbox/` 投递。

## 3. 人类主权批注与验证工作流
- **局部批注禁改区**：在任意 `wiki/` 页面中使用以下标记包裹你的原创批注，AI 编译时将被字节级硬锁拦截，绝不触碰其中任何字符：
  ```markdown
  <!-- human:start -->
  这里写下你的个人批注、反驳或写作灵感。
  <!-- human:end -->
  ```
- **整页验证保护**：当你审定某篇页面后，在顶部 Obsidian Properties 面板中向 `verified` 属性添加一条记录（例如 `human:owner`）。此后 AI 编译仅允许保持原文不变或在文末纯追加，严禁改写既有正文。
- **安全改名**：如需重命名已有 `wiki/` 页面，推荐运行 `kb refactor rename <旧路径> <新路径>`，自动完成全库相对链接与锚点重写并提交 Git 快照。
