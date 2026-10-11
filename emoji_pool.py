"""表情包池（E 模块，2026-10-11）：统一表情库存按情绪分桶，治「表情单一 😂 反复用」。

设计（用户拍板）：
- **内置默认桶 + 表情**在 `corpus.EMOJI_POOL`（所有朋友的共同起点，不可删）。
- **用户新增的桶/表情**存 `emoji_pool.json`（项目根，全局共享——换朋友不换表情库，
  与 `friend.db` 一样是本机运行时数据、不进 git）。
- 每个表情 = {"text": 表情本身, "emotion": 情绪状态描述, "img": 预留真图链接}。
- **防重样**：`recent_seen()` 扫最近几条 TA 消息，提取已经用过的表情，注入时提醒别重复。
- **注入**：`prompt_hint()` 让 TA 聊天前"翻看库存"，允许只甩一个表情当懒人回应，
  并尊重人格 `voice.emoji` 的频率/禁止（几乎不用 emoji 的人格不注入；禁止的排除）。
"""
import json
import random
import threading

import config
import corpus
import db

_lock = threading.Lock()


def _custom_path():
    return config.BASE_DIR / "emoji_pool.json"


def _load_custom() -> dict:
    """读用户新增的桶/表情。文件不存在/损坏一律回退空库存（不崩）。"""
    try:
        raw = json.loads(_custom_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"buckets": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("buckets"), dict):
        return {"buckets": {}}
    return raw


def _save_custom(data: dict) -> None:
    _custom_path().write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --- 读 ---
def buckets() -> list[str]:
    """全部桶名：内置在前，用户新增的接在后面。"""
    names = list(corpus.EMOJI_POOL.keys())
    for name in _load_custom()["buckets"]:
        if name not in names:
            names.append(name)
    return names


def items(bucket: str) -> list[dict]:
    """某个桶的全部表情：内置 + 用户新增。"""
    out = list(corpus.EMOJI_POOL.get(bucket, []))
    out.extend(_load_custom()["buckets"].get(bucket, []))
    return out


def payload() -> list[dict]:
    """给前端的完整表情池：桶 + 表情，带 builtin 标记（前端靠它决定能否删）。"""
    builtin = set(corpus.EMOJI_POOL.keys())
    result = []
    for name in buckets():
        lst = []
        for it in corpus.EMOJI_POOL.get(name, []):
            lst.append({"text": it["text"], "emotion": it.get("emotion", ""),
                        "img": it.get("img", ""), "builtin": True})
        for it in _load_custom()["buckets"].get(name, []):
            lst.append({"text": it.get("text", ""), "emotion": it.get("emotion", ""),
                        "img": it.get("img", ""), "builtin": False})
        result.append({"name": name, "builtin": name in builtin, "items": lst})
    return result


# --- 取用 / 防重样 ---
def pick(bucket: str, seen: set | None = None) -> dict | None:
    """从某桶随机抽一个表情，尽量避开 seen（同一场对话不重样）。桶空返回 None。"""
    pool = items(bucket)
    if not pool:
        return None
    seen = seen or set()
    fresh = [it for it in pool if it.get("text") not in seen]
    if not fresh:  # 桶里全被用过（极少），退回全桶重抽
        fresh = pool
    return random.choice(fresh)


def recent_seen(n: int | None = None) -> set[str]:
    """最近几条 TA 消息里出现过的表情 text（防重样：同一场对话不重样）。"""
    n = n or config.EMOJI_POOL_RECENT_N
    if n <= 0:
        return set()
    all_texts = {it.get("text") for b in buckets() for it in items(b) if it.get("text")}
    seen = set()
    for content in db.recent_assistant_messages(n):
        for t in all_texts:
            if t in content:
                seen.add(t)
    return seen


# --- 维护（用户新增桶/表情/删除） ---
def add_bucket(name: str) -> tuple[bool, str]:
    """新增一个情绪桶。内置桶名/已存在返回 (False, 原因)。"""
    name = (name or "").strip()
    if not name:
        return False, "桶名不能为空"
    if name in corpus.EMOJI_POOL:
        return False, "这是内置桶，已存在"
    with _lock:
        data = _load_custom()
        if name in data["buckets"]:
            return False, "这个桶已经存在"
        data["buckets"][name] = []
        _save_custom(data)
    return True, ""


def add_emoji(bucket: str, text: str, emotion: str = "") -> tuple[bool, str]:
    """往某个桶加一个表情（内置桶或已存在的自定义桶都行；桶不存在自动新建）。
    text/emotion 会去首尾空白。同桶内 text 重复返回 (False, 原因)。"""
    bucket = (bucket or "").strip()
    text = (text or "").strip()
    if not bucket or not text:
        return False, "桶名和表情都不能为空"
    with _lock:
        data = _load_custom()
        lst = data["buckets"].setdefault(bucket, [])
        exists = any(it.get("text") == text for it in lst) \
            or any(it.get("text") == text for it in corpus.EMOJI_POOL.get(bucket, []))
        if exists:
            return False, "这个表情已经在这个桶里了"
        lst.append({"text": text, "emotion": (emotion or "").strip(), "img": ""})
        _save_custom(data)
    return True, ""


def remove_emoji(bucket: str, text: str) -> tuple[bool, str]:
    """删一个**自定义**表情（内置表情不可删）。"""
    bucket = (bucket or "").strip()
    text = (text or "").strip()
    with _lock:
        data = _load_custom()
        lst = data["buckets"].get(bucket, [])
        new = [it for it in lst if it.get("text") != text]
        if len(new) == len(lst):
            return False, "没找到这个表情（内置表情不可删）"
        data["buckets"][bucket] = new
        _save_custom(data)
    return True, ""


def remove_bucket(name: str) -> tuple[bool, str]:
    """删一个**自定义**桶（连同里面的表情）。内置桶不可删。"""
    name = (name or "").strip()
    if name in corpus.EMOJI_POOL:
        return False, "内置桶不可删"
    with _lock:
        data = _load_custom()
        if name not in data["buckets"]:
            return False, "没有这个桶"
        del data["buckets"][name]
        _save_custom(data)
    return True, ""


# --- 注入 TA 的上下文 ---
def prompt_hint() -> str:
    """注入 TA 的表情库存：让 TA 聊天前"翻看"库存、允许只甩一个表情当懒人回应、
    提醒最近用过的别重复。尊重人格 voice.emoji（几乎不用 → 不注入；禁止的排除）。
    关闭开关或没有可用的表情返回空串。"""
    if not config.EMOJI_POOL_ENABLED:
        return ""
    import persona
    k = persona._kernel() or {}
    em = (k.get("voice") or {}).get("emoji") or {}
    freq = em.get("频率", "")
    if freq in ("极低", "几乎不用", "很低"):
        return ""
    banned = set(em.get("禁止") or [])

    lines = ["【你的表情库存】你手头有这些表情/颜文字，聊天时可以用——"
             "也可以只甩一个表情当回应、不配文字（像真人有话懒得说）。"]
    for name in buckets():
        pool = [it for it in items(name) if it.get("text") not in banned]
        if not pool:
            continue
        shown = "、".join(f"{it['text']}（{it.get('emotion', '')}）" for it in pool[:3])
        lines.append(f"- {name}：{shown}")
    seen = recent_seen()
    if seen:
        lines.append("（你最近已经用过这些，同一场对话尽量别重复："
                     + "、".join(sorted(seen)) + "）")
    return "\n".join(lines)
