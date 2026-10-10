"""语气主轨（A 模块，2026-10-10）：治语言割裂（实测 #36 #6）。

多路语气注入（mood / cost / 反谄媚）各自为政、没有主轨 → 上一秒怼、下一秒分享。
语气主轨：每轮定一个主导情绪状态（= mood），其它语气注入（cost 相处姿势）从属。

主轨 = mood（情绪）：它已经把 phase + 精力 + 时段合成一个词，就是"此刻的我"。
从属 = cost（相处姿势）：welcomed/tolerated/costly 的语态，挂在主轨之下当"松紧度"，
不再独立喊"可以多聊/简短"——情绪色彩永远跟主轨走。

排队（分享 vs 异议）：见 persona.anti_sycophancy_prompt 里的协调条款（同一句只做一件）。
"""
from datetime import datetime

import config
import cost
import mood
import work

# 从属的相处姿势：由打断成本三态推出，但措辞挂在主轨之下（"对方找你时你怎么接"）。
# 注意只描述自己的状态、不归因（反愧疚红线，见 corpus.COST_BLAME）。
_COST_STANCE = {
    "welcomed": "对方找你时你正好接得住，可以自然地多聊两句。",
    "tolerated": "对方找你，你正常回应就行，不用刻意。",
    "costly": "对方找你时你正忙/有点累，回应可以短一点、慢一点（只说自己的状态，别提对方）。",
}


def dominant(now: datetime | None = None) -> dict:
    """每轮的「语气主轨」快照：{main: 主轨(mood label), cost: 从属(cost level), phase}。

    关闭工作系统、或睡着时 main 为空（没有主轨，不注入语气块）。"""
    if not config.WORK_ENABLED:
        return {"main": "", "cost": "", "phase": "off"}
    s = work.status(now)
    if s["phase"] == "sleep":
        return {"main": "", "cost": "", "phase": "sleep"}
    m = mood.mood(now)
    c = cost.state(now)
    return {"main": m["label"], "cost": c["level"], "phase": s["phase"]}


def voice_hint(now: datetime | None = None) -> str:
    """统一的【此刻的我】语气块：主轨（mood）+ 从属（cost 相处姿势）。

    替代原来散开的「此刻的相处」+「此刻的心情」两条。关闭/睡着返回空串。"""
    d = dominant(now)
    if not d["main"]:
        return ""
    main_hint = mood.MOOD_HINTS.get(d["main"], "")
    stance = _COST_STANCE.get(d["cost"], "")
    lines = [
        f"【此刻的我】你现在的心情是「{d['main']}」——这是这一轮你说话语气的主轨，"
        f"后面所有语气都以它为准。",
    ]
    if main_hint:
        lines.append(main_hint)
    if stance:
        lines.append(f"（对方这时找你，你的松紧度）{stance}")
    return "\n".join(lines)
