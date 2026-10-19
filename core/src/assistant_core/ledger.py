"""账本（SQLite，WAL）。

三类状态都在这里：批次（收件箱的一组截图、或一封邮件）、条目（一次判断，可能对应一个
To Do 任务）、水位（邮件处理到哪里）。写事务一律 BEGIN IMMEDIATE，几个作业可以同时跑。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  batch_id    TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,            -- inbox | mail
  status      TEXT NOT NULL,            -- claimed | released | done | failed | skipped
  payload     TEXT NOT NULL DEFAULT '{}',
  outcome     TEXT,                     -- handled | none | fyi
  summary     TEXT,                     -- fyi 摘要、失败原因
  attempts    INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  claimed_at  TEXT,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS items (
  ref            TEXT PRIMARY KEY,      -- <batch_id>#<idx>，确定性，写进 To Do 备注最后一行
  batch_id       TEXT NOT NULL,
  idx            INTEGER NOT NULL,
  kind           TEXT NOT NULL,         -- task | followup | event
  status         TEXT NOT NULL,         -- pending | done | failed
  title          TEXT NOT NULL,
  list_target    TEXT NOT NULL,
  list_suggested TEXT,
  due_date       TEXT,
  confidence     TEXT,
  detail         TEXT NOT NULL DEFAULT '{}',
  todo_id        TEXT,
  error          TEXT,
  created_at     TEXT NOT NULL,
  committed_at   TEXT
);
CREATE INDEX IF NOT EXISTS items_batch ON items(batch_id);
CREATE INDEX IF NOT EXISTS items_created ON items(created_at);
CREATE TABLE IF NOT EXISTS watermarks (
  source     TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job         TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT,                     -- ok | failed | timeout | skipped
  detail      TEXT
);
CREATE INDEX IF NOT EXISTS runs_job ON runs(job, started_at);
CREATE TABLE IF NOT EXISTS today (
  mirror_id  TEXT PRIMARY KEY,          -- Today 清单里那条副本的 id
  key        TEXT NOT NULL,             -- <清单>#<原任务 id 的哈希>，也写在副本备注里
  src_list   TEXT NOT NULL,
  src_id     TEXT NOT NULL,
  title      TEXT NOT NULL,             -- 原任务标题：原任务被挪走时按它找
  status     TEXT NOT NULL,             -- open | done（勾掉）| dismissed（你删了）| removed（不该放了）
  added_on   TEXT NOT NULL,             -- 本地日期 YYYY-MM-DD
  closed_on  TEXT
);
CREATE INDEX IF NOT EXISTS today_status ON today(status, closed_on);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self.db
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        else:
            self.db.execute('COMMIT')

    # ---- 批次 -------------------------------------------------------------
    def batch(self, batch_id: str) -> sqlite3.Row | None:
        return self.db.execute('SELECT * FROM batches WHERE batch_id=?', (batch_id,)).fetchone()

    def upsert_claim(self, batch_id: str, kind: str, payload: dict) -> None:
        with self.tx() as db:
            row = db.execute('SELECT status FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
            if row is None:
                db.execute('INSERT INTO batches(batch_id, kind, status, payload, created_at, claimed_at, attempts) '
                           'VALUES (?,?,?,?,?,?,1)', (batch_id, kind, 'claimed', json.dumps(payload, ensure_ascii=False),
                                                     now_iso(), now_iso()))
            else:
                db.execute("UPDATE batches SET status='claimed', payload=?, claimed_at=?, attempts=attempts+1 "
                           'WHERE batch_id=?', (json.dumps(payload, ensure_ascii=False), now_iso(), batch_id))

    def mark_skipped(self, batch_id: str, kind: str, payload: dict, reason: str) -> None:
        with self.tx() as db:
            db.execute('INSERT OR IGNORE INTO batches(batch_id, kind, status, payload, summary, created_at, finished_at) '
                       "VALUES (?,?,'skipped',?,?,?,?)",
                       (batch_id, kind, json.dumps(payload, ensure_ascii=False), reason, now_iso(), now_iso()))

    def finish_batch(self, batch_id: str, status: str, outcome: str | None = None, summary: str | None = None) -> None:
        with self.tx() as db:
            db.execute('UPDATE batches SET status=?, outcome=?, summary=?, finished_at=? WHERE batch_id=?',
                       (status, outcome, summary, now_iso(), batch_id))

    def batches_with_status(self, kind: str, *statuses: str) -> list[sqlite3.Row]:
        marks = ','.join('?' * len(statuses))
        return self.db.execute(f'SELECT * FROM batches WHERE kind=? AND status IN ({marks}) ORDER BY created_at',
                               (kind, *statuses)).fetchall()

    def known_batch_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        marks = ','.join('?' * len(ids))
        return {r[0] for r in self.db.execute(f'SELECT batch_id FROM batches WHERE batch_id IN ({marks})', ids)}

    def release(self, batch_id: str, reason: str, max_attempts: int = 3) -> str:
        """作业结束时还没处理完的批次：放回队列，次数用完就算失败。返回新状态。"""
        row = self.batch(batch_id)
        status = 'failed' if row is not None and row['attempts'] >= max_attempts else 'released'
        with self.tx() as db:
            db.execute('UPDATE batches SET status=?, summary=? WHERE batch_id=?', (status, reason, batch_id))
        return status

    # ---- 条目（两阶段：pending → done） --------------------------------------
    def item(self, ref: str) -> sqlite3.Row | None:
        return self.db.execute('SELECT * FROM items WHERE ref=?', (ref,)).fetchone()

    def items_of(self, batch_id: str) -> list[sqlite3.Row]:
        return self.db.execute('SELECT * FROM items WHERE batch_id=? ORDER BY idx', (batch_id,)).fetchall()

    def begin_item(self, ref: str, batch_id: str, idx: int, kind: str, title: str, list_target: str,
                   list_suggested: str | None, due_date: str | None, confidence: str, detail: dict) -> None:
        with self.tx() as db:
            db.execute('INSERT INTO items(ref, batch_id, idx, kind, status, title, list_target, list_suggested, '
                       'due_date, confidence, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ref, batch_id, idx, kind, 'pending', title, list_target, list_suggested, due_date,
                        confidence, json.dumps(detail, ensure_ascii=False), now_iso()))

    def commit_item(self, ref: str, todo_id: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE items SET status='done', todo_id=?, committed_at=?, error=NULL WHERE ref=?",
                       (todo_id, now_iso(), ref))

    def fail_item(self, ref: str, error: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE items SET status='failed', error=? WHERE ref=?", (error, ref))

    def recent_titles(self, days: int = 14, limit: int = 60) -> list[str]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='seconds')
        rows = self.db.execute('SELECT title FROM items WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?',
                               (since, limit)).fetchall()
        return [r[0] for r in rows]

    def pending_items(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM items WHERE status IN ('pending','failed') ORDER BY created_at").fetchall()

    # ---- 水位 -------------------------------------------------------------
    def watermark(self, source: str) -> str | None:
        row = self.db.execute('SELECT value FROM watermarks WHERE source=?', (source,)).fetchone()
        return row[0] if row else None

    def set_watermark(self, source: str, value: str) -> None:
        with self.tx() as db:
            db.execute('INSERT INTO watermarks(source, value, updated_at) VALUES (?,?,?) '
                       'ON CONFLICT(source) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
                       (source, value, now_iso()))

    # ---- Today 清单的副本 ---------------------------------------------------
    def today_open(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM today WHERE status='open' ORDER BY added_on").fetchall()

    def today_known(self, mirror_id: str) -> bool:
        return self.db.execute('SELECT 1 FROM today WHERE mirror_id=?', (mirror_id,)).fetchone() is not None

    def today_closed_keys(self, day: str) -> set[str]:
        """这一天勾掉或删掉的：当天不再放回来。"""
        return {r[0] for r in self.db.execute(
            "SELECT key FROM today WHERE status IN ('done','dismissed') AND closed_on=?", (day,))}

    def today_add(self, mirror_id: str, src: dict, day: str) -> None:
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO today(mirror_id, key, src_list, src_id, title, status, added_on) "
                       "VALUES (?,?,?,?,?,'open',?)", (mirror_id, src['key'], src['list'], src['id'], src['title'], day))

    def today_close(self, mirror_id: str, status: str, day: str) -> None:
        with self.tx() as db:
            db.execute('UPDATE today SET status=?, closed_on=? WHERE mirror_id=?', (status, day, mirror_id))

    # ---- 运行记录 ----------------------------------------------------------
    def run_start(self, job: str) -> int:
        with self.tx() as db:
            return db.execute('INSERT INTO runs(job, started_at) VALUES (?,?)', (job, now_iso())).lastrowid

    def run_finish(self, run_id: int, status: str, detail: str = '') -> None:
        with self.tx() as db:
            db.execute('UPDATE runs SET finished_at=?, status=?, detail=? WHERE id=?',
                       (now_iso(), status, detail[:2000], run_id))

    def last_run(self, job: str, status: str | None = None) -> sqlite3.Row | None:
        if status:
            return self.db.execute('SELECT * FROM runs WHERE job=? AND status=? ORDER BY id DESC LIMIT 1',
                                   (job, status)).fetchone()
        return self.db.execute('SELECT * FROM runs WHERE job=? ORDER BY id DESC LIMIT 1', (job,)).fetchone()

    def since(self, table: str, column: str, iso: str) -> list[sqlite3.Row]:
        assert table in {'items', 'batches', 'runs'} and column.isidentifier()
        return self.db.execute(f'SELECT * FROM {table} WHERE {column} >= ? ORDER BY {column}', (iso,)).fetchall()

    def backup(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dest)
        with target:
            self.db.backup(target)
        target.close()
