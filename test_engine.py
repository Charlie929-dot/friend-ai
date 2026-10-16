"""引擎与交互（收发/状态机/锁/刷新/打断/会话/网页端/开机自启）（E4 拆分，v0.92）。

运行：`python run_tests.py engine`（或全量 `python run_tests.py`）。
本文件顶部必须 `from _test_common import *`——两段全局机制（临时目录收口 +
关双层生成）在此生效，见约定 #10/#11。
"""
from _test_common import *  # noqa: F401,F403


def test_cli_timestamps():
    """AI 消息带 [时间 AI] 前缀；用户发送后打印 [时间 你] 标记行。"""
    from channels.cli import CliChannel
    ch = CliChannel()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ch.send("在吗")
    out = buf.getvalue()
    # 发言人显示朋友自己的名字（人格卡名 > 别名 > 老朋友）
    import db as _db, persona_pool
    _db.init()
    card = persona_pool.load()
    name = card["name"] if card else (_db.get_kv("alias") or "老朋友")
    assert TS_RE.search(out) and f" {name}] 在吗" in out, \
        f"AI 消息缺时间戳或名字不对（期望 {name}）：{out!r}"

    # 模拟用户输入一行后 EOF 退出
    received = []
    real_input = builtins.input
    builtins.input = lambda: (_ for _ in ()).throw(EOFError()) if received else "你好"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ch.run(received.append)
    finally:
        builtins.input = real_input
    out = buf.getvalue()
    assert received == ["你好"], f"on_message 没收到消息：{received}"
    assert TS_RE.search(out) and " 你]" in out, f"用户消息缺时间标记：{out!r}"
    print("ok CLI 时间戳（AI 消息 + 用户消息标记）")


def test_refresh_reloads_config_and_modules():
    """/refresh 后：config 参数、persona 文案都按文件最新内容生效。"""
    # 1) config 参数：改环境变量模拟 .env 变更（load_dotenv(force=True) 已覆盖）
    old_limit = config.DAILY_PROACTIVE_LIMIT
    os.environ["DAILY_PROACTIVE_LIMIT"] = "42"
    # 2) persona 文案：运行时弄脏，refresh 后应恢复文件原值
    original_flavor = persona.DEFAULT_FLAVOR
    persona.DEFAULT_FLAVOR = "被篡改的人设"

    result = commands.handle("/refresh")

    assert result == "刷新成功", f"返回不对：{result!r}"
    assert config.DAILY_PROACTIVE_LIMIT == 42, \
        f"config 未重载：{config.DAILY_PROACTIVE_LIMIT}"
    assert persona.DEFAULT_FLAVOR == original_flavor, "persona 未恢复文件内容"

    # 还原环境，避免污染后续
    os.environ["DAILY_PROACTIVE_LIMIT"] = str(old_limit)
    import importlib
    importlib.reload(config)
    print("ok /refresh 热重载（config 参数 + persona 文案）")


def test_refresh_reschedules_jobs():
    """on_config_reloaded 后定时任务按新参数重排。"""
    from apscheduler.triggers.cron import CronTrigger
    import scheduler

    engine = types.SimpleNamespace(
        wake_up=lambda: None, go_sleep=lambda: None,
        proactive=lambda s: None, check_bubble=lambda: None,
        check_nudge=lambda: None,
        _do_sleep=lambda: None, _do_wake=lambda: None, flush_inbox=lambda: None)
    sch = scheduler.start(engine)

    old_noon = config.NOON_HOUR
    os.environ["NOON_HOUR"] = "15"
    commands.handle("/refresh")

    # 模拟 main.py 的 /refresh 善后
    real_engine = types.SimpleNamespace(sched=sch, _reset_bubble_timer=lambda: None)
    scheduler.reschedule_all(sch, real_engine)

    noon_fields = {f.name: str(f) for f in sch.get_job("noon").trigger.fields}
    assert noon_fields["hour"] == "15", f"noon 任务未重排：{noon_fields}"
    # wake/sleep 跟随人格卡（当前 friend.db 里的朋友），无人格卡才是 config 默认
    import persona as _p
    wake_fields = {f.name: str(f) for f in sch.get_job("wake").trigger.fields}
    assert wake_fields["hour"] == str(_p.behavior_params()["wake_hour"]), \
        f"wake 任务异常：{wake_fields}"

    os.environ["NOON_HOUR"] = str(old_noon)
    import importlib
    importlib.reload(config)
    sch.shutdown()
    print("ok /refresh 后定时任务重排（noon/wake/random_chat）")


def test_startup_pick_friend():
    """启动菜单：回车继续 / 编号换老朋友 / n 抽新朋友 / 乱输入兜底。"""
    import db as _db
    _db.init()
    n_archives = len(commands._archives())

    printed = []
    # 回车 → None（继续当前）
    assert commands.pick_friend_on_startup(input_fn=lambda p: "", print_fn=printed.append) is None
    # n → 抽新朋友
    assert commands.pick_friend_on_startup(input_fn=lambda p: "n", print_fn=lambda x: None) == "/new-friend"
    # 合法编号 → /friend N
    if n_archives:
        assert commands.pick_friend_on_startup(
            input_fn=lambda p: "1", print_fn=lambda x: None) == "/friend 1"
        # 超界编号 → None（兜底）
        assert commands.pick_friend_on_startup(
            input_fn=lambda p: str(n_archives + 5), print_fn=lambda x: None) is None
    # 乱输入 → None（兜底）
    assert commands.pick_friend_on_startup(input_fn=lambda p: "???", print_fn=lambda x: None) is None
    # 菜单里要能看到当前朋友和归档列表
    menu = "\n".join(printed)
    assert "当前朋友：" in menu and "抽一位新朋友" in menu
    print("ok 启动选朋友菜单（回车/编号/n/兜底）")


def test_game_mode():
    """娱乐模式：开局/结束检测、规则注入、游戏中暂停主动消息、/status 显示。"""
    import tempfile
    import db as _db
    import game
    import guard as _guard

    tmp = Path(tempfile.mkdtemp())
    # WORK_ENABLED=False：guard 的离线拦截跟「现在几点/在读不在读」耦合（约定 #10），
    # 关掉工作系统才能稳定测游戏状态机本身的拦截逻辑
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       WORK_ENABLED=False):
        # 泛泛说"玩游戏"不开局，提到具体游戏名才开
        game.on_user_text("我们玩个游戏吧")
        assert game.current() is None, "泛泛的游戏意愿不应触发状态机"
        game.on_user_text("来玩成语接龙吧")
        assert game.current()["game"] == "idiom", "成语接龙未开局"
        # 规则注入
        inj = game.prompt_injection()
        assert "成语接龙" in inj and "规则" in inj
        # 游戏中：主动消息全部暂停
        ok, reason = _guard.can_send_proactive(scenario="bubble")
        assert not ok and reason == "游戏中", f"游戏中未拦截冒泡：{ok} {reason}"
        ok, reason = _guard.can_send_proactive(scenario="morning")
        assert not ok and reason == "游戏中", f"游戏中未拦截问候：{ok} {reason}"
        # /status 显示游戏状态
        out = commands.handle("/status")
        assert "游戏中：成语接龙" in out, f"/status 缺游戏状态：{out}"
        # 游戏中提到另一个游戏名 = 换游戏
        game.on_user_text("要不玩海龟汤")
        assert game.current()["game"] == "soup", "换游戏失败"
        assert "海龟汤" in game.prompt_injection()
        # 结束游戏 → 主动消息恢复
        game.on_user_text("不玩了")
        assert game.current() is None, "游戏未结束"
        assert game.prompt_injection() is None
        ok, _ = _guard.can_send_proactive(scenario="bubble")
        assert ok, "游戏结束后主动消息未恢复"
        # 不在游戏时说"不玩了"不应报错
        game.on_user_text("不玩了")
        print("ok 娱乐模式（开局/换游戏/结束/主动消息暂停恢复/状态显示）")


def test_session_end_and_outbox():
    """话题结束 AI 判断（AI回复信号 + LLM判官）+ outbox 未读队列重放。"""
    import json as _json
    import tempfile
    import db as _db
    import llm as _llm
    import memory as _memory
    from core import Engine

    class DummyChannel:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)

    tmp = Path(tempfile.mkdtemp())
    real_chat = _llm.chat
    real_extract = _memory.extract_async
    _memory.extract_async = lambda: None  # 测试不碰真实 LLM
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        try:
            ch = DummyChannel()
            engine = Engine(ch)

            # 1) AI 回复自带结束信号 → 会话关闭（不轻催）
            _db.set_kv("session_open", "1")
            _llm.chat = lambda *a, **k: "晚安啦，好梦"
            engine._generate_reply(["我要睡了"])
            assert _db.get_kv("session_open") == "0", "AI 回复含晚安未关闭会话"

            # 2) LLM 判官：关键词都没抓住时由模型判
            _db.set_kv("session_open", "1")
            _llm.chat = lambda *a, **k: "是"
            assert engine._judge_session_end(["那我先走啦"], "嗯嗯，去吧！") is True
            assert _db.get_kv("session_open") == "0", "LLM 判官未关闭会话"
            _db.set_kv("session_open", "1")
            _llm.chat = lambda *a, **k: "否"
            assert engine._judge_session_end(["等下我有个问题"], "你说") is False
            assert _db.get_kv("session_open") == "1", "LLM 判官误关会话"
            # LLM 不可用/乱答 → 不动会话
            _llm.chat = lambda *a, **k: "（stub：还没配 key）"
            assert engine._judge_session_end(["哈哈"], "哈哈") is False
            assert _db.get_kv("session_open") == "1"

            # 3) outbox 未读队列：主动消息攒着，用户回来时重放并清空
            _llm.chat = lambda *a, **k: "（test）"
            engine._send("冒个泡～", kind="proactive")
            engine._send("中午吃了没", kind="proactive")
            q = _json.loads(_db.get_kv("outbox_queue"))
            assert len(q) == 2, f"未读队列没攒住：{q}"
            assert (tmp / "outbox.log").exists(), "outbox.log 存档丢了"
            # 回来就先看到（启动/换朋友时的重放，不用等开口说第一句）
            import core as _core
            text = _core.pending_outbox_text()
            assert "2 条留言" in text and "冒个泡" in text and "中午吃了没" in text, \
                f"回来时的留言内容不对：{text}"
            assert _db.get_kv("outbox_queue") == "", "取走后队列未清空"
            assert _core.pending_outbox_text() == "", "空队列应返回空串"
            # 开口说话也能拿到（进程没重启时的兜底路径）
            engine._send("还在吗", kind="proactive")
            ch.sent.clear()
            engine.on_user_message("我回来了")
            assert any("1 条留言" in s and "还在吗" in s for s in ch.sent), \
                f"开口后没有重放留言：{ch.sent}"
            assert _db.get_kv("outbox_queue") == "", "重放后队列未清空"
            # 没有留言时不打扰
            ch.sent.clear()
            engine.on_user_message("在吗")
            assert not any("条留言" in s for s in ch.sent), "空队列也重放了"
            print("ok 话题结束 AI 判断 + outbox 未读队列重放")
        finally:
            engine.stop()  # 停掉后台回复线程，别让它拿真实 llm.chat 调 API 卡住 PATH_LOCK
            _llm.chat = real_chat
            _memory.extract_async = real_extract


def test_typing_indicator():
    """"正在输入"：回复按字数计算输入时长，先 typing() 再发正文。"""
    import tempfile
    import time as _t
    import db as _db
    import memory as _mem
    from core import Engine

    tmp = Path(tempfile.mkdtemp())
    # WORK_ENABLED=False + 快档：只测打字节奏；工作时段 fresh DB 会判离线，
    #   消息进 inbox 不回复（时间依赖 flaky，与本测试无关）
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       TYPING_ENABLED=True, WORK_ENABLED=False, **_FAST_TIMING):
        try:
            class Ch:
                def __init__(self):
                    self.events = []  # ("typing",)/("send", text)
                def send(self, text):
                    self.events.append(("send", text))
                def typing(self):
                    self.events.append(("typing",))

            ch = Ch()
            eng = Engine(ch)
            eng._judge_session_end = lambda *a, **k: None
            real_chat = _mem.llm.chat
            real_extract = _mem.extract_async
            _mem.llm.chat = lambda *a, **k: "好呀好呀\n就这么说定了"
            _mem.extract_async = lambda: None
            try:
                eng.on_user_message("周末去看电影吗")
                deadline = _t.time() + 3
                while _t.time() < deadline:
                    sends = [e for e in ch.events if e[0] == "send"]
                    if len(sends) >= 2:
                        break
                    _t.sleep(0.02)
            finally:
                _mem.llm.chat = real_chat
                _mem.extract_async = real_extract
            kinds = [e[0] for e in ch.events]
            assert "typing" in kinds, f"没出现正在输入：{ch.events}"
            assert kinds.index("typing") < kinds.index("send"), \
                f"typing 应在正文之前：{ch.events}"
            sends = [e[1] for e in ch.events if e[0] == "send"]
            assert sends == ["好呀好呀", "就这么说定了"], f"分段发送不对：{sends}"
            print("ok 正在输入（typing 先于正文 + 分段发送）")
        finally:
            eng.stop()  # 停掉后台回复线程，别让它拿真实 llm.chat 卡住 PATH_LOCK


def test_interrupt_detection():
    """插话检测：打字阶段打断→整段回复作废；连发中途打断→停发+让步语。"""
    import tempfile
    import time as _t
    import db as _db
    import memory as _mem
    import persona as _persona
    from core import Engine

    tmp = Path(tempfile.mkdtemp())
    # WORK_ENABLED=False + 快档：本测试只测插话；开着工作系统会让"现在几点"
    #   决定是否离线（fresh DB 在工作时段必然 offline → 消息进 inbox 不回话）
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       TYPING_ENABLED=True, WORK_ENABLED=False, **_FAST_TIMING):
        try:
            class Ch:
                def __init__(self):
                    self.events = []
                def send(self, text):
                    self.events.append(("send", text))
                def typing(self):
                    self.events.append(("typing",))

            real_chat = _mem.llm.chat
            real_extract = _mem.extract_async
            _mem.extract_async = lambda: None
            eng = eng2 = None   # finally 里要 stop，先绑空值防场景一挂掉时 UnboundLocalError 掩盖断言

            def wait_for(pred, timeout=4):
                deadline = _t.time() + timeout
                while _t.time() < deadline:
                    if pred():
                        return True
                    _t.sleep(0.02)
                return False

            # 场景一：打字阶段插话 → 本次回复作废，直接回应新消息
            config.TYPING_SECONDS_PER_CHAR = 0.5   # 打字很慢，留出插话窗口
            config.TYPING_MIN_SECONDS = 0.3
            config.TYPING_MAX_SECONDS = 1.0
            ch = Ch()
            eng = Engine(ch)
            eng._judge_session_end = lambda *a, **k: None
            # 按内容应答而非调用顺序——早期测试的判官线程可能晚触发，偷走按序 mock 的回复
            def fake_chat1(messages, **k):
                last = messages[-1]["content"]
                if "等等我先说个事" in last:
                    return "回应你的插话"
                if "在吗" in last:
                    return "本来要回你的旧消息"
                return "嗯"  # 判官等其他调用的安全应答
            _mem.llm.chat = fake_chat1
            eng.on_user_message("在吗")
            assert wait_for(lambda: any(e[0] == "typing" for e in ch.events)), "没进入打字"
            eng.on_user_message("等等我先说个事")  # 打字期间插话
            assert wait_for(lambda: any(e[0] == "send" for e in ch.events)), "没有后续回复"
            _t.sleep(0.8)  # 等插话消息的回复走完
            sent_texts = [e[1] for e in ch.events if e[0] == "send"]
            assert "本来要回你的旧消息" not in sent_texts, \
                f"打字阶段被打断不应再发旧回复：{sent_texts}"
            assert "回应你的插话" in sent_texts, f"应回应插话内容：{sent_texts}"

            # 场景二：分段连发中途插话 → 停发剩余段 + 让步语
            config.TYPING_MIN_SECONDS = 0.01
            config.TYPING_MAX_SECONDS = 0.02
            config.TYPING_SECONDS_PER_CHAR = 0.001
            config.SEND_MIN_SECONDS = 0.4   # 段间间隔拉长，留出插话窗口
            config.SEND_MAX_SECONDS = 0.5
            config.SEND_SECONDS_PER_CHAR = 0.01
            ch2 = Ch()
            eng2 = Engine(ch2)
            eng2._judge_session_end = lambda *a, **k: None
            def fake_chat2(messages, **k):
                last = messages[-1]["content"]
                if "停一下我插句嘴" in last:
                    return "回应插话"
                if "讲个长的" in last:
                    return "第一段\n第二段不该发出"
                return "嗯"
            _mem.llm.chat = fake_chat2
            eng2.on_user_message("讲个长的")
            assert wait_for(lambda: any(e[1] == "第一段" for e in ch2.events if e[0] == "send")), \
                f"第一段没发出：{ch2.events}"
            eng2.on_user_message("停一下我插句嘴")  # 段间插话
            _t.sleep(1.2)  # 等段间睡眠与后续流程走完
            sent2 = [e[1] for e in ch2.events if e[0] == "send"]
            assert "第二段不该发出" not in sent2, f"插话后剩余段应停发：{sent2}"
            # 打断不再补发让步语（"你说，我听着"这类）——用户拍板：打断就是安静停发、先听
            assert not any(t in _persona.INTERRUPT_ACKS for t in sent2), \
                f"打断不应再补发让步语：{sent2}"
            print("ok 插话检测（打字打断作废 + 中途停发剩余段、不再补让步语）")
        finally:
            for _eng in (eng, eng2):
                if _eng:
                    _eng.stop()
            _mem.llm.chat = real_chat
            _mem.extract_async = real_extract


def test_db_path_lock():
    """PATH_LOCK 互斥：在途提取持锁期间切库被阻塞；放行后记忆落旧库不串新库。"""
    import tempfile
    import threading
    import time as _t
    import sqlite3
    import db as _db
    import memory as _mem

    tmp = Path(tempfile.mkdtemp())
    path_a, path_b = tmp / "a.db", tmp / "b.db"
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=path_a):
        _db.add_message("user", "我明天早上要去图书馆自习")
        _db.add_message("assistant", "好啊，几点去？")

        gate = threading.Event()
        switched = threading.Event()
        real_chat = _mem.llm.chat

        def slow_chat(messages, temperature=0.7, **k):
            gate.wait(5)  # 提取线程持锁卡在 LLM 调用
            return '[{"content": "小徐计划明天早上去图书馆自习", "category": "计划", "theme": "学业"}]'

        def do_switch():
            with _db.PATH_LOCK:      # 模拟 /friend：必须与在途提取互斥
                config.DB_PATH = path_b
                _db.init()
            switched.set()

        _mem.llm.chat = slow_chat
        _mem.extract_async()          # 后台提取：持锁，卡在 gate
        _t.sleep(0.8)                 # 确保提取线程先拿到锁
        t = threading.Thread(target=do_switch)
        t.start()
        _t.sleep(0.8)
        assert not switched.is_set(), "提取持锁期间切库线程应被阻塞"
        gate.set()
        t.join(5)
        assert switched.is_set(), "提取放行后切库线程应完成"
        _mem.llm.chat = real_chat
        # 等提取线程写完（gate 打开后它还需要一点点时间做 db 写）
        for _ in range(100):
            n = sqlite3.connect(path_a).execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            if n == 1:
                break
            _t.sleep(0.05)
        a_n = sqlite3.connect(path_a).execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        b_n = sqlite3.connect(path_b).execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        assert a_n == 1 and b_n == 0, f"记忆应落旧库 A：A={a_n} B={b_n}"
        print("ok PATH_LOCK 互斥（在途提取阻塞切库 + 记忆不串库）")


def test_friend_switch_no_deadlock():
    """换朋友不能卡死（实测 #33）：网页端 /api/send 外面握着 SEND_LOCK，
    而 /friend /new-friend 内部切库还要再拿一次 → 同一线程重入。
    SEND_LOCK 必须是可重入锁，否则「换朋友」永远停在"稍等一下"。"""
    import tempfile
    import threading as _th
    import db as _db
    import commands as _cmd

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        _db.add_message("user", "你好呀")
        # 造两个归档朋友（/friend 编号指向它们）
        _db.set_kv("friend_id", "aaa11111")
        _cmd._archive_current()
        _db.set_kv("friend_id", "bbb22222")
        _cmd._archive_current()
        assert len(_cmd._archives()) == 2, "应有两个归档朋友"

        # 1) 可重入：同一线程连拿两次不能自锁
        with _db.SEND_LOCK:
            assert _db.SEND_LOCK.acquire(timeout=2), "SEND_LOCK 必须是可重入锁"
            _db.SEND_LOCK.release()

        # 2) 完整走一遍网页端的调用链（外面握 SEND_LOCK → 内部切库）
        box = {}

        def _switch():
            with _db.SEND_LOCK:
                box["out"] = _cmd.handle("/friend 1")

        t = _th.Thread(target=_switch, daemon=True)
        t.start()
        t.join(8)
        assert not t.is_alive(), "换朋友卡死了（SEND_LOCK 重入死锁）"
        assert "欢迎回来" in box.get("out", ""), f"切换结果：{box.get('out')}"
        print("ok 换朋友不卡死（SEND_LOCK 可重入）")


def test_delete_friend():
    """删除朋友（用户拍板）：先备份到 backups/ 再删；**正在聊的这位不许删**；
    不带 yes 只给确认提示不真删。"""
    import tempfile
    import db as _db
    import commands as _cmd

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        _db.add_message("user", "你好呀")
        # 造两位归档朋友：aaa 是"当前"那位，bbb 是要删的那位
        _db.set_kv("friend_id", "aaa11111")
        _cmd._archive_current()
        _db.set_kv("friend_id", "bbb22222")
        _cmd._archive_current()
        # 当前库切回 aaa（这样 aaa = 正在聊的，bbb = 可删）
        _cmd.handle("/friend 1")
        files = _cmd._archives()
        assert len(files) == 2, f"应有两个归档：{files}"

        bbb = [p for p in files if "bbb22222" in p.name][0]
        aaa = [p for p in files if "aaa11111" in p.name][0]

        # 1) 正在聊的这位：拒绝
        out = _cmd.handle("/delfriend 1 yes")   # 编号按相识时间，先确认哪个是当前
        cur_idx = 1 if "aaa11111" in files[0].name else 2
        out = _cmd.handle(f"/delfriend {cur_idx} yes")
        assert "不能删" in out, f"当前朋友不该被删：{out}"
        assert aaa.exists(), "当前朋友的库不能被动过"

        # 2) 不带 yes：只给确认提示，不真删
        other_idx = 3 - cur_idx
        out = _cmd.handle(f"/delfriend {other_idx}")
        assert "确认请输入" in out, f"未确认应只给提示：{out}"
        assert bbb.exists(), "未确认不能删"

        # 3) 带 yes：备份 + 删除 + 留痕
        out = _cmd.handle(f"/delfriend {other_idx} yes")
        assert "告别" in out, f"删除结果：{out}"
        assert not bbb.exists(), "归档文件应已删除"
        backups = list((tmp / "backups").glob("*.db"))
        assert len(backups) == 1, f"应先备份一份：{backups}"
        assert "bbb22222" in backups[0].name, f"备份应可追溯到这位：{backups[0].name}"
        log = (tmp / "archive.log").read_text(encoding="utf-8")
        assert "删除" in log and "备份" in log, "archive.log 应留痕"
        assert len(_cmd._archives()) == 1, "列表里应只剩一位"
        print("ok 删除朋友（先备份再删 / 当前朋友拒绝 / 未确认不删）")


def test_no_groupchat_leftovers():
    """v0.27 已砍群聊：通道接口不该再有 sender/群聊概念，代码里不该再有群聊措辞。"""
    import inspect
    import channels.base as _base
    import channels.cli as _cli
    import channels.telegram as _tg
    import channels.wecom as _wc

    # 通道签名回归 1:1 私聊（send/typing 不再带 sender）
    for cls in (_cli.CliChannel, _tg.TelegramChannel, _wc.WecomChannel):
        assert list(inspect.signature(cls.send).parameters) == ["self", "text"], \
            f"{cls.__name__}.send 还带着群聊参数"
    assert list(inspect.signature(_cli.CliChannel.typing).parameters) == ["self"]
    assert list(inspect.signature(_base.Channel.send).parameters) == ["self", "text"]

    # 需求文档口径：1:1 私聊，日记/记忆里不再出现"一群人""群里"
    import diary as _diary
    assert "一群人" not in _diary.REVIEW_PROMPT, "日记复盘 prompt 还留着群聊措辞"
    print("ok 群聊残留已清理（通道签名回到 1:1 + 群聊措辞清除）")


def test_conversation_continuation():
    """C 模块「对话中的主动延续」：人设引导 AI 追问/延伸，别一问一答即止。"""
    import tempfile
    import db as _db
    import persona as _persona

    rules = _persona.COMMON_RULES
    # 延续动作：追问 / 延伸，让对话有来有回（别把天聊死）
    assert "来有回" in rules, "人设缺「对话要有来有回」的延续引导"
    assert "追问" in rules and "延伸" in rules, "人设缺追问/延伸的延续动作"
    # 分寸约束：别连环追问、别查户口、对方收尾别硬续
    assert "别连环追问" in rules, "延续引导缺「别连环追问」分寸"
    assert "别查户口" in rules, "延续引导缺「别查户口」分寸"
    assert "别硬续" in rules, "延续引导缺「收尾别硬续」分寸"
    # 延续引导要真的进到完整 system prompt（core_identity_prompt 是它的底层）
    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        assert "来有回" in _persona.core_identity_prompt(), \
            "延续引导没进 system prompt"
    print("ok 对话延续引导（追问/延伸 + 分寸约束 + 进 system prompt）")


def test_urgent_channel():
    """S3 紧急通道（打断成本的安全阀）：急事能突破，日常夸张说法不能误触。"""
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import context as _ctx
    import corpus as _corpus
    import db as _db
    import guard as _guard

    for t in ("有急事，快帮我看看", "出事了", "十万火急", "这事很急，在线等", "紧急情况"):
        assert _guard.is_urgent(t), f"急事没识别出来：{t}"
    for t in ("在吗", "我急死了", "着急", "帮我个忙呗", "快点啦", "今天真累"):
        assert not _guard.is_urgent(t), f"日常话被误判成急事：{t}"
    assert hasattr(_corpus, "URGENT_QUERY") and _corpus.URGENT_QUERY, \
        "紧急词表必须收在 corpus（会扩充）"

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        now = _dt.now()
        assert not _guard.in_urgent_window(now), "没开窗口却已经在窗口里"
        _guard.open_urgent_window(now)
        assert _guard.in_urgent_window(now), "开了紧急窗口却没生效"

        # 急事当前：「在忙」的静默期要让路（否则安全阀形同虚设）
        _db.set_kv("quiet_until", (now + _td(hours=3)).isoformat(timespec="seconds"))
        _guard.open_urgent_window(now)
        assert not _db.get_kv("quiet_until"), "急事没解除忙碌静默期"

        def _injected(messages):
            return any("急事" in (m.get("content") or "")
                       for m in messages if m["role"] == "system")

        assert _injected(_ctx.build_chat_messages("AWAKE", ["在吗"])), \
            "紧急窗口内没注入「先听TA说」"
        # 窗口过期 → 恢复常态（不能一直随叫随到，那它就没自己的日子了）
        _db.set_kv("urgent_until", (now - _td(minutes=1)).isoformat(timespec="seconds"))
        assert not _injected(_ctx.build_chat_messages("AWAKE", ["在吗"])), \
            "紧急窗口过期了还在注入"
        print("ok 紧急通道（急事命中 / 日常不误伤 / 静默期让路 / 窗口会过期）")


def test_webui():
    """G 模块网页端（自用阶段主载体）：HUD 页面 + 消息/命令 API + 紧急通道。"""
    import tempfile
    import threading
    from http.server import ThreadingHTTPServer

    import commands as _cmds
    import db as _db
    import guard as _guard
    import httpx
    import life as _life
    import webui as _webui

    real_base = config.BASE_DIR
    real_web_dir = _webui.WEB_DIR
    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        try:
            _webui.WEB_DIR = Path(real_base) / "web"     # 页面文件仍留在项目里

            def fake_engine(text, quote=None):
                """替身引擎：命令返回输出，普通消息入库并返回消息 id（真引擎同款契约）。"""
                if text.startswith("/"):
                    return _cmds.handle(text)
                return _db.add_message("user", text, quote=quote)

            ch = _webui.WebChannel()
            srv = ThreadingHTTPServer(("127.0.0.1", 0), _webui._make_handler(ch, fake_engine))
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                # ⚠️ 沙箱里有 HTTP_PROXY，127.0.0.1 会被代理劫持，必须 trust_env=False
                c = httpx.Client(trust_env=False, timeout=10)
                base = f"http://127.0.0.1:{port}"

                html = c.get(base + "/").text
                assert "barE" in html and "barP" in html and 'id="chat"' in html and 'id="txt"' in html, \
                    "页面缺聊天主结构（消息流/输入框/精力条/进度条）"
                # 「最近记住的事」卡片 + 左右抽屉（v0.61 单栏极简重做）
                assert 'id="memList"' in html and 'id="drawerL"' in html and 'id="drawerR"' in html, \
                    "页面缺记忆卡/抽屉结构"
                # 直达底部 + 新消息提示 + 引用（v0.62 交互优化）
                assert 'id="toBottom"' in html and 'id="newMsgToast"' in html and 'id="quoteBar"' in html, \
                    "页面缺直达底部/新消息提示/引用条"
                assert 'e.isComposing' in html and 'nativeEvent.isComposing' not in html, \
                    "Enter 发送的按键判断仍有 bug（残留 nativeEvent.isComposing）"
                # 消息行操作按钮（引用/复制/撤回 hover 整行出现）+ 右键多选 + 调试面板复制（v0.63）
                assert 'class="msg-actions"' in html and 'data-act="copy"' in html and 'data-act="revoke"' in html, \
                    "页面缺消息行操作按钮（复制/撤回）"
                assert 'id="selectBar"' in html and 'id="selCopy"' in html and 'id="pCopy"' in html, \
                    "页面缺多选操作条/调试面板复制按钮"
                # 表情面板（v0.68）：分桶表情库存 + 点选即发 + 新增桶/表情
                assert 'id="emojiPanel"' in html and 'id="btnEmoji"' in html and 'id="epGrid"' in html \
                    and 'id="epAddEmoji"' in html and 'id="epAddBucket"' in html, \
                    "页面缺表情面板/表情按钮结构"
                # 调优参数面板（v0.86）：菜单项 + 模态框结构
                assert 'id="mTuning"' in html and 'id="tuneModal"' in html \
                    and 'id="tuneBody"' in html and 'id="tuneAdv"' in html, \
                    "页面缺调优参数面板结构"
                # 模型设置（v0.101）：模型那格从 <datalist> 改真 <select>
                # （datalist 没有可见箭头，手机/内置浏览器直接不弹，等于只能手填）
                assert 'id="lmModelSel"' in html and 'lmModels' not in html, \
                    "模型该是真下拉（datalist 已退役）"
                # 聊天区"正在输入"气泡（v0.101）：复用一直没接线的 .typing-bubble
                assert 'setTypingBubble' in html and 'typing-bubble' in html, \
                    "聊天区缺正在输入气泡接线"

                st = c.get(base + "/api/state").json()
                assert st["name"] and st["clock"], f"状态快照缺基本信息：{st}"
                # v0.89：HUD 契约换成「在读哪本 / 进度% / 章节 / 经验」（"第几件/余额"退役）
                assert st["work"]["phase"] in _life.PHASE_LABELS, f"phase 不合法：{st}"
                for k in ("energy", "exp", "progress", "chapter", "total_chapters",
                          "book_title", "overtime"):
                    assert k in st["work"], f"HUD 状态缺 {k}：{st['work']}"

                # 调优参数面板接口（v0.86）：返回分组 + 每项元数据 + 当前值
                tc = c.get(base + "/api/tuning/config").json()
                assert isinstance(tc["groups"], list) and tc["groups"], \
                    f"/api/tuning/config 该返回分组：{tc}"
                t0 = tc["groups"][0]["items"][0]
                assert "key" in t0 and "value" in t0 and "level" in t0, \
                    f"调优参数项缺字段：{t0}"

                # 正在输入（v0.101）：窗口只由 generating 决定。旧实现的 typing() 叠了个
                # +8 秒假窗口——回复已经发出来了气泡还挂着。WebChannel 不再覆写 typing()。
                assert st["typing"] is False, f"空闲时不该显示正在输入：{st['typing']}"
                ch.generating = True
                assert c.get(base + "/api/state").json()["typing"] is True, "生成中该显示正在输入"
                ch.generating = False
                assert c.get(base + "/api/state").json()["typing"] is False, \
                    "生成结束该立刻收起（不再有 +8 秒残影）"

                r = c.post(base + "/api/send", json={"text": "你好呀"}).json()
                assert r["echo"] is True, f"普通消息应回显用户输入：{r}"
                # 回显消息 id（实测 #25 双发修复）：前端拿它推进轮询游标，
                # 这条就不会被轮询再渲染一遍
                assert isinstance(r["id"], int) and r["id"] > 0, f"普通消息该带回显 id：{r}"
                after = c.get(base + f"/api/messages?after={r['id']}").json()
                assert not any(m["id"] == r["id"] for m in after["messages"]), \
                    "游标推到 id 之后还拉回同一条（双发没修干净）"
                r2 = c.post(base + "/api/send", json={"text": "/work"}).json()
                assert r2["echo"] is False and r2["reply"], f"命令输出该回给浏览器：{r2}"
                assert r2["id"] == 0, f"命令没有消息 id：{r2}"

                msgs = c.get(base + "/api/messages?after=0").json()["messages"]
                assert any(m["role"] == "user" and m["content"] == "你好呀" for m in msgs), \
                    f"消息没进库（网页端会看不到自己发的）：{msgs}"

                # 对话引用：带 quote 发送要落库（quote_id/role/content），供前端回显 + 后端对齐
                rq = c.post(base + "/api/send", json={
                    "text": "对，就这句", "quote": {"id": 7, "role": "assistant", "content": "被引的那句"}}).json()
                assert rq["echo"] is True, f"带引用的消息也应回显：{rq}"
                qmsgs = c.get(base + "/api/messages?after=0").json()["messages"]
                qm = next(m for m in qmsgs if m["content"] == "对，就这句")
                assert qm["quote_id"] == 7 and qm["quote_role"] == "assistant" \
                    and qm["quote_content"] == "被引的那句", f"引用没落库：{qm}"
                # 不带 quote 的普通消息，引用字段应是空
                plain = next(m for m in qmsgs if m["content"] == "你好呀")
                assert plain["quote_id"] in (0, "") and not plain["quote_content"], \
                    f"无引用消息不应带引用字段：{plain}"

                # 撤回（后端对齐）：删库 + 意识流注入一条同步响应（TA 下一轮知道"你撤回了"）
                uid = _db.add_message("user", "这条待撤回")
                aid = _db.add_message("assistant", "TA 的回复不能撤回")
                rv = c.post(base + "/api/revoke", json={"id": uid}).json()
                assert rv.get("ok") is True, f"撤回自己的消息该成功：{rv}"
                left = c.get(base + "/api/messages?after=0").json()["messages"]
                assert not any(m["content"] == "这条待撤回" for m in left), \
                    "撤回后消息还在库里（前端还会看到）"
                streams = _db.recent_stream(20)
                assert any(r["kind"] == "revoke" and "这条待撤回" in r["text"]
                           for r in streams), f"撤回没进意识流：{streams}"
                rv2 = c.post(base + "/api/revoke", json={"id": aid}).json()
                assert rv2.get("ok") is False, f"不该能撤回 TA 的消息：{rv2}"
                rv3 = c.post(base + "/api/revoke", json={"id": 999999}).json()
                assert rv3.get("ok") is False, f"撤回不存在的消息该失败：{rv3}"

                # 手动安全阀：页面上勾"紧急"要能开窗口
                c.post(base + "/api/send", json={"text": "在吗", "urgent": True})
                assert _guard.in_urgent_window(), "网页端勾了紧急却没开窗口"

                # 朋友列表：网页上的「换朋友」靠它渲染，编号必须能直接喂给 /friend
                fr = c.get(base + "/api/friends").json()
                assert "current" in fr and isinstance(fr["friends"], list), \
                    f"/api/friends 结构不对：{fr}"
                for f in fr["friends"]:
                    assert set(("idx", "name", "at", "file")) <= set(f), \
                        f"朋友项字段不全：{f}"
                assert [f["idx"] for f in fr["friends"]] == list(
                    range(1, len(fr["friends"]) + 1)), "编号必须从 1 连续"

                # 「最近记住的事」卡片（v0.60）：返回 recent + total，排除自我记忆
                mm = c.get(base + "/api/memories").json()
                assert "total" in mm and isinstance(mm["recent"], list), \
                    f"/api/memories 结构不对：{mm}"

                # 表情包池接口（v0.68）：分桶库存 + 维护（新增桶/表情、删表情）
                em = c.get(base + "/api/emoji").json()
                assert "buckets" in em and isinstance(em["buckets"], list) and em["buckets"], \
                    f"/api/emoji 结构不对：{em}"
                assert any(b["name"] == "开心" and b["builtin"] for b in em["buckets"]), \
                    "内置桶「开心」该在表情池里"
                r_eb = c.post(base + "/api/emoji", json={"action": "add_bucket", "name": "测试桶"}).json()
                assert r_eb.get("ok") is True, f"新增桶该成功：{r_eb}"
                r_ee = c.post(base + "/api/emoji", json={
                    "action": "add_emoji", "bucket": "测试桶", "text": "🌊", "emotion": "测试"}).json()
                assert r_ee.get("ok") is True, f"新增表情该成功：{r_ee}"
                em2 = c.get(base + "/api/emoji").json()
                tb = next(b for b in em2["buckets"] if b["name"] == "测试桶")
                assert any(it["text"] == "🌊" and not it["builtin"] for it in tb["items"]), \
                    "新增的表情该在桶里且标记非内置"
                r_rm = c.post(base + "/api/emoji", json={
                    "action": "remove_emoji", "bucket": "测试桶", "text": "🌊"}).json()
                assert r_rm.get("ok") is True, f"删自定义表情该成功：{r_rm}"
                r_rmb = c.post(base + "/api/emoji", json={
                    "action": "remove_bucket", "name": "测试桶"}).json()
                assert r_rmb.get("ok") is True, f"删自定义桶该成功：{r_rmb}"
                r_bad = c.post(base + "/api/emoji", json={
                    "action": "remove_bucket", "name": "开心"}).json()
                assert r_bad.get("ok") is False, "内置桶不该能删"

                # 页面上做成按钮的那些命令：任何一条挂了，点下去就是没反应
                for cmd in ("/work", "/diaries", "/notes", "/profile", "/guard on"):
                    r3 = c.post(base + "/api/send", json={"text": cmd}).json()
                    assert r3.get("reply") and "⚠" not in r3["reply"], \
                        f"按钮命令 {cmd} 没正常返回：{r3}"
                print("ok 网页端 Web UI（HUD 页面 + 消息/命令 API + 紧急通道）")
            finally:
                srv.shutdown()
        finally:
            _webui.WEB_DIR = real_web_dir


def test_llm_provider_reasoning():
    """模型面板供应商声明（v0.102）：provider 列表完整 + 思考强度按供应商翻译成
    对应字段（DeepSeek=effort、Kimi/OpenAI=reasoning_effort、Ollama/自定义=不传），
    档位对不上回退默认、DeepSeek 老模型跳过。"""
    import llm as _llm

    # 1) 供应商声明完整：每个都有 id/name/base_url，reasoning 字段名/档位合法
    ps = _llm.providers()
    assert len(ps) >= 9, f"供应商声明不该少于 9 个：{len(ps)}"
    for p in ps:
        assert p.get("id") and p.get("name") and "base_url" in p, f"provider 缺字段：{p}"
        r = p.get("reasoning")
        assert r is None or (r.get("field") in ("effort", "reasoning_effort")
                             and r.get("options") and r.get("default") in r["options"]), \
            f"reasoning 声明不合法：{p}"

    # 2) base_url 匹配供应商
    assert (_llm._find_provider("https://api.deepseek.com") or {})["id"] == "deepseek"
    assert (_llm._find_provider("https://api.moonshot.cn/v1") or {})["id"] == "moonshot"
    assert (_llm._find_provider("http://127.0.0.1:11434/v1") or {})["id"] == "ollama"
    assert _llm._find_provider("https://unknown.example.com/v1") is None

    # 3) _apply_reasoning：按供应商把思考强度塞进正确字段
    with _patch_config(DEEPSEEK_BASE_URL="https://api.deepseek.com",
                       LLM_REASONING_EFFORT="high", DEEPSEEK_MODEL="deepseek-flash"):
        pl = {"model": "deepseek-flash"}
        _llm._apply_reasoning(pl)
        assert pl.get("effort") == "high", f"DeepSeek V4 该传 effort=high：{pl}"
    with _patch_config(DEEPSEEK_BASE_URL="https://api.deepseek.com",
                       LLM_REASONING_EFFORT="high", DEEPSEEK_MODEL="deepseek-chat"):
        pl = {"model": "deepseek-chat"}
        _llm._apply_reasoning(pl)
        assert "effort" not in pl, f"DeepSeek 老模型不该传 effort：{pl}"
    with _patch_config(DEEPSEEK_BASE_URL="https://api.moonshot.cn/v1",
                       LLM_REASONING_EFFORT="high", DEEPSEEK_MODEL="kimi-k3"):
        pl = {"model": "kimi-k3"}
        _llm._apply_reasoning(pl)
        assert pl.get("reasoning_effort") == "high", f"Kimi 该传 reasoning_effort：{pl}"
    with _patch_config(DEEPSEEK_BASE_URL="https://api.openai.com/v1",
                       LLM_REASONING_EFFORT="medium", DEEPSEEK_MODEL="gpt-5"):
        pl = {"model": "gpt-5"}
        _llm._apply_reasoning(pl)
        assert pl.get("reasoning_effort") == "medium", f"OpenAI 该传 reasoning_effort：{pl}"
    # 档位对不上（DeepSeek 的 max 不在 OpenAI 档位里）→ 回退该家默认 medium
    with _patch_config(DEEPSEEK_BASE_URL="https://api.openai.com/v1",
                       LLM_REASONING_EFFORT="max", DEEPSEEK_MODEL="gpt-5"):
        pl = {"model": "gpt-5"}
        _llm._apply_reasoning(pl)
        assert pl.get("reasoning_effort") == "medium", f"档位对不上该回退默认 medium：{pl}"
    # 无思考强度声明（Ollama）→ 不传任何字段
    with _patch_config(DEEPSEEK_BASE_URL="http://127.0.0.1:11434/v1",
                       LLM_REASONING_EFFORT="high", DEEPSEEK_MODEL="llama3"):
        pl = {"model": "llama3"}
        _llm._apply_reasoning(pl)
        assert "effort" not in pl and "reasoning_effort" not in pl, \
            f"Ollama 无思考强度，不该传任何字段：{pl}"

    # 4) reasoning_options：校验/面板接口返回各家的档位
    assert _llm.reasoning_options("https://api.deepseek.com") == ["low", "high", "max"]
    assert _llm.reasoning_options("https://api.openai.com/v1") == ["minimal", "low", "medium", "high"]
    assert _llm.reasoning_options("http://127.0.0.1:11434/v1") is None

    print("ok 模型供应商声明（provider 列表 + 思考强度按供应商翻译 effort/reasoning_effort/不传 + 档位回退默认）")


def test_account_management():
    """账号管理（网页改密码/TOTP）：write_env 只动目标键、其他行原样保留；
    改密码热生效；TOTP 绑定后动态码可验、解绑后关闭。"""
    import os as _os
    import tempfile

    import auth as _auth

    tmp = Path(tempfile.mkdtemp())
    env_keys = ("WEB_AUTH_ENABLED", "WEB_AUTH_PASS_HASH", "WEB_AUTH_TOTP_SECRET",
                "WEB_AUTH_SESSION_SECRET")
    saved_env = {k: _os.environ.get(k) for k in env_keys}
    with _patch_config(BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
        try:
            # .env 里有别的键（比如 API key）——写回时绝不能碰丢
            (tmp / ".env").write_text(
                "API_KEY=sk-test-别动我\nWEB_AUTH_ENABLED=on\n", encoding="utf-8")
            _os.environ["WEB_AUTH_ENABLED"] = "on"
            _auth.change_password("旧密码12345")
            assert _auth.verify_password("旧密码12345"), "改密后旧密码该能过"
            assert "API_KEY=sk-test-别动我" in (tmp / ".env").read_text(encoding="utf-8"), \
                "write_env 把 .env 里其他行碰丢了"

            # 改密码热生效（不用重启）
            _auth.change_password("新密码67890")
            assert _auth.verify_password("新密码67890"), "新密码没热生效"
            assert not _auth.verify_password("旧密码12345"), "旧密码还能用"

            # 会话密钥轮换：新旧密钥签出来的 cookie 不同（旧会话失效）
            s1 = _auth.rotate_session_secret()
            s2 = _auth.rotate_session_secret()
            assert s1 and s2 and s1 != s2, "会话密钥没轮换出不同的值"

            # TOTP：生成 → 绑定 → 当前动态码可验
            secret = _auth.new_totp_secret()
            assert secret and "=" not in secret, "密钥该是干净的 base32"
            uri = _auth.otpauth_uri(secret)
            assert uri.startswith("otpauth://totp/") and f"secret={secret}" in uri, \
                f"otpauth 链接不对：{uri}"
            _auth.activate_totp(secret)
            assert _auth.totp_enabled(), "绑定后 TOTP 该生效"
            assert _auth.verify_totp(_auth.totp_code(secret)), "绑定密钥的当前码验不过"
            # 解绑
            _auth.disable_totp()
            assert not _auth.totp_enabled(), "解绑后 TOTP 该关闭"
            print("ok 账号管理（改密热生效 + .env 保留他行 + 会话轮换 + TOTP 绑定/解绑）")
        finally:
            for k, v in saved_env.items():
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v
            _auth._refresh()


def test_autostart():
    """开机自启与常驻（G 模块）。

    只测不碰系统的部分：装/卸/启/停会真的去注册计划任务、拉进程，
    那种副作用留给手工验证（python main.py autostart on 看一眼）。
    """
    import autostart as _as
    import daemon as _dm

    # 两边各自定义了守护 PID 的路径，一旦写歪就互相找不到对方
    assert Path(_as.DAEMON_PID) == Path(_dm.DAEMON_PID), \
        f"autostart 与 daemon 的 DAEMON_PID 不一致：{_as.DAEMON_PID} vs {_dm.DAEMON_PID}"

    # 解释器必须指向仓库里的虚拟环境，不能是随便哪个 python
    assert ".venv" in _as._python(), f"python 路径没走虚拟环境：{_as._python()}"
    assert Path(_as._pythonw()).exists(), f"pythonw 不存在：{_as._pythonw()}"

    # 启动文件夹那个 vbs：必须拉起 daemon.py，且路径带引号（路径有空格也不会散）
    vbs = _as._startup_vbs()
    assert "daemon.py" in vbs and "pythonw.exe" in vbs, f"启动器内容不对：{vbs}"
    assert '"""' in vbs, f"vbs 里路径没加引号，路径有空格会散：{vbs}"

    # status 不能崩，且三块信息都得在（装没装 / 网页端 / 守护）
    s = _as.status()
    for key in ("开机自启", "网页端进程", "守护进程"):
        assert key in s, f"status 少了 {key}：{s}"

    # cli 分发：未知参数当 status 处理，别默默什么都不做
    assert "开机自启" in _as.cli("")
    assert "开机自启" in _as.cli("乱写的参数")

    # 探活函数不能把活进程杀掉（Windows 上 os.kill(pid,0) 是 TerminateProcess！）
    assert _as._pid_alive(os.getpid()) is True, "连自己都判成死了"
    assert _as._pid_alive(999999) is False, "不存在的 PID 判成活着"


def test_inner_monologue():
    """双层生成：第 1 次调用出内心独白（不可见），第 2 次拿着独白说人话。
    验证三件事：①独白真的被调用且输出不进聊天；②第 2 层的 messages 里带着独白
    （立场在这一层定住）；③独白失败时**必须**退化成单层，不能把话说不出来。
    """
    import tempfile
    import time as _t
    import llm as _llm
    import core as _core
    import db as _db

    _tmp = Path(tempfile.mkdtemp())
    # 快档 + 关工作系统（硬性约定 #10）：fresh DB 在工作时段必然判"离线专注"，
    # 消息会进 inbox 不回话——测 Engine 交互必须关掉，否则独白层压根不会被触发。
    monologue_hits, second_layer_seen = [], []

    class Ch:  # 鸭子类型即可（Engine 只调 send/typing），不必继承 Channel 抽象基类
        def __init__(self):
            self.events = []
            self.errors = []

        def send(self, text, kind="chat"):
            self.events.append(("send", text))

        def typing(self):
            self.events.append(("typing",))

    def fake_chat(messages, temperature=0.8, max_tokens=0, **k):
        joined = "\n".join(str(m.get("content", "")) for m in messages)
        if "内心独白环节" in joined:          # 第 1 层：独白
            monologue_hits.append(messages)
            return ("【第一反应】他又来了。\n【真实看法】不同意，熬夜不是这么说的。\n"
                    "【情绪移动】0\n【能量】7\n【和旧立场冲突吗】无\n"
                    "【我最想说的】我就是不想睡，怎么了。\n【我自己的话题】没有\n"
                    "【说多少】一句话\n【要不要主动起话题】否")
        second_layer_seen.append(joined)
        return "我就是不想睡，怎么了。"

    real_chat = _llm.chat
    with _patch_config(_db_init=True, DB_PATH=_tmp / "friend.db",
                       INNER_MONOLOGUE_ENABLED=True, WORK_ENABLED=False, **_FAST_TIMING):
        try:
            ch = Ch()
            eng = _core.Engine(ch)
            eng._judge_session_end = lambda *a, **k: None
            import memory as _mem
            real_extract = _mem.extract_async
            _mem.extract_async = lambda *a, **k: None
            _llm.chat = fake_chat
            try:
                eng.on_user_message("你不该熬夜，早点睡")
                for _ in range(120):
                    if any(e[0] == "send" for e in ch.events):
                        break
                    _t.sleep(0.05)
            finally:
                _mem.extract_async = real_extract
                eng.stop()

            assert monologue_hits, "独白层没被调用"
            assert second_layer_seen, "第 2 层没被调用"
            # ①独白的内容不能出现在发给聊天对象的文字里
            sent = [e[1] for e in ch.events if e[0] == "send"]
            assert sent, f"没发出任何内容：{ch.events}"
            assert all("【真实看法】" not in s for s in sent), f"独白泄漏到聊天里：{sent}"
            # ②第 2 层必须能看到独白（否则立场没法传递）+ 有把立场"说出来"的指令
            joining = "\n".join(second_layer_seen)
            assert "我就是不想睡，怎么了。" in joining, \
                f"第 2 层没拿到独白内容：{joining[:200]}"
            for must in ("内心独白", "不许改变立场", "不许投降", "只输出你会说的"):
                assert must in joining, f"第 2 层缺装配铁律（{must}）：{joining[:300]}"

            # 独白层崩掉 → 必须退化成单层，话还得说出来
            ch2 = Ch()
            eng2 = _core.Engine(ch2)
            eng2._judge_session_end = lambda *a, **k: None
            _mem.extract_async = lambda *a, **k: None
            _llm.chat = lambda messages, temperature=0.8, max_tokens=0, **k: (
                _raise_llm() if "内心独白环节" in "\n".join(
                    str(m.get("content", "")) for m in messages) else "这是单层兜底回复")
            try:
                eng2.on_user_message("在吗")
                for _ in range(120):
                    if any(e[0] == "send" for e in ch2.events):
                        break
                    _t.sleep(0.05)
            finally:
                _mem.extract_async = real_extract
                eng2.stop()
            sent2 = [e[1] for e in ch2.events if e[0] == "send"]
            assert "这是单层兜底回复" in sent2, f"独白失败应退化成单层照常回复：{ch2.events}"
        finally:
            _llm.chat = real_chat
    print("ok 双层生成（独白不可见 + 立场传进第 2 层 + 独白失败退化单层）")


def test_session_context():
    """整段会话全量读（v0.80，实测 #42 #2）：起点=最后一次起床，起床前的截掉、
    之后的全部保留（治"对方说这里说过，TA 翻不了记录"）；上限 1000 只防刷 token。"""
    import tempfile
    from datetime import datetime as _dt
    import config as _cfg
    import db as _db
    import stream as _stream

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):

        def _msg(role, content, ts, kind="chat"):
            with _db._conn() as c:
                c.execute(
                    "INSERT INTO messages(ts, role, kind, content) VALUES(?,?,?,?)",
                    (ts, role, kind, content))

        # 昨天晚上的消息（隔夜，必须截掉）
        _msg("user", "昨晚那句", "2026-10-10T23:00:00")
        # 今天起床前的（早起留言，TA 没看到，也截掉）
        _msg("user", "起床前那句", "2026-10-11T07:30:00")
        _stream.append("wake", "我伸了懒腰，起床了", _dt(2026, 10, 11, 8, 0))
        # 起床之后的（全量保留，不管多少条）
        _msg("user", "起床后第1句", "2026-10-11T08:10:00")
        _msg("assistant", "我在呢", "2026-10-11T08:11:00")
        _msg("user", "这里说过", "2026-10-11T09:00:00")
        _msg("assistant", "我说过这句", "2026-10-11T09:01:00", kind="proactive")

        msgs = _db.session_context()
        texts = [m["content"] for m in msgs]
        assert "这里说过" in texts and "我说过这句" in texts, texts
        assert "昨晚那句" not in texts and "起床前那句" not in texts, texts
        assert len(msgs) == 4, texts
        # proactive 也算 TA 的发言，role 是 assistant
        assert next(m for m in msgs if m["content"] == "我说过这句")["role"] == "assistant"
        # 正序：越早的在前
        assert texts == ["起床后第1句", "我在呢", "这里说过", "我说过这句"], texts

        # 再睡一觉起来 → 会话断开，旧段被新 wake 截掉
        _stream.append("sleep", "我困了，去睡了", _dt(2026, 10, 11, 23, 30))
        _stream.append("wake", "又起床了", _dt(2026, 10, 12, 9, 0))
        _msg("user", "新会话第1句", "2026-10-12T09:05:00")
        msgs2 = _db.session_context()
        texts2 = [m["content"] for m in msgs2]
        assert texts2 == ["新会话第1句"], texts2

        # 没有任何 wake 记录 + 传了 day → 从生活日起点取
        with _db._conn() as c:
            c.execute("DELETE FROM consciousness_stream")
        msgs3 = _db.session_context(day="2026-10-12")
        texts3 = [m["content"] for m in msgs3]
        assert "新会话第1句" in texts3 and "这里说过" not in texts3, texts3

        # 上限：max_messages 生效（防恶意刷 token 的兜底）
        assert len(_db.session_context(max_messages=2)) == 2

        print("ok 整段会话全量读（起床切会话 + 隔夜截断 + proactive 算发言 + 正序）")


def _raise_llm():
    raise __import__("llm").LLMUnavailable("模拟独白层挂了")


def test_offline_inbox_stream_dedup():
    """离线留言（实测反馈）：离线期间意识流不逐条刷「留言了」，
    上线 flush 时记一次「XX给我留言了，我看看」再逐条补记「XX说：…」；
    睡话轻提示也只在离线段第一条发一声。"""
    import tempfile
    import db as _db
    import offline as _off
    import stream as _stream
    from core import Engine

    tmp = Path(tempfile.mkdtemp())
    with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db",
                       WORK_ENABLED=False):
        class Ch:
            def __init__(self): self.sends = []
            def send(self, text): self.sends.append(text)
            def typing(self): pass

        ch = Ch()
        eng = Engine(ch)
        try:
            _db.set_state("SLEEPING")              # 睡着 = 离线
            eng.on_user_message("哈喽，好久没聊了")
            eng.on_user_message("最近感觉好忙，不知道在忙啥")
            assert _off.pending_inbox_count() == 2, "留言没进队列"
            kinds = [(r["kind"], r["text"]) for r in _stream.recent(50)]
            assert not [t for k, t in kinds if "留言了" in t], f"离线期间不该记留言提示：{kinds}"
            assert not [t for k, t in kinds if k == "user"], f"留言该等上线再补记：{kinds}"
            assert len(ch.sends) == 1, f"睡话轻提示只该发一声：{ch.sends}"

            eng._reply_inbox_worker = lambda texts: None   # 只验意识流记账，不真回
            eng.flush_inbox()
            kinds = [(r["kind"], r["text"]) for r in _stream.recent(50)]
            notices = [t for k, t in kinds if k == "inbox" and "留言了" in t]
            says = [t for k, t in kinds if k == "user" and "说：" in t]
            assert len(notices) == 1, f"「留言了」应只记一次：{notices}"
            assert len(says) == 2, f"留言的消息行该补记 2 条：{says}"
        finally:
            eng.stop()
    print("ok 离线留言意识流（离线不刷屏，上线一次「留言了」+ 逐条补记）")
