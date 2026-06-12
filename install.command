#!/bin/bash
# 飞书妙记 → Mac 一键安装。双击即可。
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
PY=/usr/bin/python3
LABEL=com.feishu.minutes-sync
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "════════════════════════════════════════════"
echo "  飞书妙记 → Mac 本地同步 · 安装"
echo "  安装目录：$DIR"
echo "════════════════════════════════════════════"
echo

# 1) Python 与依赖
echo "▸ 检查 Python 依赖（pyyaml / cryptography）…"
if ! $PY -c "import yaml, cryptography" 2>/dev/null; then
  echo "  正在安装依赖…"
  $PY -m pip install --user --quiet pyyaml cryptography || {
    echo "  ⚠️ 依赖安装失败，请手动运行：$PY -m pip install --user pyyaml cryptography"; exit 1; }
fi
echo "  ✓ 依赖就绪"

# 2) 配置文件
if [ ! -f "$DIR/config.json" ]; then
  cp "$DIR/config.example.json" "$DIR/config.json"
  echo "▸ 已创建 config.json（默认配置）"
else
  echo "▸ 已存在 config.json，保留你的配置"
fi

# 3) 跑一次首同步（在装后台服务前，避免端口占用）
echo "▸ 首次同步测试…"
$PY "$DIR/feishu_minutes_sync.py" once || true

# 4) 生成并加载后台服务（launchd）
echo "▸ 安装后台服务…"
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__PYTHON__|$PY|g" -e "s|__DIR__|$DIR|g" \
    "$DIR/com.feishu.minutes-sync.plist.template" > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "  ✓ 后台服务已安装并启动（开机自启、常驻同步）"

# 5) 生成桌面 App（双击打开标注/控制台）
echo "▸ 生成桌面 App…"
APP="$DIR/飞书妙记.app"
rm -rf "$APP"
osacompile -e "do shell script \"bash '$DIR/open_console.sh'\"" -o "$APP" 2>/dev/null \
  && echo "  ✓ 已生成 $APP（可拖到程序坞）" \
  || echo "  （桌面 App 生成跳过，可改用 open_console.sh）"

echo
echo "════════════════════════════════════════════"
echo "  ✅ 安装完成！"
echo "════════════════════════════════════════════"
echo
echo "下一步（只需一次）："
echo "  1. 用 Edge 或 Chrome 登录 https://meetings.feishu.cn/minutes/me"
echo "  2. 之后用录音豆录音、上传到飞书妙记，约 1 分钟内会自动同步到本地"
echo "     默认保存在 ~/Documents/FeishuMinutes/"
echo
echo "标注说话人 / 控制台：双击「飞书妙记.app」，或打开 http://127.0.0.1:8765/"
echo "卸载：双击 uninstall.command"
echo
read -n 1 -s -r -p "按任意键关闭…"
