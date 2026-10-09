#!/bin/sh
# 安装 agent-eval-kit 的 git hook（落位门禁）
#
# 为什么需要跑这一步：.git/hooks/ 不被 git 跟踪 ——
# 换机器 / 重新 clone 之后 hook 会消失，而且是**静默消失**（提交照常成功，只是不再检查）。
# 所以源文件放在 tools/git-hooks/ 随仓库走，用本脚本装一次。
#
# 用法：sh tools/install-hooks.sh

set -e

ROOT=$(git rev-parse --show-toplevel)
SRC="$ROOT/tools/git-hooks"
DST="$ROOT/.git/hooks"

PY=$(command -v python3 || true)
if [ -z "$PY" ]; then
  echo "✗ 找不到 python3，门禁无法安装"
  exit 1
fi

mkdir -p "$DST"
for f in "$SRC"/*; do
  [ -f "$f" ] || continue
  name=$(basename "$f")
  sed "s|__PYTHON__|$PY|g" "$f" > "$DST/$name"
  chmod +x "$DST/$name"
  echo "  已安装 $name  →  $DST/$name   (python: $PY)"
done

echo "✓ 落位门禁已启用：每次 git commit 自动运行"
echo "  手工试跑：python3 tools/layout_gate.py"
