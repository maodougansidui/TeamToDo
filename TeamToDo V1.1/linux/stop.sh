#!/bin/bash
# 停止 TeamToDo 服务
pkill -f "server.py" 2>/dev/null
pkill -f "TeamToDo" 2>/dev/null
sleep 1
if pgrep -f "server.py" >/dev/null 2>&1; then
    echo "仍有进程在运行，尝试强制结束..."
    pkill -9 -f "server.py" 2>/dev/null
fi
echo "已停止。"
