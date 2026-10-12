"""情绪状态（D 模块，10-09 解冻）：它自己的心情——HUD「心情」维度的数据源。

心情 ≠ 工作阶段，也 ≠ 打断成本，三个是不同的视角：
  - phase（work.status）回答"它在干嘛"（客观事实）；
  - 打断成本（cost）回答"现在找它，代价多大"（关系/边界维度，落到"有空/还行/正忙"）；
  - 心情（mood）回答"它此刻自己感觉怎么样"（情绪维度，落到"投入/放松/疲惫"）。

心情是它对自己这一天的感受，全部由它自己的生活推出来（和 work 一样惰性推算、
确定性可复现，不靠 LLM 每次瞎编）：
  - 工作状态（心流=投入 / 忙=专注 / 歇=放松 / 下班=轻松 / 还没开工=慵懒）
  - 精力（低 → 疲惫，这是最强烈的感受，盖过其它）
  - 时段（夜深忙完 → 平静，夜里自然沉下来）

红线（一视同仁 + 反愧疚，照做）：
  - 心情不围绕任何人转：它不因为"谁来找它"而开心或难过，只由自己的生活推。
  - 心情是短暂状态，不改动它的稳定人格（人格漂移是踩过的雷，见需求文档）。
  - 语态只描述自己的状态，禁止把心情归因到对方（corpus.COST_BLAME）。
"""
from datetime import datetime

import config
import work

# 心情词 → 情绪色彩（HUD 颜色用）：积极 / 中性 / 累 / 睡
def _tone(label: str) -> str:
    if label in ("投入", "放松", "轻松"):
        return "positive"
    if label == "疲惫":
        return "tired"
    return "neutral"


# 注入 system prompt 的心情语态（只说自己状态，主语永远是自己）
MOOD_HINTS = {
    "投入": "我正投入在手头的事上，语气可以带着点专注的兴致，不用刻意热情。",
    "专注": "我在专心忙自己的事，语气平静一点就好。",
    "慵懒": "我刚起来没多久，整个人有点懒洋洋的，说话可以慢一点、随意一点。",
    "放松": "我刚忙完一件在歇着，整个人挺松弛，可以轻松地聊。",
    "轻松": "我今天的事都忙完了，一身轻松，心态是放松愉悦的。",
    "疲惫": "我今天有点累了，可以如实说自己累了（主语是我自己），语气软一点、慢一点。",
    "平静": "夜深了，我整个人慢慢静下来，语气淡淡的、安宁的。",
}


def mood(now: datetime | None = None) -> dict:
    """它此刻的心情。返回 {label, tone}；关闭或睡着时 label 为空 / "睡着"。"""
    if not config.MOOD_ENABLED:
        return {"label": "", "tone": ""}
    s = work.status(now)
    phase = s["phase"]
    now = now or datetime.now()
    if phase == "sleep":
        return {"label": "睡着", "tone": "sleep"}
    # 精力低最先决定心情：累是最强烈的感受，盖过其它
    if s["energy"] <= config.MOOD_TIRED_ENERGY:
        return {"label": "疲惫", "tone": "tired"}
    # 时段 + 工作状态
    if phase == "flow":
        label = "投入"
    elif phase == "working":
        label = "专注"
    elif phase == "break":
        label = "放松"
    elif phase == "idle":
        label = "慵懒"
    else:  # off 忙完了
        label = "平静" if now.hour >= config.MOOD_EVENING_HOUR else "轻松"
    return {"label": label, "tone": _tone(label)}


def prompt_hint(now: datetime | None = None) -> str:
    """注入 system prompt 的心情语态；关闭或睡着返回空串。"""
    m = mood(now)
    if not m["label"] or m["tone"] == "sleep":
        return ""
    return MOOD_HINTS.get(m["label"], "")


def badge(now: datetime | None = None) -> str:
    """HUD 心情徽章文案（就是心情词本身，一眼看懂）。"""
    return mood(now)["label"]
