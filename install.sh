#!/bin/bash
# 安装或更新个人助理。可以重复运行：已有的数据、账本、清单都会保留。
#   ./install.sh            安装全部（含 launchd 作业）
#   ./install.sh --no-jobs  只装环境，不加载后台作业
set -euo pipefail
cd "$(dirname "$0")"
PROJECT="$PWD"
export DSH_HOME="$HOME/.dsh-assistant" DSH_AGENTS_HOME="$HOME/.dsh-assistant/agents"

step() { printf '\n== %s\n' "$1"; }

step "检查依赖"
for need in /opt/homebrew/bin/dsh "$HOME/.local/bin/uv" "/Applications/To Do MCP.app/Contents/MacOS/mstodo-mcp" \
            "$HOME/.claude/skills/thunderbird-mail/mail-mcp-bridge.js"; do
  [ -e "$need" ] && echo "有：$need" || { echo "缺少：$need"; exit 1; }
done

step "个人设置（config.json、prefs.md 不进代码库；没有就从示例复制）"
[ -f config.json ] || { cp config.example.json config.json; echo "已生成 config.json：把 mail.sources 和 self_addresses 改成你的邮箱"; }
[ -f prefs.md ] || { cp prefs.example.md prefs.md; echo "已生成 prefs.md：把「我是谁」改成你的称呼和邮箱"; }

step "assistant-core（Python 环境）"
(cd core && "$HOME/.local/bin/uv" sync --quiet && "$HOME/.local/bin/uv" run --quiet pytest -q)

step "dsh 环境：$DSH_HOME，profile collect"
mkdir -p "$DSH_HOME" "$DSH_AGENTS_HOME" && chmod 700 "$DSH_HOME"
if [ ! -f "$DSH_HOME/profiles/collect/package.json" ]; then
  (cd /tmp && dsh --profile collect --from-default-profile headless --dump-config >/dev/null)
fi
esc=$(printf '%s' "$PROJECT" | sed 's/[&|\\]/\\&/g')
sed "s|__PROJECT__|$esc|g" profile/collect.cordis.patch.yml > "$DSH_HOME/profiles/collect/cordis.patch.yml"
dsh --profile collect --dump-config >/dev/null && echo "profile 组合检查通过"

step "密钥（从你平时用的 ~/.dsh 复制，不打印）"
/usr/bin/python3 jobs/import_secrets.py

step "账本、To Do 清单、邮件水位"
core/.venv/bin/assistant-core init

step "快捷指令"
if [ ! -f "phone/dist/记待办.shortcut" ]; then /usr/bin/python3 phone/build_shortcut.py; fi
ls phone/dist

step "作业启动器（~/Applications/TaskAssistant.app）"
if [ ! -x "$HOME/Applications/TaskAssistant.app/Contents/MacOS/TaskAssistant" ]; then jobs/launcher/build.sh; fi

if [ "${1:-}" != "--no-jobs" ]; then
  step "launchd 作业"
  /usr/bin/python3 jobs/launchd.py install
fi

cat <<EOF

装好了。接下来在手机上：
  1. 打开 phone/dist 里的快捷指令导入（在 Mac 上双击导入，iCloud 会同步到 iPhone）。
  2. 在微信界面运行「记待办」就会截屏生成任务：用 Siri、操作按钮或辅助触控运行，
     不要在主屏幕点图标（那样截到的是主屏幕）。详见 README.md。
暂停所有作业：touch "$PROJECT/PAUSE"；恢复：rm "$PROJECT/PAUSE"。
卸载后台作业：/usr/bin/python3 jobs/launchd.py uninstall
EOF
