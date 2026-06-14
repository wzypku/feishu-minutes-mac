#!/bin/bash
# 可选增强：声纹识别。装好后，标注过的人下次能被自动认出来并预填。
# 需要 ffmpeg 和一个独立 Python venv（约 100MB，含 sherpa-onnx + 声纹模型）。
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
xattr -dr com.apple.quarantine "$DIR" 2>/dev/null || true

echo "════════════════════════════════════════════"
echo "  声纹识别 · 可选增强安装"
echo "════════════════════════════════════════════"
echo

# 1) ffmpeg
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "⚠️ 需要 ffmpeg。请先安装：brew install ffmpeg"
  echo "（没有 brew 就先装 Homebrew: https://brew.sh）"
  read -n 1 -s -r -p "装好 ffmpeg 后再跑本脚本。按任意键关闭…"; exit 1
fi
echo "✓ ffmpeg 已就绪"

# 2) 选一个 Python 建 venv（sherpa-onnx 需要 3.8+）
PY=""
for c in python3.12 python3.11 python3.13 python3.10 /opt/homebrew/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done
[ -z "$PY" ] && { echo "⚠️ 找不到 python3"; exit 1; }
echo "▸ 用 $PY 建独立环境…"
rm -rf voiceprint-venv
"$PY" -m venv voiceprint-venv
VPY="$DIR/voiceprint-venv/bin/python"
"$VPY" -m pip install --quiet --upgrade pip
echo "▸ 安装 sherpa-onnx + numpy（无需 torch，约一两分钟）…"
"$VPY" -m pip install --quiet sherpa-onnx numpy
"$VPY" -c "import sherpa_onnx, numpy; print('  ✓ sherpa-onnx', getattr(sherpa_onnx,'__version__','ok'))"

# 3) 下载声纹模型（CAM++ zh-cn, ~27MB）
mkdir -p voiceprint-models
MODEL="$DIR/voiceprint-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
if [ ! -f "$MODEL" ]; then
  echo "▸ 下载声纹模型（~27MB）…"
  curl -sL -o "$MODEL" "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
fi
[ -f "$MODEL" ] && echo "  ✓ 模型就绪（$(du -h "$MODEL" | cut -f1)）"

echo
echo "✅ 声纹识别已启用！"
echo "  · 你每标注一个人，TA 的声纹就会存进库（voiceprints.json，仅本地）"
echo "  · 之后新会议里，如果声纹高置信匹配，网页会自动预填名字、标出置信度"
echo "  · 想关掉：把 config.json 的 voiceprint_enabled 设为 false"
echo
read -n 1 -s -r -p "按任意键关闭…"
