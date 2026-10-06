"""朋友联动：拉群互相对话（P2 MVP）。

形态：/group <编号...> 把当前朋友和指定归档老朋友拉进一个群。
- 每位朋友用自己的人设卡、自己的记忆（临时把 db 指到TA的库）生成回复，
  CLI/IM 里每条消息显示发言人自己的名字（channel.send 的 sender 参数）。
- 防无限循环：全员回应一轮 → 提及接力（被点名的插队）→ 概率串话，
  AI 发言总轮次硬顶 MAX_AI_TURNS，之后安静下来等用户。
- 群聊期间主动冒泡/问候暂停（guard 统一拦截），散群自动恢复。
- /endgroup 散群：完整群聊记录写回每位朋友的库（kind='group'，不进 1:1
  上下文和回填范围），并各自提取一遍记忆——同一场群聊，每个人记住的
  侧重点可以不一样。
- 会话只存在于运行期（重启即散群），群聊记录靠写回各库的 kind='group' 持久。
"""
import random
import time as _time
from datetime import datetime

import config
import db
import llm
import memory
import persona

MAX_AI_TURNS = 6      # 用户每说一句话，AI 发言总轮次硬顶（防无限循环）
CROSS_TALK_PROB = 0.6  # 全员回应后，开始自由串话的概率
STOP_TALK_PROB = 0.4   # 每句串话后停下来的概率

GROUP_RULES = """【群聊模式】你现在在一个群聊里。
- 群成员：{members}。你是{self_name}，记录里其他带名字的发言都不是你说的。
- 直接说你要说的话，不要带名字前缀，不要替别人发言。
- 想跟某位成员说话就在话里带TA的名字（比如"稳稳你也太卷了"）。
- 你们都是小徐（用户）的AI朋友，彼此也认识：可以接梗、附和，也可以不同意对方。
- 回复要短，一两句话，像微信群聊。"""

_session = None


class Participant:
    """一位群成员：TA 的数据库文件 + 显示名。"""

    def __init__(self, db_path, name: str):
        self.db_path = db_path
        self.name = name


class GroupSession:
    def __init__(self, participants: list[Participant]):
        self.participants = participants
        self.transcript = []  # list[(speaker, text)]，speaker="小徐" 或朋友名
        self.active = True


def is_active() -> bool:
    return _session is not None and _session.active


def current() -> GroupSession | None:
    return _session if is_active() else None


def start(participants: list[Participant]) -> str:
    global _session
    _session = GroupSession(participants)
    names = "、".join(p.name for p in participants)
    return f"群拉好了：{names} 都在。说吧，大家都听着呢～（/endgroup 散群）"


def on_user_text(text: str, channel) -> None:
    """群里收到用户消息：全员依次回应 → 提及接力 → 概率串话，到上限就安静。"""
    s = _session
    s.transcript.append(("小徐", text))
    ai_turns = 0
    spoken = set()

    # 第一轮：每位朋友都回应；话里点了名的插队接话（每人限一次）
    queue = list(s.participants)
    while queue and ai_turns < MAX_AI_TURNS:
        p = queue.pop(0)
        if p.name in spoken:
            continue
        reply = _speak(p, s, channel)
        ai_turns += 1
        spoken.add(p.name)
        for other in s.participants:
            if (other.name != p.name and other.name not in spoken
                    and other.name in reply
                    and all(o is not other for o in queue)):
                queue.append(other)

    # 自由串话：有人想再接两句，随机来，随时停
    while ai_turns < MAX_AI_TURNS and len(s.participants) > 1:
        if random.random() >= CROSS_TALK_PROB:
            break
        _speak(random.choice(s.participants), s, channel)
        ai_turns += 1
        if random.random() < STOP_TALK_PROB:
            break


def end() -> str:
    """散群：群聊记录写回每位朋友的库，各自提取记忆。"""
    global _session
    s, _session = _session, None
    if not s:
        return "现在没有在群的。"
    real_db = config.DB_PATH
    names = "、".join(p.name for p in s.participants)
    for p in s.participants:
        try:
            config.DB_PATH = p.db_path
            for sp, tx in s.transcript:
                role = "user" if sp == "小徐" else "assistant"
                db.add_message(role, f"【群聊·{sp}】{tx}", kind="group")
            _extract_group_memories(s, p)
        finally:
            config.DB_PATH = real_db
    return f"散啦～和{names}的这场群聊，大家都各自记下了点什么。"


def _speak(p: Participant, s: GroupSession, channel) -> str:
    """让一位朋友在群里发言：临时把 db 指到TA的库，用TA的人设和记忆。"""
    real_db = config.DB_PATH
    try:
        config.DB_PATH = p.db_path
        members = "、".join([*(x.name for x in s.participants), "小徐（用户）"])
        sys_prompt = (persona.build_system_prompt("AWAKE") + "\n"
                      + GROUP_RULES.format(members=members, self_name=p.name))
        # 拿最近的群聊内容召回TA自己的旧记忆
        recent = " ".join(t for _, t in s.transcript[-6:])
        recalled = memory.recall(recent, top_k=3) if recent else []
        if recalled:
            mem_text = "\n".join("- " + m["content"] for m in recalled)
            sys_prompt += f"\n【你想起来的旧记忆】（自然使用，别生硬复述）\n{mem_text}"
        log = "\n".join(f"{sp}：{tx}" for sp, tx in s.transcript[-20:])
        msgs = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"【群聊记录】\n{log}\n\n（轮到你了，直接说内容）"},
        ]
        reply = llm.chat(msgs).strip()
        # 模型偶尔自带名字前缀，剥掉
        for sep in ("：", ":"):
            if reply.startswith(f"{p.name}{sep}"):
                reply = reply[len(p.name) + 1:].strip()
        s.transcript.append((p.name, reply))
        _send_segments(channel, p.name, reply)
        return reply
    finally:
        config.DB_PATH = real_db


def _send_segments(channel, name: str, text: str) -> None:
    """按换行拆段连发，段间小停顿，像真人连着发几条。"""
    segs = [x.strip() for x in text.split("\n") if x.strip()]
    for i, seg in enumerate(segs):
        if i > 0:
            _time.sleep(min(max(len(segs[i - 1]) * config.SEND_SECONDS_PER_CHAR,
                                config.SEND_MIN_SECONDS), config.SEND_MAX_SECONDS))
        channel.send(seg, sender=name)


def _extract_group_memories(s: GroupSession, p: Participant) -> None:
    """散群后让每位朋友从群聊记录里提取记忆（存进TA自己的库，此时 db 已指向TA）。"""
    log = "\n".join(f"{'用户' if sp == '小徐' else sp}：{tx}"
                    for sp, tx in s.transcript[-30:])
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    user_msg = (f"【现在时间】{now:%Y-%m-%d} 周{week} {now:%H:%M}\n\n"
                f"【最近对话】\n{log}")
    known = [m["content"] for m in db.all_memories(limit=50)]
    if known:
        user_msg += ("\n\n【已知记忆（内容几乎一样的不要重复提取）】\n"
                     + "\n".join(f"- {x}" for x in known[:50]))
    try:
        raw = llm.chat([
            {"role": "system", "content": memory.EXTRACT_PROMPT},
            {"role": "user", "content": user_msg},
        ], temperature=0.2)
        for it in memory._parse_json_array(raw):
            memory._apply(it)
    except Exception as e:
        print(f"[group] {p.name} 提取群聊记忆失败：{e}")
