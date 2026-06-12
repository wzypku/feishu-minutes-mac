#!/bin/bash
# 卸载飞书妙记后台服务（保留已同步的本地文件）。双击即可。
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.feishu.minutes-sync
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "▸ 停止并移除后台服务…"
launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "  ✓ 已移除后台服务"
echo
echo "已保留：你的笔记/音频（~/Documents/FeishuMinutes）、config.json。"
echo "如需彻底清除，手动删除上述目录与本文件夹即可。"
echo
read -n 1 -s -r -p "按任意键关闭…"
