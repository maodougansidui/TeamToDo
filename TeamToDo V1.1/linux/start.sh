#!/bin/bash
# TeamToDo 服务启动脚本（UOS 统信 / 银河麒麟 / Ubuntu / Debian / CentOS）
# 用法:
#   ./start.sh                  默认 8765 端口，自动打开浏览器
#   ./start.sh --port 9000
#   ./start.sh --db /mnt/share/tasks.db
#   ./start.sh --no-browser

cd "$(dirname "$0")" || exit 1

PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        ver=$("$c" -c 'import sys;print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ -n "$major" ] && { [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 6 ]; }; }; then
            PY="$c"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "错误: 未找到 Python 3.6 及以上版本"
    echo "  UOS/麒麟: sudo apt install python3"
    echo "  CentOS:   sudo yum install python3"
    exit 1
fi

echo "使用解释器: $PY ($("$PY" --version 2>&1))"
exec "$PY" server.py "$@"
