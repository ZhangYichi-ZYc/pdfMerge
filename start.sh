#!/usr/bin/env bash
# 启动 PDF 合并服务（Linux / macOS）。
#
# 换一台机器部署时，若 python 不在 PATH 上，先设置：
#     export PYTHON=/path/to/python
# 端口等其余配置见 README 的环境变量表。

set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python}"

if ! "$PYTHON" -c "import fastapi" >/dev/null 2>&1; then
  echo
  echo "依赖尚未安装。请先执行："
  echo "    $PYTHON -m pip install -r requirements.txt"
  echo
  exit 1
fi

echo "正在启动 PDF 合并服务（按 Ctrl+C 停止）..."
exec "$PYTHON" -m backend "$@"
