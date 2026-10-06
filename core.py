"""核心引擎：收发消息的业务逻辑，与 IM 通道解耦。"""
import json
import random
import threading
import time as _time
from datetime import datetime, timedelta

import config
import db
import game
import guard
import llm
import memory
import persona
from channels.base import Channel


# 会话结束信号：用户说了这些 = 这段会话告一段落，不要轻催
ENDING_SIGNALS = ["拜拜", "晚安", "去忙", "先这样", "下了", "睡了", "睡觉",
                  "我去写", "写作业去", "去吃饭", "去上课", "溜了", "不聊了", "先不聊"]

# 内容太长、阅读超限时先回一句
LONG_TEXT_NOTES = [
    "内容有点多，我慢慢看哈",
    "好家伙，信息量有点大，容我缓缓",
    "等下，这也太长了吧，我慢慢看",
]


def take_outbox() -> list[dict]:
    """取出并清空未读主动消息队列（存在各自 friend.db，按朋友隔离）。

    主动消息发出时就进队，用户回来（启动 / 换朋友 / 开口说话）时重放。
    """
    raw = db.get_kv("outbox_queue")
    if not raw:
        return []
    db.set_kv("outbox_queue", "")
    try:
        queue = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(queue, list):
        return []
    return [m for m in queue if isinstance(m, dict) and m.get("text")]


def format_outbox(items: list[dict]) -> str:
    """把未读留言渲染成一条重放消息。"""
    lines = []
    for m in items:
        ts = ""
        try:
            ts = datetime.fromisoformat(m["ts"]).strftime("%m-%d %H:%M")
        except (KeyError, ValueError):
            pass
        lines.append(f"[{ts}] {m['text']}" if ts else m["text"])
    return f"（你不在的时候 TA 发的 {len(items)} 条留言）\n" + "\n".join(lines)


def pending_outbox_text() -> str:
    """有待重放的留言就返回渲染好的文本，没有返回空串（启动/换朋友时调用）。"""
    items = take_outbox()
    return format_outbox(items) if items else ""


class Engine:
    def __init__(self, channel: Channel):
        self.channel = channel
        self.sched = None  # main 启动调度器后挂载，用于人格变更时重排作息
        # --- 阅读模拟：攒消息 → 假装在读 → 到点才回复 ---
        self._pending: list[str] = []
        self._reading_until = 0.0
        self._reading_deadline = 0.0  # 本轮等待的绝对上限
        self._sending = False
        self._lock = threading.Lock()
        threading.Thread(target=self._reply_worker, daemon=True).start()

    # --- 收到用户消息 ---
    def on_user_message(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._flush_outbox()  # 你回来了：先把离开期间 TA 的留言重放给你
        db.add_message("user", text)
        guard.check_busy_signal(text)
        game.on_user_text(text)  # 娱乐模式：提到游戏名开局，"不玩了"结束
        db.reset_counter("unanswered_proactive")  # 用户回复了，清零连续未回复
        self._reset_bubble_timer()  # 聊上了，冒泡倒计时重新计

        with self._lock:
            first = not self._pending
            self._pending.append(text)
            delay = min(len(text) * config.READING_SECONDS_PER_CHAR,
                        config.READING_MAX_SECONDS)
            now = _time.time()
            if first:
                self._reading_deadline = now + config.READING_MAX_SECONDS
                self._reading_until = now + delay
                long_note = delay >= config.READING_MAX_SECONDS
            else:
                # 新消息累加阅读时间，但不超过本轮上限
                self._reading_until = min(self._reading_until + delay,
                                          self._reading_deadline)
                long_note = False
        if long_note:
            self._send(random.choice(LONG_TEXT_NOTES), kind="chat")

        # 会话级时间观：判断这段会话是继续还是告一段落
        if any(s in text for s in ENDING_SIGNALS):
            db.set_kv("session_open", "0")      # 道别了，不催
        else:
            db.set_kv("session_open", "1")      # 会话进行中，启动等待计时
            self._reset_nudge_timer()

    # --- 后台回复线程：读完才回，回完这段再处理新攒的消息 ---
    def _interrupted(self) -> bool:
        """回复生成/发送期间用户又发来了新消息（插话）。"""
        with self._lock:
            return bool(self._pending)

    def _reply_worker(self) -> None:
        while True:
            _time.sleep(0.2)
            with self._lock:
                if self._sending or not self._pending:
                    continue
                if _time.time() < self._reading_until:
                    continue
                self._sending = True
                batch = self._pending      # 取走这批消息
                self._pending = []         # 清空，下一批重新计阅读时间
            try:
                self._generate_reply(batch)
            finally:
                with self._lock:
                    self._sending = False

    def _generate_reply(self, batch: list[str]) -> None:
        state = db.get_state()
        if state == "SLEEPING":
            self._send(random.choice(persona.SLEEPY_REPLIES), kind="chat")
            return

        messages = [{"role": "system", "content": persona.build_system_prompt(state)}]
        # 娱乐模式：游戏中把规则注入上下文，AI 按规则陪玩
        game_rules = game.prompt_injection()
        if game_rules:
            messages.append({"role": "system", "content": game_rules})
        # 记忆召回：和这段话相关的旧记忆注入上下文（跨越 10 轮窗口的限制）
        combined = " ".join(batch)
        if combined:
            recalled = memory.recall(combined)
            if recalled:
                # 带时间锚点：事发时间（time_label）+ 存储时间（created_at）是两个维度，
                # AI 能说出"你今天下午不是说……"，也知道这条是啥时候记下的
                def _mem_line(m):
                    anchor = f"（{m['time_label']}）" if m.get("time_label") \
                        else (f"（{m['created_at'][5:10]} 记）" if m.get("created_at") else "")
                    return "- " + anchor + m["content"]
                mem_text = "\n".join(_mem_line(m) for m in recalled)
                messages.append({"role": "system",
                                 "content": f"【你想起来的旧记忆】（自然使用，别生硬复述；"
                                            f"同主题多条按时间先后排列，靠后的是最新进展）\n{mem_text}"})
        messages += db.recent_context(config.CONTEXT_TURNS)
        try:
            reply = llm.chat(messages)
        except llm.LLMUnavailable as e:
            reply = f"（脑子卡住了：{e}）"

        # 分段连发：按换行拆成几条消息，像真人连着发
        segments = [s.strip() for s in reply.split("\n") if s.strip()]
        # "正在输入"：按整条回复的字数假装敲键盘，到点再发出正文
        if config.TYPING_ENABLED and segments:
            total = sum(len(s) for s in segments)
            duration = min(max(total * config.TYPING_SECONDS_PER_CHAR,
                               config.TYPING_MIN_SECONDS), config.TYPING_MAX_SECONDS)
            typing_fn = getattr(self.channel, "typing", None)  # 通道可选能力
            if typing_fn:
                typing_fn()
            _time.sleep(duration)
        interrupted = False
        for i, seg in enumerate(segments):
            if i > 0:
                _time.sleep(min(max(len(segments[i - 1]) * config.SEND_SECONDS_PER_CHAR,
                                    config.SEND_MIN_SECONDS), config.SEND_MAX_SECONDS))
            # 插话检测（发送前最后一刻检查）：TA 打字/连发期间你又说话了——
            # 停发剩余段，先听你说
            if self._interrupted():
                if i > 0:  # 已经发出去几段了，补一句让步语
                    self._send(random.choice(persona.INTERRUPT_ACKS), kind="chat")
                interrupted = True
                break
            self._send(seg, kind="chat")

        # 话题结束判断：AI 自己的回复带结束信号 → 直接关闭会话（不轻催）；
        # 关键词都没抓住时，让 AI 异步判一下这段聊天是不是自然结束了。
        # 被打断（插话）说明聊天正酣，跳过收尾判断。
        if interrupted:
            pass
        elif any(s in reply for s in ENDING_SIGNALS):
            db.set_kv("session_open", "0")
        else:
            threading.Thread(target=self._judge_session_end,
                             args=(batch, reply), daemon=True).start()

        memory.extract_async()  # 后台提取这段对话里值得记住的事
        self.check_auto_familiar()

    # --- 话题结束的 AI 判官：关键词没抓住的收尾由模型判断 ---
    def _judge_session_end(self, batch: list[str], reply: str) -> bool:
        """这段聊天自然结束了吗？结束了关闭会话（不再轻催）。返回是否判定结束。"""
        if db.get_kv("session_open", "1") != "1":
            return False  # 已经关了（用户关键词或 AI 回复命中）
        convo = "\n".join(f"用户：{x}" for x in batch) + f"\nAI：{reply}"
        try:
            ans = llm.chat([
                {"role": "system", "content": (
                    "判断这段聊天是否已经自然结束（一方道别、说去忙/去睡、"
                    "对话收尾双方都不期待继续）。只回答一个字：是 或 否。")},
                {"role": "user", "content": convo},
            ], temperature=0.1)
        except llm.LLMUnavailable:
            return False
        if ans.strip().startswith("是"):
            db.set_kv("session_open", "0")
            return True
        return False

    # --- 未读留言队列：用户不在时主动消息攒着，回来时重放（按朋友隔离） ---
    def _flush_outbox(self) -> None:
        items = take_outbox()
        if items:
            self.channel.send(format_outbox(items))

    # --- 主动消息（调度器调用） ---
    def proactive(self, scenario: str) -> bool:
        """尝试发一条主动消息，返回是否实际发出。"""
        ok, reason = guard.can_send_proactive(scenario=scenario)
        if not ok:
            print(f"[proactive:{scenario}] 被防打扰拦截：{reason}")
            return False

        state = db.get_state()
        prompt = persona.build_proactive_prompt(state, scenario)
        # 主动话题接记忆：random 场景一半概率翻一条旧记忆接着聊
        if scenario == "random" and random.random() < 0.5:
            mem = memory.random_memory()
            if mem:
                prompt[-1]["content"] += (
                    f"\n（你忽然想起关于TA的一件事：「{mem['content']}」。"
                    f"可以就着它聊，比如问问后续；想聊别的也行。）")
        try:
            reply = llm.chat(prompt, temperature=1.0)
        except llm.LLMUnavailable as e:
            print(f"[proactive:{scenario}] LLM 不可用：{e}")
            return False

        if scenario == "bubble":
            db.incr_counter("bubble_count")
            self._reset_bubble_timer()
        else:
            db.incr_counter("proactive_count")
        db.incr_counter("unanswered_proactive")
        self._send(reply, kind="proactive")
        return True

    # --- 初见阶段的冒泡机制：空闲 30-60 分钟没聊就冒个泡 ---
    def _reset_bubble_timer(self) -> None:
        mult = persona.behavior_params()["bubble_mult"]  # 话痨人格冒得勤，高冷人格冒得少
        delay = max(5, int(random.randint(config.BUBBLE_IDLE_MIN_MINUTES,
                                          config.BUBBLE_IDLE_MAX_MINUTES) / mult))
        db.set_kv("next_bubble_at",
                  (datetime.now() + timedelta(minutes=delay)).isoformat(timespec="seconds"))

    # --- 人格变更（/new-friend 抽了新朋友）后的善后 ---
    def on_persona_changed(self) -> None:
        self._reset_bubble_timer()
        if self.sched:
            import scheduler as sched_mod
            p = persona.behavior_params()
            sched_mod.reschedule_routine(self.sched, self,
                                         p["wake_hour"], p["sleep_hour"])

    # --- /refresh 热重载后的善后：定时任务按新配置/人格重排 ---
    def on_config_reloaded(self) -> None:
        self._reset_bubble_timer()
        if self.sched:
            import scheduler as sched_mod
            sched_mod.reschedule_all(self.sched, self)

    # --- 会话级等待：聊到一半对方没声了，10-20 分钟后可能轻催一下 ---
    def _reset_nudge_timer(self) -> None:
        delay = random.randint(config.NUDGE_IDLE_MIN_MINUTES,
                               config.NUDGE_IDLE_MAX_MINUTES)
        db.set_kv("next_nudge_at",
                  (datetime.now() + timedelta(minutes=delay)).isoformat(timespec="seconds"))
        db.set_kv("nudged", "0")

    def check_nudge(self) -> None:
        """调度器每分钟调用：会话开着 + 等到点了 → 轻催（每次沉默最多一次）。"""
        if db.get_state() == "SLEEPING":
            return
        if db.get_kv("session_open", "0") != "1":
            return
        if db.get_kv("nudged", "0") == "1":
            return
        next_at = db.get_kv("next_nudge_at")
        if not next_at or datetime.now() < datetime.fromisoformat(next_at):
            return
        if random.random() < config.NUDGE_PROBABILITY:
            self.proactive("nudge")
        db.set_kv("nudged", "1")  # 不管这次催没催，这段沉默只等一回

    def check_bubble(self) -> None:
        """调度器每分钟调用：初见阶段 + 到了冒泡时间 → 冒泡。"""
        if db.get_kv("relationship_stage", "new") != "new":
            return
        next_at = db.get_kv("next_bubble_at")
        if not next_at:
            self._reset_bubble_timer()
            return
        if datetime.now() >= datetime.fromisoformat(next_at):
            sent = self.proactive("bubble")
            if not sent:
                # 被拦截（如睡觉中）就别每分钟重试刷屏，15 分钟后再看
                db.set_kv("next_bubble_at", (datetime.now() + timedelta(
                    minutes=15)).isoformat(timespec="seconds"))

    # --- 信息攒够了，自动进入熟悉阶段 ---
    def check_auto_familiar(self) -> None:
        if db.get_kv("relationship_stage", "new") != "new":
            return
        profile_count = len([l for l in persona._read_profile().splitlines() if l.strip()])
        known = profile_count + len(db.get_facts()) + db.memory_count()
        if known >= config.AUTO_FAMILIAR_INFO_THRESHOLD:
            db.set_kv("relationship_stage", "familiar")
            self._send("（不知不觉已经了解你这么多了——我觉得咱们算朋友啦。"
                       "以后我不老冒泡烦你，有事才来。）", kind="chat")

    # --- 作息切换（调度器调用） ---
    def wake_up(self) -> None:
        db.set_state("AWAKE")
        self.proactive("morning")

    def go_sleep(self) -> None:
        # 晚安消息要在睡前发，所以先临时保持 AWAKE 发出去
        self.proactive("night")
        db.set_state("SLEEPING")

    def _send(self, text: str, kind: str) -> None:
        db.add_message("assistant", text, kind=kind)
        self.channel.send(text)
        if kind == "proactive":
            # 未读队列：攒着等用户回来时重放（存各自 friend.db，按朋友隔离）
            raw = db.get_kv("outbox_queue")
            try:
                queue = json.loads(raw) if raw else []
            except ValueError:
                queue = []
            queue.append({"ts": datetime.now().isoformat(timespec="seconds"), "text": text})
            db.set_kv("outbox_queue", json.dumps(queue, ensure_ascii=False))
            # 同时落 outbox.log 做长期存档
            with open(config.BASE_DIR / "outbox.log", "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
