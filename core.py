"""核心引擎：收发消息的业务逻辑，与 IM 通道解耦。"""
import json
import random
import threading
import time as _time
from datetime import datetime, timedelta

import config
import context
import corpus
import db
import game
import guard
import llm
import memory
import persona
from channels.base import Channel


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
        self._stopped = False  # 停止后台回复线程（测试用）
        self._lock = threading.Lock()
        threading.Thread(target=self._reply_worker, daemon=True).start()

    def stop(self) -> None:
        """停止后台回复线程（测试/进程收尾用）。"""
        self._stopped = True

    # --- 收到用户消息 ---
    def on_user_message(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._flush_outbox()  # 你回来了：先把离开期间 TA 的留言重放给你
        db.add_message("user", text)
        # 紧急通道优先于忙碌信号：真有急事时"在忙"的静默期不该继续生效
        if guard.is_urgent(text):
            guard.open_urgent_window()
        else:
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
            self._send(corpus.pick(corpus.LONG_TEXT_NOTES, "long_note"), kind="chat")

        # 会话级时间观：判断这段会话是继续还是告一段落
        if any(s in text for s in corpus.ENDING_SIGNALS):
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
        while not self._stopped:
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
                with db.PATH_LOCK:  # 回复全流程与切换朋友互斥，不写错库
                    self._generate_reply(batch)
            finally:
                with self._lock:
                    self._sending = False

    def _generate_reply(self, batch: list[str]) -> None:
        state = db.get_state()
        if state == "SLEEPING":
            self._send(corpus.pick(corpus.SLEEPY_REPLIES, "sleepy"), kind="chat")
            return

        # 信息调度层统一组装上下文：人设+便签+profile → 游戏规则 → 相关记忆 → 最近上下文
        messages = context.build_chat_messages(state, batch, game.prompt_injection())
        reply = self._generate_reply_text(messages, batch)
        # 措辞红线（H 模块·反愧疚）：代价只呈现不归因。prompt 已约束，这里是兜底改写——
        # 归因措辞任何状态都不该出现，命中就换成"只说自己的状态"的安全句（不归因）。
        if persona.has_cost_blame(reply):
            print(f"[红线] 回复把代价归因到对方，已兜底改写：{reply[:40]}")
            reply = corpus.pick(corpus.COST_SAFE_LINES, "cost_safe")

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
                    self._send(corpus.pick(corpus.INTERRUPT_ACKS, "interrupt"),
                               kind="chat")
                interrupted = True
                break
            self._send(seg, kind="chat")

        # 话题结束判断：AI 自己的回复带结束信号 → 直接关闭会话（不轻催）；
        # 关键词都没抓住时，让 AI 异步判一下这段聊天是不是自然结束了。
        # 被打断（插话）说明聊天正酣，跳过收尾判断。
        if interrupted:
            pass
        elif any(s in reply for s in corpus.ENDING_SIGNALS):
            db.set_kv("session_open", "0")
        else:
            threading.Thread(target=self._judge_locked,
                             args=(batch, reply), daemon=True).start()

        memory.extract_async()  # 后台提取这段对话里值得记住的事
        self.check_auto_familiar()

    def _generate_reply_text(self, messages: list[dict], batch: list[str]) -> str:
        """生成回复：回忆/追问信号下让 AI 主动调记忆检索工具，否则走普通快速路径。

        普通闲聊不带工具（省 token、省延迟），只有用户"反复要求回忆/追问细节"
        时才启用 function calling——AI 自己决定查什么、查 A 库还是展开 B 库。"""
        if config.MEMORY_TOOLS_ENABLED and memory.wants_memory_search(" ".join(batch)):
            return self._chat_with_memory_tools(messages)
        try:
            return llm.chat(messages)
        except llm.LLMUnavailable as e:
            return corpus.LLM_ERROR.format(e)

    def _chat_with_memory_tools(self, messages: list[dict]) -> str:
        """带记忆检索工具的回复生成（function calling）。

        程序自动召回仍是第一层兜底（messages 里已注入相关记忆），工具调用是
        "主动深挖"——用户反复要求回忆、想不起、要细节时，AI 主动调
        search_memories / recall_detail。最多 MEMORY_TOOL_MAX_ROUNDS 轮，
        防止无限循环；最终没生成文本时用无工具调用兜底。"""
        final_text = ""
        for _ in range(config.MEMORY_TOOL_MAX_ROUNDS):
            try:
                msg = llm.chat_with_tools(messages, memory.MEMORY_TOOLS)
            except llm.LLMUnavailable as e:
                return corpus.LLM_ERROR.format(e)
            tool_calls = msg.get("tool_calls") or []
            final_text = msg.get("content") or ""
            if not tool_calls:
                return final_text  # 不调工具：直接用这轮的文本
            # 回填 assistant 的工具调用消息
            messages.append({
                "role": "assistant",
                "content": final_text,
                "tool_calls": tool_calls,
            })
            # 执行每个工具，结果作为 tool 消息回填给下一轮
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                result = memory.run_memory_tool(name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result,
                })
        # 超过轮数上限仍无文本：无工具兜底拿一个回复
        if not final_text.strip():
            try:
                return llm.chat(messages)
            except llm.LLMUnavailable as e:
                return corpus.LLM_ERROR.format(e)
        return final_text

    def _judge_locked(self, batch: list[str], reply: str) -> None:
        with db.PATH_LOCK:  # 与切换朋友互斥
            self._judge_session_end(batch, reply)

    # --- 话题结束的 AI 判官：关键词没抓住的收尾由模型判断 ---
    def _judge_session_end(self, batch: list[str], reply: str) -> bool:
        """这段聊天自然结束了吗？结束了关闭会话（不再轻催）。返回是否判定结束。"""
        if db.get_kv("session_open", "1") != "1":
            return False  # 已经关了（用户关键词或 AI 回复命中）
        convo = "\n".join(f"用户：{x}" for x in batch) + f"\n{persona.friend_name()}：{reply}"
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
        with db.PATH_LOCK:  # 与切换朋友互斥：提示词/记忆/发送全程同一个库
            return self._proactive(scenario)

    def _proactive(self, scenario: str) -> bool:
        ok, reason = guard.can_send_proactive(scenario=scenario)
        if not ok:
            print(f"[proactive:{scenario}] 被防打扰拦截：{reason}")
            return False

        state = db.get_state()
        reply = None
        # 冒泡一部分直接抽固定语料：不重样、省 token（语料在 corpus.py 统一维护）
        if scenario == "bubble" and random.random() < config.BUBBLE_FIXED_PROB:
            reply = corpus.pick(corpus.BUBBLE_LINES, "bubble")
        if reply is None:
            prompt = context.build_proactive_messages(state, scenario)
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
            self._send(corpus.AUTO_FAMILIAR_NOTE, kind="chat")

    # --- 作息切换（调度器调用） ---
    def wake_up(self) -> None:
        db.set_state("AWAKE")
        self.proactive("morning")

    def go_sleep(self) -> None:
        # 晚安消息要在睡前发，所以先临时保持 AWAKE 发出去
        self.proactive("night")
        # 睡前写日记 + 复盘当天（P2：记忆 v3 每日回顾 + 日记系统 + 回顾线）
        if config.DIARY_ENABLED:
            self.daily_review()
        db.set_state("SLEEPING")

    # --- 每日回顾 + 写日记（P2） ---
    def daily_review(self) -> str:
        """睡前/手动触发：读当日完整对话 → 复盘 + 写日记 + 补记/修正记忆。幂等。"""
        import diary
        try:
            with db.PATH_LOCK:  # 与切换朋友互斥：复盘/写日记全程读写同一个库
                return diary.daily_review()
        except llm.LLMUnavailable as e:
            return corpus.LLM_ERROR.format(e)

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
