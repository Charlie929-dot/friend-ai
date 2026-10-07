"""朋友联动：拉群互相对话（P2 MVP）。

形态：/group <编号...> 把当前朋友和指定归档老朋友拉进一个群。
- 每位朋友用自己的人设卡、自己的记忆（临时把 db 指到TA的库）生成回复，
  CLI/IM 里每条消息显示发言人自己的名字（channel.send 的 sender 参数）。
- 时间感和私聊同步：用户发言有"阅读"等待、回复前有"正在输入"模拟、
  成员间接话有随机小间隔；长文本会有人先嘟囔一句"慢慢看"。
- 防无限循环：全员回应一轮 → 提及接力（被点名的插队）→ 概率串话，
  AI 发言总轮次硬顶 MAX_AI_TURNS，之后安静下来等用户。
- 自主闲聊：用户沉默一段时间后（随机阈值），AI 们会自己有一搭没一搭地聊——
  发言间隔拉长（省 token），单次闲聊轮次有上限；睡觉的成员不参与；
  用户一开口立刻打断闲聊，话题回到用户。/groupidle on|off 可开关。
- 群聊期间主动冒泡/问候暂停（guard 统一拦截），散群自动恢复。
- /endgroup 散群：完整群聊记录写回每位朋友的库（kind='group'，不进 1:1
  上下文和回填范围），并各自提取一遍记忆——同一场群聊，每个人记住的
  侧重点可以不一样。
- 会话在运行期内存里，但每轮对话后 transcript 会落盘（group_session.json）：
  /refresh 会先自动散群沉淀再重载；进程重启后启动时自动补写回（recover_pending）。
"""
import json
import random
import threading
import time as _time
from collections import deque
from datetime import datetime

import config
import context
import corpus
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
- 你们都是小徐的朋友，彼此也认识：可以接梗、附和，也可以不同意对方。
- 回复要短，一两句话，像微信群聊。"""

# 自主闲聊（没有话题焦点时）的额外指引
IDLE_HINT = """【现在的情况】小徐有一会儿没说话了，群里冷下来了。
- 你们自己随便聊：聊你自己的生活、最近遇到的事、奇怪的想法，或者接上面某位的话。
- 别总围着小徐，也别做总结陈词。有一搭没一搭，想哪说哪。"""

# 用户把焦点交给朋友们的信号：说这类话后朋友们该自己聊起来，别等长空闲（3-8 分钟）
HANDOFF_SIGNALS = (
    "你们认识", "认识一下", "你们聊", "你们自己", "互相认识",
    "介绍一下彼此", "你们玩", "你们先聊", "别管我",
)


def _is_handoff(text: str) -> bool:
    """用户是否把话题焦点交给朋友们（"你们认识一下新朋友"这类）。"""
    return any(sig in text for sig in HANDOFF_SIGNALS)

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
        self.channel = None          # 发送通道（/group 后由 main 挂载，或首次发言时带上）
        self.lock = threading.RLock()          # 用户回应流程 vs 自主闲聊 互斥
        self.user_event = threading.Event()    # 置位=用户刚说过话/正在回应用户
        self.interrupt = threading.Event()     # 置位=发言流程进行中用户又说话了
        self.last_user_ts = _time.time()       # 用户最近一次说话时间
        self.last_idle_ts = 0.0                # 上一轮自主闲聊结束时间
        self.idle_after = _idle_after_seconds()  # 本轮触发闲聊需要的空闲时长
        self.summary = ""                      # 滚出窗口内容的累计摘要（长会话防遗忘）
        self.summarized_upto = 0               # 已摘要到的 transcript 下标
        # --- 异步回应：用户消息不阻塞发送，回应在后台线程串行消化 ---
        self.user_queue = deque()              # 待处理的用户消息 (text, handoff)
        self._cond = threading.Condition()
        self.respond_done = threading.Event()  # 最近一轮回应是否完成（测试/等待用）
        self.respond_done.set()
        threading.Thread(target=_respond_loop, args=(self,), daemon=True).start()


def is_active() -> bool:
    return _session is not None and _session.active


def current() -> GroupSession | None:
    return _session if is_active() else None


def idle_enabled() -> bool:
    """自主闲聊开关：kv 运行时覆盖优先（/groupidle），默认看 config。"""
    try:
        v = db.get_kv("group_idle")
        if v:
            return v == "on"
    except Exception:
        pass
    return config.GROUP_IDLE_CHATTER


def _idle_after_seconds() -> float:
    return random.uniform(config.GROUP_IDLE_AFTER_MIN_MINUTES,
                          config.GROUP_IDLE_AFTER_MAX_MINUTES) * 60


def start(participants: list[Participant]) -> str:
    global _session
    _session = GroupSession(participants)
    threading.Thread(target=_idle_loop, args=(_session,), daemon=True).start()
    names = "、".join(p.name for p in participants)
    return f"群拉好了：{names} 都在。说吧，大家都听着呢～（/endgroup 散群）"


def attach_channel(channel) -> None:
    """把当前通道挂到群会话上（main 在 /group 后调用），自主闲聊才有地方发言。"""
    s = current()
    if s:
        s.channel = channel


def on_user_text(text: str, channel) -> None:
    """群里收到用户消息：立即入队返回（不阻塞发送），后台串行回应。

    - 用户消息直接发：不必等 AI 们说完才能发下一条（CLI 不再被卡住）。
    - AI 们不用管新消息：正在发的这条发完，再看新消息（不打断打字）。
    - 选择性回应：每次随机 1~GROUP_RESPOND_MAX 位朋友回应，不全员刷屏；
      话里点名了谁，谁必回。
    若用户把焦点交给朋友们（"你们认识一下"），回应完很快开始自由闲聊。"""
    s = _session
    if not s:
        return
    s.channel = channel
    s.last_user_ts = _time.time()
    handoff = _is_handoff(text)
    s.user_event.set()   # 用户回来了：闲聊停下
    s.interrupt.set()    # 在途回应：发完手头这条就停，看这条新消息
    s.transcript.append(("小徐", text))  # 原子 append，立即
    with s._cond:
        s.user_queue.append((text, handoff))
        s._cond.notify_all()


def _respond_loop(self) -> None:
    """后台回应线程：串行消费用户消息队列，一条消息跑一轮回应。"""
    while self.active:
        with self._cond:
            while self.active and not self.user_queue:
                self._cond.wait(timeout=1.0)
            if not self.active:
                return
            text, handoff = self.user_queue.popleft()
        self.respond_done.clear()
        try:
            with self.lock:   # 回应流程 vs 自主闲聊 互斥
                self.interrupt.clear()  # 我是最新一轮，之前的打断归我消化
                _respond_flow(self, self.channel, text)
                _persist(self)  # 每轮落盘：/refresh 或重启都不丢群聊内容
                if handoff:
                    # 焦点交给朋友们了：把空闲阈值压短，回应完立刻开始自由闲聊
                    self.last_idle_ts = 0.0
                    self.idle_after = config.GROUP_HANDOFF_IDLE_SECONDS
        except Exception as e:
            print(f"[group] 回应出错：{e}")
        finally:
            self.respond_done.set()
            with self._cond:
                if not self.user_queue:
                    self.user_event.clear()  # 队列清空，闲聊线程可继续等空闲


def wait_responders(timeout: float = 5.0) -> None:
    """等后台回应线程把当前用户消息队列处理完（测试 / 同步场景用）。"""
    s = current()
    if not s:
        return
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        with s._cond:
            if not s.user_queue and s.respond_done.is_set():
                return
        _time.sleep(0.01)


def _pick_responders(s: GroupSession, user_text: str) -> list[Participant]:
    """选择性回应：每次用户发言随机抽 1~GROUP_RESPOND_MAX 位朋友回应，
    不再全员各说一句（防刷屏）。话里点名了谁，谁必回。"""
    lo = min(config.GROUP_RESPOND_MIN, len(s.participants))
    hi = min(config.GROUP_RESPOND_MAX, len(s.participants))
    lo = min(lo, hi)
    k = random.randint(lo, hi)
    picked = random.sample(s.participants, k)
    for p in s.participants:
        if p.name and p.name in user_text and p not in picked:
            picked.append(p)   # 被点名的必回
    return list(picked)


def _respond_flow(s: GroupSession, channel, user_text: str) -> None:
    ai_turns = 0
    spoken = set()
    read = min(len(user_text) * config.READING_SECONDS_PER_CHAR,
               config.READING_MAX_SECONDS)

    # 内容太长：先有位朋友嘟囔一句"慢慢看"（固定语料，不走 LLM）
    if read >= config.READING_MAX_SECONDS > 0:
        p = random.choice(s.participants)
        note = corpus.pick(corpus.LONG_TEXT_NOTES, "group_long_note")
        s.transcript.append((p.name, note))
        channel.send(note, sender=p.name)

    # 第一轮：选择性回应（随机抽 1~N 位，不全员刷屏）；话里点名了谁谁必回
    queue = _pick_responders(s, user_text)
    while queue and ai_turns < MAX_AI_TURNS:
        p = queue.pop(0)
        if p.name in spoken:
            continue
        # 阅读模拟：第一个回话的把整句读完；后面的人边听边看，反应快一些
        if ai_turns == 0:
            wait = read + random.uniform(0.3, 1.2)
        else:
            wait = random.uniform(config.GROUP_SPEAKER_GAP_MIN_SECONDS,
                                  config.GROUP_SPEAKER_GAP_MAX_SECONDS)
        if s.interrupt.wait(wait):
            break  # 等待期间用户又说话了：停，发完手头的就不再往下（新消息会开新一轮）
        reply = _speak(p, s, channel)
        if not reply:
            break  # 发言被插话作废
        ai_turns += 1
        spoken.add(p.name)
        for other in s.participants:
            if (other.name != p.name and other.name not in spoken
                    and other.name in reply
                    and all(o is not other for o in queue)):
                queue.append(other)

    # 自由串话：有人想再接两句，随机来，随时停
    while (ai_turns < MAX_AI_TURNS and len(s.participants) > 1
           and not s.interrupt.is_set()):
        if random.random() >= CROSS_TALK_PROB:
            break
        if s.interrupt.wait(random.uniform(config.GROUP_SPEAKER_GAP_MIN_SECONDS,
                                           config.GROUP_SPEAKER_GAP_MAX_SECONDS)):
            break
        if not _speak(random.choice(s.participants), s, channel):
            break
        ai_turns += 1
        if random.random() < STOP_TALK_PROB:
            break


def _persist_file():
    return config.BASE_DIR / "group_session.json"


def _persist(s: GroupSession) -> None:
    """把进行中的群会话落盘，防 reload/重启丢失。"""
    try:
        data = {
            "participants": [{"db_path": str(p.db_path), "name": p.name}
                             for p in s.participants],
            "transcript": [[sp, tx] for sp, tx in s.transcript],
        }
        _persist_file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[group] 会话落盘失败：{e}")


def recover_pending() -> str:
    """启动时调用：上次群聊没散场进程就退了的话，补写回并各自提取记忆。"""
    global _session
    f = _persist_file()
    if _session is not None or not f.exists():
        return ""
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        parts = [Participant(p["db_path"], p["name"]) for p in data["participants"]]
        s = GroupSession(parts)
        s.transcript = [(sp, tx) for sp, tx in data["transcript"]]
        if not s.transcript:
            f.unlink(missing_ok=True)
            return ""
        _session = s
        end()
        names = "、".join(p.name for p in parts)
        return f"[群聊恢复] 上次和{names}的群聊没散场就退出了，已补写记录并各自记下。"
    except Exception as e:
        f.unlink(missing_ok=True)  # 文件坏了别反复卡启动
        return f"[群聊恢复] 恢复失败（{e}），已丢弃残留会话。"


def end() -> str:
    """散群：群聊记录写回每位朋友的库，各自提取记忆。"""
    global _session
    with db.PATH_LOCK:  # 与 1:1 后台回复/提取互斥：写回期间库指针稳定
        s, _session = _session, None
        if not s:
            return "现在没有在群的。"
        s.active = False
        s.user_event.set()  # 叫醒闲聊线程退出
        with s._cond:       # 叫醒回应线程退出
            s._cond.notify_all()
        real_db = config.DB_PATH
        names = "、".join(p.name for p in s.participants)
        # 先把记录写回所有库，再删落盘文件，最后提取记忆——顺序保证崩溃也不丢内容
        for p in s.participants:
            try:
                config.DB_PATH = p.db_path
                for sp, tx in s.transcript:
                    role = "user" if sp == "小徐" else "assistant"
                    db.add_message(role, f"【群聊·{sp}】{tx}", kind="group")
            finally:
                config.DB_PATH = real_db
        _persist_file().unlink(missing_ok=True)  # 内容已入库，落盘文件使命完成
        for p in s.participants:
            try:
                config.DB_PATH = p.db_path
                _extract_group_memories(s, p)
            except Exception as e:
                print(f"[group] {p.name} 提取群聊记忆失败：{e}")
            finally:
                config.DB_PATH = real_db
    return f"散啦～和{names}的这场群聊，大家都各自记下了点什么。"


def _speak(p: Participant, s: GroupSession, channel, idle: bool = False) -> str:
    """让一位朋友在群里发言：临时把 db 指到TA的库，用TA的人设和记忆。

    回复生成后按字数模拟"正在输入"，到点再发出（和私聊一致）。
    idle=True 是自主闲聊：睡觉的成员不发言；等待期间用户回来则这句作废。"""
    with db.PATH_LOCK:  # 与 1:1 后台回复/提取互斥：发言期间库指针稳定
        real_db = config.DB_PATH
        try:
            config.DB_PATH = p.db_path
            sleeping = db.get_state() == "SLEEPING"
            if sleeping and idle:
                return ""  # 自主闲聊：睡着的人不参与
            if sleeping:
                # 回应流程：睡着的人回一句迷糊话（和私聊一致，不调 LLM，省钱）
                reply = corpus.pick(corpus.SLEEPY_REPLIES, "sleepy")
            else:
                members = "、".join([*(x.name for x in s.participants), "小徐（用户）"])
                sys_prompt = (persona.build_system_prompt("AWAKE") + "\n"
                              + GROUP_RULES.format(members=members, self_name=p.name))
                if idle:
                    sys_prompt += "\n" + IDLE_HINT
                # 长会话防遗忘：滚出窗口的更早内容压成摘要，别把"这是新朋友阿燃"忘了
                _maybe_summarize(s)
                # 信息调度层统一组装：人设+群聊规则 → 群聊记忆优先召回 →
                # 摘要兜底 → 窗口内群聊记录
                msgs = context.build_group_messages(
                    sys_prompt, s.transcript, idle=idle, summary=s.summary)
                try:
                    reply = llm.chat(msgs).strip()
                except Exception as e:
                    # 和私聊一致：LLM 挂了就说"脑子卡住了"，别让一位朋友卡死整轮群聊
                    reply = corpus.LLM_ERROR.format(e)
                # 模型偶尔自带名字前缀，剥掉
                for sep in ("：", ":"):
                    if reply.startswith(f"{p.name}{sep}"):
                        reply = reply[len(p.name) + 1:].strip()
        finally:
            config.DB_PATH = real_db

    # "正在输入"：按字数假装敲键盘，到点再发出正文。
    # 用户插话不再作废这条——把"手头的消息"发完，回应流程在发言间隙再看新消息。
    if config.TYPING_ENABLED and reply:
        duration = min(max(len(reply) * config.TYPING_SECONDS_PER_CHAR,
                           config.TYPING_MIN_SECONDS), config.TYPING_MAX_SECONDS)
        typing_fn = getattr(channel, "typing", None)  # 通道可选能力
        if typing_fn:
            typing_fn(sender=p.name)
        _time.sleep(duration)
    s.transcript.append((p.name, reply))
    _send_segments(channel, p.name, reply)
    return reply


def _send_segments(channel, name: str, text: str) -> None:
    """按换行拆段连发，段间小停顿，像真人连着发几条。"""
    segs = [x.strip() for x in text.split("\n") if x.strip()]
    for i, seg in enumerate(segs):
        if i > 0:
            _time.sleep(min(max(len(segs[i - 1]) * config.SEND_SECONDS_PER_CHAR,
                                config.SEND_MIN_SECONDS), config.SEND_MAX_SECONDS))
        channel.send(seg, sender=name)


def _maybe_summarize(s: GroupSession) -> None:
    """长会话防遗忘（信息调度层）：把滚出窗口的更早群聊内容增量压进摘要。

    只在 transcript 超过窗口、且有新内容滚出时触发一次；摘要结果挂到
    session.summary 上，由 context.build_group_messages 注入。摘要失败不阻塞发言。"""
    if not config.GROUP_SUMMARY_ENABLED:
        return
    overflow = len(s.transcript) - config.GROUP_CONTEXT_MESSAGES
    if overflow <= 0 or s.summarized_upto >= overflow:
        return
    head = s.transcript[s.summarized_upto:overflow]
    s.summarized_upto = overflow
    new_sum = context.summarize_transcript(head)
    if new_sum:
        s.summary = (s.summary + "\n" + new_sum).strip() if s.summary else new_sum


# --- 自主闲聊：没有话题焦点时，AI 们自己聊 ---

def _idle_loop(s: GroupSession) -> None:
    """群会话的后台线程：等空闲 → 到点开聊 → 用户一说话立刻停。

    整段套 try/except：闲聊里 LLM 偶发报错（限流/网络）不能把线程打挂，
    否则"自由闲聊"就再也起不来了。"""
    while s.active:
        try:
            # 轮询切片随触发阈值缩放：测试里阈值压到零点几秒也能及时响应
            s.user_event.wait(min(1.0, max(0.1, s.idle_after / 3)))
            if not s.active:
                return
            if s.user_event.is_set():
                # 用户回应流程进行中（event 已置位，上面的 wait 会秒回）——睡一下再看，别空转
                _time.sleep(min(1.0, max(0.1, s.idle_after / 3)))
                continue
            if not idle_enabled() or s.channel is None or len(s.participants) < 2:
                continue
            base = max(s.last_user_ts, s.last_idle_ts)
            if _time.time() - base < s.idle_after:
                continue
            _idle_burst(s)
            s.last_idle_ts = _time.time()
            s.idle_after = _idle_after_seconds()  # 下一轮重新掷一个间隔
        except Exception as e:
            print(f"[group] 自主闲聊出错，歇一下再试：{e}")
            _time.sleep(5)


def _idle_burst(s: GroupSession) -> None:
    """一轮自主闲聊：间隔拉长（省 token 也像真人有一搭没一搭），轮数有上限。"""
    turns = 0
    skips = 0
    prev = None
    while s.active and turns < config.GROUP_IDLE_MAX_TURNS \
            and not s.user_event.is_set():
        candidates = [p for p in s.participants if p is not prev] \
            or list(s.participants)
        p = random.choice(candidates)
        gap = random.uniform(config.GROUP_IDLE_GAP_MIN_SECONDS,
                             config.GROUP_IDLE_GAP_MAX_SECONDS)
        if s.user_event.wait(gap):
            break  # 用户回来了：立刻停，话题回到用户
        if not s.active:
            break
        with s.lock:
            if s.user_event.is_set():
                break
            reply = _speak(p, s, s.channel, idle=True)
        if not reply:
            skips += 1
            if skips >= len(s.participants):
                break  # 都在睡觉/都被打断，这轮算了
            continue
        skips = 0
        prev = p
        turns += 1
        _persist(s)


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
            {"role": "system", "content": memory._extract_prompt()},
            {"role": "user", "content": user_msg},
        ], temperature=0.2)
        for it in memory._parse_json_array(raw):
            memory._apply(it, source="group")
    except Exception as e:
        print(f"[group] {p.name} 提取群聊记忆失败：{e}")
