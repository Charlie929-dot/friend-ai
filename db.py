"""SQLite 存储层：状态机、消息记录、防打扰计数。单用户场景，一切从简。"""
import json
import re
import sqlite3
import threading
from datetime import date, datetime

import config

# 库切换互斥锁：所有依赖"当前朋友库"一致性的多步流程（回复生成、记忆提取、
# 主动消息、群聊发言）与切库操作（/friend、/new-friend、群聊/散群）互斥。
# 不修这个锁的话，后台线程（记忆提取/回复）会在用户切换朋友的瞬间
# 把数据写进换人后的库（实测出现过记忆串库）。
PATH_LOCK = threading.RLock()

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
    subject TEXT NOT NULL DEFAULT '',     -- 视角：我/对方/某人/我们（空=历史遗留未知）
    created_at TEXT NOT NULL,  -- 存储时间（什么时候记下的，与事发时间是两个维度）
    recalled_at TEXT NOT NULL DEFAULT '',  -- 上次被召回时间
    recall_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',  -- ok / pending_delete（AI 提议删除，待用户确认）
    source TEXT NOT NULL DEFAULT 'private'  -- private（1:1 提取）/ group（群聊提取）
);
CREATE TABLE IF NOT EXISTS diary (
    date TEXT PRIMARY KEY,              -- 生活日日期（YYYY-MM-DD，睡一觉才算新的一天）
    content TEXT NOT NULL,              -- 日记正文（第一人称）
    reflection TEXT NOT NULL DEFAULT '',  -- 当日复盘（答得如何/误解/感受）
    mood TEXT NOT NULL DEFAULT '',        -- 当天心情词
    created_at TEXT NOT NULL            -- 写日记的时间
);
CREATE TABLE IF NOT EXISTS long_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,             -- 文档字符串（摘要，一两句；召回只看这个）
    content TEXT NOT NULL,             -- 全文（一句两句说不完的长内容）
    category TEXT NOT NULL,            -- 类别：画像/偏好/计划/事件/人际/情绪
    tags TEXT NOT NULL DEFAULT '',
    time_label TEXT NOT NULL DEFAULT '',  -- 事发时间标签
    place TEXT NOT NULL DEFAULT '',
    person TEXT NOT NULL DEFAULT '',
    theme TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',     -- 视角：我/对方/某人/我们
    created_at TEXT NOT NULL,
    recalled_at TEXT NOT NULL DEFAULT '',
    recall_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',    -- ok / pending_delete
    source TEXT NOT NULL DEFAULT 'private'  -- private / group
);
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL,           -- 对象名字串（AI 对 TA 的称呼；真人=config.USER_NAME 作键）
    field TEXT NOT NULL,            -- 字段名（称呼/生日/城市/学校专业/作息习惯/兴趣/在忙/雷区…）
    value TEXT NOT NULL,            -- 字段值
    source TEXT NOT NULL DEFAULT 'manual',  -- manual=亲口/手填，extracted=AI 提取
    updated_at TEXT NOT NULL
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
        # 旧库迁移：记忆来源标签（信息调度层：群聊记忆 group / 1:1 记忆 private）
        if "source" not in cols:
            c.execute("ALTER TABLE memories ADD COLUMN source TEXT NOT NULL DEFAULT 'private'")
        # 旧库迁移：A/B 两级库——A 库记忆可挂一条 B 库长文（long_id 指向 long_memories.id，0=无）
        if "long_id" not in cols:
            c.execute("ALTER TABLE memories ADD COLUMN long_id INTEGER NOT NULL DEFAULT 0")
        # 旧库迁移：记忆视角 subject（多对象一视同仁：我/对方/某人/我们）
        if "subject" not in cols:
            c.execute("ALTER TABLE memories ADD COLUMN subject TEXT NOT NULL DEFAULT ''")
        lcols = [r["name"] for r in c.execute("PRAGMA table_info(long_memories)")]
        if "subject" not in lcols:
            c.execute("ALTER TABLE long_memories ADD COLUMN subject TEXT NOT NULL DEFAULT ''")
        _migrate_profiles(c)


def _migrate_profiles(c) -> None:
    """一次性把 kv.profile_text（单份用户清单）迁进 profiles 多对象表。

    - person：真人对象键 = config.USER_NAME（真人作键，"称呼"是可选字段）——
      与 persona 卡名（其他 AI 朋友）对称，都是"名字"。
    - source：profile_auto_keys 里 AI 写过的字段标 extracted，其余 manual。
    - 用 kv.profiles_migrated=1 标记，只迁一次；/new-friend 清库后自然重置。
    """
    def _kv(key: str) -> str:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""

    if _kv("profiles_migrated"):
        return
    raw = _kv("profile_text")
    locked = _kv("profile_locked")
    if locked != "1":
        # V1 老朋友（无 profile_locked）沿用全局 profile.md 的行为
        path = config.BASE_DIR / "profile.md"
        if path.exists():
            raw = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                            if l.strip() and not l.strip().startswith("#"))
    auto_keys = set(filter(None, _kv("profile_auto_keys").split(",")))
    now = datetime.now().isoformat(timespec="seconds")
    for line in raw.splitlines():
        m = re.match(r"^([^：:]{1,10})[：:]\s*(.+)$", line.strip())
        if not m:
            continue
        key, value = m.group(1).strip(), m.group(2).strip()
        if not value:
            continue
        source = "extracted" if key in auto_keys else "manual"
        c.execute("INSERT INTO profiles(person, field, value, source, updated_at) "
                  "VALUES(?, ?, ?, ?, ?)",
                  (config.USER_NAME, key, value, source, now))
    c.execute("INSERT INTO kv(key, value) VALUES('profiles_migrated', '1')")


def clear_all():
    """清空所有数据（/new-friend 用），保留表结构。"""
    with _conn() as c:
        for table in ("kv", "messages", "counters", "memories", "long_memories", "profiles"):
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


def group_messages_page(after_id: int, limit: int) -> list[dict]:
    """按 id 顺序取群聊记录（补提取群聊记忆用），after_id 之后的一批。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ts, role, content FROM messages "
            "WHERE kind='group' AND id > ? ORDER BY id LIMIT ?",
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
               theme: str = "", source: str = "private", subject: str = "",
               long_id: int = 0) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO memories(content, category, tags, time_label, place, person, theme, subject, created_at, source, long_id)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (content, category, tags, time_label, place, person, theme, subject,
             datetime.now().isoformat(timespec="seconds"), source, long_id),
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


# --- 记忆系统 v3：B 库长文本（A/B 两级库） ---
# A 库 = memories 表（概要/短内容，一句话），召回只看 A 库；B 库 = long_memories
# 表（长文本全文）。A 库记忆带 long_id 时是一条"伪链接"，指向 B 库全文——
# AI 先看摘要，觉得相关才"跳转"展开全文，避免把长文全塞进上下文。
def add_long_memory(summary: str, content: str, category: str = "事件", tags: str = "",
                    time_label: str = "", place: str = "", person: str = "",
                    theme: str = "", source: str = "private", subject: str = "") -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO long_memories(summary, content, category, tags, time_label, place, person, theme, subject, created_at, source)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (summary, content, category, tags, time_label, place, person, theme, subject,
             datetime.now().isoformat(timespec="seconds"), source),
        )
        return cur.lastrowid


def get_long_memory(long_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM long_memories WHERE id=?", (long_id,)).fetchone()
    return dict(row) if row else None


def all_long_memories(limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM long_memories ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_long_memory(long_id: int) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT summary FROM long_memories WHERE id=?", (long_id,)).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM long_memories WHERE id=?", (long_id,))
        # 断开 A 库记忆对这条长文的引用，避免留下悬空伪链接
        c.execute("UPDATE memories SET long_id=0 WHERE long_id=?", (long_id,))
        return row["summary"]


def touch_long_memory(long_id: int):
    """展开长文时更新统计。"""
    with _conn() as c:
        c.execute(
            "UPDATE long_memories SET recalled_at=?, recall_count=recall_count+1 WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), long_id),
        )


def set_long_memory_status(long_id: int, status: str):
    with _conn() as c:
        c.execute("UPDATE long_memories SET status=? WHERE id=?", (status, long_id))


def long_memory_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM long_memories").fetchone()["n"]


# --- profiles 多对象表（多对象一视同仁：每个对象一份 profile） ---
def profile_field(person: str, field: str) -> dict | None:
    """某对象某个字段的最新一行（含 value/source）。"""
    with _conn() as c:
        row = c.execute(
            "SELECT id, field, value, source FROM profiles "
            "WHERE person=? AND field=? ORDER BY id DESC LIMIT 1",
            (person, field),
        ).fetchone()
    return dict(row) if row else None


def profile_fields(person: str) -> list[dict]:
    """某对象的全部字段行（按 id 顺序）。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, field, value, source FROM profiles WHERE person=? ORDER BY id",
            (person,),
        ).fetchall()
    return [dict(r) for r in rows]


def profile_text(person: str) -> str:
    """把某对象的字段渲染成 "字段：值" 文本（旧格式，下游注入兼容）。"""
    return "\n".join(f"{r['field']}：{r['value']}" for r in profile_fields(person))


def set_profile_field(person: str, field: str, value: str, source: str = "manual") -> None:
    """写入/更新一个字段。source: manual（用户手填）/ extracted（AI 提取）。"""
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        cur = c.execute(
            "UPDATE profiles SET value=?, source=?, updated_at=? WHERE person=? AND field=?",
            (value, source, now, person, field),
        )
        if cur.rowcount == 0:
            c.execute(
                "INSERT INTO profiles(person, field, value, source, updated_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (person, field, value, source, now),
            )


def clear_profiles() -> None:
    with _conn() as c:
        c.execute("DELETE FROM profiles")


# --- 日记 + 每日回顾（P2：记忆 v3 每日回顾 + 日记系统 + 今日对话回顾线） ---
def today_messages(day: str | None = None) -> list[dict]:
    """某一天（生活日）的完整对话（回顾线）：id/ts/role/content，按时间正序。"""
    day = day or date.today().isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ts, role, content FROM messages "
            "WHERE kind IN ('chat','proactive') AND ts LIKE ? ORDER BY id",
            (day + "%",),
        ).fetchall()
    return [dict(r) for r in rows]


def add_diary(day: str, content: str, reflection: str = "", mood: str = "") -> None:
    """写一条日记（同一生活日覆盖更新）。"""
    with _conn() as c:
        c.execute(
            "INSERT INTO diary(date, content, reflection, mood, created_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET content=excluded.content, "
            "reflection=excluded.reflection, mood=excluded.mood, "
            "created_at=excluded.created_at",
            (day, content, reflection, mood, datetime.now().isoformat(timespec="seconds")),
        )


def get_diary(day: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM diary WHERE date=?", (day,)).fetchone()
    return dict(row) if row else None


def list_diary_dates() -> list[str]:
    with _conn() as c:
        rows = c.execute("SELECT date FROM diary ORDER BY date DESC").fetchall()
    return [r["date"] for r in rows]


def purge_messages_before(ts_cutoff: str) -> int:
    """回顾线彻底删除：清掉 ts_cutoff 之前的原始对话消息。返回删除条数。"""
    with _conn() as c:
        cur = c.execute("DELETE FROM messages WHERE ts < ?", (ts_cutoff,))
        return cur.rowcount
