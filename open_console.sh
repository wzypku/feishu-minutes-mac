#!/bin/bash
# 打开飞书妙记的本地控制台/标注页（并确保后台服务在跑）
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.feishu.minutes-sync
PORT=$(/usr/bin/python3 -c "import json;print(json.load(open('$DIR/config.json'))['web_port'])" 2>/dev/null || echo 8765)

# 后台服务没在跑就拉起来
if ! launchctl list | grep -q "$LABEL"; then
  launchctl load "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
  sleep 2
fi

# 等网页端口就绪（最多 ~5 秒）
for i in $(seq 1 10); do
  if /usr/bin/curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then break; fi
  sleep 0.5
done

open "http://127.0.0.1:$PORT/"
