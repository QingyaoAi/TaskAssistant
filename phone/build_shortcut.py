#!/usr/bin/env python3
"""生成并签名个人助理的快捷指令（做法同「快捷添加日历」）。

    python3 phone/build_shortcut.py                    生成并签名，输出到 phone/dist/
    python3 phone/build_shortcut.py --install          只把内容变了的重新生成，并导入 Mac 上的「快捷指令」
                                                       （iCloud 同步到 iPhone）；install.sh 每次都跑这个
    python3 phone/build_shortcut.py --install --force  全部重新导入（只改了图标、分享菜单这类动作以外的设置时用）

导入不是原地替换，而是新建一条同名的：Siri 按名字找，照常能用；iPhone 上操作按钮、辅助触控
绑定的可能要重新选一次。所以内容没变就不导入。

生成四个：
  记待办          运行时截下当前屏幕存进收件箱（要在微信界面里用 Siri、操作按钮或辅助触控运行）；
                  也出现在分享菜单里，收下分享的图片、文字、链接。
  记待办＋        同上截屏，再选「口述一句 / 打字一句 / 附上剪贴板文字 / 不加了」。
  口述待办        Siri：「嘿 Siri，口述待办」，只听写一句话。
  记待办 (Mac)    Mac 上框选截图（需要在快捷指令设置里允许运行脚本）。

所有文件都存进 iCloud 云盘「快捷指令」文件夹下的 /TaskAssistant/inbox/，文件名带类型和时间：
  ss-20261018-143212-4821.png      截图
  sp-20261018-143212-4821.png      截图（＋ 同名 -note.txt 补充 / -clip.txt 剪贴板）
  sh-20261018-143212-4821-1.jpg    分享（每个分享项一个序号）
  vo-20261018-143212-4821.txt      口述
  mc-20261018-143212-4821.png      Mac 截图
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / 'dist'
SHORTCUTS_DB = Path.home() / 'Library' / 'Shortcuts' / 'Shortcuts.sqlite'
UUID_RE = re.compile(r'[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}')
FOLDER = '/TaskAssistant/inbox/'
ICON = {'WFWorkflowIconStartColor': 4282601983, 'WFWorkflowIconGlyphNumber': 61440}
MAC_INBOX = '$HOME/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/TaskAssistant/inbox'
OBJ = '￼'  # 文本里放变量的占位符


def uid() -> str:
    return str(uuid.uuid4()).upper()


def action(identifier: str, **params) -> dict:
    return {'WFWorkflowActionIdentifier': 'is.workflow.actions.' + identifier, 'WFWorkflowActionParameters': params}


def out(action_uuid: str, name: str) -> dict:
    return {'Type': 'ActionOutput', 'OutputUUID': action_uuid, 'OutputName': name}


def var(name: str) -> dict:
    return {'Type': 'Variable', 'VariableName': name}


def attachment(ref: dict) -> dict:
    """整个参数就是一个变量。"""
    return {'Value': ref, 'WFSerializationType': 'WFTextTokenAttachment'}


def text(*parts) -> dict:
    """字符串和变量拼成的文本参数。parts 里的 dict 是变量引用。"""
    string, ranges = '', {}
    for part in parts:
        if isinstance(part, dict):
            ranges[f'{{{len(string.encode("utf-16-le")) // 2}, 1}}'] = part
            string += OBJ
        else:
            string += part
    return {'Value': {'string': string, 'attachmentsByRange': ranges}, 'WFSerializationType': 'WFTextTokenString'}


def stamp_actions() -> tuple:
    """时间戳和随机数，拼文件名用。"""
    date_id, rand_id = uid(), uid()
    acts = [
        action('format.date', UUID=date_id, WFDateFormatStyle='Custom', WFDateFormat='yyyyMMdd-HHmmss',
               WFDate=text({'Type': 'CurrentDate'})),
        action('number.random', UUID=rand_id, WFRandomNumberMinimum=1000, WFRandomNumberMaximum=9999),
    ]
    return acts, out(date_id, 'Formatted Date'), out(rand_id, 'Random Number')


def save(item: dict, *name_parts) -> list:
    """存进收件箱（不询问位置、不覆盖）。

    「存储文件」把路径的最后一段当文件名、自动补扩展名（实测：路径写成 /TaskAssistant/inbox/ 会存成
    TaskAssistant/inbox.png），所以文件名直接写进路径，不用「设定名称」。
    """
    return [
        action('documentpicker.save', UUID=uid(), WFInput=attachment(item), WFAskWhereToSave=False,
               WFFileStorageService='iCloud Drive', WFFileDestinationPath=text(FOLDER, *name_parts),
               WFSaveFileOverwrite=False),
    ]


def workflow(actions: list, share: bool = False) -> dict:
    return {
        'WFWorkflowClientVersion': '3612.0.2.2',
        'WFWorkflowMinimumClientVersion': 900,
        'WFWorkflowMinimumClientVersionString': '900',
        'WFWorkflowIcon': ICON,
        'WFWorkflowImportQuestions': [],
        'WFWorkflowTypes': ['ActionExtension'] if share else [],
        'WFWorkflowInputContentItemClasses': [
            'WFImageContentItem', 'WFPhotoMediaContentItem', 'WFStringContentItem', 'WFRichTextContentItem',
            'WFURLContentItem', 'WFSafariWebPageContentItem', 'WFGenericFileContentItem',
        ] if share else [],
        'WFWorkflowOutputContentItemClasses': [],
        'WFWorkflowHasShortcutInputVariables': share,
        'WFQuickActionSurfaces': [],
        'WFWorkflowActions': actions,
    }


def quick() -> dict:
    """记待办：有分享输入就逐项存（sh-），否则截屏存（ss-）。"""
    stamps, date, rand = stamp_actions()
    g_if, g_rep, shot = uid(), uid(), uid()
    acts = stamps + [
        action('conditional', GroupingIdentifier=g_if, WFControlFlowMode=0, WFCondition=100,
               WFInput={'Type': 'Variable', 'Variable': attachment({'Type': 'ExtensionInput'})}),
        action('repeat.each', UUID=uid(), GroupingIdentifier=g_rep, WFControlFlowMode=0,
               WFInput=attachment({'Type': 'ExtensionInput'})),
        *save(var('Repeat Item'), 'sh-', date, '-', rand, '-', var('Repeat Index')),
        action('repeat.each', UUID=uid(), GroupingIdentifier=g_rep, WFControlFlowMode=2),
        action('conditional', GroupingIdentifier=g_if, WFControlFlowMode=1),
        action('takescreenshot', UUID=shot),
        *save(out(shot, 'Screenshot'), 'ss-', date, '-', rand),
        action('conditional', UUID=uid(), GroupingIdentifier=g_if, WFControlFlowMode=2),
        action('vibrate'),
    ]
    return workflow(acts, share=True)


def plus() -> dict:
    """记待办＋：先截屏（菜单出现之前），再选补充方式。"""
    shot = uid()
    stamps, date, rand = stamp_actions()
    g_menu = uid()
    items = ['口述一句', '打字一句', '附上剪贴板文字', '不加了']
    dictated, typed, clip = uid(), uid(), uid()
    acts = [action('takescreenshot', UUID=shot), *stamps,
            *save(out(shot, 'Screenshot'), 'sp-', date, '-', rand),
            action('choosefrommenu', UUID=uid(), GroupingIdentifier=g_menu, WFControlFlowMode=0,
                   WFMenuPrompt='补充点什么？', WFMenuItems=items),
            action('choosefrommenu', GroupingIdentifier=g_menu, WFControlFlowMode=1, WFMenuItemTitle=items[0]),
            action('dictatetext', UUID=dictated, WFSpeechLanguage='zh-CN'),
            *save(out(dictated, 'Dictated Text'), 'sp-', date, '-', rand, '-note'),
            action('choosefrommenu', GroupingIdentifier=g_menu, WFControlFlowMode=1, WFMenuItemTitle=items[1]),
            action('ask', UUID=typed, WFAskActionPrompt='补充一句（如：周五前回他）', WFInputType='Text'),
            *save(out(typed, 'Provided Input'), 'sp-', date, '-', rand, '-note'),
            action('choosefrommenu', GroupingIdentifier=g_menu, WFControlFlowMode=1, WFMenuItemTitle=items[2]),
            action('getclipboard', UUID=clip),
            *save(out(clip, 'Clipboard'), 'sp-', date, '-', rand, '-clip'),
            action('choosefrommenu', GroupingIdentifier=g_menu, WFControlFlowMode=1, WFMenuItemTitle=items[3]),
            action('choosefrommenu', UUID=uid(), GroupingIdentifier=g_menu, WFControlFlowMode=2),
            action('vibrate')]
    return workflow(acts)


def voice() -> dict:
    """口述待办：给 Siri 用。"""
    dictated = uid()
    stamps, date, rand = stamp_actions()
    acts = [action('dictatetext', UUID=dictated, WFSpeechLanguage='zh-CN'), *stamps,
            *save(out(dictated, 'Dictated Text'), 'vo-', date, '-', rand),
            action('vibrate')]
    return workflow(acts)


def mac() -> dict:
    script = (f'd="{MAC_INBOX}"\nmkdir -p "$d"\n'
              'f="$d/mc-$(date +%Y%m%d-%H%M%S)-$((RANDOM % 9000 + 1000)).png"\n'
              'screencapture -i -x "$f"\n[ -s "$f" ] || rm -f "$f"\n')
    return workflow([action('runshellscript', UUID=uid(), Shell='/bin/bash', Script=script)])


def build(name: str, wf: dict, attempts: int = 5) -> Path:
    """签名。`shortcuts sign` 处理不了中文路径，签名服务器也偶尔返回 500，所以用英文临时名、失败重试。"""
    DIST.mkdir(exist_ok=True)
    target = DIST / f'{name}.shortcut'
    with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
        unsigned, signed = Path(tmp) / 'unsigned.shortcut', Path(tmp) / 'signed.shortcut'
        with open(unsigned, 'wb') as f:
            plistlib.dump(wf, f, fmt=plistlib.FMT_BINARY)
        for i in range(attempts):
            r = subprocess.run(['shortcuts', 'sign', '--mode', 'anyone', '--input', str(unsigned),
                                '--output', str(signed)], capture_output=True, text=True)
            if r.returncode == 0 and signed.exists():
                os.replace(signed, target)
                return target
            print(f'  {name}：签名失败（{(r.stderr or r.stdout).strip()}），{10 * (i + 1)} 秒后重试')
            time.sleep(10 * (i + 1))
    raise RuntimeError(f'{name} 签名失败')


SHORTCUTS = {'记待办': quick, '记待办＋': plus, '口述待办': voice, '记待办 (Mac)': mac}


def normalized(actions: list) -> str:
    """每次生成的 UUID 都不一样：按出现顺序换成编号再比，动作没变就相等。"""
    ids: dict[str, str] = {}
    text = json.dumps(actions, sort_keys=True, ensure_ascii=False)
    return UUID_RE.sub(lambda m: ids.setdefault(m[0], f'#{len(ids)}'), text)


def installed() -> dict | None:
    """Mac 上「快捷指令」库里每个快捷指令的动作（normalized）。读不了返回 None。"""
    if not SHORTCUTS_DB.exists():
        return None
    with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
        for f in SHORTCUTS_DB.parent.glob(SHORTCUTS_DB.name + '*'):  # 连 -wal、-shm 一起拷，只读副本
            shutil.copy(f, tmp)
        try:
            db = sqlite3.connect(Path(tmp) / SHORTCUTS_DB.name)
            rows = db.execute('SELECT s.ZNAME, a.ZDATA FROM ZSHORTCUT s JOIN ZSHORTCUTACTIONS a ON a.ZSHORTCUT = s.Z_PK '
                              'WHERE s.ZTOMBSTONED = 0').fetchall()
            db.close()
        except sqlite3.Error:
            return None
    out = {}
    for name, data in rows:
        try:
            out[name] = normalized(plistlib.loads(data))
        except (plistlib.InvalidFileException, ValueError, TypeError):
            continue
    return out


def install(force: bool = False, wait: float = 120) -> int:
    """内容变了的重新签名、导入，并等到库里确实是新的为止。"""
    have = installed()
    generated = {name: make() for name, make in SHORTCUTS.items()}
    stale = {name: wf for name, wf in generated.items()
             if force or have is None or have.get(name) != normalized(wf['WFWorkflowActions'])}
    if not stale:
        print('快捷指令都是最新的，不用重新添加')
        return 0
    if have is None:
        print('读不到 Mac 上「快捷指令」的库，没法确认哪些要更新，全部重新导入')
    failed = []
    for name, wf in stale.items():
        subprocess.run(['open', str(build(name, wf))], check=True)
        if have is None:
            print(f'已打开：{name}（弹出窗口的话点「添加快捷指令」或「替换」）')
            continue
        want, start, hinted = normalized(wf['WFWorkflowActions']), time.monotonic(), False
        while (installed() or {}).get(name) != want:
            if time.monotonic() - start > wait:
                failed.append(name)
                break
            if not hinted and time.monotonic() - start > 10:
                print(f'  {name}：弹出了导入窗口的话，请点「添加快捷指令」或「替换」')
                hinted = True
            time.sleep(2)
        else:
            print(f'已更新：{name}')
    print('iCloud 会同步到 iPhone。重新导入的是新建的快捷指令：Siri 照常能用；'
          'iPhone 上的操作按钮、辅助触控如果不灵了，重新选一次「记待办」。')
    if failed:
        print(f"没等到导入完成：{'、'.join(failed)}。点完窗口后再运行一次 {Path(__file__).name} --install")
    return 1 if failed else 0


def main(argv: list) -> int:
    if '--install' in argv:
        return install(force='--force' in argv)
    for name, make in SHORTCUTS.items():
        print('已生成：', build(name, make()))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
