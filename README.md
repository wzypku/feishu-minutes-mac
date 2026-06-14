# 飞书妙记 → Mac 本地同步（含说话人标注 + 一键发送）

把**录音豆 / 飞书妙记**的录音，自动同步到你的 Mac：音频 + 文字记录存成 Markdown，
还能用本地网页给说话人标注真名、一键把会议记录发邮件给参与者。全程本地运行，不需要服务器。

> 适用于 macOS。整套东西就是几个 Python 脚本 + 一个后台服务 + 一个本地网页，
> 轻量、自包含、可随手分享。

---

## 安装（两步）

1. **双击 `install.command`**
   （首次可能被 Gatekeeper 拦：右键 → 打开，或到「系统设置 → 隐私与安全性」点「仍要打开」。）
   它会自动：装好 Python 依赖、生成配置、安装常驻后台服务、做一个桌面 App、跑一次测试同步。

2. **用 Edge 或 Chrome 登录** https://meetings.feishu.cn/minutes/me
   （程序直接读你浏览器里的飞书登录态，不用手动复制 Cookie，也不存任何密码。）

完成。之后用录音豆录音、上传到飞书妙记，约 1 分钟内会自动同步到本地
`~/Documents/FeishuMinutes/`。

---

## 用法

- **看笔记**：`~/Documents/FeishuMinutes/`，每条录音一个文件夹（`日期_标题/`），
  里面是 `标题.md`（带文字记录）+ 音频文件。
- **标注说话人 / 控制台**：双击 **`飞书妙记.app`**，或浏览器打开 http://127.0.0.1:8765/
  - 新转写需要标注说话人时会**自动弹出网页**
  - 网页里能**播放录音**边听边认、看每人**发言示例**、**下拉选历史联系人**自动带出邮箱
  - 填好保存，自动把「说话人 1」改写成真名，并生成可解析的 `participants`
- **发邮件**：在已标注的转写页点「发送给参与者」，用你 Mac 上的 **Microsoft Outlook**
  发出（默认先出草稿让你确认；想直接发，把 `config.json` 的 `email_auto_send` 设为 `true`）。

---

## 可选：跨设备同步（Git）

想在多台设备看笔记，可把 `.md` 推到一个你自己的 Git 仓库：
编辑 `config.json`：
```json
"git_sync": true,
"git_repo_dir": "/你的/某个仓库克隆路径",
"git_notes_subdir": "feishu-minutes",
"git_branch": "main"
```
程序只会把 `.md` 笔记 push 到该仓库的 `feishu-minutes/` 子目录（音频不上传，只留本地），
且只动这个子目录、不碰你仓库里的其它内容。

---

## 配置项（`config.json`）

| 字段 | 说明 | 默认 |
|---|---|---|
| `save_dir` | 笔记/音频保存目录 | `~/Documents/FeishuMinutes` |
| `poll_interval_seconds` | 检查飞书的间隔（秒） | `60` |
| `web_port` | 本地网页端口 | `8765` |
| `web_autopopup` | 新转写自动弹标注网页 | `true` |
| `download_audio` | 是否下载音频 | `true` |
| `git_sync` / `git_repo_dir` | 跨设备 Git 同步 | 关 |
| `email_auto_send` | 发邮件时直接发（否则出草稿） | `false` |

---

## 可选增强：声纹自动识别

标注过的人，下次开会能被**自动认出来**：网页会预填名字并标出置信度，你确认即可。

启用：双击 **`install_voiceprint.command`**（需要 `ffmpeg`：`brew install ffmpeg`）。
它会建一个独立 Python 环境装 `sherpa-onnx`（**不需要 torch**）并下载一个声纹模型（~27MB）。

原理：
- 你每标注一个人，程序就按 SRT 时间戳切出 TA 的语音、提取声纹存进 `voiceprints.json`（**仅本地**）
- 新会议来时，对每个说话人提取声纹、和库里比对；**只有足够高置信**（且明显高于第二名）才会预填，
  认不准就留空让你手填，**不会乱认**
- 多次标注同一个人会自动平均、越用越准

实测：从一场会注册的声纹，在另一场会里正确认出本人（~0.88），认错的人只有 ~0.35–0.47，区分明显。

关掉：`config.json` 里 `voiceprint_enabled` 设为 `false`，或不安装这个增强即可（不影响其它功能）。

## 卸载

双击 `uninstall.command`（会停掉后台服务，保留你已同步的笔记和音频）。

---

## 常见问题 / 排错

- **双击 `install.command` 被拦（"无法打开，因为来自身份不明的开发者"）**
  右键点它 → 选「打开」→ 再点「打开」。这是 macOS 对下载文件的安全提示，只需第一次。
  （安装脚本会自动解除随包其它文件的隔离，所以只用对它本身放行一次。）
- **没自动同步？** 确认浏览器（Edge/Chrome）里登录着飞书妙记；看日志
  `tail -f ~/<安装目录>/launchd.out.log`。
- **网页打不开？** 后台服务可能没起：`launchctl load ~/Library/LaunchAgents/com.feishu.minutes-sync.plist`。
- **发邮件按钮没反应？** 需要本机装并登录 Microsoft Outlook。没有 Outlook 就用不了发送，
  但同步和标注不受影响。
- **第一次跑 `python3` 弹"安装命令行工具"** 点安装即可（macOS 自带 Python 需要它）。

---

## License

MIT，见 `LICENSE`。自由分享、修改、二次分发。

---

## 它是怎么工作的（给好奇的人）

- 通过妙记网页接口（`meetings.feishu.cn/minutes/api`）+ 浏览器里的登录态，列出妙记、
  取音频下载地址、导出文字记录。
- 一个 `launchd` 后台服务每 60 秒轮询；新内容就下载、生成 Markdown。
- 一个本地 `http.server` 提供标注网页（只监听 `127.0.0.1`，仅本机可访问）。
- 发邮件走本机已登录的 Outlook（AppleScript），不存任何邮箱密码。

依赖：macOS 自带 Python3 + `pyyaml`、`cryptography`（安装脚本会自动装）。
