#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书妙记 → 本地 Mac 自动同步

监听飞书妙记（含录音豆上传的录音），有新内容时自动：
  1. 下载原始音频文件
  2. 导出文字记录（转写）
  3. 尽力获取智能纪要（AI summary）
  4. 合成一份 Markdown 笔记保存到本地

用法:
  python3 feishu_minutes_sync.py once    # 跑一轮检查后退出
  python3 feishu_minutes_sync.py run     # 持续轮询（launchd 用这个）
  python3 feishu_minutes_sync.py probe <object_token>  # 调试：打印某条妙记的原始接口数据

依赖: 仅 Python3 标准库。
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "sync.log")

API_BASE = "https://meetings.feishu.cn/minutes/api"


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(title, text):
    """macOS 桌面通知"""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{text}" with title "{title}"'],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def resolve_cookie(cfg):
    """优先用浏览器实时登录态；其次用 config.json 里手填的 cookie。"""
    if cfg.get("cookie_source", "browser") != "manual":
        try:
            import edge_cookies
            cookie, src = edge_cookies.get_cookie()
            if cookie:
                log(f"已从 {src} 浏览器读取飞书登录态")
                return cookie
            log("未能从浏览器读取到飞书 Cookie（是否已在 Edge/Chrome 登录"
                " meetings.feishu.cn？），尝试使用 config.json 中手填的 cookie。")
        except Exception as e:
            log(f"浏览器读取 Cookie 失败（{e!r}），回退到 config.json。")
    manual = cfg.get("cookie", "")
    if re.search(r"bv_csrf_token=[0-9a-f-]{36}", manual) and manual.isascii():
        return manual
    return None


def load_config():
    if not os.path.exists(CONFIG_PATH):
        log(f"缺少配置文件 {CONFIG_PATH}，请先按 README.md 填写。")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    cookie = resolve_cookie(cfg)
    if not cookie:
        log("无法获取有效的飞书 Cookie：请在 Edge/Chrome/Arc 中登录 "
            "meetings.feishu.cn，或在 config.json 中手填 cookie（见 README.md）。")
        notify("飞书妙记同步", "未获取到飞书登录态，请登录 meetings.feishu.cn")
        sys.exit(1)
    cfg["cookie"] = cookie
    cfg.setdefault("save_dir", os.path.expanduser("~/Documents/FeishuMinutes"))
    cfg.setdefault("poll_interval_seconds", 60)
    cfg.setdefault("space_name", 1)  # 1=主页（共享+我的）, 2=我的内容
    cfg.setdefault("download_audio", True)
    cfg.setdefault("transcript_with_speaker", True)
    cfg.setdefault("transcript_with_timestamp", False)
    cfg.setdefault("language", "zh_cn")
    cfg.setdefault("voiceprint_enabled", True)
    cfg.setdefault("voiceprint_threshold", 0.62)
    cfg.setdefault("voiceprint_margin", 0.06)
    return cfg


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"done_tokens": [], "first_run_done": False}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


class FeishuMinutes:
    def __init__(self, cfg):
        self.cfg = cfg
        cookie = cfg["cookie"].strip()
        m = re.search(r"bv_csrf_token=([0-9a-f-]+)", cookie)
        self.headers = {
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "cookie": cookie,
            "bv-csrf-token": m.group(1) if m else "",
            "referer": "https://meetings.feishu.cn/minutes/me",
            "content-type": "application/x-www-form-urlencoded",
        }

    def _request(self, method, url, raw=False, timeout=60):
        req = urllib.request.Request(url, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise PermissionError("cookie 已失效") from e
            raise
        if raw:
            return data
        return json.loads(data.decode("utf-8"))

    # ---- 接口封装 ----

    def list_minutes(self):
        """拉取全部妙记元信息（自动翻页），按时间从旧到新返回"""
        items, seen, timestamp = [], set(), None
        while True:
            url = (f"{API_BASE}/space/list?size=20"
                   f"&space_name={self.cfg['space_name']}")
            if timestamp:
                url += f"&timestamp={timestamp}"
            data = self._request("GET", url)
            d = data.get("data", {})
            if "list" not in d:
                raise PermissionError("cookie 已失效")
            page = d["list"]
            new = [x for x in page if x["object_token"] not in seen]
            seen.update(x["object_token"] for x in new)
            items.extend(new)
            # 翻页依赖最后一项的 share_time；拿不到新数据或没有可用游标就停，避免死循环
            timestamp = page[-1].get("share_time") if page else None
            if not (d.get("has_more") and new and timestamp):
                break
        return list(reversed(items))

    def get_status(self, token):
        url = (f"{API_BASE}/status?object_token={token}"
               f"&language={self.cfg['language']}&_t={int(time.time()*1000)}")
        return self._request("GET", url).get("data", {})

    def export_transcript(self, token):
        params = {
            "object_token": token,
            "add_speaker": str(self.cfg["transcript_with_speaker"]).lower(),
            "add_timestamp": str(self.cfg["transcript_with_timestamp"]).lower(),
            "format": 2,  # 2=txt, 3=srt
        }
        url = f"{API_BASE}/export?{urllib.parse.urlencode(params)}"
        text = self._request("POST", url, raw=True).decode("utf-8", "replace")
        # 接口出错时返回 JSON 而不是纯文本
        if text.lstrip().startswith("{") and '"code"' in text[:200]:
            return None
        return text.strip()

    def export_srt(self, token):
        """导出带时间戳+说话人的 SRT（声纹分段用）。失败返回 None。"""
        params = {"object_token": token, "add_speaker": "true",
                  "add_timestamp": "true", "format": 3}
        url = f"{API_BASE}/export?{urllib.parse.urlencode(params)}"
        text = self._request("POST", url, raw=True).decode("utf-8", "replace")
        if text.lstrip().startswith("{") and '"code"' in text[:200]:
            return None
        return text

    def fetch_summary(self, token):
        """尽力获取智能纪要，拿不到返回 None"""
        candidates = [
            f"{API_BASE}/summary?object_token={token}&language={self.cfg['language']}&_t={int(time.time()*1000)}",
            f"{API_BASE}/summary/status?object_token={token}&language={self.cfg['language']}",
        ]
        for url in candidates:
            try:
                data = self._request("GET", url)
            except Exception:
                continue
            if not isinstance(data, dict) or data.get("code") not in (0, None):
                continue
            text = extract_text(data.get("data"))
            if text and len(text) > 20:
                return text
        return None

    def download_file(self, url, dest):
        req = urllib.request.Request(url, headers=self.headers)
        tmp = dest + ".part"
        with urllib.request.urlopen(req, timeout=600) as resp, open(tmp, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp, dest)


def extract_text(node, depth=0):
    """从未知结构的 JSON 里递归提取可读文本（用于智能纪要）"""
    if depth > 8 or node is None:
        return ""
    if isinstance(node, str):
        s = node.strip()
        return s if len(s) > 1 and not s.startswith("http") else ""
    if isinstance(node, list):
        parts = [extract_text(x, depth + 1) for x in node]
        return "\n".join(p for p in parts if p)
    if isinstance(node, dict):
        keys = ["title", "headline", "summary", "content", "text",
                "chapter", "chapters", "paragraphs", "sentences", "list"]
        parts = []
        for k in keys:
            if k in node:
                p = extract_text(node[k], depth + 1)
                if p:
                    parts.append(p)
        if not parts:  # 没有命中常见字段就全量遍历
            for v in node.values():
                p = extract_text(v, depth + 1)
                if p:
                    parts.append(p)
        return "\n".join(parts)
    return ""


class MinuteSkip(Exception):
    """该条妙记无法处理（无权限/不可导出），应跳过且不再重试。"""


def sanitize(name):
    name = re.sub(r'[\/\\:\*\?"<>\|\n\r]', "_", name).strip()
    return name[:80] or "untitled"


def parse_transcript_date(transcript):
    """从转写首行解析录制时间，如 '2025年7月10日 下午 8:17'。返回 epoch 毫秒或 None。"""
    if not transcript:
        return None
    head = transcript[:80]
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日"
                  r"(?:\s*(上午|下午)?\s*(\d{1,2}):(\d{2}))?", head)
    if not m:
        return None
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    hh = int(m[5]) if m[5] else 0
    mm = int(m[6]) if m[6] else 0
    if m[4] == "下午" and hh < 12:
        hh += 12
    try:
        return int(time.mktime((y, mo, d, hh, mm, 0, 0, 0, -1)) * 1000)
    except (ValueError, OverflowError):
        return None


def guess_audio_ext(url):
    path = urllib.parse.urlparse(url).path.lower()
    for ext in (".m4a", ".mp3", ".aac", ".wav", ".mp4"):
        if path.endswith(ext):
            return ext
    return ".mp4"


# ---------------- 说话人标注 ----------------
# 转写里说话人是通用标签 "说话人 1"、"说话人 2"…，需要你标注成真人。

GENERIC_SPEAKER_LINE = re.compile(r"^\s*(说话人\s*\d+)\s*$")


def detect_generic_speakers(transcript):
    """按出现顺序返回去重后的通用说话人标签，如 ['说话人 1','说话人 2']。"""
    seen, out = set(), []
    for line in (transcript or "").splitlines():
        m = GENERIC_SPEAKER_LINE.match(line)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip()
            if label not in seen:
                seen.add(label)
                out.append(label)
    return out


def sample_quotes_for(transcript, label, n=3, maxlen=80):
    """取某说话人前 n 段发言的开头，帮助辨认是谁。"""
    lines = (transcript or "").splitlines()
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    quotes = []
    for i, line in enumerate(lines):
        m = GENERIC_SPEAKER_LINE.match(line)
        if m and norm(m.group(1)) == label:
            for nxt in lines[i + 1:]:
                t = nxt.strip()
                if t:
                    quotes.append(t[:maxlen] + ("…" if len(t) > maxlen else ""))
                    break
        if len(quotes) >= n:
            break
    return quotes


def sample_quote_for(transcript, label):
    """取某说话人第一段发言的开头（YAML 注释用，单行短）。"""
    qs = sample_quotes_for(transcript, label, n=1, maxlen=38)
    return qs[0] if qs else ""


def speaker_block_lines(speakers, transcript):
    """status + speakers(含示例注释) + participants 这几行（不含 title 等）。"""
    if not speakers:
        return ["status: labeled", "participants: []"]
    out = ["status: needs-speakers",
           "# 在每个说话人后面填：名字 <邮箱>（邮箱可选）。"
           "保存后约 1 分钟内会自动更新转写并生成 participants。",
           "speakers:"]
    for label in speakers:
        q = sample_quote_for(transcript, label)
        if q:
            out.append(f"  # 例：{q}")
        out.append(f"  {label}: ")
    out.append("participants: []")
    return out


def render_frontmatter(base, speakers, transcript):
    """生成带 --- 围栏的 frontmatter 文本。
    base: 含 title/date/duration/source。speakers: 通用标签列表（空=无需标注）。"""
    out = ["---",
           f'title: "{str(base.get("title", "")).replace(chr(34), chr(39))}"',
           f'date: {base.get("date", "")}',
           f'duration: {base.get("duration", "")}',
           f'source: {base.get("source", "")}']
    out += speaker_block_lines(speakers, transcript)
    out.append("---")
    return "\n".join(out)


def init_speakers(cfg):
    """给现有（本功能上线前生成的）转写补上 speakers 标注块。返回处理条数。"""
    save_dir = os.path.expanduser(cfg["save_dir"])
    count = 0
    for root, _d, files in os.walk(save_dir):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            path = os.path.join(root, fn)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            front, raw, body = split_frontmatter(text)
            if front is None or "status" in front:
                continue
            speakers = detect_generic_speakers(body)
            extra = "\n".join(speaker_block_lines(speakers, body))
            with open(path, "w", encoding="utf-8") as f:
                f.write("---\n" + raw.rstrip("\n") + "\n" + extra + "\n---\n" + body)
            count += 1
            tag = f"{len(speakers)} 位待标注" if speakers else "无需标注"
            log(f"  已补标注块 [{tag}] → {os.path.basename(root)}")
    return count


def split_frontmatter(text):
    """返回 (front_dict, raw_fm_text, body)；无 frontmatter 时 front_dict=None。"""
    if not text.startswith("---"):
        return None, "", text
    lines = text.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None, "", text
    raw_fm = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:])
    try:
        front = yaml.safe_load(raw_fm) or {}
    except Exception:
        return None, raw_fm, body
    return front, raw_fm, body


def speaker_name(value):
    """从 '张三 <a@b.com>' 取出名字 '张三'；没有尖括号就整串当名字。"""
    m = re.match(r"(.*?)\s*<.+?>\s*$", value)
    return (m.group(1) if m else value).strip()


def apply_speaker_labels(md_path):
    """若该文件 status=needs-speakers 且 speakers 已全部填好，就改写正文+生成 participants。
    返回 'applied' / 'pending' / 'skip'。"""
    with open(md_path, encoding="utf-8") as f:
        text = f.read()
    front, _raw, body = split_frontmatter(text)
    if not front or front.get("status") != "needs-speakers":
        return "skip"
    spk = front.get("speakers") or {}
    if not spk:
        return "skip"
    mapping = {}
    for label, val in spk.items():
        if val is None or str(val).strip() == "":
            return "pending"
        mapping[re.sub(r"\s+", " ", str(label)).strip()] = str(val).strip()

    new_body = body
    for label, val in mapping.items():
        nm = speaker_name(val)
        new_body = re.sub(rf"(?m)^\s*{re.escape(label)}\s*$", nm, new_body)
    # 标注完成，移除"待标注"提示行
    new_body = re.sub(r"(?m)^> 📝 待标注：.*\n?", "", new_body)

    front["status"] = "labeled"
    front["speakers"] = mapping
    front["participants"] = list(mapping.values())
    new_fm = yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip()
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("---\n" + new_fm + "\n---\n" + new_body)
    merge_contacts(front["participants"])  # 记住这些人，供下次下拉选择
    return "applied"


CONTACTS_PATH = os.path.join(BASE_DIR, "contacts.json")


def load_contacts():
    if os.path.exists(CONTACTS_PATH):
        try:
            with open(CONTACTS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def parse_participant(p):
    """'张三 <a@b.com>' -> ('张三','a@b.com')；没邮箱则 email=''。"""
    p = str(p)
    m = re.search(r"<\s*(.+?)\s*>", p)
    email = m.group(1).strip() if m else ""
    name = speaker_name(p)
    return name, email


def merge_contacts(participants):
    """把参与者并入通讯录（按邮箱去重，没邮箱按名字），返回完整通讯录。"""
    contacts = load_contacts()
    by_key = {}
    for c in contacts:
        key = (c.get("email") or "").lower() or c.get("name", "")
        if key:
            by_key[key] = c
    changed = False
    for p in participants:
        name, email = parse_participant(p)
        if not name:
            continue
        key = email.lower() if email else name
        if key not in by_key:
            by_key[key] = {"name": name, "email": email}
            changed = True
        elif email and not by_key[key].get("email"):
            by_key[key]["email"] = email
            changed = True
    result = sorted(by_key.values(), key=lambda c: c.get("name", ""))
    if changed:
        with open(CONTACTS_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def seed_contacts_from_files(cfg):
    """从所有已标注文件里收集参与者，建立/补全通讯录。"""
    save_dir = os.path.expanduser(cfg["save_dir"])
    everyone = []
    for root, _d, files in os.walk(save_dir):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            try:
                with open(os.path.join(root, fn), encoding="utf-8") as f:
                    front, _r, _b = split_frontmatter(f.read())
                if front and front.get("participants"):
                    everyone += [str(p) for p in front["participants"]]
            except Exception:
                pass
    return merge_contacts(everyone)


APPLESCRIPT_PATH = os.path.join(BASE_DIR, "send_email.applescript")


def _transcript_section(body):
    """取『## 文字记录』之后的正文；没有就返回整段 body。"""
    idx = body.find("## 文字记录")
    if idx == -1:
        return body.strip()
    return body[idx + len("## 文字记录"):].strip()


def compose_email(cfg, md_path, auto_send=False):
    """用 Outlook 把这条转写做成邮件（草稿或直接发）。返回结果 dict。"""
    import html as _html
    with open(md_path, encoding="utf-8") as f:
        front, _raw, body = split_frontmatter(f.read())
    if not front:
        return {"ok": False, "error": "无法解析文件"}
    title = front.get("title", "会议记录")
    date = front.get("date", "")
    source = front.get("source", "")
    parts = [str(p) for p in (front.get("participants") or [])]
    to = []
    no_email = []
    for p in parts:
        name, email = parse_participant(p)
        (to if email else no_email).append((name, email))
    if not to:
        return {"ok": False, "error": "没有带邮箱的参与者，无法发送",
                "no_email": [n for n, _ in no_email]}

    transcript = _transcript_section(body)
    head = f"{title}\n时间：{date}\n"
    if source:
        head += f"飞书链接：{source}\n"
    if no_email:
        head += "（另有未填邮箱的参与者：" + "、".join(n for n, _ in no_email) + "）\n"
    head += "\n以下为会议文字记录：\n\n"
    html_body = _html.escape(head + transcript).replace("\n", "<br>")
    subject = f"{title}（会议记录 {date}）"

    args = [subject, html_body, md_path, "1" if auto_send else "0"]
    for name, email in to:
        args += [name or email, email]
    try:
        r = subprocess.run(["osascript", APPLESCRIPT_PATH] + args,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or "osascript 失败").strip()[:300]}
        return {"ok": True, "mode": r.stdout.strip() or "drafted",
                "to": to, "no_email": [n for n, _ in no_email]}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def srt_speaker_segments(srt_text):
    """解析 SRT -> {说话人标签: [(start_s, end_s), ...]}（网页"只听TA"用）。"""
    segs = {}

    def t2s(t):
        h, m, rest = t.split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    for block in (srt_text or "").split("\n\n"):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        if len(lines) < 3:
            continue
        mt = re.match(r"(\d\d:\d\d:\d\d,\d+)\s*-->\s*(\d\d:\d\d:\d\d,\d+)", lines[1])
        sp = re.match(r"(说话人\s*\d+)\s*[:：]", lines[2])
        if not mt or not sp:
            continue
        label = re.sub(r"\s+", " ", sp.group(1)).strip()
        try:
            segs.setdefault(label, []).append((t2s(mt.group(1)), t2s(mt.group(2))))
        except ValueError:
            continue
    return segs


def get_cached_srt(cfg, md_path):
    """拿该转写的 SRT：优先读音频旁的缓存 .transcript.srt，没有就拉一次并缓存。"""
    folder = os.path.dirname(md_path)
    cache = os.path.join(folder, ".transcript.srt")
    if os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                return f.read()
        except OSError:
            pass
    with open(md_path, encoding="utf-8") as f:
        front, _r, _b = split_frontmatter(f.read())
    token = (front.get("source", "").rsplit("/", 1)[-1]) if front else ""
    if not token:
        return ""
    try:
        srt = FeishuMinutes(cfg).export_srt(token) or ""
    except Exception:
        return ""
    if srt:
        try:
            with open(cache, "w", encoding="utf-8") as f:
                f.write(srt)
        except OSError:
            pass
    return srt


def transcript_keywords(body):
    """提取转写里的『关键词:』那一行，用作列表里的内容概要。"""
    lines = (body or "").splitlines()
    for i, l in enumerate(lines):
        s = l.strip()
        if s.startswith("关键词"):
            after = re.sub(r"^关键词\s*[:：]\s*", "", s)
            if after:
                return after
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    return nxt.strip()
    return ""


# ---------------- 声纹识别（可选增强） ----------------
# 标注过的人存进声纹库；新转写若声纹高置信匹配，就预填名字供你确认。
# 重活（提取嵌入）在独立 venv 里用 sherpa-onnx 跑；主程序只做向量运算（numpy）。

VOICEPRINT_DB = os.path.join(BASE_DIR, "voiceprints.json")
VENV_PY = os.path.join(BASE_DIR, "voiceprint-venv", "bin", "python")
WORKER = os.path.join(BASE_DIR, "voiceprint_worker.py")
VP_MODEL = os.path.join(BASE_DIR, "voiceprint-models",
                        "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx")


def voiceprint_available(cfg):
    return (cfg.get("voiceprint_enabled", True)
            and os.path.exists(VENV_PY) and os.path.exists(VP_MODEL)
            and os.path.exists(WORKER))


def _load_vpdb():
    if os.path.exists(VOICEPRINT_DB):
        try:
            with open(VOICEPRINT_DB, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_vpdb(db):
    with open(VOICEPRINT_DB, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False)


def _extract_embeddings(cfg, md_path):
    """对该转写的音频跑 worker，返回 {说话人标签: np.array 嵌入}。失败返回 {}。"""
    import label_server
    if not voiceprint_available(cfg):
        return {}
    folder = os.path.dirname(md_path)
    audio = label_server._find_audio(folder)
    if not audio:
        return {}
    with open(md_path, encoding="utf-8") as f:
        front, _r, _b = split_frontmatter(f.read())
    token = (front.get("source", "").rsplit("/", 1)[-1]) if front else ""
    if not token:
        return {}
    try:
        api = FeishuMinutes(cfg)
        srt = api.export_srt(token)
        if not srt:
            return {}
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".srt", delete=False,
                                         encoding="utf-8") as tf:
            tf.write(srt)
            srt_path = tf.name
        r = subprocess.run([VENV_PY, WORKER, VP_MODEL, audio, srt_path],
                           capture_output=True, text=True, timeout=180)
        os.unlink(srt_path)
        data = json.loads(r.stdout or "{}")
        if "error" in data:
            log(f"  声纹提取出错：{data['error']}")
            return {}
        import numpy as np
        return {k: np.asarray(v, dtype=np.float32) for k, v in data.items()}
    except Exception as e:
        log(f"  声纹提取失败：{e!r}")
        return {}


def voiceprint_enroll(cfg, md_path):
    """已标注的转写：把每个说话人的声纹按 名字 存进库（与历史平均）。"""
    if not voiceprint_available(cfg):
        return
    import numpy as np
    with open(md_path, encoding="utf-8") as f:
        front, _r, _b = split_frontmatter(f.read())
    if not front or front.get("status") != "labeled":
        return
    spk = front.get("speakers") or {}
    if not spk:
        return
    embs = _extract_embeddings(cfg, md_path)
    if not embs:
        return
    db = _load_vpdb()
    changed = False
    for label, value in spk.items():
        label = re.sub(r"\s+", " ", str(label)).strip()
        if label not in embs:
            continue
        name, email = parse_participant(str(value))
        key = (email.lower() if email else name)
        if not key:
            continue
        new = embs[label]
        if key in db:
            old = np.asarray(db[key]["emb"], dtype=np.float32)
            cnt = db[key].get("count", 1)
            avg = (old * cnt + new) / (cnt + 1)
            avg = avg / (np.linalg.norm(avg) or 1)
            db[key] = {"name": name, "email": email,
                       "emb": avg.tolist(), "count": cnt + 1}
        else:
            db[key] = {"name": name, "email": email,
                       "emb": new.tolist(), "count": 1}
        changed = True
    if changed:
        _save_vpdb(db)
        log(f"  声纹库已更新（{os.path.basename(os.path.dirname(md_path))}）")


def voiceprint_recognize(cfg, md_path):
    """待标注的转写：用声纹库匹配，把高置信结果写进 frontmatter 的 voiceprint 字段。"""
    if not voiceprint_available(cfg):
        return False
    import numpy as np
    db = _load_vpdb()
    if not db:
        return False
    with open(md_path, encoding="utf-8") as f:
        text = f.read()
    front, _r, body = split_frontmatter(text)
    if not front or front.get("status") != "needs-speakers":
        return False
    embs = _extract_embeddings(cfg, md_path)
    if not embs:
        return False

    thr = float(cfg.get("voiceprint_threshold", 0.62))
    margin = float(cfg.get("voiceprint_margin", 0.06))
    keys = list(db.keys())
    mat = np.asarray([db[k]["emb"] for k in keys], dtype=np.float32)
    suggestions = {}
    for label, v in embs.items():
        sims = mat @ (v / (np.linalg.norm(v) or 1))
        order = np.argsort(sims)[::-1]
        best = float(sims[order[0]])
        second = float(sims[order[1]]) if len(order) > 1 else -1.0
        if best >= thr and (best - second) >= margin:
            c = db[keys[order[0]]]
            suggestions[label] = {"name": c["name"], "email": c.get("email", ""),
                                  "score": round(best, 3)}
    if not suggestions:
        return False
    front["voiceprint"] = suggestions
    new_fm = yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip()
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("---\n" + new_fm + "\n---\n" + body)
    names = "、".join(f"{s['name']}({s['score']})" for s in suggestions.values())
    log(f"  声纹识别：{os.path.basename(os.path.dirname(md_path))} → {names}")
    return True


def scan_and_apply_labels(cfg):
    """扫描归档目录，应用所有已填好的标注。返回应用的条数。"""
    save_dir = os.path.expanduser(cfg["save_dir"])
    applied = 0
    for root, _d, files in os.walk(save_dir):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            path = os.path.join(root, fn)
            try:
                if apply_speaker_labels(path) == "applied":
                    applied += 1
                    log(f"  已应用说话人标注 → {os.path.basename(root)}")
                    notify("说话人标注已应用", os.path.basename(root))
                    try:
                        voiceprint_enroll(cfg, path)  # 标注完顺手存声纹
                    except Exception as e:
                        log(f"  声纹注册出错：{e!r}")
            except Exception as e:
                log(f"  标注应用出错 {fn}: {e!r}")
    return applied


def list_pending_labels(cfg):
    """返回需要你标注（status=needs-speakers 且未填全）的文件路径列表。"""
    save_dir = os.path.expanduser(cfg["save_dir"])
    pending = []
    for root, _d, files in os.walk(save_dir):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    front, _r, _b = split_frontmatter(f.read())
                if front and front.get("status") == "needs-speakers":
                    spk = front.get("speakers") or {}
                    if any(v is None or str(v).strip() == "" for v in spk.values()):
                        pending.append(path)
            except Exception:
                pass
    return sorted(pending)


def build_markdown(meta, ts, summary, transcript, audio_filename):
    token = meta["object_token"]
    date_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts / 1000))
    dur_ms = meta.get("duration") or 0
    dur = f"{int(dur_ms/60000)}分{int(dur_ms/1000)%60}秒" if dur_ms else "未知"
    base = {"title": meta.get("topic", ""), "date": date_str, "duration": dur,
            "source": f"https://meetings.feishu.cn/minutes/{token}"}
    speakers = detect_generic_speakers(transcript)
    lines = [
        render_frontmatter(base, speakers, transcript),
        "",
        f"# {meta.get('topic', '无标题')}",
        "",
        f"- 录制时间：{date_str}",
        f"- 时长：{dur}",
        f"- 飞书链接：https://meetings.feishu.cn/minutes/{token}",
    ]
    if audio_filename:
        lines.append(f"- 音频文件：[{audio_filename}](./{urllib.parse.quote(audio_filename)})")
    if speakers:
        lines += ["", f"> 📝 待标注：本转写有 {len(speakers)} 位说话人，"
                  "请在文件顶部 frontmatter 的 speakers 里填写真实姓名后保存。"]
    lines.append("")
    if summary:
        lines += ["## 智能纪要", "", summary, ""]
    else:
        lines += ["## 智能纪要", "", "_（未能通过接口获取，可在飞书链接中查看）_", ""]
    if transcript:
        lines += ["## 文字记录", "", transcript, ""]
    return "\n".join(lines)


def process_minute(api, cfg, meta):
    """处理一条妙记。成功返回 True；内容还没生成好返回 False（下轮重试）"""
    token = meta["object_token"]
    topic = meta.get("topic", "")
    status = api.get_status(token)

    video_info = status.get("video_info") or {}
    download_url = video_info.get("video_download_url")
    try:
        transcript = api.export_transcript(token)
    except PermissionError:
        # 列表已能拉取（cookie 有效），单条 403 = 该妙记不可导出（如系统教程），跳过
        raise MinuteSkip("无导出权限")
    if not transcript and not download_url:
        log(f"  [{topic}] 转写/音频尚未就绪，下轮再试")
        return False

    # 列表接口不返回时间，优先用转写首行里的录制时间，否则用当前时间
    ts = (meta.get("start_time") or meta.get("create_time")
          or parse_transcript_date(transcript) or time.time() * 1000)
    prefix = time.strftime("%Y-%m-%d_%H%M", time.localtime(ts / 1000))
    folder_name = f"{prefix}_{sanitize(topic)}"
    folder = os.path.join(os.path.expanduser(cfg["save_dir"]), folder_name)
    os.makedirs(folder, exist_ok=True)

    audio_filename = None
    if cfg["download_audio"] and download_url:
        fname = f"{sanitize(topic)}{guess_audio_ext(download_url)}"
        audio_path = os.path.join(folder, fname)
        if os.path.exists(audio_path):
            audio_filename = fname
        else:
            log(f"  [{topic}] 下载音频…")
            try:
                api.download_file(download_url, audio_path)
                audio_filename = fname
            except urllib.error.HTTPError as e:
                log(f"  [{topic}] 音频不可下载（{e.code}），仅保存文字记录")

    summary = api.fetch_summary(token)
    md = build_markdown(meta, ts, summary, transcript, audio_filename)
    md_path = os.path.join(folder, f"{sanitize(topic)}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    needs = detect_generic_speakers(transcript)
    log(f"  [{topic}] 已保存 → {md_path}"
        + (f"（待标注 {len(needs)} 位说话人）" if needs else ""))
    if needs:
        notify("新转写待标注说话人", f"{topic}：{len(needs)} 位，点开网页标注")
    else:
        notify("飞书妙记已同步", topic or folder_name)
    return md_path


def mirror_md_to_repo(save_dir, notes_dir):
    """把 save_dir 下的所有 .md 镜像到 notes_dir（保留子目录结构），返回是否有改动。"""
    changed = False
    for root, _dirs, files in os.walk(save_dir):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            rel = os.path.relpath(os.path.join(root, fn), save_dir)
            dst = os.path.join(notes_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            src_bytes = open(os.path.join(root, fn), "rb").read()
            if not os.path.exists(dst) or open(dst, "rb").read() != src_bytes:
                with open(dst, "wb") as f:
                    f.write(src_bytes)
                changed = True
    return changed


def git_push_notes(cfg):
    """把 .md 笔记镜像进专用克隆并提交推送到 world-feed（仅 Markdown）。无变化则跳过。"""
    if not cfg.get("git_sync", True):
        return
    repo = os.path.expanduser(cfg.get("git_repo_dir", ""))
    subdir = cfg.get("git_notes_subdir", "feishu-minutes")
    branch = cfg.get("git_branch", "main")
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        return
    save_dir = os.path.expanduser(cfg["save_dir"])
    notes_dir = os.path.join(repo, subdir)
    os.makedirs(notes_dir, exist_ok=True)

    def git(*args, check=True):
        return subprocess.run(["git", "-C", repo, *args],
                              capture_output=True, text=True, timeout=180,
                              check=check)
    try:
        mirror_md_to_repo(save_dir, notes_dir)
        # 只暂存笔记子目录（绝不碰仓库里的其它内容；音频被 .gitignore 排除）
        git("add", "-A", "--", subdir)
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M")
        git("commit", "-q", "-m", f"Sync 飞书妙记 notes {stamp}")
        # 先把远端新提交 rebase 进来（专用克隆，无外部未提交改动，rebase 安全）
        git("fetch", "-q", "origin", branch, check=False)
        git("rebase", "-q", f"origin/{branch}", check=False)
        push = git("push", "-q", "origin", f"HEAD:{branch}", check=False)
        if push.returncode == 0:
            log("  已推送新笔记到 world-feed")
        else:
            log(f"  Git 推送失败（已本地提交，下轮重试）：{push.stderr.strip()[:200]}")
    except Exception as e:
        log(f"  Git 同步出错（不影响本地保存）：{e!r}")


def backfill_audio(api, cfg, state):
    """给『有 .md 但缺音频』的转写补下载音频（首次同步时音频常常还没生成好）。
    永久拿不到的记进 state['no_audio'] 不再重试。返回补好的条数。"""
    import label_server  # 复用找音频逻辑
    save_dir = os.path.expanduser(cfg["save_dir"])
    no_audio = set(state.get("no_audio", []))
    fixed = 0
    for root, _d, files in os.walk(save_dir):
        mds = [f for f in files if f.endswith(".md")]
        if not mds or label_server._find_audio(root):
            continue
        md_path = os.path.join(root, mds[0])
        try:
            with open(md_path, encoding="utf-8") as f:
                front, _r, _b = split_frontmatter(f.read())
            token = (front.get("source", "").rsplit("/", 1)[-1]) if front else ""
            if not token or token in no_audio:
                continue
            status = api.get_status(token)
            url = (status.get("video_info") or {}).get("video_download_url")
            if not url:
                continue
            topic = front.get("title", os.path.basename(root))
            dest = os.path.join(root, f"{sanitize(topic)}{guess_audio_ext(url)}")
            log(f"  补下载音频 [{topic}]…")
            api.download_file(url, dest)
            fixed += 1
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                no_audio.add(token)
            log(f"  补音频失败 [{os.path.basename(root)}]：{e.code}")
        except Exception as e:
            log(f"  补音频出错 [{os.path.basename(root)}]：{e!r}")
    if no_audio != set(state.get("no_audio", [])):
        state["no_audio"] = sorted(no_audio)
        save_state(state)
    return fixed


def web_base_url(cfg):
    return f"http://127.0.0.1:{int(cfg.get('web_port', 8765))}"


def open_label_page(cfg, new_pending):
    """有新转写待标注时，在浏览器弹出标注页（单个直接进编辑页，多个进列表）。"""
    save_dir = os.path.expanduser(cfg["save_dir"])
    if len(new_pending) == 1:
        rel = os.path.relpath(new_pending[0], save_dir)
        url = f"{web_base_url(cfg)}/edit?f={urllib.parse.quote(rel)}"
    else:
        url = f"{web_base_url(cfg)}/"
    try:
        subprocess.run(["open", url], capture_output=True, timeout=10)
        log(f"  已弹出标注网页：{url}")
    except Exception as e:
        log(f"  打开标注网页失败：{e!r}（可手动访问 {web_base_url(cfg)}/）")


def run_once(api, cfg, state):
    minutes = api.list_minutes()
    log(f"云端共 {len(minutes)} 条妙记")

    # 首次运行：默认把已有的全部标记为已处理，只同步之后的新录音。
    # 想把历史录音也全部拉下来，把 config.json 里 sync_existing 设为 true。
    if not state["first_run_done"]:
        if not cfg.get("sync_existing", False):
            state["done_tokens"] = [m["object_token"] for m in minutes]
            log("首次运行：已跳过历史妙记（如需同步历史，设置 sync_existing=true 并删除 state.json）")
        state["first_run_done"] = True
        save_state(state)

    done = set(state["done_tokens"])
    todo = [m for m in minutes if m["object_token"] not in done]
    if todo:
        log(f"发现 {len(todo)} 条新妙记")
    saved_any = False
    new_pending = []  # 本轮新生成、需要标注说话人的文件
    for meta in todo:
        try:
            md_path = process_minute(api, cfg, meta)
            if md_path:
                state["done_tokens"].append(meta["object_token"])
                save_state(state)
                saved_any = True
                with open(md_path, encoding="utf-8") as f:
                    front, _r, _b = split_frontmatter(f.read())
                if front and front.get("status") == "needs-speakers":
                    new_pending.append(md_path)
        except MinuteSkip as e:
            log(f"  [{meta.get('topic','?')}] 跳过（{e}）")
            state["done_tokens"].append(meta["object_token"])
            save_state(state)
        except Exception as e:
            log(f"  [{meta.get('topic','?')}] 处理出错：{e!r}，下轮重试")

    # 给缺音频的转写补下载（首次同步时音频常还没生成）
    try:
        backfill_audio(api, cfg, state)
    except Exception as e:
        log(f"补音频环节出错：{e!r}")
    # 新转写先跑声纹识别，把高置信结果预填好，再弹网页
    for p in new_pending:
        try:
            voiceprint_recognize(cfg, p)
        except Exception as e:
            log(f"  声纹识别出错：{e!r}")
    # 有新转写待标注 → 自动弹出本地网页让你标注
    if new_pending and cfg.get("web_autopopup", True):
        open_label_page(cfg, new_pending)
    # 应用你已填好的说话人标注（改写正文 + 生成 participants）
    scan_and_apply_labels(cfg)
    # 本轮有新笔记/新标注，或上一轮提交后没推成功，都尝试推送
    git_push_notes(cfg)
    return saved_any


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"
    cfg = load_config()
    api = FeishuMinutes(cfg)

    if cmd == "probe":
        token = sys.argv[2]
        print(json.dumps(api.get_status(token), ensure_ascii=False, indent=2))
        return

    if cmd == "init-speakers":
        n = init_speakers(cfg)
        print(f"已为 {n} 个转写补上说话人标注块。")
        return

    if cmd == "pending":
        files = list_pending_labels(cfg)
        if not files:
            print("没有待标注的转写 🎉")
        else:
            print(f"以下 {len(files)} 个转写待标注说话人（编辑顶部 frontmatter 的 speakers）：")
            for p in files:
                print("  " + p)
        return

    if cmd == "label":
        # 打开本地标注网页（守护进程已在后台提供服务）
        files = list_pending_labels(cfg)
        url = web_base_url(cfg) + "/"
        if files:
            rel = os.path.relpath(files[0], os.path.expanduser(cfg["save_dir"]))
            url = f"{web_base_url(cfg)}/edit?f={urllib.parse.quote(rel)}"
        print(f"打开标注网页（待标注 {len(files)} 个）：{url}")
        # 若服务没起来（守护进程没跑），先本进程起一个再打开
        try:
            urllib.request.urlopen(web_base_url(cfg) + "/", timeout=2)
        except Exception:
            import label_server
            label_server.start_in_thread(cfg)
            time.sleep(1)
        subprocess.run(["open", url], capture_output=True)
        return

    if cmd == "apply":
        n = scan_and_apply_labels(cfg)
        git_push_notes(cfg)
        print(f"已应用 {n} 个转写的标注。" if n else "没有可应用的新标注。")
        return

    if cmd == "backfill-audio":
        state = load_state()
        n = backfill_audio(api, cfg, state)
        print(f"补下载了 {n} 个音频。" if n else "没有需要补的音频。")
        return

    if cmd == "serve":
        import label_server
        addr = label_server.start_in_thread(cfg)
        if not addr:
            return
        print(f"标注网页：http://{addr[0]}:{addr[1]}/   (Ctrl+C 退出)")
        subprocess.run(["open", web_base_url(cfg) + "/"], capture_output=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    # run / once：启动后台标注网页（端口被占用则跳过）
    if cfg.get("web_enabled", True):
        try:
            import label_server
            label_server.start_in_thread(cfg)
        except Exception as e:
            log(f"网页服务未启动：{e!r}")

    state = load_state()
    cookie_dead_notified = False
    while True:
        try:
            run_once(api, cfg, state)
            cookie_dead_notified = False
        except PermissionError:
            # Cookie 失效：尝试从浏览器重新读取最新登录态，自动恢复
            fresh = resolve_cookie(cfg)
            if fresh and fresh != cfg["cookie"]:
                cfg["cookie"] = fresh
                api = FeishuMinutes(cfg)
                log("已用浏览器中的最新登录态刷新 Cookie，继续运行。")
            else:
                log("cookie 已失效，且未能从浏览器获取到新的登录态。"
                    "请在 Edge/Chrome 重新登录 meetings.feishu.cn。")
                if not cookie_dead_notified:
                    notify("飞书妙记同步已暂停",
                           "登录态失效，请在浏览器重新登录 meetings.feishu.cn")
                    cookie_dead_notified = True
        except Exception as e:
            log(f"本轮检查失败：{e!r}")
        if cmd != "run":
            break
        time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    main()
