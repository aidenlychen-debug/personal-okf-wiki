#!/usr/bin/env bash
set -euo pipefail

BASELINE_TAG="v3.1.2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

if ! git rev-parse -q --verify "refs/tags/${BASELINE_TAG}" >/dev/null; then
  echo "[ERROR] 未找到基线标签 ${BASELINE_TAG}，请先确认 Git 标签存在。" >&2
  exit 1
fi

if [[ "${1:-}" != "--confirm" ]]; then
  echo "=== 基线回滚预览（Dry-Run）==="
  echo "目标基线: ${BASELINE_TAG} ($(git rev-parse --short "${BASELINE_TAG}"))"
  echo "当前 HEAD: $(git rev-parse --short HEAD)"
  echo ""
  echo "将执行以下操作（不会影响 .venv/ 与 .obsidian/）："
  echo "  1. git reset --hard ${BASELINE_TAG}"
  echo "  2. 清除 inbox/、raw/、wiki/ 下全部测试新增文件及 inbox/.processed/"
  echo "  3. 清除全部 refs/kb/recovery/* 测试恢复引用"
  echo "  4. 删除 .kb/kb.db* 与 .kb/daemon.log* 并执行 kb rebuild 重建纯净空库"
  echo ""
  echo "当前待清理的未追踪测试文件："
  git clean -nd -- inbox raw wiki || true
  echo ""
  echo "确认执行回滚，请运行："
  echo "  ./scripts/reset-to-baseline.sh --confirm"
  exit 0
fi

# 检查 kb watch 是否在运行
if [[ -x ".venv/bin/kb" ]]; then
  if .venv/bin/kb status --json 2>/dev/null | grep -q '"running": true'; then
    echo "[ERROR] 检测到 kb watch 正在运行，请先停止守护进程后再执行回滚。" >&2
    exit 1
  fi
fi

echo "1/5 正在回滚 Git 至基线 ${BASELINE_TAG}..."
git reset --hard "${BASELINE_TAG}"

echo "2/5 正在清除 inbox/、raw/、wiki/ 与 inbox/.processed/ 测试文件..."
git clean -fd -- inbox raw wiki
rm -rf inbox/.processed/* inbox/.processed/.[!.]* 2>/dev/null || true

echo "3/5 正在清除 refs/kb/recovery/* 测试恢复引用..."
while IFS= read -r ref; do
  [[ -n "${ref}" ]] && git update-ref -d "${ref}"
done < <(git for-each-ref --format='%(refname)' refs/kb/recovery/ || true)

echo "4/5 正在补齐标准目录结构..."
mkdir -p inbox/.processed raw/assets wiki/sources wiki/concepts wiki/cases wiki/evidences wiki/insights

echo "5/5 正在清理 .kb/ 测试数据并重建纯净数据库..."
rm -f .kb/kb.db .kb/kb.db-wal .kb/kb.db-shm .kb/daemon.log* .kb/watch.pid
if [[ -x ".venv/bin/kb" ]]; then
  .venv/bin/kb rebuild --json >/dev/null
  .venv/bin/kb status
else
  echo "[WARN] 未找到 .venv/bin/kb，已清理 .kb/，请手动执行 kb rebuild。"
fi

echo ""
echo "✅ 已成功回滚至基线 ${BASELINE_TAG}，测试记录已全部清除。"
