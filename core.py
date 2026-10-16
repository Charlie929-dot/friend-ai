"""核心引擎：收发消息的业务逻辑，与 IM 通道解耦。"""
import json
import random
import re
import threading
import time as _time
from datetime import datetime, timedelta

import config
import context
import corpus
import db
import events
import game
import guard
import interests
import llm
import memory
import offline
import persona
import selfmem
import sources
import stream
import todos
import tools
from channels.base import Channel


def _is_llm_error(text: str) -> bool:
    """这条回复是不是 LLM 失败的兜底文案（v0.88 C2：失败不落库污染上下文）。

    用 corpus.LLM_ERROR 的模板前缀识别（" （脑子卡住了：…）"），不靠异常对象——
    失败可能发生在后台线程里，能拿到手的只有文本。"""
    t = (text or "").strip()
    prefix = corpus.LLM_ERROR.split("{")[0]
    return bool(prefix) and t.startswith(prefix)


def _note_browsed_discussed(batch: list[str]) -> None:
    """闸门 2（批 5）：这轮被追问细节、且当天确实注入了【我今天刷到的】→ 把注入的那几条
    补成 B 库"今日素材"（纯规则、零额外 LLM）。

    位置：回复线程里、**LLM 失败早退之后**——回复都没生成成功，谈不上"认真讨论过"。
    粒度粗糙（EXPAND_QUERY 是通用追问词，追问别的事也会命中）——量级有界、记进已知边界。
    """
    if not getattr(config, "BROWSE_ENABLED", True):
        return
    if not re.search(corpus.EXPAND_QUERY, " ".join(batch)):
        return
    import sources as _sources
    items = _sources.items_today()
    if not items:
        return
    n = 0
    for it in items:
        if it.get("noted"):
            continue
        summary = (it.get("title") or (it.get("summary") or "")[:40]).strip()
        body = (it.get("summary") or it.get("title") or "").strip()
        src = (it.get("source") or "").strip()
        if src:
            body = f"{body}（来自 {src}）"
        db.add_today_note(summary=summary, content=body)
        db.mark_info_noted(it["id"])
        n += 1
    if n:
        print(f"[闸门2] 今天刷到的 {n} 条补成了今日素材")


def _clear_carried_ids() -> None:
    """不发 / 发不出去时清掉本轮携带的素材 id。

    v0.97 批 3 审查修订（修既有瑕疵）：旧实现只在**发出后**清 `proactive_todo_id`，
    LLM 失败 / 无弹药的 return False 路径不清 —— 陈年 id 会被下一次**任何**主动消息
    （含非 random 场景）错杀。对 info item 的后果是误勾 `shared`、永不能再当弹药。
    """
    db.set_kv("proactive_todo_id", "")
    db.set_kv("proactive_info_id", "")


def _settle_carried_ids() -> None:
    """主动消息发出后，把携带的素材 id 按 id 打勾。

    §5 拍板 #6③ + v0.97 批 3 审查修订：打勾**不得**包在 `TODOS_ENABLED` 门内
    （info item 的打勾与待办开关无关）；两个 id 各自读完即清空。
    """
    raw = db.get_kv("proactive_todo_id", "")
    if raw and raw != "0":
        try:
            todo_id = int(raw)
        except ValueError:
            todo_id = 0
        if todo_id and config.TODOS_ENABLED:
            done = todos.mark_done_by_id(todo_id)
            if done:
                print(f"[待办] 打勾（id {todo_id}）：{done[:30]}")
    db.set_kv("proactive_todo_id", "")
    raw = db.get_kv("proactive_info_id", "")
    if raw and raw != "0":
        try:
            info_id = int(raw)
        except ValueError:
            info_id = 0
        if info_id and db.mark_info_shared(info_id):
            print(f"[刷到] 打勾（id {info_id}）")
    db.set_kv("proactive_info_id", "")


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
        # 每条 = {"text": 原文, "quote": 引用回复(或 None)}——引用信息跟着消息走，
        # 回复线程才能知道"这句话在回哪句"（见 _generate_reply 的注入）。
        self._pending: list[dict] = []
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
    def on_user_message(self, text: str, quote: dict | None = None) -> int:
        """处理一条用户消息，返回它落库后的 id（前端靠它推进轮询游标，
        避免手动回显的消息又被轮询渲染一遍——实测 #25 发送出现两次）。

        quote：可选，引用回复（{"id","role","content"}）。随消息落库，也随消息
        进待回复队列——回复线程会把它拼进上下文，让 TA 知道你在引用哪句。"""
        text = text.strip()
        if not text:
            return 0
        self._flush_outbox()  # 你回来了：先把离开期间 TA 的留言重放给你
        mid = db.add_message("user", text, quote=quote)
        # 紧急通道优先于忙碌信号：真有急事时"在忙"的静默期不该继续生效
        if guard.is_urgent(text):
            guard.open_urgent_window()
            if db.get_state() == "SLEEPING":
                db.set_state("AWAKE")  # 急事把 TA 唤醒
        else:
            guard.check_busy_signal(text)
        game.on_user_text(text)  # 娱乐模式：提到游戏名开局，"不玩了"结束
        db.reset_counter("unanswered_proactive")  # 用户回复了，清零连续未回复
        self._reset_bubble_timer()  # 聊上了，冒泡倒计时重新计

        # 离线留言：TA 专注工作/睡觉时你发的话，攒着等 TA 上线正经回（急事除外）。
        # 意识流不在这里记——上线 flush_inbox 时记一次「留言了，我看看」再逐条补记，
        # 免得离线期间一条留言一行提示刷屏（实测反馈：连发 9 条 = 9 行重复）。
        if offline.is_offline() and not guard.is_urgent(text):
            if offline.push_inbox(text) == 1:
                self._send_offline_notice()  # 只在离线段第一条时轻提示一声
            return mid
        # 统一事件源（§5 拍板 #6①）：用户消息作为独立事件进意识流——记法"XX说：…"。
        # TA 的脑子里，别人说的话和 TA 自己的事一样，都是"今天发生的事"。
        stream.append("user", f"{persona.user_name()}说：{text}")
        # 在线但有积压留言（TA 刚转在线、tick 还没跑到）：先回留言再回这条新的
        if offline.pending_inbox_count():
            self.flush_inbox()

        with self._lock:
            first = not self._pending
            self._pending.append({"text": text, "quote": quote})
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
        return mid

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
                batch = self._pending      # 取走这批消息（每条 {"text","quote"}）
                self._pending = []         # 清空，下一批重新计阅读时间
            texts = [m["text"] for m in batch]
            # 引用只认"这批里最后一条带引用的"——用户最后那句在回哪句，才是当前语境
            quote = next((m["quote"] for m in reversed(batch) if m.get("quote")), None)
            set_gen = getattr(self.channel, "set_generating", None)
            if set_gen:
                set_gen(True)   # 开始生成回复 → 网页端显示"正在回复"
            try:
                with db.PATH_LOCK:  # 回复全流程与切换朋友互斥，不写错库
                    self._generate_reply(texts, quote=quote)
            finally:
                with self._lock:
                    self._sending = False
                if set_gen:
                    set_gen(False)

    def _generate_reply(self, batch: list[str], from_inbox: bool = False,
                        quote: dict | None = None) -> None:
        state = db.get_state()
        if state == "SLEEPING":
            self._send(corpus.pick(corpus.SLEEPY_REPLIES, "sleepy"), kind="chat")
            return

        # 信息调度层统一组装上下文：人设+便签+profile → 游戏规则 → 相关记忆 → 最近上下文
        # quote 是引用回复：让 TA 知道这句话在回哪句（拼成一条 system 注入，见 context）
        messages = context.build_chat_messages(state, batch, game.prompt_injection(),
                                               inbox=from_inbox, quote=quote)
        # 双层生成（人格优化）：先把不可见的内心独白跑出来——立场在这一层定住，
        # 第 2 层只负责"怎么说"，改不了"说什么"。独白拿不到就原样单层生成。
        messages = self._with_inner_monologue(messages, batch)
        reply = self._generate_reply_text(messages, batch)
        if _is_llm_error(reply):
            # v0.88 C2（批 0）：LLM 失败文案**不当正经回复落库**——它一旦以 chat 落进
            # messages，后面每一轮 session_context 都会把这句"（脑子卡住了：…）"读回去，
            # 污染上下文。改走 kind=notice：前端灰色小字、session_context 天然不读
            # （只取 chat/proactive）、不进目录不可引用。
            print(f"[LLM] 回复生成失败，按提示落库不进上下文：{reply[:40]}")
            self._send(reply, kind="notice")
            return
        # 闸门 2（批 5）：回复都生成成功了，才有"认真讨论过"可言（细则见函数注释）。
        _note_browsed_discussed(batch)
        # 措辞红线（H 模块·反愧疚）：代价只呈现不归因。prompt 已约束，这里是兜底改写——
        # 归因措辞任何状态都不该出现，命中就换成"只说自己的状态"的安全句（不归因）。
        if persona.has_cost_blame(reply):
            print(f"[红线] 回复把代价归因到对方，已兜底改写：{reply[:40]}")
            reply = corpus.pick(corpus.COST_SAFE_LINES, "cost_safe")

        # 人格红线体检（只告警、不改写）：反谄媚五条靠 prompt 约束，这里留证据——
        # 部署之后想知道"人格有没有被磨平"，看有没有这几条告警就行。
        warns = persona.redline_report(" ".join(batch), reply)
        if warns:
            print(f"[红线] {'／'.join(warns)}：{reply[:50]}")

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
                # v0.89 §7①：间隔取决于**下一句**多长（真人是打完这句再发这句，
                # 不是发完上句才按上句长度等）。原来取 segments[i-1]，索引差一位。
                _time.sleep(min(max(len(seg) * config.SEND_SECONDS_PER_CHAR,
                                    config.SEND_MIN_SECONDS), config.SEND_MAX_SECONDS))
            # 插话检测（发送前最后一刻检查）：TA 打字/连发期间你又说话了——
            # 停发剩余段，先听你说（不再补发"你说，我听着"这类让步语——用户明确不要）
            if self._interrupted():
                interrupted = True
                break
            self._send(seg, kind="chat")

        # 兴趣修炼（S4）：聊天源——回复里也表达兴趣（"最近有点迷上X"），一并记线索。
        for t in interests.detect_and_record(reply):
            print(f"[兴趣] 转正：{t}")

        # 自我意识流（S4）：记下"我说了什么"（摘要）——让流里有一条完整的自我叙事。
        # 全文在 messages 表里，这里只存摘要供注入；"我在想/我说"是每轮对话的锚点。
        said = " ".join(reply.split())
        if said:
            # 存全文（v0.80：注入也不再截断，截了就等于没说过这句话）
            stream.append("reply", f"我说：{said}")
            # 待办打勾：刚才那段话里说到了哪件攒着的事 → 标记做完（做完不再当话题）
            if config.TODOS_ENABLED:
                for t in todos.mark_done(said):
                    print(f"[待办] 打勾：{t[:30]}")

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

    # --- 双层生成（人格优化 v0.54）：内心独白 → 说出的话 ---
    def _with_inner_monologue(self, messages: list[dict], batch: list[str]) -> list[dict]:
        """在系统 prompt 之后、生成之前，先跑一层看不见的内心独白把立场定住。

        为什么这样改能治"谁跟它说话它就变成谁的形状"：
        - 独白层里用户的话只是输入之一，人格内核是主导（用户看不到 → 不必讨好）
        - 第 2 层看到的已经是"我不同意"，它只能决定怎么说
        - "不想聊/没感觉"在独白里是合法输出 → 敷衍、短回、转移话题自然长出来

        独白失败（LLM 不可用/超时）不影响主流程：返回原 messages 退化成单层。
        """
        if not config.INNER_MONOLOGUE_ENABLED:
            return messages
        text = self._inner_monologue(messages, batch)
        if not text:
            return messages
        return list(messages) + [
            {"role": "system", "content": persona.utterance_instruction(text)}]

    def _inner_monologue(self, messages: list[dict], batch: list[str]) -> str:
        """第 1 次调用：结构化内心独白（不发给对方）。失败返回空串由上层兜底。"""
        system = persona.monologue_prompt()
        # 独白层要看到的三样东西：①最近在说什么 ②关于我自己的记忆（一致性检查）
        # ③ 我上次的内部状态。省掉"对方的 profile/便签"，那会让判断偏向对方。
        parts = []
        if config.SELF_MEMORY_ENABLED:
            hint = selfmem.prompt_hint(" ".join(batch))
            if hint:
                parts.append(hint)
            shint = selfmem.state_hint()
            if shint:
                parts.append(shint)
        # 整段会话全量（v0.80）：独白层以前也只读 20 条，同样瞎——判"和旧立场冲不冲突"
        # 靠的就是早些时候说过的话（实测 #42）
        recent = db.session_context(config.CONTEXT_MAX_MESSAGES)
        if parts:
            system = system + "\n\n" + "\n".join(parts)
        try:
            raw = llm.chat(
                [{"role": "system", "content": system}]
                + recent
                + [{"role": "user", "content": persona.MONOLOGUE_FORMAT + "\n\n"
                    + f"【TA刚才说的】\n{' '.join(batch)}"}],
                temperature=config.INNER_MONOLOGUE_TEMPERATURE,
                max_tokens=config.INNER_MONOLOGUE_MAX_TOKENS,
                thinking=False,   # 独白要采样多样性（temperature 生效），关思考
            )
        except llm.LLMUnavailable as e:
            print(f"[独白] 跳过（LLM 不可用：{e}）")
            return ""
        raw = (raw or "").strip()
        if len(raw) < 8:     # 空转/ stub 文案都视为没读到人
            return ""
        print(f"[独白] {raw[:120].replace(chr(10), ' / ')}…")
        # 兴趣修炼（S4）：聊天源——独白里流露"感兴趣"的想法，即时用词表记一条兴趣线索
        # （纯规则、不调 LLM）。同一主题累计到阈值会转正，转正时打一条日志。
        promoted_now = interests.detect_and_record(raw)
        for t in promoted_now:
            print(f"[兴趣] 转正：{t}")
        # 自我意识流（S4）：内心独白持久化——不再用完就扔，跨轮有连续性。
        # 每轮注入时只取摘要，全文留在这里供睡前复盘浓缩。
        stream.append("monologue", f"我在想：{raw}")
        # 待办队列（v0.80）：独白里冒出来的念头（【我自己的话题】非空）入队等它自己聊
        if config.TODOS_ENABLED:
            topic = todos.from_monologue(raw)
            if topic:
                todos.add(topic, source="monologue")
        return raw

    def _generate_reply_text(self, messages: list[dict], batch: list[str]) -> str:
        """生成回复：有工具信号 → tool loop；否则普通快速路径。

        v0.94 §3.1：分流与工具循环已收拢到 tools（工具层纯机制），这里只做一行转发。"""
        return tools.run_reply(messages, batch)

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
            ], temperature=0.1, thinking=False)   # 短回执（是/否）：关思考省 token
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

    # --- 离线留言（你 → 它）：TA 离线（专注工作/睡觉）时你发的消息攒着，上线后回 ---
    def _send_offline_notice(self) -> None:
        """离线时收到消息的轻提示：睡 → 迷糊语；工作专注 → 第一人称"正忙"（不调 LLM）。
        kind=notice：前端渲染成灰色居中小字（离线自动提示），不当成一条正经消息。"""
        reason = offline.offline_reason()
        if reason == "sleep":
            self._send(corpus.pick(corpus.SLEEPY_REPLIES, "sleepy"), kind="notice")
        else:
            self._send(corpus.pick(corpus.OFFLINE_FOCUS_NOTICES, "offline_focus"), kind="notice")

    def flush_inbox(self) -> None:
        """上线了：把离线期间攒下的留言正经回应一遍（后台线程，复用 _sending 互斥）。
        意识流在这里补记：先一行「留言了，我看看」，再逐条「XX说：…」——
        入库时刻是补记时刻（睡着的那段时间 TA 对留言无感知，醒来才一并"看见"）。"""
        items = offline.take_inbox()
        if not items:
            return
        stream.append("inbox", f"{persona.user_name()}给我留言了，我看看")
        for m in items:
            stream.append("user", f"{persona.user_name()}说：{m['text']}")
        texts = [m["text"] for m in items]
        threading.Thread(target=self._reply_inbox_worker, args=(texts,), daemon=True).start()

    def _reply_inbox_worker(self, texts: list[str]) -> None:
        """等正常回复线程空闲，再以"刚上线看到留言"的语境回应离线留言。"""
        while not self._stopped:
            with self._lock:
                if not self._sending:
                    self._sending = True
                    break
            _time.sleep(0.2)
        if self._stopped:
            return
        set_gen = getattr(self.channel, "set_generating", None)
        try:
            if set_gen:
                set_gen(True)
            with db.PATH_LOCK:
                self._generate_reply(texts, from_inbox=True)
        finally:
            with self._lock:
                self._sending = False
            if set_gen:
                set_gen(False)

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
            if prompt is None:
                # v0.89 §6：random 场景没有真实弹药就不发（旧行为是让 LLM 自由发挥
                # 编一个"我刚刷手机看到…"，于是出现连发几条都跟事实对不上的情况）
                print(f"[proactive:{scenario}] 没有真实的事可说，这次不发")
                _clear_carried_ids()
                return False
            try:
                reply = llm.chat(prompt, temperature=1.0, thinking=False)
            except llm.LLMUnavailable as e:
                print(f"[proactive:{scenario}] LLM 不可用：{e}")
                _clear_carried_ids()
                return False

        if scenario == "bubble":
            db.incr_counter("bubble_count")
            self._reset_bubble_timer()
        else:
            db.incr_counter("proactive_count")
        db.incr_counter("unanswered_proactive")
        # 分段连发：主动消息没走 _generate_reply 的分段，LLM 会用换行把两段话并成一条
        # （如"热粥"+"《左传》"黏一起）。按换行拆开逐条发，像真人连发几条；
        # 段间隔与正常回复同一套口径（v0.99 修：原来 4 条同一秒拍出来）。
        for i, seg in enumerate(s.strip() for s in reply.split("\n") if s.strip()):
            if i > 0:
                _time.sleep(min(max(len(seg) * config.SEND_SECONDS_PER_CHAR,
                                    config.SEND_MIN_SECONDS), config.SEND_MAX_SECONDS))
            self._send(seg, kind="proactive")
        # 意识流补记"我说了什么"（v0.99 修：主动消息原来不留流，自我叙事断片；
        # 独白不加是设计——主动消息本来就是"我自己想说"，单层生成）
        said = " ".join(reply.split())
        if said:
            stream.append("reply", f"我说：{said}")
        # §5 拍板 #6③：主动消息发出后，把携带的素材 id 按 id 打勾（废弃 6 字字面指纹）
        _settle_carried_ids()
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

    # --- 作息切换（调度器调用；自主作息说"还能撑/想赖床"就先进 SLEEPY 过渡） ---
    def go_sleep(self) -> None:
        """锚点到点：自主作息说还能撑 → 进 SLEEPY（stay_up）等 tick；撑不住 → 直接入睡。"""
        if datetime.now() < offline.tonight_sleep_at(datetime.now()):
            db.set_state("SLEEPY")
            db.set_kv("sleepy_reason", "stay_up")
            return
        self._do_sleep()

    def _do_sleep(self) -> None:
        """真正入睡：道晚安 + 写日记 + 进入睡眠。"""
        # 晚安消息要在睡前发，所以先临时保持 AWAKE 发出去
        self.proactive("night")
        # 睡前写日记 + 复盘当天（P2：记忆 v3 每日回顾 + 日记系统 + 回顾线）
        if config.DIARY_ENABLED:
            self.daily_review()
        db.set_state("SLEEPING")
        db.set_kv("sleepy_reason", "")
        stream.append("sleep", "我困了，去睡了")

    def wake_up(self) -> None:
        """锚点到点：自主作息说还想赖床 → 进 SLEEPY（lie_in）等 tick；睡饱了 → 直接醒。
        问的是 offline.today_wake_at——"今天该几点起"（老 bug：问成 next_wake_at，
        早晨一触发就返回明天，于是永远赖床、一整天不醒，实测 #26）。"""
        if datetime.now() < offline.today_wake_at(datetime.now()):
            db.set_state("SLEEPY")
            db.set_kv("sleepy_reason", "lie_in")
            return
        self._do_wake()

    def _do_wake(self) -> None:
        """真正醒来：道早安 + 回离线留言 + 补昨晚错过的关怀提醒。"""
        db.set_state("AWAKE")
        db.set_kv("sleepy_reason", "")
        stream.append("wake", "我伸了懒腰，起床了")
        self.proactive("morning")
        self.flush_inbox()
        events.flush_missed(self)  # 昨晚说好要提醒的，结果睡着了，现在补上

    # --- 每日回顾 + 写日记（P2） ---
    def daily_review(self) -> str:
        """睡前/手动触发：读当日完整对话 → 复盘 + 写日记 + 补记/修正记忆。幂等。"""
        import diary
        try:
            with db.PATH_LOCK:  # 与切换朋友互斥：复盘/写日记全程读写同一个库
                return diary.daily_review()
        except llm.LLMUnavailable as e:
            return corpus.LLM_ERROR.format(e)

    # --- 关怀事件（S4）：履行对朋友的承诺，到点提醒 / 醒来补错过 ---
    def care_remind(self, event: dict, missed: bool = False) -> bool:
        """履行一份对朋友的承诺：到点轻轻提醒，或醒来补昨晚错过的。返回是否发出。

        missed=True 时带"昨晚说好结果我睡着了"的自嘲语境。优雅失败：对方彻底
        不理了（连续未回复熔断）就不再追，这承诺就不补了。
        """
        if db.get_counter("unanswered_proactive") >= config.MAX_UNANSWERED_PROACTIVE:
            return False
        what = event.get("what", "")
        when = event.get("when", "")
        state = db.get_state()
        if missed:
            ask = (f"（内心活动，不用回复这句话）你之前答应过对方要「{what}」，"
                   f"说好的时间是 {when}，结果你当时睡着了没提醒。现在刚醒，自然地补一句："
                   f"带一点\"昨晚我睡着了\"的自嘲或小歉意，然后把该提醒的事补上。"
                   f"一两句话，别啰嗦。")
        else:
            ask = (f"（内心活动，不用回复这句话）你答应过对方要「{what}」，"
                   f"现在到点了（{when}）。用你自己的口吻轻轻提醒TA一句，"
                   f"一两句话，别啰嗦。")
        try:
            reply = llm.chat([
                {"role": "system", "content": persona.build_system_prompt(state)},
                {"role": "user", "content": ask},
            ], temperature=1.0, thinking=False)
        except llm.LLMUnavailable as e:
            print(f"[care] 提醒生成失败：{e}")
            return False
        if not reply or reply == corpus.LLM_STUB:
            return False
        db.incr_counter("unanswered_proactive")  # 发了主动消息，纳入熔断感知
        self._send(reply, kind="proactive")
        return True

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
