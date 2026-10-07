#!/bin/bash
# TeamToDo 安装脚本（UOS 统信 / 银河麒麟 / Ubuntu 等）
# 功能: 安装到用户目录 + 创建桌面/应用菜单快捷方式 + 可选开机自启

set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${HOME}/.local/share/TeamToDo"
BIN="${HOME}/.local/bin/teamtodo"

echo "============================================================"
echo "  TeamToDo 安装程序"
echo "============================================================"
echo "  安装目录: $DEST"
echo

mkdir -p "$DEST" "$HOME/.local/bin" "$HOME/.local/share/applications"
cp -r "$SRC"/backend/* "$DEST"/ 2>/dev/null || true
[ -d "$SRC/db" ] && { mkdir -p "$DEST/db"; cp "$SRC"/db/*.sql "$DEST"/db/ 2>/dev/null || true; }
[ -f "$SRC/scripts/linux/start.sh" ] && cp "$SRC/scripts/linux/start.sh" "$DEST"/start.sh
[ -f "$SRC/scripts/linux/stop.sh" ] && cp "$SRC/scripts/linux/stop.sh" "$DEST"/stop.sh
chmod +x "$DEST"/*.sh

cat > "$BIN" <<EOF
#!/bin/bash
exec "$DEST/start.sh" "\$@"
EOF
chmod +x "$BIN"

ICON="$DEST/static/icon.png"
[ -f "$ICON" ] || ICON=""

cat > "$HOME/.local/share/applications/teamtodo.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=TeamToDo 任务协同
Comment=内部任务协同管理系统
Exec=$DEST/start.sh
Icon=${ICON:-utilities-terminal}
Terminal=true
Categories=Office;Utility;
EOF

echo "[OK] 已创建应用菜单/桌面快捷方式: TeamToDo 任务协同"

AUTOSTART="$HOME/.config/autostart"
if [ ! -d "$AUTOSTART" ]; then mkdir -p "$AUTOSTART"; fi
cp "$HOME/.local/share/applications/teamtodo.desktop" "$AUTOSTART/" 2>/dev/null || true
echo "[OK] 已设置开机自启（可在系统设置-开机启动中关闭）"

echo
echo "============================================================"
echo "  安装完成"
echo "  启动命令: teamtodo"
echo "  或直接运行: $DEST/start.sh"
echo "  访问地址: http://127.0.0.1:8765"
echo "  登录账号: admin / admin123"
echo "============================================================"
