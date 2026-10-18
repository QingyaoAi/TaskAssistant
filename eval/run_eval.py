#!/usr/bin/env python3
"""准确率基线：只跑识别，不挂任何写入工具，不碰 To Do。

用法（用 assistant-core 的 Python 运行）：
    core/.venv/bin/python eval/run_eval.py              评测 eval/cases/ 里有标注的截图
    core/.venv/bin/python eval/run_eval.py --workers 3  并行

准备：
  1. 把真实截图（png / jpg / heic）放进 eval/cases/。
  2. 在 eval/labels.csv 里每张一行（表头见 labels.template.csv）：
       file        文件名
       captured    截图日期 YYYY-MM-DD（换算「周五前」的基准）
       expect      task / followup / event / none（这张图最主要的那件事；没事就 none）
       list        期望的清单（none 时留空）
       due_date    期望的截止日（没有就留空）
结果写到 eval/results-<时间>.csv，并打印三个数：是否有事的判断准确率、清单准确率、截止日准确率。
"""
import argparse
import csv
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'jobs'))
sys.path.insert(0, str(ROOT / 'core' / 'src'))
import run  # noqa: E402  调度器里的环境设置
from assistant_core import dates  # noqa: E402

CASES = ROOT / 'eval' / 'cases'
CACHE = ROOT / 'eval' / '.cache'
PATCH = ROOT / 'eval' / 'eval.patch.yml'

PROMPT = """评测模式。读取 skill「assistant-inbox」，按其中「认清画面」「找出事项」「每条怎么填」的规则判断这张截图，
但不要调用任何 mcp__assistant__ 工具（这里也没有），只看图并输出结果。

截图文件：{path}
截图日期：{captured}（{weekday}）
日历对照表：
{calendar}

用 read_image 看图后，只输出一个 JSON 数组，不要任何其他文字。每件事一个对象：
{{"kind": "task|followup|event", "title": "...", "suggested_list": "...", "due_date": "YYYY-MM-DD 或 null",
  "due_basis": "...", "confidence": "high|medium|low"}}
没有要处理的事就输出 []。"""


def prepare(path: Path) -> Path:
    if path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.gif'):
        return path
    CACHE.mkdir(exist_ok=True)
    out = CACHE / (path.stem + '.jpg')
    if not out.exists():
        subprocess.run(['sips', '-s', 'format', 'jpeg', str(path), '--out', str(out)], capture_output=True, check=True)
    return out


def judge(row: dict) -> dict:
    img = prepare(CASES / row['file'])
    captured = date.fromisoformat(row.get('captured') or datetime.fromtimestamp(img.stat().st_mtime).date().isoformat())
    prompt = PROMPT.format(path=img.relative_to(ROOT), captured=captured, weekday=dates.weekday_cn(captured),
                           calendar=dates.calendar_table(captured))
    r = subprocess.run([run.DSH, '--profile', 'collect', '--patch', str(PATCH), prompt], capture_output=True,
                       text=True, env=run.child_env(), cwd=ROOT, timeout=600)
    text = r.stdout.strip()
    try:
        items = json.loads(text[text.index('['): text.rindex(']') + 1])
    except ValueError:
        items = None
    return {**row, 'raw': text[-500:], 'items': items}


def score(result: dict) -> dict:
    items = result['items']
    expect = (result.get('expect') or 'none').strip()
    if items is None:
        return {'has_event_ok': False, 'list_ok': False, 'due_ok': False, 'got': '（输出无法解析）'}
    first = items[0] if items else {}
    got_kind = first.get('kind', 'none') if items else 'none'
    has_ok = (expect == 'none') == (not items)
    list_ok = expect == 'none' or (first.get('suggested_list') or '').lower() == (result.get('list') or '').lower()
    due_ok = expect == 'none' or (first.get('due_date') or '') == (result.get('due_date') or '')
    return {'has_event_ok': has_ok, 'list_ok': has_ok and list_ok, 'due_ok': has_ok and due_ok,
            'got': f"{got_kind} | {first.get('title', '')} | {first.get('suggested_list', '')} | {first.get('due_date', '')}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=1)
    ap.add_argument('--labels', default=str(ROOT / 'eval' / 'labels.csv'))
    args = ap.parse_args()
    rows = list(csv.DictReader(open(args.labels, encoding='utf-8-sig')))
    rows = [r for r in rows if (CASES / r['file']).exists()]
    if not rows:
        print('eval/cases 里没有和 labels.csv 对得上的截图。')
        return 1
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(judge, rows))
    scored = [{**r, **score(r)} for r in results]
    out = ROOT / 'eval' / f'results-{datetime.now():%Y%m%d-%H%M%S}.csv'
    with open(out, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['file', 'expect', 'list', 'due_date', 'got', 'has_event_ok', 'list_ok', 'due_ok'])
        for s in scored:
            w.writerow([s['file'], s.get('expect'), s.get('list'), s.get('due_date'), s['got'],
                        s['has_event_ok'], s['list_ok'], s['due_ok']])
    n = len(scored)
    pct = lambda k: f"{sum(s[k] for s in scored)}/{n}（{100 * sum(s[k] for s in scored) / n:.0f}%）"  # noqa: E731
    print(f'有没有事判断对：{pct("has_event_ok")}\n清单对：{pct("list_ok")}\n截止日对：{pct("due_ok")}\n明细：{out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
