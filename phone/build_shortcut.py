#!/usr/bin/env python3
"""生成并签名个人助理的快捷指令（做法同「快捷添加日历」）。

    python3 phone/build_shortcut.py        输出到 phone/dist/

生成四个：
  记待办          运行时截下当前屏幕存进收件箱（要在微信界面里用 Siri、操作按钮或辅助触控运行）；
                  也出现在分享菜单里，收下分享的图片、文字、链接。
  记待办＋        同上截屏，再选「口述一句 / 打字一句 / 附上剪贴板文字 / 不加了」。
  口述待办        Siri：「嘿 Siri，口述待办」，只听写一句话。
  记待办 (Mac)    Mac 上框选截图（需要在快捷指令设置里允许运行脚本）。

所有文件都存进 iCloud 云盘「快捷指令」文件夹下的 /个人助理/inbox/，文件名带类型和时间：
  ss-20261018-143212-4821.png      截图
  sp-20261018-143212-4821.png      截图（＋ 同名 -note.txt 补充 / -clip.txt 剪贴板）
  sh-20261018-143212-4821-1.jpg    分享（每个分享项一个序号）
  vo-20261018-143212-4821.txt      口述
  mc-20261018-143212-4821.png      Mac 截图
"""
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / 'dist'
FOLDER = '/个人助理/inbox/'
ICON = {'WFWorkflowIconStartColor': 4282601983, 'WFWorkflowIconGlyphNumber': 61440}
MAC_INBOX = '$HOME/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/个人助理/inbox'
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

    「存储文件」把路径的最后一段当文件名、自动补扩展名（实测：路径写成 /个人助理/inbox/ 会存成
    个人助理/inbox.png），所以文件名直接写进路径，不用「设定名称」。
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


def main() -> int:
    for name, wf in [('记待办', quick()), ('记待办＋', plus()), ('口述待办', voice()), ('记待办 (Mac)', mac())]:
        print('已生成：', build(name, wf))
    return 0


if __name__ == '__main__':
    sys.exit(main())
