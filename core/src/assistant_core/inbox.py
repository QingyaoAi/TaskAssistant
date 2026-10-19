"""收件箱：手机和 Mac 存进 iCloud 的截图、文字、口述。

手机端只管存文件，元数据都在文件名里：
    <类型>-<yyyyMMdd-HHmmss>-<随机数>[-<序号>].<扩展名>
类型：ss 截图 · sp 截图＋补充（同名 .txt 是补充）· sh 分享 · vo 口述 · mc Mac 截图。
不认识的文件名也收，按「分享」处理，时间取文件修改时间。

文件分两步离开 iCloud：调度器先在收件箱安静下来后把它们挪到本地的 state/incoming/（stage），
模型认领时再从 incoming 移进 state/processing/<批次>，处理完移进 archive/。这样只有调度器
碰 iCloud（macOS 隐私授权给它一次即可），dsh 和模型完全不碰。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import dates
from .config import Config, state_dir
from .ledger import Ledger

NAME_RE = re.compile(r'^(?P<kind>ss|sp|sh|vo|mc)-(?P<ts>\d{8}-\d{6})-(?P<rand>[0-9A-Za-z]{1,8})'
                     r'(?:-(?P<n>\d+|note|clip))?(?: \d+)?\.(?P<ext>[A-Za-z0-9]+)$')
IMAGE_EXT = {'png', 'jpg', 'jpeg', 'heic', 'heif', 'webp', 'gif', 'tiff'}
READABLE_IMAGE = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
SCREENSHOT_KINDS = {'ss', 'sp', 'mc'}
KIND_LABEL = {'ss': 'iPhone 截图', 'sp': 'iPhone 截图（带补充）', 'sh': '分享', 'vo': '口述', 'mc': 'Mac 截图'}
TEXT_ROLE = {'sp': 'note', 'sh': 'shared', 'vo': 'voice', 'ss': 'note', 'mc': 'note'}
STALE_CLAIM = timedelta(minutes=15)


@dataclass
class Unit:
    stem: str
    kind: str
    ts: datetime
    files: list[Path] = field(default_factory=list)


NOTE_WINDOW = 180  # 文件名不规范时：截图后这么多秒内存进来的文字算作对它的补充


def _parse(path: Path, tz) -> tuple[str, str, datetime]:
    m = NAME_RE.match(path.name)
    if m:
        ts = datetime.strptime(m['ts'], '%Y%m%d-%H%M%S').replace(tzinfo=tz)
        return f"{m['kind']}-{m['ts']}-{m['rand']}", m['kind'], ts
    # 文件名不规范（旧版快捷指令会存成 inbox.png、inbox-2.png、inbox.txt）：图片当截图，文字当口述，
    # 时间取文件创建时间。
    st = path.stat()
    ts = datetime.fromtimestamp(min(getattr(st, 'st_birthtime', st.st_mtime), st.st_mtime), tz)
    kind = 'ss' if path.suffix.lower().lstrip('.') in IMAGE_EXT else 'vo'
    return f"{kind}-{ts:%Y%m%d-%H%M%S}-{zlib.crc32(path.name.encode()) % 10000:04d}", kind, ts


def _attach_loose_notes(units: list[Unit]) -> list[Unit]:
    """文件名不规范的文字，如果紧跟在一张截图后面（NOTE_WINDOW 内），就并进那张截图当补充。"""
    out: list[Unit] = []
    for unit in sorted(units, key=lambda u: (u.ts, u.stem)):
        loose_text = unit.kind == 'vo' and all(not NAME_RE.match(f.name) for f in unit.files)
        host = next((u for u in reversed(out) if u.kind in SCREENSHOT_KINDS), None)
        if loose_text and host and 0 <= (unit.ts - host.ts).total_seconds() <= NOTE_WINDOW:
            host.files.extend(unit.files)
            continue
        out.append(unit)
    return out


def incoming_dir() -> Path:
    return state_dir('incoming')


def scan(cfg: Config, root: Path | None = None) -> tuple[list[Unit], list[Path]]:
    """返回 (已下载好的条目, 还是 iCloud 占位符的文件)。root 默认是本地的 incoming。"""
    inbox = root or incoming_dir()
    if not inbox.exists():
        return [], []
    units: dict[str, Unit] = {}
    placeholders = []
    for path in sorted(inbox.iterdir()):
        if path.name.startswith('.'):
            if path.name.endswith('.icloud'):
                placeholders.append(path)
            continue
        if not path.is_file():
            continue
        stem, kind, ts = _parse(path, cfg.tz)
        units.setdefault(stem, Unit(stem, kind, ts)).files.append(path)
    return _attach_loose_notes(list(units.values())), placeholders


def request_download(placeholders: list[Path]) -> None:
    for ph in placeholders:
        original = ph.with_name(ph.name[1:-len('.icloud')])
        subprocess.run(['brctl', 'download', str(original)], capture_output=True, timeout=30)


def group(units: list[Unit], gap_seconds: int) -> list[list[Unit]]:
    """连续截图（间隔不超过 gap）合成一批；分享和口述各自成批。"""
    batches: list[list[Unit]] = []
    for unit in units:
        last = batches[-1] if batches else None
        if (last and unit.kind in SCREENSHOT_KINDS and last[-1].kind in SCREENSHOT_KINDS
                and (unit.ts - last[-1].ts).total_seconds() <= gap_seconds):
            last.append(unit)
        else:
            batches.append([unit])
    return batches


def sources(cfg: Config) -> list[Path]:
    """手机可能把文件存到的地方：inbox 本身，以及 iCloud 同名冲突时生成的「TaskAssistant 2」之类的副本、
    误存在「TaskAssistant」根目录里的文件（快捷指令路径写错时会存成 TaskAssistant/inbox.png）。
    旧版快捷指令用的「个人助理/inbox」（legacy_inbox_dirs）也照这样找。"""
    found = []
    for inbox in (cfg.inbox_dir, *cfg.legacy_inbox_dirs):
        root = inbox.parent
        found += [inbox, root]
        if root.parent.exists():
            for twin in sorted(root.parent.glob(root.name + ' *')):
                if twin.is_dir():
                    found += [twin / inbox.name, twin]
    return [d for d in found if d.is_dir()]


def snapshot(cfg: Config) -> dict[str, int]:
    return {str(p): p.stat().st_size for d in sources(cfg) for p in d.iterdir()
            if p.is_file() and p.name != '.DS_Store'}


def wait_quiet(cfg: Config, max_wait: float = 300, poll: float = 5) -> bool:
    """等 iCloud 收件箱安静下来（quiet_seconds 内没有新文件、大小不变、没有占位符）。有东西可处理返回 True。"""
    deadline = time.monotonic() + max_wait
    last, since = None, time.monotonic()
    while True:
        units, placeholders = [], []
        for d in sources(cfg):
            u, ph = scan(cfg, d)
            units += u
            placeholders += ph
        if placeholders:
            request_download(placeholders)
        snap = snapshot(cfg)
        if not snap:
            return False
        if snap != last:
            last, since = snap, time.monotonic()
        elif not placeholders and time.monotonic() - since >= cfg.raw['quiet_seconds']:
            return bool(units)
        if time.monotonic() > deadline:
            return bool(units)
        time.sleep(poll)


def stage(cfg: Config) -> int:
    """把 iCloud 收件箱里已下载好的文件挪到本地 incoming。返回挪了几个文件。"""
    dest = incoming_dir()
    moved = 0
    units = [u for d in sources(cfg) for u in scan(cfg, d)[0]]
    for unit in units:
        for f in unit.files:
            target = dest / f.name
            if target.exists():
                target = dest / f'{f.stem}-{int(time.time())}{f.suffix}'
            shutil.move(str(f), target)
            moved += 1
    return moved


def waiting(cfg: Config) -> int:
    """还没交给模型的文件数（iCloud 里的 + 本地 incoming 里的）。"""
    total = 0
    for root in (*sources(cfg), incoming_dir()):
        try:
            units, placeholders = scan(cfg, root)
        except OSError:  # 还没授权访问 iCloud 云盘时，简报照出，只是少这一项
            continue
        total += sum(len(u.files) for u in units) + len(placeholders)
    return total


def _sips(*args: str) -> None:
    subprocess.run(['sips', *args], capture_output=True, check=True, timeout=60)


def _prepare_image(src: Path, dest_dir: Path, max_edge: int) -> Path:
    ext = src.suffix.lower().lstrip('.')
    if ext in READABLE_IMAGE:
        dest = dest_dir / src.name
        shutil.move(str(src), dest)
    else:  # HEIC 等模型不认的格式：转 JPEG
        dest = dest_dir / (src.stem + '.jpg')
        _sips('-s', 'format', 'jpeg', str(src), '--out', str(dest))
        src.unlink()
    try:
        _sips('-Z', str(max_edge), str(dest))
    except subprocess.CalledProcessError:
        pass
    return dest


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ('utf-8', 'utf-16', 'gb18030'):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace').strip()


def _claim_new(cfg: Config, ledger: Ledger, batch: list[Unit]) -> dict:
    batch_id = batch[0].stem
    dest = state_dir('processing', batch_id)
    images, texts = [], []
    for unit in batch:
        for f in sorted(unit.files):
            ext = f.suffix.lower().lstrip('.')
            if ext in IMAGE_EXT:
                images.append(str(_prepare_image(f, dest, cfg.raw['max_image_edge'])))
            else:
                text = _read_text(f)
                shutil.move(str(f), dest / f.name)
                m = NAME_RE.match(f.name)
                suffix = m['n'] if m else None
                role = {'note': 'note', 'clip': 'shared'}.get(suffix or '', TEXT_ROLE.get(unit.kind, 'shared'))
                if text:
                    texts.append({'role': role, 'text': text[:4000]})
    payload = {
        'batch_id': batch_id,
        'source': KIND_LABEL.get(batch[0].kind, '分享'),
        'device': 'mac' if batch[0].kind == 'mc' else 'iphone',
        'captured_at': batch[0].ts.isoformat(timespec='seconds'),
        'images': images,
        'texts': texts,
    }
    ledger.upsert_claim(batch_id, 'inbox', payload)
    return payload


def view(cfg: Config, ledger: Ledger, payload: dict) -> dict:
    anchor = datetime.fromisoformat(payload['captured_at']).astimezone(cfg.tz).date()
    out = {k: payload[k] for k in ('batch_id', 'source', 'device', 'captured_at', 'images', 'texts')}
    out['anchor_date'] = f'{anchor.isoformat()} {dates.weekday_cn(anchor)}'
    out['calendar'] = dates.calendar_table(anchor)
    done = [{'index': r['idx'], 'kind': r['kind'], 'title': r['title'], 'status': r['status']}
            for r in ledger.items_of(payload['batch_id'])]
    if done:
        out['already_recorded'] = done
    return out


def release_stale(ledger: Ledger, kind: str) -> None:
    cutoff = (datetime.now(timezone.utc) - STALE_CLAIM).isoformat(timespec='seconds')
    for row in ledger.batches_with_status(kind, 'claimed'):
        if (row['claimed_at'] or '') < cutoff:
            ledger.release(row['batch_id'], '上次运行没有处理完')


def claim(cfg: Config, ledger: Ledger, limit: int = 3) -> list[dict]:
    release_stale(ledger, 'inbox')
    out = []
    for row in ledger.batches_with_status('inbox', 'released')[:limit]:
        payload = json.loads(row['payload'])
        ledger.upsert_claim(row['batch_id'], 'inbox', payload)
        out.append(view(cfg, ledger, payload))
    if len(out) < limit:
        units, _ = scan(cfg)
        known = ledger.known_batch_ids([u.stem for u in units])
        fresh = [u for u in units if u.stem not in known]
        for batch in group(fresh, cfg.raw['batch_gap_seconds'])[: limit - len(out)]:
            out.append(view(cfg, ledger, _claim_new(cfg, ledger, batch)))
    return out


def move_out(batch_id: str, status: str) -> None:
    src = state_dir('processing') / batch_id
    if not src.exists():
        return
    if status == 'done':
        dest_root = state_dir('archive', datetime.now().strftime('%Y-%m'))
    else:
        dest_root = state_dir('failed')
    dest = dest_root / batch_id
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(src), dest)
