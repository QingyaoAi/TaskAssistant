#!/bin/bash
# 编译「TaskAssistant.app」（只有一个命令行启动器，不显示 Dock 图标），装到 ~/Applications。
# 图标来自项目根目录的 TaskAssistant.jpeg（make_icon.py 生成 AppIcon.icns）。
# 重新编译后签名会变，macOS 会再问一次 iCloud 云盘的访问权限。
set -euo pipefail
cd "$(dirname "$0")"
PROJECT="$(cd ../.. && pwd)"
NAME="TaskAssistant"
APP="$HOME/Applications/$NAME.app"
BUILD="$(mktemp -d)"

[ -f AppIcon.icns ] || /usr/bin/python3 make_icon.py
mkdir -p "$BUILD/$NAME.app/Contents/MacOS" "$BUILD/$NAME.app/Contents/Resources"
swiftc -O -o "$BUILD/$NAME.app/Contents/MacOS/$NAME" main.swift
cp AppIcon.icns "$BUILD/$NAME.app/Contents/Resources/AppIcon.icns"
cat > "$BUILD/$NAME.app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>          <string>local.taskassistant</string>
  <key>CFBundleName</key>                <string>$NAME</string>
  <key>CFBundleDisplayName</key>         <string>$NAME</string>
  <key>CFBundleExecutable</key>          <string>$NAME</string>
  <key>CFBundleIconFile</key>            <string>AppIcon</string>
  <key>CFBundlePackageType</key>         <string>APPL</string>
  <key>CFBundleShortVersionString</key>  <string>0.1</string>
  <key>CFBundleVersion</key>             <string>2</string>
  <key>LSUIElement</key>                 <true/>
  <key>LSMinimumSystemVersion</key>      <string>13.0</string>
  <key>AssistantProject</key>            <string>$PROJECT</string>
  <key>NSFileProviderDomainUsageDescription</key>
  <string>读取 iCloud 云盘里「快捷指令/TaskAssistant/inbox」中你从手机存来的截图和文字，整理成待办。</string>
</dict>
</plist>
EOF
codesign --force --sign - --identifier local.taskassistant "$BUILD/$NAME.app"
mkdir -p "$HOME/Applications"
rm -rf "$APP"
mv "$BUILD/$NAME.app" "$APP"
rm -rf "$BUILD"
# 让访达和程序坞立刻用上新图标
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" >/dev/null 2>&1 || true
echo "已安装：$APP"
