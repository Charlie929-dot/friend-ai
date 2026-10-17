"""娱乐：和朋友玩游戏（MVP：成语接龙 / 海龟汤）。

设计：
- **游戏不是模式**（v0.104 改）：开局只往意识流里注一条——"我们开始玩X了，我想想
  规则是…"，之后 TA 该怎么聊就怎么聊，聊着聊着顺手玩两把，想聊别的就跟着聊。
  不再有【娱乐模式】整块塞进 system prompt（那样每轮都被按着头打游戏）。
- 轻量状态机只记"在玩哪个、什么时候开始"，存 kv（随朋友库隔离）；
  不硬编码游戏过程，规则由 TA 自己在意识流里记着（就是它脑子里那句"我想想规则是…"）。
- 触发：用户消息里提到具体游戏名（"玩成语接龙""来局海龟汤"）→ 开局；
  说"不玩了/结束游戏"→ 结束。泛泛说"玩游戏"不触发（AI 会自然接话商量玩啥）。
- 空闲过期：45 分钟没任何消息（含 TA 自己主动问过一句也算活着）自动清局，
  防陈年规则一直挂在意识流里（卫生问题，不是状态机）。
"""
import json
from datetime import datetime, timedelta

import config
import corpus
import db

GAMES = {
    "idiom": {
        "label": "成语接龙",
        "triggers": ["成语接龙"],
        # recall = TA 脑子里回忆规则的口吻（进意识流），不是给模型的指令
        "recall": (
            "我们在玩成语接龙，我想想规则是：一人说一个成语，下一个要用上一个的"
            "最后一个字开头（同音也算），接不上、说重复或者说错就算输，输了接受小惩罚。"
            "这是我们一起玩的一件事——TA 想聊别的我就跟着聊，别把天聊成游戏问答。"
        ),
    },
    "soup": {
        "label": "海龟汤",
        "triggers": ["海龟汤"],
        "recall": (
            "我们在玩海龟汤，我想想规则是：我出题的话只给汤面，TA 问我只答"
            "「是/不是/不重要/部分是」，TA 猜出汤底我就把整段故事揭晓；"
            "轮到我猜时，我每轮只问一个「是/否」问题，问完就停下来等 TA 答。"
            "这是我们一起玩的一件事——TA 想聊别的我就跟着聊，别把天聊成游戏问答。"
        ),
    },
}

# 结束游戏的信号（在 corpus.GAME_STOP_WORDS 统一维护）

def current() -> dict | None:
    """当前游戏状态（{"game": key, "started_at": iso}），没在玩返回 None。

    带空闲过期（v0.103 批 1）：空闲 = now − max(started_at, 消息表最后一条消息时间)，
    超过 `GAME_IDLE_EXPIRE_MINUTES` 直接清 kv（**不经 `stop()`**——`stop()` 会递归
    调 `current()` 套娃，审查已踩明的坑）。
    """
    raw = db.get_kv("game_state")
    if not raw:
        return None
    try:
        g = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(g, dict) or not g.get("game"):
        return None
    # 空闲过期：任何一方（含 TA 自己主动问过一句）都算这局还活着
    last_active = None
    try:
        started = datetime.fromisoformat(g.get("started_at", ""))
    except (ValueError, TypeError):
        started = None
    last_msg = db.last_message_time()
    try:
        last_active = datetime.fromisoformat(last_msg) if last_msg else started
    except (ValueError, TypeError):
        last_active = started
    if not last_active:
        return g          # 没有 started_at 也没消息，无法判断，先按在玩处理
    if datetime.now() - last_active > timedelta(minutes=config.GAME_IDLE_EXPIRE_MINUTES):
        db.set_kv("game_state", "")     # 直接清，不经 stop()（防套娃递归）
        return None
    return g


def _note(text: str, now: datetime | None = None) -> int:
    """往意识流里记一条游戏相关的念头（关意识流时静默跳过）。"""
    if not config.CONSCIOUS_STREAM_ENABLED:
        return 0
    try:
        import stream
        return stream.append("game", text, now)
    except Exception:
        return 0


def start(key: str) -> None:
    db.set_kv("game_state", json.dumps(
        {"game": key, "started_at": datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False))
    g = GAMES.get(key)
    if g:
        _note(f"我们开始玩【{g['label']}】了，我想想规则是：{g['recall']}")


def stop() -> dict | None:
    """结束当前游戏，返回结束前的状态（没有则 None）。"""
    g = current()
    if g:
        db.set_kv("game_state", "")
        label = GAMES.get(g.get("game"), {}).get("label", "游戏")
        _note(f"【{label}】玩到这儿，不玩了")
    return g


def detect_start(text: str) -> str | None:
    """用户消息里提到具体游戏名 → 返回游戏 key（已在游戏中则换个游戏也算）。"""
    for key, g in GAMES.items():
        if any(t in text for t in g["triggers"]):
            return key
    return None


def detect_stop(text: str) -> bool:
    return any(w in text for w in corpus.GAME_STOP_WORDS)


def on_user_text(text: str) -> None:
    """在 Engine.on_user_message 里调用：根据用户的话开/关游戏。"""
    if detect_stop(text) and current():
        stop()
        return
    key = detect_start(text)
    if key:
        start(key)  # 提到新游戏名直接开（已在玩等于换一个）
