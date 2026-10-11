"""表情包池（E 模块，2026-10-11）：统一表情库存按情绪分桶，治「表情单一 😂 反复用」。

设计（用户拍板）：
- **内置默认桶 + 表情**在 `corpus.EMOJI_POOL`（所有朋友的共同起点，不可删）。
- **用户新增的桶/表情**存 `emoji_pool.json`（项目根，全局共享——换朋友不换表情库，
  与 `friend.db` 一样是本机运行时数据、不进 git）。
- 每个表情 = {"text": 表情本身, "emotion": 情绪状态描述, "img": 表情图文件名（可空，
  v0.70 落地：用户新增表情可传图，存 `emoji_images/`，聊天里整条只发这个表情时渲染成图）}。
- **防重样**：`recent_seen()` 扫最近几条 TA 消息，提取已经用过的表情，注入时提醒别重复。
- **注入**：`prompt_hint()` 让 TA 聊天前"翻看库存"，允许只甩一个表情当懒人回应，
  并尊重人格 `voice.emoji` 的频率/禁止（几乎不用 emoji 的人格不注入；禁止的排除）。
"""
import base64
import binascii
import hashlib
import json
import random
import threading

import config
import corpus
import db

_lock = threading.Lock()

# 表情图：只认这几种格式（按文件魔数判，不信扩展名）
_IMG_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
              "gif": "image/gif", "webp": "image/webp"}
_IMG_MAX_BYTES = 2 * 1024 * 1024   # 单张限 2MB，表情包足够了


def _custom_path():
    return config.BASE_DIR / "emoji_pool.json"


def _img_dir():
    """表情图目录（项目根 emoji_images/，运行时数据、不进 git）。"""
    d = config.BASE_DIR / "emoji_images"
    d.mkdir(exist_ok=True)
    return d


def save_img(data_b64: str) -> tuple[str, str]:
    """存一张表情图（base64，可带 data: 前缀）。返回 (文件名, 错误)。
    文件名 = 内容 md5 前 16 位 + 按魔数判出的扩展名（同图去重）。"""
    raw = None
    if data_b64:
        s = data_b64.strip()
        if s.startswith("data:") and "," in s:
            s = s.split(",", 1)[1]
        try:
            raw = base64.b64decode(s, validate=True)
        except (binascii.Error, ValueError):
            return "", "图片数据不是合法的 base64"
    if not raw:
        return "", "图片数据是空的"
    if len(raw) > _IMG_MAX_BYTES:
        return "", "图片太大（限 2MB）"
    ext = ""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = "png"
    elif raw.startswith(b"\xff\xd8\xff"):
        ext = "jpg"
    elif raw.startswith((b"GIF87a", b"GIF89a")):
        ext = "gif"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        ext = "webp"
    if not ext:
        return "", "只支持 png / jpg / gif / webp 图片"
    name = hashlib.md5(raw).hexdigest()[:16] + "." + ext
    try:
        (_img_dir() / name).write_bytes(raw)
    except OSError as e:
        return "", f"图片写不进去：{e}"
    return name, ""


def img_file(name: str):
    """按文件名取表情图路径。名字不合法或文件不存在返回 None（防目录穿越）。"""
    name = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not name or "." not in name or ".." in name:
        return None
    if name.rsplit(".", 1)[-1].lower() not in _IMG_TYPES:
        return None
    p = _img_dir() / name
    return p if p.is_file() else None


def img_type(name: str) -> str:
    return _IMG_TYPES.get((name or "").rsplit(".", 1)[-1].lower(),
                          "application/octet-stream")


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


def add_emoji(bucket: str, text: str, emotion: str = "", img: str = "") -> tuple[bool, str]:
    """往某个桶加一个表情（内置桶或已存在的自定义桶都行；桶不存在自动新建）。
    text/emotion 会去首尾空白。同桶内 text 重复返回 (False, 原因)。
    img 是 `save_img()` 存好的文件名（可空；内置桶不允许带图——内置库存只有文字）。"""
    bucket = (bucket or "").strip()
    text = (text or "").strip()
    if not bucket or not text:
        return False, "桶名和表情都不能为空"
    img = (img or "").strip()
    with _lock:
        data = _load_custom()
        lst = data["buckets"].setdefault(bucket, [])
        exists = any(it.get("text") == text for it in lst) \
            or any(it.get("text") == text for it in corpus.EMOJI_POOL.get(bucket, []))
        if exists:
            return False, "这个表情已经在这个桶里了"
        lst.append({"text": text, "emotion": (emotion or "").strip(), "img": img})
        _save_custom(data)
    return True, ""


def remove_emoji(bucket: str, text: str) -> tuple[bool, str]:
    """删一个**自定义**表情（内置表情不可删）。带图的一并删图文件。"""
    bucket = (bucket or "").strip()
    text = (text or "").strip()
    with _lock:
        data = _load_custom()
        lst = data["buckets"].get(bucket, [])
        gone = [it for it in lst if it.get("text") == text]
        if not gone:
            return False, "没找到这个表情（内置表情不可删）"
        data["buckets"][bucket] = [it for it in lst if it.get("text") != text]
        _save_custom(data)
    for it in gone:
        img = (it.get("img") or "").strip()
        if img:
            p = img_file(img)
            if p:
                try:
                    p.unlink()
                except OSError:
                    pass
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
        gone = data["buckets"].pop(name)
        _save_custom(data)
    for it in gone:   # 连带删掉桶里自定义表情的图文件
        img = (it.get("img") or "").strip()
        if img:
            p = img_file(img)
            if p:
                try:
                    p.unlink()
                except OSError:
                    pass
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
