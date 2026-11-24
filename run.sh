#!/usr/bin/env bash
# 在 PyCharm / VS Code 的终端里直接运行本项目：
#   ./run.sh
# 脚本会自动挑一个装好 numpy / scikit-learn / matplotlib 的 Python 解释器，
# 避免在“裸解释器”上运行时悄悄跳过聚类和绘图。
set -e
cd "$(dirname "$0")"

candidates=()
if [ -n "${PYTHON:-}" ]; then candidates+=("$PYTHON"); fi
candidates+=("/opt/anaconda3/bin/python3")
candidates+=("$(command -v python3 2>/dev/null || true)")
candidates+=("$(command -v python 2>/dev/null || true)")
candidates+=("/opt/homebrew/bin/python3.14")
candidates+=("/usr/bin/python3")

for py in "${candidates[@]}"; do
    [ -n "$py" ] && [ -x "$py" ] || continue
    if "$py" -c "import numpy, sklearn, matplotlib" >/dev/null 2>&1; then
        echo "使用解释器：$py"
        exec "$py" src/run_analysis.py "$@"
    fi
done

echo "找不到装好 numpy / scikit-learn / matplotlib 的解释器。" >&2
echo "请先安装依赖，例如：" >&2
echo "  /opt/anaconda3/bin/python3 -m pip install -r requirements.txt" >&2
echo "或用 PYTHON=/path/to/python ./run.sh 指定解释器。" >&2
exit 2
