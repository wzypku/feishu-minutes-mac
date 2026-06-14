#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
声纹嵌入提取 worker（在独立 venv 里用 sherpa-onnx 跑，不依赖 torch）。

用法:
  python voiceprint_worker.py <model.onnx> <audio_file> <srt_file>
输出（stdout，JSON）:
  {"说话人 1": [float, ...], "说话人 2": [...]}   # 仅含音频足够长的说话人
音频不足或出错的说话人会被略过；整体出错则输出 {"error": "..."}。
"""

import json
import re
import subprocess
import sys

import numpy as np
import sherpa_onnx

SR = 16000
MIN_SECONDS = 3.0  # 某说话人有效语音不足这么长就不提取（不可靠）


def srt_segments(srt_text):
    """解析 SRT -> {说话人标签: [(start_s, end_s), ...]}"""
    segs = {}
    for block in srt_text.split("\n\n"):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        if len(lines) < 3:
            continue
        mt = re.match(r"(\d\d:\d\d:\d\d,\d+)\s*-->\s*(\d\d:\d\d:\d\d,\d+)", lines[1])
        sp = re.match(r"(说话人\s*\d+)\s*[:：]", lines[2])
        if not mt or not sp:
            continue

        def t2s(t):
            h, m, rest = t.split(":")
            s, ms = rest.split(",")
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

        label = re.sub(r"\s+", " ", sp.group(1)).strip()
        segs.setdefault(label, []).append((t2s(mt.group(1)), t2s(mt.group(2))))
    return segs


def decode_audio(path):
    raw = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", path, "-ac", "1", "-ar", str(SR),
         "-f", "f32le", "-"],
        capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def main():
    try:
        model, audio, srt_path = sys.argv[1], sys.argv[2], sys.argv[3]
        wav = decode_audio(audio)
        if wav.size == 0:
            print(json.dumps({"error": "音频解码为空"}))
            return
        segs = srt_segments(open(srt_path, encoding="utf-8").read())

        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model, num_threads=2)
        ext = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

        out = {}
        for label, ranges in segs.items():
            chunks = [wav[int(a * SR):int(b * SR)] for a, b in ranges]
            chunks = [c for c in chunks if c.size > 0]
            if not chunks:
                continue
            samples = np.concatenate(chunks)
            if samples.size < MIN_SECONDS * SR:
                continue
            st = ext.create_stream()
            st.accept_waveform(SR, samples)
            st.input_finished()
            v = np.asarray(ext.compute(st), dtype=np.float32)
            n = np.linalg.norm(v)
            if n > 0:
                out[label] = (v / n).tolist()
        print(json.dumps(out, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": repr(e)}))


if __name__ == "__main__":
    main()
