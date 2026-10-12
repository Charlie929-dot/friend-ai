"""打断成本机制（S3 招牌）：它忙不忙、累不累，决定"现在找它"的代价有多高。

这是产品核心的落点：只有当"打断它"是有成本的、而且这个成本看得见，对方才会
自发尊重它的边界——它不是随叫随到的工具，是有自己日子的朋友。

公式（需求文档定）：costIndex = R × (1 + F)
  R = 阻力（resistance）：由当前 phase/flow 决定的客观投入度，0~1。
      flow（心流，刚进入状态）打断最伤 → R 最高；
      working（忙自己的事）次之；idle/break（闲）低；off（忙完）为 0。
  F = 疲劳（fatigue）：由精力 energy 推导，0~1，精力越低越累、被打断代价越高。
      F = (ENERGY_MAX - energy) / ENERGY_MAX

三态（阈值见 config）：
  welcomed   欢迎（它正好有空，多聊几句）
  tolerated  可以（它接得住，正常聊）
  costly     有代价（它正投入/正累，回复短、慢）

红线（反愧疚）：代价只呈现、不归因。costly 时它可以说"我正忙着/有点累"，
但主语永远是自己，禁止"你打断我/因为你"（词表 corpus.COST_BLAME，core 兜底改写）。
"""
from datetime import datetime

import config
import work

# 三态注入 system prompt 的语态（只说自己状态，红线见 corpus.COST_BLAME）
LEVEL_HINTS = {
    "welcomed": "我现在正好有空，心态松弛，不用急着结束，可以自然地多聊两句。",
    "tolerated": "我手头有不太要紧的事，正常回应就行，不必刻意。",
    "costly": "我正忙到一半（或有点累），被打断是有点代价的。"
              "可以如实说自己正忙/累，但主语是我自己，别提对方、别抱怨。",
}

# HUD 徽章文案（简短：一眼看出"现在找它合不合适"）
LEVEL_BADGE = {
    "welcomed": "有空",
    "tolerated": "还行",
    "costly": "正忙",
    "sleep": "睡着",
}


def resistance(phase: str) -> float:
    """phase → 阻力系数 R（0~1）。sleep 不参与打断成本，返回 0。"""
    table = {
        "flow": config.COST_R_FLOW,
        "working": config.COST_R_WORKING,
        "idle": config.COST_R_IDLE,
        "break": config.COST_R_BREAK,
        "off": config.COST_R_OFF,
    }
    return table.get(phase, 0.0)


def fatigue(energy: int) -> float:
    """精力 → 疲劳系数 F（0~1）。精力越低越累。"""
    if config.WORK_ENERGY_MAX <= 0:
        return 0.0
    return max(0.0, min(1.0, (config.WORK_ENERGY_MAX - energy) / config.WORK_ENERGY_MAX))


def state(now: datetime | None = None) -> dict:
    """打断成本快照。HUD / prompt / 红线兜底都读这个。

    返回：level(welcomed/tolerated/costly/sleep) + cost_index + R + F + phase + energy。
    """
    s = work.status(now)
    phase = s["phase"]
    if phase == "sleep":
        return {"level": "sleep", "cost_index": 0.0, "R": 0.0, "F": 0.0,
                "phase": "sleep", "energy": s["energy"]}
    r = resistance(phase)
    f = fatigue(s["energy"])
    ci = r * (1.0 + f)
    if ci < config.COST_WELCOMED_MAX:
        lv = "welcomed"
    elif ci < config.COST_TOLERATED_MAX:
        lv = "tolerated"
    else:
        lv = "costly"
    return {"level": lv, "cost_index": round(ci, 2), "R": r, "F": f,
            "phase": phase, "energy": s["energy"]}


def prompt_hint(now: datetime | None = None) -> str:
    """注入 system prompt 的打断成本语态；工作系统关闭或睡觉返回空串。"""
    if not config.WORK_ENABLED:
        return ""
    lv = state(now)["level"]
    if lv == "sleep":
        return ""
    return LEVEL_HINTS.get(lv, "")


def badge(now: datetime | None = None) -> str:
    """HUD 用的三态徽章文案（简短）。"""
    return LEVEL_BADGE.get(state(now)["level"], "")
