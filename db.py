"""SQLite 存储层：状态机、消息记录、防打扰计数。单用户场景，一切从简。"""
import json
import sqlite3
from datetime import date, datetime

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,          -- ISO 时间
    role TEXT NOT NULL,        -- user / assistant
    kind TEXT NOT NULL,        -- chat / proactive / system
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    date TEXT NOT NULL,        -- YYYY-MM-DD
    key TEXT NOT NULL,         -- proactive_count / unanswered_proactive
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, key)
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,     -- 记忆正文（一句话）
    category TEXT NOT NULL,    -- 类别：画像/偏好/计划/事件/人际/情绪
    tags TEXT NOT NULL DEFAULT '',    -- 自由补充标签，逗号分隔
    time_label TEXT NOT NULL DEFAULT '',  -- 事发时间标签（绝对日期，如"2026-10-06 下午"）
    place TEXT NOT NULL DEFAULT '',       -- 地点标签
    person TEXT NOT NULL DEFAULT '',      -- 人物标签
    theme TEXT NOT NULL DEFAULT '',       -- 主题标签（互斥大类）
    created_at TEXT NOT NULL,  -- 存储时间（什么时候记下的，与事发时间是两个维度）
    recalled_at TEXT NOT NULL DEFAULT '',  -- 上次被召回时间
    recall_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok'  -- ok / pending_delete（AI 提议删除，待用户确认）
);
"""


def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with _conn() as c:
        c.executescript(_SCHEMA)
        # 旧库迁移：记忆五维标签（时间/地点/人物/主题）后加的列
        cols = [r["name"] for r in c.execute("PRAGMA table_info(memories)")]
        for col in ("time_label", "place", "person", "theme"):
            if col not in cols:
                c.execute(f"ALTER TABLE memories ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        # 旧库迁移：删除待确认状态（记忆系统 v2）
        if "status" not in cols:
            c.execute("ALTER TABLE memories ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'")


def clear_all():
    """清空所有数据（/new-friend 用），保留表结构。"""
    with _conn() as c:
        for table in ("kv", "messages", "counters", "memories"):
            c.execute(f"DELETE FROM {table}")


# --- 状态机 ---
def get_state() -> str:
    with _conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key='state'").fetchone()
        return row["value"] if row else "AWAKE"


def set_state(state: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO kv(key, value) VALUES('state', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (state,),
        )


def get_kv(key: str, default: str = "") -> str:
    with _conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_kv(key: str, value: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# --- 消息记录 ---
def add_message(role: str, content: str, kind: str = "chat"):
    with _conn() as c:
        c.execute(
            "INSERT INTO messages(ts, role, kind, content) VALUES(?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), role, kind, content),
        )


def recent_context(turns: int) -> list[dict]:
    """最近 N 轮对话，拼 LLM 上下文用。proactive 消息也算 assistant 发言。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content FROM messages WHERE kind IN ('chat','proactive') "
            "ORDER BY id DESC LIMIT ?",
            (turns * 2,),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def messages_page(after_id: int, limit: int) -> list[dict]:
    """按 id 顺序取历史消息（记忆回填用），after_id 之后的一批。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ts, role, content FROM messages "
            "WHERE kind IN ('chat','proactive') AND id > ? ORDER BY id LIMIT ?",
            (after_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def last_user_message_ts() -> datetime | None:
    with _conn() as c:
        row = c.execute(
            "SELECT ts FROM messages WHERE role='user' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return datetime.fromisoformat(row["ts"]) if row else None


# --- 事实便签：用户亲口告诉 AI、需要长期记住的事（记忆系统 P1 的轻量前身） ---
def get_facts() -> list[str]:
    raw = get_kv("facts")
    return json.loads(raw) if raw else []


def add_fact(text: str) -> int:
    facts = get_facts()
    facts.append(text)
    set_kv("facts", json.dumps(facts, ensure_ascii=False))
    return len(facts)


def delete_fact(index: int) -> str | None:
    facts = get_facts()
    if 1 <= index <= len(facts):
        removed = facts.pop(index - 1)
        set_kv("facts", json.dumps(facts, ensure_ascii=False))
        return removed
    return None


# --- 防打扰计数 ---
def incr_counter(key: str) -> int:
    today = date.today().isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO counters(date, key, value) VALUES(?, ?, 1) "
            "ON CONFLICT(date, key) DO UPDATE SET value = value + 1",
            (today, key),
        )
        row = c.execute(
            "SELECT value FROM counters WHERE date=? AND key=?", (today, key)
        ).fetchone()
        return row["value"]


def get_counter(key: str) -> int:
    today = date.today().isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT value FROM counters WHERE date=? AND key=?", (today, key)
        ).fetchone()
        return row["value"] if row else 0


def reset_counter(key: str):
    today = date.today().isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO counters(date, key, value) VALUES(?, ?, 0) "
            "ON CONFLICT(date, key) DO UPDATE SET value = 0",
            (today, key),
        )


# --- 记忆系统 v1：AI 自动从对话提取的长期记忆（五维标签） ---
def add_memory(content: str, category: str = "画像", tags: str = "",
               time_label: str = "", place: str = "", person: str = "",
               theme: str = "") -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO memories(content, category, tags, time_label, place, person, theme, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (content, category, tags, time_label, place, person, theme,
             datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def all_memories(limit: int = 200) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM memories ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def find_memory_by_keyword(keyword: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM memories WHERE content LIKE ? ORDER BY id DESC LIMIT 1",
            (f"%{keyword}%",),
        ).fetchone()
    return dict(row) if row else None


def update_memory(mem_id: int, content: str, tags: str | None = None):
    with _conn() as c:
        if tags is None:
            c.execute("UPDATE memories SET content=? WHERE id=?", (content, mem_id))
        else:
            c.execute("UPDATE memories SET content=?, tags=? WHERE id=?",
                      (content, tags, mem_id))


def delete_memory(mem_id: int) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT content FROM memories WHERE id=?", (mem_id,)).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM memories WHERE id=?", (mem_id,))
        return row["content"]


def set_memory_status(mem_id: int, status: str):
    """status: ok / pending_delete（AI 提议删除，待用户确认）。"""
    with _conn() as c:
        c.execute("UPDATE memories SET status=? WHERE id=?", (status, mem_id))


def pending_delete_memories() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM memories WHERE status='pending_delete' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def touch_memory(mem_id: int):
    """召回时更新统计。"""
    with _conn() as c:
        c.execute(
            "UPDATE memories SET recalled_at=?, recall_count=recall_count+1 WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), mem_id),
        )


def memory_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
