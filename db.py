"""SQLite 存储层：状态机、消息记录、防打扰计数。单用户场景，一切从简。"""
import json
import sqlite3
import threading
from datetime import date, datetime, timedelta

import config

# 库切换互斥锁：所有依赖"当前朋友库"一致性的多步流程（回复生成、记忆提取、
# 主动消息）与切库操作（/friend、/new-friend）互斥。
# 不修这个锁的话，后台线程（记忆提取/回复）会在用户切换朋友的瞬间
# 把数据写进换人后的库（实测出现过记忆串库）。
PATH_LOCK = threading.RLock()

# 收发入口轻锁：/api/send 的消息接收与切库操作互斥（保证"这条消息进的是哪个库"
# 有确定的先后序），但**不与回复生成互斥**——回复生成全程握 PATH_LOCK（含 LLM
# 调用和打字 sleep，可达几十秒），普通消息若也等它，就会出现"TA 回复完
# 你发的消息才发出去"的排队假象（实测 #30）。收发接收只做落库+入队，毫秒级。
# ⚠️ **必须是可重入锁（RLock）**：网页端 /api/send 外面已经握着它，斜杠命令
# （/friend /new-friend）内部切库时还要再拿一次，同一线程重入——用普通 Lock
# 会**自锁死**，表现就是网页端「换朋友」永远卡在"稍等一下"（实测 #33）。
SEND_LOCK = threading.RLock()

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
    content TEXT NOT NULL,
    quote_id INTEGER NOT NULL DEFAULT 0,       -- 引用回复：被引消息 id（0=无引用）
    quote_role TEXT NOT NULL DEFAULT '',       -- 被引消息角色（user/assistant）
    quote_content TEXT NOT NULL DEFAULT ''     -- 被引消息原文（展示 + 兜底）
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
    created_at TEXT NOT NULL,
    recalled_at TEXT NOT NULL DEFAULT '',
    recall_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok'    -- ok / pending_delete
);
CREATE TABLE IF NOT EXISTS memory_vectors (
    memory_id INTEGER PRIMARY KEY,     -- 对应 memories.id
    vec BLOB NOT NULL                  -- float32 向量（语义召回，本地 bge-small-zh-v1.5）
);
CREATE TABLE IF NOT EXISTS consciousness_stream (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,          -- ISO 时间
    day TEXT NOT NULL,         -- 生活日 YYYY-MM-DD（跨天/睡前浓缩用）
    kind TEXT NOT NULL,        -- 事件类型：wake/sleep/work/inbox/monologue/reply/revoke
    text TEXT NOT NULL         -- 第一人称行（"我起床了 / 我在想：… / 我说：…"）
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
        # 旧库迁移：A/B 两级库——A 库记忆可挂一条 B 库长文（long_id 指向 long_memories.id，0=无）
        if "long_id" not in cols:
            c.execute("ALTER TABLE memories ADD COLUMN long_id INTEGER NOT NULL DEFAULT 0")
        # 旧库迁移：艾宾浩斯衰减（decay_level=0 原文，压到 DECAY_MAX_LEVEL 就删；pinned=1 永久）
        if "decay_level" not in cols:
            c.execute("ALTER TABLE memories ADD COLUMN decay_level INTEGER NOT NULL DEFAULT 0")
        if "pinned" not in cols:
            c.execute("ALTER TABLE memories ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        # 旧库迁移：对话引用（前端"引用回复"，quote_id/quote_role/quote_content）
        mcols = [r["name"] for r in c.execute("PRAGMA table_info(messages)")]
        if "quote_id" not in mcols:
            c.execute("ALTER TABLE messages ADD COLUMN quote_id INTEGER NOT NULL DEFAULT 0")
        if "quote_role" not in mcols:
            c.execute("ALTER TABLE messages ADD COLUMN quote_role TEXT NOT NULL DEFAULT ''")
        if "quote_content" not in mcols:
            c.execute("ALTER TABLE messages ADD COLUMN quote_content TEXT NOT NULL DEFAULT ''")


def clear_all():
    """清空所有数据（/new-friend 用），保留表结构。"""
    with _conn() as c:
        for table in ("kv", "messages", "counters", "memories", "long_memories",
                      "memory_vectors", "consciousness_stream"):
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
def add_message(role: str, content: str, kind: str = "chat", quote: dict | None = None) -> int:
    """落一条消息，返回它的 id（前端回显后靠它推进轮询游标——
    不然手动回显的消息 900ms 后又被轮询渲染一遍，出现"发送两次"，实测 #25）。

    quote：可选，引用回复。契约 = {"id": 被引消息 id, "role": "user"|"assistant",
    "content": 被引消息原文}。这里只存——"让 TA 回复时知道你在引用哪句"已由
    core._generate_reply 透传给 context.build_chat_messages 注入回复上下文。"""
    q = quote or {}
    qid = int(q.get("id") or 0)
    qrole = str(q.get("role") or "")
    qcontent = str(q.get("content") or "")
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO messages(ts, role, kind, content, quote_id, quote_role, quote_content) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), role, kind, content,
             qid, qrole, qcontent),
        )
        return int(cur.lastrowid or 0)


def delete_message(msg_id: int) -> dict | None:
    """撤回一条消息：只允许撤回对方自己发的（role='user'），返回被删消息
    {id, role, content} 供上层记意识流；没有这条 / 不是 user 消息返回 None。"""
    with _conn() as c:
        row = c.execute(
            "SELECT id, role, content FROM messages WHERE id=?", (msg_id,)
        ).fetchone()
        if not row or row["role"] != "user":
            return None
        c.execute("DELETE FROM messages WHERE id=?", (msg_id,))
        return dict(row)


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
            "SELECT id, ts, role, content, quote_id, quote_role, quote_content FROM messages "
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
               theme: str = "", long_id: int = 0) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO memories(content, category, tags, time_label, place, person, theme, created_at, long_id)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (content, category, tags, time_label, place, person, theme,
             datetime.now().isoformat(timespec="seconds"), long_id),
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
        c.execute("DELETE FROM memory_vectors WHERE memory_id=?", (mem_id,))
        return row["content"]


# --- 记忆向量（语义召回：本地 bge-small-zh-v1.5 的 float32 向量，BLOB 存） ---
def set_memory_vector(mem_id: int, vec: bytes):
    with _conn() as c:
        c.execute(
            "INSERT INTO memory_vectors(memory_id, vec) VALUES(?, ?) "
            "ON CONFLICT(memory_id) DO UPDATE SET vec=excluded.vec",
            (mem_id, vec),
        )


def get_memory_vector(mem_id: int) -> bytes | None:
    with _conn() as c:
        row = c.execute(
            "SELECT vec FROM memory_vectors WHERE memory_id=?", (mem_id,)
        ).fetchone()
    return row["vec"] if row else None


def get_memory_vectors(mem_ids: list[int]) -> dict[int, bytes]:
    """批量取向量：{memory_id: vec_bytes}。只查有记录的那些。"""
    if not mem_ids:
        return {}
    out = {}
    # 分块查询，避免 IN 子句参数超限（记忆量小，一般一次即可）
    for i in range(0, len(mem_ids), 500):
        chunk = mem_ids[i:i + 500]
        q = "SELECT memory_id, vec FROM memory_vectors WHERE memory_id IN (%s)" % (
            ",".join("?" * len(chunk)))
        with _conn() as c:
            rows = c.execute(q, chunk).fetchall()
        for r in rows:
            out[r["memory_id"]] = r["vec"]
    return out


def delete_memory_vector(mem_id: int):
    with _conn() as c:
        c.execute("DELETE FROM memory_vectors WHERE memory_id=?", (mem_id,))


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
                    theme: str = "") -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO long_memories(summary, content, category, tags, time_label, place, person, theme, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (summary, content, category, tags, time_label, place, person, theme,
             datetime.now().isoformat(timespec="seconds")),
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


def today_long_notes(day: str | None = None, limit: int = 3) -> list[dict]:
    """今天做过/读到的事（B 库，tags 含「今日素材」）：注入当天上下文用。

    筛选条件就是"创建于今天"——跨天自动从上下文里消失，而 B 库的记录本身
    保留（用户要的"从上下文中清除，记忆还是保留"）。
    """
    day = day or date.today().isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM long_memories WHERE tags LIKE ? AND created_at LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            (f"%{_TODAY_NOTE_TAG}%", f"{day}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


_TODAY_NOTE_TAG = "今日素材"   # B 库"今天做过/读到的事"的标记（写入在 work._gen_shareable）


def add_today_note(summary: str, content: str, title: str = "") -> int:
    """落一条"今天做过/读到的事"进 B 库（工作单元产出素材时调用，不额外调 LLM）。"""
    return add_long_memory(summary, content, "事件", _TODAY_NOTE_TAG,
                           date.today().isoformat(), "", "", "其他")


def memories_for_decay(now: datetime, idle_days: int, recall_bonus: float,
                       pin_recalls: int, max_level: int, limit: int) -> list[dict]:
    """挑出该遗忘一级的记忆：距"最后一次被想起"超过有效期 且 还没压到上限。

    有效期 = idle_days × (1 + recall_count × recall_bonus)——被想起得越多撑得越久；
    recall_count ≥ pin_recalls 的会先被标 pinned（永久），不再进这个列表。
    ⚠️ 自我记忆（tags 以 `self_` 开头，见 selfmem.py）**不参与遗忘**——
    它们是人格连续性的锚（"我说过什么立场/我答应过什么"），淡忘了等于人格断裂。
    """
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM memories WHERE status='ok' AND pinned=0 "
            # 排除自我记忆：不用 LIKE（SQLite 里 `_` 是通配符、也没有默认转义符），
            # 直接取前 5 个字符比 'self_' 更不容易写错。
            "AND decay_level < ? AND (tags IS NULL OR substr(tags,1,5) <> 'self_') "
            "ORDER BY id", (max_level,)).fetchall()]
    out = []
    for m in rows:
        base = m.get("recalled_at") or m.get("created_at") or ""
        try:
            last = datetime.fromisoformat(base)
        except (ValueError, TypeError):
            continue
        allow = timedelta(days=idle_days * (1 + int(m.get("recall_count", 0)) * recall_bonus))
        if now - last >= allow:
            out.append(m)
        if len(out) >= limit:
            break
    return out


def set_memory_decay(mem_id: int, level: int, content: str):
    """更新一条记忆的衰减等级与（压缩后的）内容。"""
    with _conn() as c:
        c.execute("UPDATE memories SET decay_level=?, content=? WHERE id=?",
                  (level, content, mem_id))
        c.execute("DELETE FROM memory_vectors WHERE memory_id=?", (mem_id,))  # 内容变了，向量作废重算


def pin_memory(mem_id: int):
    """标为永久记住（被想起够多次），从此不参与衰减。"""
    with _conn() as c:
        c.execute("UPDATE memories SET pinned=1 WHERE id=?", (mem_id,))


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


# --- 自我意识流（S4「完全自我意识」）：一条持续追加的第一人称自我叙事 ---
# 事件驱动追加（作息切换 / 工作结算 / 收到留言 / 内心独白 / 回复），每轮注入
# "当前状态行 + 最近几条流"。day = 生活日（由调用方按 persona._life_day 算好传入，
# 避免 db 层 import persona 造成循环依赖）。
def add_stream(kind: str, text: str, day: str) -> int:
    """追加一条意识流，返回 id。text 是第一人称行（"我起床了 / 我在想：…"）。"""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO consciousness_stream(ts, day, kind, text) VALUES(?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), day, kind, text),
        )
        return int(cur.lastrowid or 0)


def recent_stream(limit: int, day: str | None = None) -> list[dict]:
    """最近 N 条意识流（新的在前）。day 限定只看某个生活日。"""
    with _conn() as c:
        if day:
            rows = c.execute(
                "SELECT * FROM consciousness_stream WHERE day=? "
                "ORDER BY id DESC LIMIT ?", (day, limit)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM consciousness_stream "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def stream_for_day(day: str) -> list[dict]:
    """某生活日的全部意识流（按时间正序），睡前浓缩成 self_* 用。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM consciousness_stream WHERE day=? ORDER BY id", (day,)
        ).fetchall()
    return [dict(r) for r in rows]


def purge_stream_before(day: str) -> int:
    """删除某生活日之前的意识流（睡前浓缩后清理旧流，防止无限增长）。"""
    with _conn() as c:
        cur = c.execute("DELETE FROM consciousness_stream WHERE day < ?", (day,))
        return cur.rowcount
