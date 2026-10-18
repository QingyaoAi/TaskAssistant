# 个人助理 v0.1

把微信截图和邮件里的事收集成 Microsoft To Do 任务，每天早上给你一份简报。基于 DeepSeek Harness（dsh）。

**所有自动建的任务都进 Inbox-ToDo**，由你自己分类：拖到对应清单 = 接受，删掉 = 不是任务。每条任务的备注第一行写着建议的清单，供参考。

## 安装

需要：

- macOS（launchd、iCloud 云盘、快捷指令）和 Xcode 命令行工具（编译启动器用 `swiftc`）；
- [uv](https://docs.astral.sh/uv/)，装在 `~/.local/bin/uv`；
- DeepSeek Harness（`dsh`，装在 `/opt/homebrew/bin/dsh`）和 DeepSeek API key；
- Microsoft To Do 的 MCP 服务器（默认 `/Applications/To Do MCP.app/Contents/MacOS/mstodo-mcp`）；
- 邮件作业还需要 Thunderbird 和它的 MCP 桥（默认 `~/.claude/skills/thunderbird-mail/mail-mcp-bridge.js`）。

这些路径写在 `install.sh` 开头和 `core/src/assistant_core/config.py` 里，装在别处就改那里。然后：

```bash
./install.sh
```

第一次运行会从示例生成 `config.json` 和 `prefs.md`。这两个文件只存在本机，不进代码库：

- `config.json`：在 `mail.sources` 里填 Thunderbird 的邮箱文件夹，在 `mail.self_addresses` 里填你自己的邮箱；
- `prefs.md`：在「我是谁」里写别人怎么称呼你、你有哪些邮箱，再按需要改清单分工。

## 手机上怎么用

导入快捷指令：`install.sh` 会在 `phone/dist/` 里生成并签名快捷指令。在 Mac 上双击其中的文件导入，iCloud 会同步到 iPhone；也可以用 AirDrop 发到手机上导入。已经装了旧版（2026-10-18 15:40 以前生成的）也不用重装：旧版把文件存成「个人助理/inbox.png」「inbox.txt」，Mac 端照样认得——图片当截图，紧跟着的文字当补充。

**「记待办」运行时会截下当前屏幕。** 所以要在微信还开着的时候运行它，用下面这些不离开当前 app 的方式之一：

- 「嘿 Siri，记待办」；
- 操作按钮（iPhone 15 Pro 及以后）：设置 → 操作按钮 → 快捷指令 → 记待办；
- 辅助触控悬浮按钮：设置 → 辅助功能 → 触控 → 辅助触控 → 自定操作 → 轻点一下 → 记待办；
- iOS 18 控制中心：添加「记待办」按钮（截图时机需实测）。

在主屏幕点图标或在快捷指令 app 里运行，截到的会是主屏幕；这种截图会被记为失败，第二天的简报里会提醒。

| 你要做的 | 怎么做 |
|---|---|
| 看到一条要办的微信 | 在微信界面运行「记待办」（截屏，震一下表示收到） |
| 长对话 | 滑一屏，再运行一次，重复；1 分钟内的连续截图会合成一条理解 |
| 想补一句话或附上复制的文字 | 运行「记待办＋」：截屏后选「口述一句 / 打字一句 / 附上剪贴板文字」 |
| 已经用按键截了图 | 点左下角的截图缩略图 → 分享 → 记待办；相册里的截图、别的 app 里的文字或链接也这样分享 |
| 不方便动手 | 「嘿 Siri，口述待办」 |
| Mac 上的微信 | 在「快捷指令」里给「记待办 (Mac)」设一个键盘快捷键；需要在快捷指令设置里允许运行脚本 |

截图存在 iCloud 云盘「快捷指令/个人助理/inbox」；Mac 醒着时一般 1–3 分钟后出现在 Inbox-ToDo。Mac 合盖期间会排队，醒来后处理。

## 每天会发生什么

| 作业 | 时间 | 做什么 |
|---|---|---|
| inbox | 收件箱有新文件就运行，另每 10 分钟兜底 | 截图、文字、口述 → 任务 |
| mail | 工作日 8–22 点每 30 分钟；周末每 2 小时 | 只看你在 Thunderbird 里加了星标或打了标签（重要、工作、待办等）的邮件，最近 14 天，每个会话看最新一封 → 任务 |
| morning | 每天 07:00 | 生成简报：Assistant 清单里的「☀️ 今日简报」，07:30 提醒 |
| maintain | 每天 04:30 | 重试没写进 To Do 的任务、清理过期文件、备份账本 |

邮件作业需要 Thunderbird 开着；没开时会跳过，简报里会提示。星标常常是后来才加的，所以每次都看最近 14 天（`config.json` 里的 `mail.lookback_days`），处理过的不会重复处理；最近两周已经建过的任务（包括从截图建的）也不会再建一次。

**提醒**：原文写明了截止日的任务、以及会议活动，会在 To Do 里设「截止前 2 天早上 9 点」的提醒，手机上的 To Do 会弹通知；不到 2 天的，10 分钟后提醒。截止日是推测出来的不设提醒。每天的简报里还有「未来 2 天到期」一栏，所有清单都算在内。提前几天、几点提醒可以在 `config.json` 里加 `"reminders": {"lead_days": 2, "hour": 9}` 修改。

## 常用操作

```bash
touch PAUSE            # 暂停所有作业；rm PAUSE 恢复
core/.venv/bin/assistant-core status          # 最近一周的情况
/usr/bin/python3 jobs/launchd.py status       # 后台作业加载状态
~/Applications/TaskAssistant.app/Contents/MacOS/TaskAssistant inbox --force   # 立刻跑一次（mail / morning 同理）
/usr/bin/python3 jobs/launchd.py uninstall    # 卸载后台作业（数据保留）
./install.sh                                  # 安装或更新（可重复运行）
```

- **准确率评测**：把真实截图放进 `eval/cases/`，在 `eval/labels.csv` 里标出正确答案，运行 `core/.venv/bin/python eval/run_eval.py`，可以看看识别和建议清单准不准。
- **直投（默认关闭）**：如果以后想让把握大的任务直接进对应清单，在 `config.json` 的 `"direct"` 里写清单名即可；现在是空的，所以全部进 Inbox-ToDo。
- **改判断规则**：清单分工和重点联系人写在 `prefs.md`，助理的工作手册在 `AGENTS.md`，两个作业的具体做法在 `.dsh/skills/`。改完下一次作业就生效。
- **换了 DeepSeek key**：在 dsh 网页「设置 → 模型」里改好，再运行 `/usr/bin/python3 jobs/import_secrets.py`。

## 安全边界

- 模型只能通过 assistant MCP 的 4 个工具干活：认领截图、认领邮件、建任务、结束批次。它拿不到 To Do 的删除功能和 Thunderbird 的任何功能。
- 收集会话里没有 shell、网络、子 agent，文件系统只读（实测写文件会被拒绝）。截图里的「@助理 ……」写进任务前会被转义。
- 任务的清单由代码决定，截止日的星期由代码校验；每条任务备注最后一行是 `ref`，崩溃后能按它找回，不会丢、也不会重复。
- 截图和邮件内容会发给 DeepSeek API；会话日志不上传。截图存档、dsh 的图片副本和会话保留 30 天，日志保留 90 天。

## 目录

```text
AGENTS.md                            助理手册
prefs.md  config.json                你的偏好和设置（只在本机；示例见 *.example.*）
.dsh/skills/                         assistant-inbox、assistant-mail 两个 skill
core/                                assistant-core：账本、收件箱、邮件、To Do、简报（Python）
profile/collect.cordis.patch.yml     dsh profile（装到 ~/.dsh-assistant/profiles/collect）
jobs/                                调度器 run.py、launchd、启动器 TaskAssistant.app、密钥导入
phone/                               快捷指令生成脚本和成品
eval/                                准确率评测
state/                               账本、收件箱暂存、存档、简报、日志（不要手动改）
```

## 出问题时

- 作业失败会弹系统通知，细节在 `state/logs/<作业>/`：`runner.log` 是每次的结果，`*.jsonl` 是模型的完整过程。
- 截图看不清或处理失败的，原件在 `state/failed/`，第二天的简报里会列出来。
- 后台作业经 `~/Applications/TaskAssistant.app` 运行。它第一次读 iCloud 时（以及重新编译之后），macOS 会问「TaskAssistant」能否访问 iCloud 云盘，选「允许」。

## 许可

[MIT](LICENSE)
