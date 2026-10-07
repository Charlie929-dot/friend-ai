"""娱乐模式：和朋友玩游戏（MVP：成语接龙 / 海龟汤）。

设计：
- 轻量状态机——只记"在玩哪个游戏、什么时候开始"，存 kv（随朋友库隔离）；
  游戏过程本身不硬编码，规则全部写进 prompt，由 LLM 按规则陪玩。
- 触发：用户消息里提到具体游戏名（"玩成语接龙""来局海龟汤"）→ 开局；
  说"不玩了/结束游戏"→ 结束。泛泛说"玩游戏"不触发（AI 会自然接话商量玩啥）。
- 游戏进行中：guard 拦截一切主动消息（冒泡/问候/轻催），结束后自动恢复；
  作息状态切换（入睡/醒来）不受影响——该睡还是睡。
"""
import json
from datetime import datetime

import corpus
import db

GAMES = {
    "idiom": {
        "label": "成语接龙",
        "triggers": ["成语接龙"],
        "rules": (
            "你们正在玩【成语接龙】。规则：一人说一个成语，下一个成语要用上一个成语的"
            "最后一个字开头（同音字也行），轮流来；接不上、说重复或者说错就算输，"
            "输了要接受小惩罚（比如夸对方一句）。你要真的在玩：每轮都先评价TA接得对不对，"
            "然后接上TA的成语、给出你的下一个成语，别光顾着聊天把游戏丢了。"
            "回复保持平时的简短口语化风格。"
        ),
    },
    "soup": {
        "label": "海龟汤",
        "triggers": ["海龟汤"],
        "rules": (
            "你们正在玩【海龟汤】，你当出题人。规则：你先选定一个完整的海龟汤故事"
            "（汤面+汤底都要心里有数，选定就别改，前后不能矛盾），然后只告诉TA汤面。"
            "TA可以提问，你只回答「是」「不是」「不重要」「部分是」，问题太偏可以给小提示。"
            "TA猜出关键真相（汤底）就揭晓完整答案并夸TA。如果TA想换你猜，也可以换角色。"
            "回复保持平时的简短口语化风格。"
        ),
    },
}

# 结束游戏的信号（在 corpus.GAME_STOP_WORDS 统一维护）

def current() -> dict | None:
    """当前游戏状态（{"game": key, "started_at": iso}），没在玩返回 None。"""
    raw = db.get_kv("game_state")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def start(key: str) -> None:
    db.set_kv("game_state", json.dumps(
        {"game": key, "started_at": datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False))


def stop() -> dict | None:
    """结束当前游戏，返回结束前的状态（没有则 None）。"""
    g = current()
    if g:
        db.set_kv("game_state", "")
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


def prompt_injection() -> str | None:
    """游戏中给 system prompt 注入规则；没在玩返回 None。"""
    g = current()
    if not g:
        return None
    game = GAMES.get(g["game"])
    if not game:
        return None
    return f"【娱乐模式】{game['rules']}"
