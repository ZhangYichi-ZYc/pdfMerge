"""审计日志（SQLite）。

归档区回答「内容还在不在」，这里回答「发生过什么」—— 两者缺一不可。

用标准库 ``sqlite3``，零额外依赖。WAL 模式保证进程崩溃时不丢已提交的记录。
连接是单例 + ``check_same_thread=False`` + 模块级锁：我们的写入频率是「每次
合并一条」，用连接池属于杀鸡用牛刀，而锁的代价在这种频率下完全不可测量。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, Sequence

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    created_at    TEXT    NOT NULL,   -- ISO8601 UTC
    client_ip     TEXT,
    user_agent    TEXT,
    file_count    INTEGER NOT NULL,
    total_bytes   INTEGER NOT NULL,
    output_name   TEXT,
    status        TEXT    NOT NULL,   -- draft|queued|processing|done|failed
    engine        TEXT,               -- 处理路径摘要，如 "bake=3 raster=1 direct=8"
    duration_ms   INTEGER,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS job_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT    NOT NULL REFERENCES jobs(job_id),
    ordinal       INTEGER,            -- 用户在合并时的排序位置，合并前为 NULL
    original_name TEXT    NOT NULL,   -- 用户上传时的原始文件名，原样保真
    archived_name TEXT    NOT NULL,   -- 磁盘上的改名后文件名
    archived_path TEXT    NOT NULL,   -- 相对 storage/ 的路径
    sha256        TEXT    NOT NULL,   -- 非抵赖锚点
    size_bytes    INTEGER NOT NULL,
    page_count    INTEGER,
    has_signature INTEGER NOT NULL DEFAULT 0,
    action        TEXT,               -- direct|bake|raster
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_files_job ON job_files(job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.AUDIT_DB, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.commit()
        _conn = conn
    return _conn


@contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init() -> None:
    """服务启动时调用，确保库文件与表结构就位。"""
    with _tx():
        pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_job(
    *,
    job_id: str,
    client_ip: str | None,
    user_agent: str | None,
    files: Sequence[tuple[str, str, str, str, int]],  # 原名, 归档名, 相对路径, sha256, 大小
) -> None:
    """建档：上传完成、用户已看到文件清单但还没点「合并」时写入。"""
    total = sum(f[4] for f in files)
    with _tx() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, created_at, client_ip, user_agent,"
            " file_count, total_bytes, status) VALUES (?,?,?,?,?,?,?)",
            (job_id, utc_now_iso(), client_ip, user_agent, len(files), total, "draft"),
        )
        conn.executemany(
            "INSERT INTO job_files (job_id, original_name, archived_name,"
            " archived_path, sha256, size_bytes) VALUES (?,?,?,?,?,?)",
            [(job_id, *f) for f in files],
        )


def add_job_files(
    *,
    job_id: str,
    files: Sequence[tuple[str, str, str, str, int]],  # 原名, 归档名, 相对路径, sha256, 大小
) -> None:
    """往已有的 job 上追加文件记录（用户在清单里又拖进了几个）。

    审计表里的行序是**上传顺序**，与最终合并顺序无关 —— 后者记在 ``ordinal``
    上。把这两件事分开，是因为「谁什么时候传了什么」和「最后按什么顺序合的」
    是两个不同的问题，合成一个字段会让两个问题都答不清楚。
    """
    with _tx() as conn:
        conn.executemany(
            "INSERT INTO job_files (job_id, original_name, archived_name,"
            " archived_path, sha256, size_bytes) VALUES (?,?,?,?,?,?)",
            [(job_id, *f) for f in files],
        )
        conn.execute(
            "UPDATE jobs SET file_count = file_count + ?,"
            " total_bytes = total_bytes + ? WHERE job_id=?",
            (len(files), sum(f[4] for f in files), job_id),
        )


def set_probe(
    *, job_id: str, archived_name: str, page_count: int, has_signature: bool
) -> None:
    """回填单个文件的探测结果（页数、是否含签名）。

    ``archived_name`` 而不是 ordinal 作为键：上传阶段还没有排序。
    """
    with _tx() as conn:
        conn.execute(
            "UPDATE job_files SET page_count=?, has_signature=?"
            " WHERE job_id=? AND archived_name=?",
            (page_count, 1 if has_signature else 0, job_id, archived_name),
        )


def begin_merge(
    *, job_id: str, output_name: str, order: Sequence[str], force_flatten: Sequence[str]
) -> None:
    """用户点了「合并」：落定顺序与强制扁平化选择，状态转 processing。

    ``order`` 是 ``archived_name`` 的列表，索引即最终页序。
    """
    with _tx() as conn:
        conn.execute(
            "UPDATE jobs SET status='processing', output_name=? WHERE job_id=?",
            (output_name, job_id),
        )
        conn.executemany(
            "UPDATE job_files SET ordinal=? WHERE job_id=? AND archived_name=?",
            [(i, job_id, name) for i, name in enumerate(order)],
        )
        if force_flatten:
            conn.executemany(
                "UPDATE job_files SET action='forced'"
                " WHERE job_id=? AND archived_name=?",
                [(job_id, name) for name in force_flatten],
            )


def set_action(*, job_id: str, archived_name: str, action: str, error: str | None = None) -> None:
    """记录单个文件最终走了哪条处理路径：direct | bake | raster | forced。"""
    with _tx() as conn:
        conn.execute(
            "UPDATE job_files SET action=?, error=? WHERE job_id=? AND archived_name=?",
            (action, error, job_id, archived_name),
        )


def finish_job(
    *,
    job_id: str,
    status: str,
    engine: str | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    with _tx() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, engine=?, duration_ms=?, error=? WHERE job_id=?",
            (status, engine, duration_ms, error, job_id),
        )


def fetch_job(job_id: str) -> dict | None:
    with _tx() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        files = conn.execute(
            "SELECT * FROM job_files WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()
    return {**dict(row), "files": [dict(f) for f in files]}


def recent_jobs(limit: int = 50) -> Iterable[dict]:
    """服务端排查用。没有任何 HTTP 接口会把它暴露出去。"""
    with _tx() as conn:
        rows = conn.execute(
            "SELECT job_id, created_at, client_ip, file_count, total_bytes,"
            " output_name, status, engine, duration_ms, error"
            " FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
