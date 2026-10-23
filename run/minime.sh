#!/bin/zsh
# minime 运维入口（薄封装，实际逻辑在 run/minime.py）
#   ./run/minime.sh setup            首次/全量构建
#   ./run/minime.sh update           日常增量
#   ./run/minime.sh collect          立即采集（到点才跑）
#   ./run/minime.sh collect-force    忽略间隔全部重采
#   ./run/minime.sh collect-status   各信息源健康度
#   ./run/minime.sh web              启动 Web 面板
#   ./run/minime.sh mcp              前台运行 MCP（stdio）
#   ./run/minime.sh mcp-http         HTTP 方式暴露 MCP
#   ./run/minime.sh watch            常驻自动采集
#   ./run/minime.sh doctor           自检
#   ./run/minime.sh logs [-f]        查看日志
#   ./run/minime.sh pack ...         数据源包
#   ./run/minime.sh install-cron     安装每 30 分钟定时采集
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Python 解释器：优先 $MINIME_PY，其次项目内 .venv，最后当前 python3
if [ -n "$MINIME_PY" ]; then PY="$MINIME_PY"
elif [ -x "$ROOT/../.venv/bin/python" ]; then PY="$ROOT/../.venv/bin/python"
elif [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"
else PY="python3"; fi
cd "$ROOT"

case "${1:-status}" in
  collect-status) exec $PY collector/collect.py --status ;;
  collect-force)  exec $PY run/minime.py collect --force ;;
  mcp-http)       shift; exec $PY run/minime.py mcp --http "${@}" ;;
  install-cron)   exec $PY run/minime.py install ;;
  *)              exec $PY run/minime.py "${@}" ;;
esac
