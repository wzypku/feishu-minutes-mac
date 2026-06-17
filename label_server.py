#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地说话人标注网页。

在浏览器里给每个『说话人 N』填名字+邮箱（可边听录音边辨认），点保存即可：
会写回 .md 的 speakers，自动改写正文、生成 participants，并推送到 world-feed。

只监听 127.0.0.1，仅本机可访问。
"""

import html
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

import feishu_minutes_sync as fms

AUDIO_EXTS = (".mp4", ".m4a", ".mp3", ".aac", ".wav")
AUDIO_MIME = {".mp4": "video/mp4", ".m4a": "audio/mp4", ".mp3": "audio/mpeg",
              ".aac": "audio/aac", ".wav": "audio/wav"}

PAGE_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, "PingFang SC", sans-serif; margin: 0;
  background: #f5f6f8; color: #1f2329; }
.wrap { max-width: 760px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 20px; margin: 8px 0 4px; }
.sub { color: #8f959e; font-size: 13px; margin-bottom: 20px; }
.card { background: #fff; border-radius: 12px; padding: 16px 18px; margin: 14px 0;
  box-shadow: 0 1px 3px rgba(0,0,0,.06); }
.spk { font-weight: 600; color: #3370ff; margin-bottom: 8px; }
.q { color: #646a73; font-size: 13px; line-height: 1.7; margin: 2px 0;
  padding-left: 10px; border-left: 3px solid #e5e6eb; }
.row { display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
.row input { flex: 1 1 200px; padding: 9px 11px; border: 1px solid #d7d9de;
  border-radius: 8px; font-size: 15px; }
label.fld { font-size: 12px; color: #8f959e; display:block; margin-bottom:3px; }
.col { flex: 1 1 200px; }
button { background: #3370ff; color: #fff; border: 0; border-radius: 8px;
  padding: 12px 22px; font-size: 15px; font-weight: 600; cursor: pointer; }
button.ghost { background:#fff; color:#3370ff; border:1px solid #d7d9de; }
audio { width: 100%; margin: 6px 0 2px; }
a { color: #3370ff; text-decoration: none; }
.bar { position: sticky; bottom: 0; background: #f5f6f8; padding: 14px 0;
  display:flex; gap:12px; }
.item { display:flex; justify-content:space-between; align-items:center;
  padding:12px 0; border-bottom:1px solid #eee; }
.entry { display:flex; align-items:center; gap:10px; padding:12px 4px;
  border-bottom:1px solid #eee; color:#1f2329; }
.entry:last-child { border-bottom:0; }
.entrylink { display:block; flex:1; min-width:0; color:#1f2329; }
.hidebtn { background:#fff; color:#8f959e; border:1px solid #d7d9de;
  border-radius:8px; padding:6px 10px; font-size:12px; cursor:pointer;
  white-space:nowrap; flex:none; }
.hidebtn:hover { color:#d4380d; border-color:#d4380d; }
.etop { display:flex; justify-content:space-between; align-items:center; }
.etitle { font-weight:600; color:#1f2329; }
.tag { font-size:12px; padding:2px 8px; border-radius:10px; }
.tag.need { background:#fff3e0; color:#d46b08; }
.tag.done { background:#e7f9ec; color:#13a452; }
.tag.sent { background:#e8f0fe; color:#3370ff; margin-right:6px; }
.hint { background:#eef4ff; color:#3370ff; padding:10px 12px; border-radius:8px;
  font-size:13px; margin-bottom:14px; }
"""


def _safe_path(save_dir, rel):
    base = os.path.realpath(save_dir)
    full = os.path.realpath(os.path.join(base, rel))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("非法路径")
    return full


def _find_audio(folder):
    try:
        for fn in sorted(os.listdir(folder)):
            if os.path.splitext(fn)[1].lower() in AUDIO_EXTS:
                return os.path.join(folder, fn)
    except OSError:
        pass
    return None


def _page(title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head>"
            f"<body><div class='wrap'>{body}</div></body></html>").encode("utf-8")


def make_handler(cfg):
    save_dir = os.path.expanduser(cfg["save_dir"])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        # ---------- 列表页 ----------
        def page_index(self, show_hidden=False):
            pending, labeled = [], []
            hidden_count = 0
            for root, _d, files in os.walk(save_dir):
                for fn in files:
                    if not fn.endswith(".md"):
                        continue
                    path = os.path.join(root, fn)
                    try:
                        with open(path, encoding="utf-8") as f:
                            front, _r, body = fms.split_frontmatter(f.read())
                    except Exception:
                        continue
                    if not front:
                        continue
                    is_hidden = bool(front.get("hidden"))
                    if is_hidden:
                        hidden_count += 1
                        if not show_hidden:
                            continue  # 隐藏的：默认不出现在列表里
                    rel = os.path.relpath(path, save_dir)
                    info = {"title": front.get("title", fn), "rel": rel,
                            "date": str(front.get("date", "")),
                            "duration": str(front.get("duration", "")),
                            "kw": fms.transcript_keywords(body),
                            "parts": front.get("participants") or [],
                            "hidden": is_hidden,
                            "sent_at": str(front.get("sent_at", ""))}
                    # 待标注 = 有未填的说话人；其余（含单人录音）都归"已处理"，全部显示
                    st = front.get("status")
                    is_pending = False
                    if st == "needs-speakers":
                        spk = front.get("speakers") or {}
                        is_pending = (not spk) or any(
                            v is None or str(v).strip() == "" for v in spk.values())
                    (pending if is_pending else labeled).append(info)
            pending.sort(key=lambda x: x["date"], reverse=True)
            labeled.sort(key=lambda x: x["date"], reverse=True)

            def card(info, tag_cls, tag_txt):
                meta = " · ".join(x for x in [info["date"], info["duration"]] if x)
                kw = (f"<div class='q' style='border:0;padding:0;margin-top:4px'>🔖 "
                      f"{html.escape(info['kw'])}</div>") if info["kw"] else ""
                ppl = ""
                if info["parts"]:
                    names = "、".join(fms.speaker_name(str(p)) for p in info["parts"])
                    ppl = (f"<div class='q' style='border:0;padding:0;margin-top:2px'>👥 "
                           f"{html.escape(names)}</div>")
                q = urllib.parse.quote(info["rel"])
                # data-rel 存原始路径（HTML 转义即可），JS 里用 encodeURIComponent 单次编码，
                # 避免 onclick 内联字符串里的引号/编码问题
                drel = html.escape(info["rel"], quote=True)
                if info.get("hidden"):
                    btn = (f"<button type='button' class='hidebtn' data-rel=\"{drel}\" "
                           f"data-act='unhide'>↩︎ 取消隐藏</button>")
                else:
                    btn = (f"<button type='button' class='hidebtn' data-rel=\"{drel}\" "
                           f"data-act='hide'>🙈 隐藏</button>")
                sent = ""
                if info.get("sent_at"):
                    sent = (f"<span class='tag sent' title='发送于 {html.escape(info['sent_at'])}'>"
                            f"✉️ 已发送</span>")
                return (f"<div class='entry'>"
                        f"<a class='entrylink' href='/edit?f={q}'>"
                        f"<div class='etop'><span class='etitle'>{html.escape(info['title'])}</span>"
                        f"<span style='flex:none'>{sent}<span class='tag {tag_cls}'>{tag_txt}</span></span></div>"
                        f"<div class='sub' style='margin:2px 0 0'>🕒 {html.escape(meta)}</div>"
                        f"{kw}{ppl}</a>{btn}</div>")

            rows = ""
            if not pending:
                rows += "<div class='card'>🎉 没有待标注的转写</div>"
            else:
                rows += "<div class='card'>" + "".join(
                    card(i, "need", "待标注") for i in pending) + "</div>"
            if labeled:
                rows += "<div class='sub' style='margin-top:18px'>已标注</div>"
                rows += "<div class='card'>" + "".join(
                    card(i, "done", "已标注") for i in labeled[:60]) + "</div>"

            toggle = ""
            if hidden_count:
                if show_hidden:
                    toggle = "<a href='/'>← 隐藏已隐藏项</a>"
                else:
                    toggle = f"<a href='/?hidden=1'>👁 显示已隐藏（{hidden_count}）</a>"
            js = ("<script>document.addEventListener('click',function(e){"
                  "var b=e.target.closest('.hidebtn');if(!b)return;"
                  "e.preventDefault();e.stopPropagation();b.disabled=true;b.textContent='…';"
                  "fetch('/hide',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},"
                  "body:'f='+encodeURIComponent(b.dataset.rel)+'&act='+b.dataset.act})"
                  ".then(function(r){if(!r.ok)throw 0;location.reload();})"
                  ".catch(function(){b.disabled=false;b.textContent='✗ 失败，重试';});});</script>")
            body = (f"<h1>飞书妙记 · 说话人标注</h1>"
                    f"<div class='sub'>待标注 {len(pending)} 个 · 已标注 {len(labeled)} 个"
                    + (f" · 已隐藏 {hidden_count} 个" if hidden_count else "")
                    + f"</div>{rows}"
                    + (f"<div class='bar'>{toggle}</div>" if toggle else "") + js)
            self._send(200, _page("说话人标注", body))

        # ---------- 编辑页 ----------
        def page_edit(self, rel):
            path = _safe_path(save_dir, rel)
            with open(path, encoding="utf-8") as f:
                front, _raw, body = fms.split_frontmatter(f.read())
            if not front:
                self._send(404, _page("出错", "<h1>无法解析该文件</h1>"))
                return
            title = front.get("title", os.path.basename(path))
            meta = " · ".join(x for x in [str(front.get("date", "")),
                                          str(front.get("duration", ""))] if x)
            kw = fms.transcript_keywords(body)
            head = (f"<h1>{html.escape(title)}</h1>"
                    f"<div class='sub'>🕒 {html.escape(meta)}</div>"
                    + (f"<div class='sub'>🔖 {html.escape(kw)}</div>" if kw else ""))
            spk = front.get("speakers") or {}
            labels = list(spk.keys())

            if front.get("status") == "labeled":
                parts = front.get("participants") or []
                items = "".join(f"<div class='q'>{html.escape(str(p))}</div>" for p in parts)
                emails = [fms.parse_participant(str(p)) for p in parts]
                to_names = [n for n, e in emails if e]
                has_email = bool(to_names)
                auto = bool(cfg.get("email_auto_send", False))
                send_btn = ""
                sent_at = front.get("sent_at", "")
                if has_email:
                    if auto:
                        btxt = (f"✉️ 重新发送给 {len(to_names)} 位参与者" if sent_at
                                else f"✉️ 直接发送给 {len(to_names)} 位参与者")
                        confirm = ("onsubmit=\"return confirm('确定发送给："
                                   + "、".join(to_names) + "？发送后无法撤回。')\"")
                    else:
                        btxt = "✉️ 重新打开草稿" if sent_at else "✉️ 用 Outlook 打开草稿"
                        confirm = ""
                    send_btn = (f"<form method='POST' action='/send' style='display:inline' "
                                f"{confirm}>"
                                f"<input type='hidden' name='f' value='{html.escape(rel)}'>"
                                f"<button type='submit'>{btxt}</button></form>")
                else:
                    send_btn = "<div class='sub'>（参与者都没填邮箱，无法发送）</div>"
                sent_banner = ""
                if sent_at:
                    sent_to = front.get("sent_to") or []
                    sent_banner = (
                        f"<div class='hint' style='background:#e8f0fe;color:#3370ff'>"
                        f"✉️ 已于 <b>{html.escape(str(sent_at))}</b> 发送给："
                        + "、".join(html.escape(str(s)) for s in sent_to) + "</div>")
                body_html = (
                    f"{head}{sent_banner}"
                    f"<div class='hint'>这条已标注。participants（可直接用于发送）：</div>"
                    f"<div class='card'>{items or '（无）'}</div>"
                    f"<div class='bar'>{send_btn} "
                    f"<a href='/' style='align-self:center'>返回列表</a></div>")
                self._send(200, _page(title, body_html))
                return

            import json as _json
            import re as _re

            # 每个说话人的语音时间段（用于"只听 TA"），来自缓存的 SRT
            allsegs = {}
            try:
                srt = fms.get_cached_srt(cfg, path)
                if srt:
                    allsegs = fms.srt_speaker_segments(srt)
            except Exception:
                pass

            def pick_segs(label, cap=30.0):
                picked, tot = [], 0.0
                for s, e in allsegs.get(label, []):
                    if e <= s:
                        continue
                    picked.append([round(s, 2), round(e, 2)])
                    tot += e - s
                    if tot >= cap:
                        break
                return picked

            audio = _find_audio(os.path.dirname(path))
            audio_html = ""
            if audio:
                arel = os.path.relpath(audio, save_dir)
                audio_html = (f"<div class='card'><div class='spk'>🎧 边听边认</div>"
                              f"<audio id='player' controls preload='none' "
                              f"src='/audio?f={urllib.parse.quote(arel)}'></audio>"
                              f"<div class='sub'>每位说话人下方有「▶ 只听 TA」，"
                              f"点了只播这个人的话、自动跳过别人</div></div>")

            # 通讯录：下拉选择，选名字自动带出邮箱
            contacts = fms.load_contacts()
            options = "".join(f"<option value='{html.escape(c.get('name',''))}'>"
                              for c in contacts)
            cmap = _json.dumps({c.get("name", ""): c.get("email", "") for c in contacts},
                               ensure_ascii=False)

            vp = front.get("voiceprint") or {}  # 声纹识别预填
            seg_js = []  # 按卡片序号存每人片段
            cards = ""
            for i, label in enumerate(labels):
                cur = "" if spk.get(label) is None else str(spk.get(label))
                cur_name = fms.speaker_name(cur) if cur else ""
                me = _re.search(r"<(.+?)>", cur)
                cur_email = me.group(1).strip() if me else ""
                vp_hint = ""
                sug = vp.get(label)
                if sug and not cur_name:  # 没手填时用声纹建议预填
                    cur_name = sug.get("name", "")
                    cur_email = sug.get("email", "") or cur_email
                if sug and sug.get("uncertain"):
                    badge = "<span class='tag need'>🔊 声纹·待确认</span>"
                    alt = sug.get("alt", "")
                    vp_hint = (f"<div class='q' style='border-color:#d46b08;color:#d46b08'>"
                               f"🔊 声纹：可能是 <b>{html.escape(sug.get('name',''))}</b>"
                               f"（{sug.get('score','')}）"
                               + (f"，也接近 {html.escape(alt)}" if alt else "")
                               + "，已暂填，<b>请确认</b></div>")
                elif sug:
                    badge = "<span class='tag done'>🔊 声纹已认</span>"
                    vp_hint = (f"<div class='q' style='border-color:#3370ff;color:#3370ff'>"
                               f"🔊 声纹识别：很可能是 <b>{html.escape(sug.get('name',''))}</b>"
                               f"（置信度 {sug.get('score','')}）— 已自动填入，确认或改正即可</div>")
                elif not cur_name:
                    badge = "<span class='tag need'>❓ 待你确认</span>"
                else:
                    badge = ""

                segs = pick_segs(label)
                seg_js.append(segs)
                play_btn = ""
                if segs and audio:
                    n = len(allsegs.get(label, []))
                    play_btn = (f"<button type='button' class='ghost' "
                                f"style='padding:6px 12px;font-size:13px;margin:6px 0' "
                                f"onclick='playSpk({i})'>▶ 只听 {html.escape(label)}（{n} 段）</button>")
                quotes = fms.sample_quotes_for(body, label, n=3)
                qhtml = "".join(f"<div class='q'>“{html.escape(q)}”</div>" for q in quotes)
                cards += (
                    f"<div class='card'><div class='spk'>{html.escape(label)} {badge}</div>"
                    f"{vp_hint}{play_btn}{qhtml}"
                    f"<input type='hidden' name='label{i}' value='{html.escape(label)}'>"
                    f"<div class='row'>"
                    f"<div class='col'><label class='fld'>名字（可下拉选历史联系人）</label>"
                    f"<input name='name{i}' value='{html.escape(cur_name)}' list='contacts' "
                    f"data-i='{i}' oninput='fillEmail(this)' placeholder='如 张三' "
                    f"autocomplete='off'></div>"
                    f"<div class='col'><label class='fld'>邮箱（可选）</label>"
                    f"<input name='email{i}' id='email{i}' value='{html.escape(cur_email)}' "
                    f"placeholder='zhangsan@example.com' autocomplete='off'></div>"
                    f"</div></div>")

            js = (f"<datalist id='contacts'>{options}</datalist>"
                  f"<script>const CMAP={cmap};const SEGS={_json.dumps(seg_js)};"
                  f"function fillEmail(inp){{const e=document.getElementById('email'+inp.dataset.i);"
                  f"if(CMAP[inp.value]&&!e.value){{e.value=CMAP[inp.value];}}}}"
                  f"let _q=null,_qi=0;const _A=()=>document.getElementById('player');"
                  f"function _step(){{const a=_A();if(!a||!_q||_qi>=_q.length){{if(a)a.pause();return;}}"
                  f"a.currentTime=_q[_qi][0];a.play();"
                  f"a.ontimeupdate=function(){{if(a.currentTime>=_q[_qi][1]){{_qi++;"
                  f"if(_q&&_qi<_q.length){{a.currentTime=_q[_qi][0];}}else{{a.pause();a.ontimeupdate=null;}}}}}};}}"
                  f"function playSpk(i){{_q=SEGS[i]||[];_qi=0;if(_q.length)_step();}}</script>")

            body_html = (
                f"{head}"
                f"<div class='hint'>给每位说话人填上名字（邮箱可选）。名字框可下拉选以前填过的人，"
                f"自动带出邮箱。保存后会自动改写转写、生成 participants 并同步。</div>"
                f"{audio_html}"
                f"<form method='POST' action='/save'>"
                f"<input type='hidden' name='f' value='{html.escape(rel)}'>"
                f"{cards}"
                f"<div class='bar'><button type='submit'>保存并应用</button>"
                f"<a href='/' style='align-self:center'>返回列表</a></div>"
                f"</form>{js}")
            self._send(200, _page(title, body_html))

        # ---------- 保存 ----------
        def do_save(self, form):
            rel = form.get("f", [""])[0]
            path = _safe_path(save_dir, rel)
            with open(path, encoding="utf-8") as f:
                front, _raw, body = fms.split_frontmatter(f.read())
            spk = front.get("speakers") or {}
            i = 0
            while f"label{i}" in form:
                label = form[f"label{i}"][0]
                name = form.get(f"name{i}", [""])[0].strip()
                email = form.get(f"email{i}", [""])[0].strip()
                if name:
                    spk[label] = name + (f" <{email}>" if email else "")
                i += 1
            front["speakers"] = spk
            new_fm = yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip()
            with open(path, "w", encoding="utf-8") as f:
                f.write("---\n" + new_fm + "\n---\n" + body)
            result = fms.apply_speaker_labels(path)
            if result == "applied":
                try:
                    fms.voiceprint_enroll(cfg, path)  # 存声纹，下次自动认
                except Exception:
                    pass
            try:
                fms.git_push_notes(cfg)
            except Exception:
                pass
            if result == "applied":
                msg = ("✅ 已标注完成并同步到 world-feed。",
                       "正文里的说话人已替换成真名，participants 已生成。")
            else:
                msg = ("💾 已保存。", "还有说话人没填名字，填完再保存即可自动应用。")
            body_html = (f"<h1>{msg[0]}</h1><div class='sub'>{msg[1]}</div>"
                         f"<div class='bar'><a href='/'>← 回到列表，继续标注下一个</a></div>"
                         f"<script>setTimeout(function(){{location.href='/'}},1400)</script>")
            self._send(200, _page("已保存", body_html))

        # ---------- 发送邮件 ----------
        def do_send(self, form):
            rel = form.get("f", [""])[0]
            path = _safe_path(save_dir, rel)
            auto = bool(cfg.get("email_auto_send", False))
            res = fms.compose_email(cfg, path, auto_send=auto)
            if not res.get("ok"):
                extra = ""
                if res.get("no_email"):
                    extra = "<div class='sub'>未填邮箱：" + "、".join(
                        html.escape(n) for n in res["no_email"]) + "</div>"
                body_html = (f"<h1>⚠️ 无法发送</h1>"
                             f"<div class='sub'>{html.escape(res.get('error',''))}</div>{extra}"
                             f"<div class='bar'><a href='/edit?f={urllib.parse.quote(rel)}'>"
                             f"← 返回</a></div>")
                self._send(200, _page("发送", body_html))
                return
            # 真正发出去（auto_send）才标记已发送；只生成草稿不算
            if res["mode"] == "sent":
                try:
                    fms.mark_sent(path, res["to"])
                except Exception:
                    pass
            to_html = "".join(f"<div class='q'>{html.escape(n)} &lt;{html.escape(e)}&gt;</div>"
                              for n, e in res["to"])
            noemail = ""
            if res.get("no_email"):
                noemail = ("<div class='sub' style='margin-top:8px'>以下参与者没填邮箱、未加入收件人："
                           + "、".join(html.escape(n) for n in res["no_email"]) + "</div>")
            if res["mode"] == "sent":
                lead = "✅ 已通过 Outlook 发送给："
            else:
                lead = "✉️ 已在 Outlook 打开草稿（请检查后点发送），收件人："
            body_html = (f"<h1>{lead.split('，')[0]}</h1>"
                         f"<div class='hint'>{lead}</div><div class='card'>{to_html}</div>"
                         f"{noemail}"
                         f"<div class='bar'><a href='/'>← 返回列表</a></div>")
            self._send(200, _page("发送", body_html))

        # ---------- 隐藏/取消隐藏 ----------
        def do_hide(self, form):
            rel = form.get("f", [""])[0]
            act = form.get("act", ["hide"])[0]
            path = _safe_path(save_dir, rel)
            with open(path, encoding="utf-8") as f:
                front, _raw, body = fms.split_frontmatter(f.read())
            if front is None:
                self._send(404, b"bad", "text/plain")
                return
            if act == "unhide":
                front.pop("hidden", None)
            else:
                front["hidden"] = True
            new_fm = yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip()
            with open(path, "w", encoding="utf-8") as f:
                f.write("---\n" + new_fm + "\n---\n" + body)
            self._send(200, b"ok", "text/plain")

        # ---------- 音频 ----------
        def serve_audio(self, rel):
            path = _safe_path(save_dir, rel)
            if not os.path.exists(path):
                self._send(404, b"not found", "text/plain")
                return
            ext = os.path.splitext(path)[1].lower()
            with open(path, "rb") as f:
                data = f.read()
            self._send(200, data, AUDIO_MIME.get(ext, "application/octet-stream"))

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            try:
                if u.path == "/":
                    self.page_index(show_hidden=bool(q.get("hidden")))
                elif u.path == "/edit":
                    self.page_edit(q.get("f", [""])[0])
                elif u.path == "/audio":
                    self.serve_audio(q.get("f", [""])[0])
                else:
                    self._send(404, _page("404", "<h1>404</h1>"))
            except Exception as e:
                self._send(500, _page("出错", f"<h1>出错</h1><pre>{html.escape(repr(e))}</pre>"))

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8")
            form = urllib.parse.parse_qs(raw, keep_blank_values=True)
            try:
                p = urllib.parse.urlparse(self.path).path
                if p == "/save":
                    self.do_save(form)
                elif p == "/send":
                    self.do_send(form)
                elif p == "/hide":
                    self.do_hide(form)
                else:
                    self._send(404, _page("404", "<h1>404</h1>"))
            except Exception as e:
                self._send(500, _page("出错", f"<h1>出错</h1><pre>{html.escape(repr(e))}</pre>"))

    return Handler


_server = None


def start_in_thread(cfg):
    """在后台线程启动网页服务。返回 (host, port) 或 None。"""
    global _server
    if _server is not None:
        return _server.server_address
    try:
        fms.seed_contacts_from_files(cfg)  # 启动时收集历史联系人，供下拉
    except Exception:
        pass
    port = int(cfg.get("web_port", 8765))
    try:
        _server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg))
    except OSError as e:
        fms.log(f"网页服务启动失败（端口 {port} 被占用？）：{e}")
        return None
    t = threading.Thread(target=_server.serve_forever, daemon=True)
    t.start()
    fms.log(f"说话人标注网页已启动：http://127.0.0.1:{port}/")
    return ("127.0.0.1", port)


if __name__ == "__main__":
    cfg = fms.load_config()
    addr = start_in_thread(cfg)
    if addr:
        print(f"http://{addr[0]}:{addr[1]}/  (Ctrl+C 退出)")
        import time
        while True:
            time.sleep(3600)
