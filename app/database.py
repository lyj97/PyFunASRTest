"""
SQLite 任务持久化：
  - 建表、CRUD 操作
  - 过期音频文件 TTL 清理（启动时自动执行）
"""
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 运行时由 config 写入 ──────────────────────────
_DB_PATH: Path = Path("tasks.db")
_AUDIO_DIR: Path = Path("task_audio")
_TTL_DAYS: int = 7


def configure(db_path: Path, audio_dir: Path, ttl_days: int) -> None:
    """由 lifespan 在启动时调用，注入路径配置。"""
    global _DB_PATH, _AUDIO_DIR, _TTL_DAYS
    _DB_PATH = db_path
    _AUDIO_DIR = audio_dir
    _TTL_DAYS = ttl_days
    _AUDIO_DIR.mkdir(parents=True, exist_ok=True)


def _connect() -> sqlite3.Connection:
    """返回一个启用 WAL 模式的 SQLite 连接（行工厂为 Row）。"""
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── 建表 / 迁移 ───────────────────────────────────

def init_db() -> None:
    """创建任务表（幂等）；如果是旧版数据库则补列。"""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id        TEXT PRIMARY KEY,
                filename       TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'pending',
                progress       INTEGER NOT NULL DEFAULT 0,
                asr_result     TEXT,           -- JSON: {text, segments}
                llm_chunks     TEXT NOT NULL DEFAULT '',
                llm_done       INTEGER NOT NULL DEFAULT 0,
                error_msg      TEXT,
                audio_path     TEXT,
                speaker_roles  TEXT,           -- JSON: 用户更正的说话人角色
                created_at     TEXT NOT NULL,
                updated_at     TEXT NOT NULL
            )
        """)
        # 兼容旧版：若缺少 speaker_roles 列则动态添加
        existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "speaker_roles" not in existing:
            conn.execute("ALTER TABLE tasks ADD COLUMN speaker_roles TEXT")
        conn.commit()
    logger.info("数据库初始化完成：%s", _DB_PATH)


# ── TTL 清理 ──────────────────────────────────────

def cleanup_expired_tasks() -> None:
    """删除超过 TTL 的已完成/出错任务及其音频文件。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_TTL_DAYS)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT task_id, audio_path FROM tasks WHERE created_at < ? AND status IN ('done','error')",
            (cutoff,),
        ).fetchall()
        for row in rows:
            if row["audio_path"]:
                p = Path(row["audio_path"])
                if p.exists():
                    try:
                        p.unlink()
                        logger.info("清理过期音频：%s", p)
                    except Exception as e:
                        logger.warning("删除音频失败 %s: %s", p, e)
            conn.execute("DELETE FROM tasks WHERE task_id = ?", (row["task_id"],))
        if rows:
            conn.commit()
            logger.info("已清理 %d 条过期任务", len(rows))


# ── CRUD ─────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_task(filename: str, audio_path: str) -> str:
    """创建新任务，返回 task_id。"""
    task_id = str(uuid.uuid4())
    now = _now()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO tasks
               (task_id, filename, status, progress, llm_chunks, llm_done, audio_path, created_at, updated_at)
               VALUES (?, ?, 'pending', 0, '', 0, ?, ?, ?)""",
            (task_id, filename, audio_path, now, now),
        )
        conn.commit()
    return task_id


def get_task(task_id: str) -> Optional[dict]:
    """根据 task_id 查询任务，不存在返回 None。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    # 反序列化 JSON 字段
    for field in ("asr_result", "speaker_roles"):
        if d.get(field):
            try:
                d[field] = json.loads(d[field])
            except Exception:
                pass
    d["llm_done"] = bool(d["llm_done"])
    return d


def list_tasks(limit: int = 50) -> list[dict]:
    """返回最近 limit 条任务（按创建时间倒序）。"""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT task_id, filename, status, progress, llm_done, error_msg, created_at, updated_at
               FROM tasks ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["llm_done"] = bool(d["llm_done"])
        result.append(d)
    return result


def update_progress(task_id: str, progress: int, status: str = "asr_running") -> None:
    """更新 ASR 进度。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET progress=?, status=?, updated_at=? WHERE task_id=?",
            (progress, status, _now(), task_id),
        )
        conn.commit()


def update_asr_result(task_id: str, asr_result: dict) -> None:
    """写入 ASR 完成结果（进度=100，状态切换为 asr_done，等待用户触发 LLM）。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET asr_result=?, progress=100, status='asr_done', updated_at=? WHERE task_id=?",
            (json.dumps(asr_result, ensure_ascii=False), _now(), task_id),
        )
        conn.commit()


def update_status(task_id: str, status: str) -> None:
    """通用状态更新。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
            (status, _now(), task_id),
        )
        conn.commit()


def save_speaker_corrections(task_id: str, corrections: dict) -> None:
    """保存用户更正的说话人角色（覆盖写入）。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET speaker_roles=?, updated_at=? WHERE task_id=?",
            (json.dumps(corrections, ensure_ascii=False), _now(), task_id),
        )
        conn.commit()


def append_llm_chunk(task_id: str, chunk: str) -> None:
    """追加 LLM 流式 chunk。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET llm_chunks = llm_chunks || ?, updated_at=? WHERE task_id=?",
            (chunk, _now(), task_id),
        )
        conn.commit()


def update_llm_done(task_id: str, error: Optional[str] = None) -> None:
    """标记 LLM 阶段完成或出错。"""
    status = "error" if error else "done"
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET llm_done=1, status=?, error_msg=?, updated_at=? WHERE task_id=?",
            (status, error, _now(), task_id),
        )
        conn.commit()


def update_task_error(task_id: str, error_msg: str) -> None:
    """标记任务全局错误（ASR 阶段失败）。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET status='error', error_msg=?, updated_at=? WHERE task_id=?",
            (error_msg, _now(), task_id),
        )
        conn.commit()


def get_audio_dir() -> Path:
    """返回音频存储目录。"""
    return _AUDIO_DIR
