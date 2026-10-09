"""P1 新功能回归测试：CLI 时间戳 + /refresh 热重载。
运行：python test_p1_features.py
"""
import builtins
import contextlib
import io
import os
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import commands
import config
import persona

TS_RE = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

# 时间模拟（阅读/打字/发送间隔）的测试用快档：压到毫秒级，别把测试拖慢
_FAST_TIMING = {
    "READING_SECONDS_PER_CHAR": 0.0, "READING_MAX_SECONDS": 5.0,
    "TYPING_SECONDS_PER_CHAR": 0.0, "TYPING_MIN_SECONDS": 0.0,
    "TYPING_MAX_SECONDS": 0.01,
    "SEND_SECONDS_PER_CHAR": 0.0, "SEND_MIN_SECONDS": 0.0, "SEND_MAX_SECONDS": 0.01,
}


def _set_timing(values: dict) -> dict:
    """批量改 config 计时参数，返回旧值（finally 里还原）。"""
    saved = {k: getattr(config, k) for k in values}
    for k, v in values.items():
        setattr(config, k, v)
    return saved


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


def test_stable_archive(tmp_path=None):
    """归档 V2：一个好朋友一份文件（覆盖更新）、别名不改文件名、归档日志。"""
    import tempfile
    import db as _db
    import persona_pool

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    try:
        _db.init()
        # 朋友甲：聊过 + 人格卡
        _db.set_kv("relationship_stage", "new")
        _db.add_message("user", "你好甲")
        persona_pool.save({"name": "甲", "label": "阳光话痨", "text": "x",
                           "wake_hour": 7, "sleep_hour": 23, "bubble_mult": 1.0,
                           "quirk": "q", "drawn_at": "2026-10-06T00:00:00"})
        name1 = commands._archive_current()
        assert name1 == "甲", f"归档返回名不对：{name1}"
        archives1 = commands._archives()
        assert len(archives1) == 1
        first_at = commands._archive_display(archives1[0])[1]
        # 再归档一次：仍然只有一份（覆盖，不产生新文件）
        name1b = commands._archive_current()
        archives1b = commands._archives()
        assert len(archives1b) == 1, "重复归档产生了第二份文件"
        assert archives1b[0] == archives1[0], "归档文件名变了（应该稳定）"
        assert commands._archive_display(archives1b[0])[1] >= first_at

        # 朋友乙：换库（模拟 /new-friend 清空重来）
        # archived_at 精度到秒，跨一秒保证两人归档时刻可区分（真实使用必然间隔）
        import time as _t
        _t.sleep(1.1)
        _db.clear_all()
        _db.set_kv("relationship_stage", "new")
        _db.add_message("user", "你好乙")
        persona_pool.save({"name": "乙", "label": "老干部", "text": "x",
                           "wake_hour": 6, "sleep_hour": 22, "bubble_mult": 1.0,
                           "quirk": "q", "drawn_at": "2026-10-06T01:00:00"})
        commands._archive_current()
        assert len(commands._archives()) == 2

        # 换回甲 → 再换回来：切换不产生新快照
        r = commands.handle("/friend 1")
        assert "甲" in r, f"/friend 切换反馈不对：{r}"
        assert len(commands._archives()) == 2
        commands.handle("/friend 2")
        commands.handle("/friend 1")
        assert len(commands._archives()) == 2, "来回切换产生了重复归档"

        # /alias 只改库内记录，文件名不动
        target_before = commands._archives()[0].name
        r = commands.handle("/alias 1 老铁")
        assert "老铁" in r
        assert commands._archives()[0].name == target_before, "/alias 改了文件名"
        assert commands._archive_display(commands._archives()[0])[0] == "老铁"

        # /friends 输出带最后归档时间
        out = commands.handle("/friends")
        assert "老铁" in out and "最后归档" in out, f"/friends 输出：{out}"

        # 归档日志
        log = (tmp / "archive.log").read_text(encoding="utf-8")
        assert "归档 甲" in log and "归档 乙" in log, f"archive.log：{log}"
        print("ok 归档 V2（稳定文件/覆盖更新/别名不改名/归档日志）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db


def test_game_mode():
    """娱乐模式：开局/结束检测、规则注入、游戏中暂停主动消息、/status 显示。"""
    import tempfile
    import db as _db
    import game
    import guard as _guard

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    try:
        _db.init()
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
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db


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

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    real_chat = _llm.chat
    real_extract = _memory.extract_async
    _memory.extract_async = lambda: None  # 测试不碰真实 LLM
    try:
        _db.init()
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
        config.BASE_DIR, config.DB_PATH = real_base, real_db


def test_memory_append_timeline():
    """追加式时间线记忆：同主题不合并全保留；原文相同才跳过；
    时间标签参与召回（相对词换算日期）；同组更新历史带出；/memories 分组展示。"""
    import tempfile
    from datetime import datetime
    import commands as _cmd
    import db as _db
    import memory as _mem

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    try:
        _db.init()
        # prompt 新规则：绝对日期 + 追加式，不再有合并规则
        assert "YYYY-MM-DD" in _mem.EXTRACT_PROMPT
        assert "一件事只留一条" not in _mem.EXTRACT_PROMPT
        # 红线代码化（2026-10-07 用户拍板）：确定性规则移出 prompt，只留一句兜底
        assert "时序铁律" not in _mem.EXTRACT_PROMPT
        assert "这条对方还没回应" in _mem.EXTRACT_PROMPT   # 结构化标注的说明保留
        # 身份清洗（2026-10-07 用户拍板）：prompt 全项目不出现 AI 身份——TA 就是抽到的角色本人
        assert "AI" not in _mem.EXTRACT_PROMPT
        import persona as _persona
        assert "AI" not in _persona.COMMON_RULES

        # _validate：推测/元信息/承诺安全网（丢弃）+ 自称改写（不丢弃）
        assert _mem._validate("可能明天要考试", False) is None                       # 推测
        assert _mem._validate("用户没有主动告知生日", False) is None                 # 元信息
        assert _mem._validate("小徐答应告诉我吃了什么", True) is None                # 尾部未回应→丢
        assert _mem._validate("小徐答应告诉我吃了什么", False) == "小徐答应告诉我吃了什么"
        assert _mem._validate("小徐答应告诉AI吃了什么", False) == "小徐答应告诉我吃了什么"  # 改写
        assert _mem._validate("小徐对小徐（AI）说「我喜欢你」", False) == "小徐对小徐说「我喜欢你」"  # 注释自称
        assert _mem._validate("小徐的AI项目稳步推进", False) == "小徐的AI项目稳步推进"      # AI话题合法
        assert _mem._validate("小徐觉得AI很可爱", False) == "小徐觉得AI很可爱"            # 评价AI技术=合法

        # _format_convo：用户最后发言之后的"我"的消息标注未回应
        rows = [{"role": "assistant", "content": "先去吃个午饭"},
                {"role": "user", "content": "嗯嗯"},
                {"role": "assistant", "content": "吃完跟我说吃了啥"}]
        convo, tail = _mem._format_convo(rows)
        assert "我（这条对方还没回应）：吃完跟我说吃了啥" in convo
        assert "我：先去吃个午饭" in convo
        assert tail is True
        convo2, tail2 = _mem._format_convo(rows[:2])
        assert "还没回应" not in convo2 and tail2 is False

        # 1) 同主题换说法 → 两条都保留（追加，不合并）
        _db.add_memory("小徐的AI项目正在稳步推进", "事件", theme="技术")
        _mem._apply({"content": "小徐的AI项目规模很大，每天只能推进一部分",
                     "category": "事件", "theme": "技术"})
        assert _db.memory_count() == 2, "同主题新进展应追加而非合并"
        # 2) 原文完全相同 → 跳过
        _mem._apply({"content": "小徐的AI项目正在稳步推进", "category": "事件", "theme": "技术"})
        assert _db.memory_count() == 2, "完全相同的原文不应重复记"

        # 3) 记忆量超过全量阈值后，时间标签参与召回：
        #    query 说"今天下午" 应命中今天日期+下午的标签
        today = datetime.now().date().isoformat()
        _db.add_memory("小徐计划先写图像作业再弄项目", "计划",
                       time_label=f"{today} 下午", theme="学业")
        _db.add_memory("小徐打算周日去爬山", "计划", theme="出行")
        _db.add_memory("小徐爬山改到周六了", "计划", theme="出行")
        for i in range(10):  # 填充无关记忆，超过全量返回阈值（10 条）
            _db.add_memory(f"无关记忆填充第{i}号关于天气", "事件", theme="其他")
        hits = _mem.recall("我今天下午该干嘛来着", top_k=5)
        assert any("图像作业" in m["content"] for m in hits), \
            f"时间标签未参与召回：{[m['content'] for m in hits]}"

        # 4) 同组更新历史：命中旧计划时带出同组更新的那条（改期）
        hits = _mem.recall("周日去爬山的计划", top_k=5)
        contents = [m["content"] for m in hits]
        assert any("改到周六" in c for c in contents), f"未带出同组更新：{contents}"

        # 5) /memories 分组展示：同 (category, theme) 归一组，组内旧→新；
        #    每条带存储时间（记于MM-DD HH:MM），与事发时间标签是两个维度
        out = _cmd.handle("/memories")
        assert "▸ 计划·出行" in out, f"缺分组标题：{out}"
        i_old, i_new = out.index("周日去爬山"), out.index("改到周六")
        assert i_old < i_new, "组内应按时间正序（新的在后面）"
        assert "记于" in out, f"缺存储时间：{out}"
        assert f"@{today} 下午" in out, f"缺事发时间标签：{out}"
        print("ok 追加式时间线记忆（追加/相同跳过/时间标签召回/同组带出/分组展示+双时间维度）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_memory_v2():
    """记忆系统 v2：历史回填（断点/历史时间锚定）+ 删除待确认 + status 列迁移。"""
    import json
    import sqlite3
    import tempfile
    import commands as _cmd
    import db as _db
    import memory as _mem

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    real_chat = _mem.llm.chat
    try:
        _db.init()
        # 0) status 列迁移：模拟旧库（删掉 status 列）后 init 应自动补回
        with sqlite3.connect(str(config.DB_PATH)) as c:
            c.execute("ALTER TABLE memories DROP COLUMN status")
        _db.init()
        with sqlite3.connect(str(config.DB_PATH)) as c:
            cols = [r[1] for r in c.execute("PRAGMA table_info(memories)")]
        assert "status" in cols, "旧库迁移未补 status 列"

        # 1) 历史回填：旧消息分批提取，参照时间用消息当天而非回填当天
        for i, (role, text) in enumerate([
                ("user", "我们今天下午聊聊SVD图像压缩吧"),
                ("assistant", "好啊，SVD 就是把矩阵分解成三个矩阵……"),
                ("user", "懂了！你讲得比老师清楚"),
                ("assistant", "哈哈那明天继续"),
        ]):
            _db.add_message(role, text)
        with sqlite3.connect(str(config.DB_PATH)) as c:  # 改成历史时间 10-03
            c.execute("UPDATE messages SET ts='2026-10-03T15:00:00'")
        calls = []

        def fake_chat(messages, temperature=0.7):
            calls.append(messages[-1]["content"])
            return json.dumps([{"content": "我给小徐详细讲过SVD图像压缩的原理",  # 新红线：第一人称
                                "category": "事件", "theme": "学业",
                                "time": "2026-10-03 下午"}], ensure_ascii=False)

        _mem.llm.chat = fake_chat
        added = _mem.backfill_history(progress=lambda *a: None)
        # 断言最终状态而非精确条数：早期测试可能残留后台线程晚触发 llm.chat（竞态），
        # 会抢先 apply 同一条记忆导致 added=0，但终态一致
        assert any("【现在时间】2026-10-03" in c for c in calls), \
            f"参照时间应为消息当天：{calls[:2]}"
        mems = _db.all_memories()
        svd = [m for m in mems if "SVD" in m["content"]]
        assert svd, f"回填未产生记忆：{[m['content'] for m in mems]}"
        assert svd[0]["time_label"] == "2026-10-03 下午", \
            f"时间标签未锚定历史日期：{svd[0]['time_label']}"
        # 断点：再跑一次不重复回填
        assert _mem.backfill_history(progress=lambda *a: None) == 0, "断点失效，重复回填"

        # 2) 删除待确认：AI 提议 → 标记不真删 → recall 排除 → keepmem 恢复 → delmem 真删
        _mem.llm.chat = real_chat  # 后续不再需要 LLM
        _mem._apply({"content": "小徐被AI称为技术宅", "category": "画像"})
        _mem._apply({"content": "技术宅", "delete": True})
        pend = _db.pending_delete_memories()
        assert len(pend) == 1 and "技术宅" in pend[0]["content"], "删除提议未标记"
        assert _db.memory_count() == 2, "提议删除不应真删"
        assert all("技术宅" not in m["content"] for m in _mem.recall("小徐 技术")), \
            "待删除记忆不应参与召回"
        out = _cmd.handle("/memories")
        assert "待你确认" in out and "技术宅" in out, f"/memories 缺待确认区：{out}"
        assert "留着" in _cmd.handle(f"/keepmem {pend[0]['id']}")
        assert not _db.pending_delete_memories(), "keepmem 未恢复"
        assert "已忘掉" in _cmd.handle(f"/delmem {pend[0]['id']}")
        assert all("技术宅" not in m["content"] for m in _db.all_memories()), "delmem 未真删"
        print("ok 记忆 v2（迁移/回填+历史时间锚定/断点/删除待确认全流程）")
    finally:
        _mem.llm.chat = real_chat
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_typing_indicator():
    """"正在输入"：回复按字数计算输入时长，先 typing() 再发正文。"""
    import tempfile
    import time as _t
    import db as _db
    import memory as _mem
    from core import Engine

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    saved = (config.TYPING_ENABLED, config.TYPING_SECONDS_PER_CHAR,
             config.TYPING_MIN_SECONDS, config.TYPING_MAX_SECONDS,
             config.READING_MAX_SECONDS, config.READING_SECONDS_PER_CHAR)
    config.TYPING_ENABLED = True
    config.TYPING_SECONDS_PER_CHAR = 0.001   # 把等待压到毫秒级
    config.TYPING_MIN_SECONDS = 0.01
    config.TYPING_MAX_SECONDS = 0.05
    config.READING_MAX_SECONDS = 0.05
    config.READING_SECONDS_PER_CHAR = 0.0
    try:
        _db.init()

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
        (config.TYPING_ENABLED, config.TYPING_SECONDS_PER_CHAR,
         config.TYPING_MIN_SECONDS, config.TYPING_MAX_SECONDS,
         config.READING_MAX_SECONDS, config.READING_SECONDS_PER_CHAR) = saved
        config.BASE_DIR, config.DB_PATH = real_base, real_db


def test_profile_autofill():
    """AI 自动维护 profile：写入/稳定字段冲突待确认/可变字段更新/手动字段不覆盖/证据留痕。"""
    import tempfile
    import commands as _cmd
    import db as _db
    import memory as _mem

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    try:
        _db.init()
        ev = "对方：我最近都熬夜到凌晨\n我：原来你是夜猫子"
        # 1) 提取结果带 profile 字段 → 写进清单并锁定 + 证据留痕
        _mem._apply({"content": "小徐是夜猫子作息", "category": "画像",
                     "profile": "作息习惯：夜猫子，凌晨才睡"}, evidence=ev)
        assert _db.get_kv("profile_locked") == "1", "自动填写应锁定清单"
        assert "作息习惯：夜猫子，凌晨才睡" in _db.get_kv("profile_text"), \
            f"清单没写进去：{_db.get_kv('profile_text')!r}"
        pe = _mem.profile_evidence()
        assert "作息习惯" in pe and pe["作息习惯"].get("long_id"), "应留证据映射"
        # A 库记忆挂了证据链接（long_id → B 库原文）
        mem = [m for m in _db.all_memories() if m["content"] == "小徐是夜猫子作息"][0]
        assert mem["long_id"] == pe["作息习惯"]["long_id"], "A 库应挂 B 库证据链接"
        # /profile 展示证据标记；/profile why 取当时对话原文
        out = _cmd.handle("/profile")
        assert "证据#" in out, f"/profile 缺证据标记：{out}"
        why = _cmd.handle("/profile why 作息习惯")
        assert "夜猫子" in why, f"/profile why 缺证据原文：{why}"

        # 2) 稳定字段（作息习惯）遇到冲突新值 → 不静默覆盖，标 pending 待确认
        _mem._apply({"content": "小徐最近开始早睡了", "category": "画像",
                     "profile": "作息习惯：最近在调整，十一点前就睡"},
                    evidence="对方：我昨晚十一点就睡了")
        assert "凌晨才睡" in _db.get_kv("profile_text"), \
            f"稳定字段不应被静默覆盖：{_db.get_kv('profile_text')!r}"
        assert "十一点前就睡" not in _db.get_kv("profile_text"), "冲突值不应写入"
        pending = _mem.profile_pending()
        assert "作息习惯" in pending and "十一点前就睡" in pending["作息习惯"]["value"], \
            f"冲突应标 pending：{pending}"

        # 2b) 用户接受 pending → 覆盖旧值并转为正式证据
        assert _mem.accept_pending_profile("作息习惯") == "最近在调整，十一点前就睡"
        assert "十一点前就睡" in _db.get_kv("profile_text") \
            and "凌晨才睡" not in _db.get_kv("profile_text")
        assert "作息习惯" not in _mem.profile_pending(), "accept 后应清掉 pending"

        # 2c) 用户否决 pending：保留旧值，丢弃新说法
        _mem._apply({"content": "小徐说他要早睡", "category": "画像",
                     "profile": "作息习惯：每天九点睡"}, evidence="对方：我以后九点睡")
        assert _mem.reject_pending_profile("作息习惯") == "每天九点睡"
        assert "每天九点睡" not in _db.get_kv("profile_text"), "reject 后不应写入新值"

        # 3) 可变字段（在忙的事）→ 可直接更新（自然变动）
        _mem._apply({"content": "小徐在写图像作业", "category": "画像",
                     "profile": "在忙的事：写图像作业"})
        _mem._apply({"content": "小徐开始做项目了", "category": "画像",
                     "profile": "在忙的事：做AI朋友项目"})
        assert "做AI朋友项目" in _db.get_kv("profile_text") \
            and "写图像作业" not in _db.get_kv("profile_text"), \
            f"可变字段未更新：{_db.get_kv('profile_text')!r}"

        # 4) 用户手动加的字段 → AI 不覆盖
        _db.set_kv("profile_text", _db.get_kv("profile_text") + "\n称呼：小徐")
        _mem._apply({"content": "小徐让大家叫他徐哥", "category": "画像",
                     "profile": "称呼：徐哥"})
        assert "称呼：小徐" in _db.get_kv("profile_text"), \
            f"手动字段被覆盖：{_db.get_kv('profile_text')!r}"

        # 5) 无"字段：值"结构的 profile 不写入
        _mem._apply_profile("没有结构的一整句话")
        assert "没有结构" not in _db.get_kv("profile_text")
        # 6) 记忆本身照常入库（profile 是附加动作）
        assert any(m["content"] == "小徐是夜猫子作息" for m in _db.all_memories())
        print("ok AI 自动维护 profile（写入/稳定冲突待确认/可变更新/不覆盖手动/证据留痕）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_interrupt_detection():
    """插话检测：打字阶段打断→整段回复作废；连发中途打断→停发+让步语。"""
    import tempfile
    import time as _t
    import db as _db
    import memory as _mem
    import persona as _persona
    from core import Engine

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    saved = (config.TYPING_ENABLED, config.TYPING_SECONDS_PER_CHAR,
             config.TYPING_MIN_SECONDS, config.TYPING_MAX_SECONDS,
             config.READING_MAX_SECONDS, config.READING_SECONDS_PER_CHAR,
             config.SEND_MIN_SECONDS, config.SEND_MAX_SECONDS,
             config.SEND_SECONDS_PER_CHAR)
    config.TYPING_ENABLED = True
    config.READING_MAX_SECONDS = 0.05
    config.READING_SECONDS_PER_CHAR = 0.0
    try:
        _db.init()

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
        assert any(t in _persona.INTERRUPT_ACKS for t in sent2), \
            f"缺让步语（{ _persona.INTERRUPT_ACKS }）：{sent2}"
        print("ok 插话检测（打字打断作废 + 中途停发让步）")
    finally:
        eng.stop()
        eng2.stop()
        _mem.llm.chat = real_chat
        _mem.extract_async = real_extract
        (config.TYPING_ENABLED, config.TYPING_SECONDS_PER_CHAR,
         config.TYPING_MIN_SECONDS, config.TYPING_MAX_SECONDS,
         config.READING_MAX_SECONDS, config.READING_SECONDS_PER_CHAR,
         config.SEND_MIN_SECONDS, config.SEND_MAX_SECONDS,
         config.SEND_SECONDS_PER_CHAR) = saved
        config.BASE_DIR, config.DB_PATH = real_base, real_db


def test_db_path_lock():
    """PATH_LOCK 互斥：在途提取持锁期间切库被阻塞；放行后记忆落旧库不串新库。"""
    import tempfile
    import threading
    import time as _t
    import sqlite3
    import db as _db
    import memory as _mem

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    path_a, path_b = tmp / "a.db", tmp / "b.db"
    config.BASE_DIR = tmp
    try:
        config.DB_PATH = path_a
        _db.init()
        _db.add_message("user", "我明天早上要去图书馆自习")
        _db.add_message("assistant", "好啊，几点去？")

        gate = threading.Event()
        switched = threading.Event()
        real_chat = _mem.llm.chat

        def slow_chat(messages, temperature=0.7):
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
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db


def test_corpus_pick():
    """固定语料库：语料从 corpus 统一管理、persona 别名兼容、pick 不连续重复。"""
    import corpus
    import persona as _persona
    # 别名兼容：旧引用（core/测试）拿到的就是 corpus 里的那份
    assert _persona.SLEEPY_REPLIES is corpus.SLEEPY_REPLIES
    assert _persona.INTERRUPT_ACKS is corpus.INTERRUPT_ACKS
    # 语料丰富度：睡觉/插话/冒泡都比原来的一小把多
    assert len(corpus.SLEEPY_REPLIES) >= 10
    assert len(corpus.INTERRUPT_ACKS) >= 6
    assert len(corpus.BUBBLE_LINES) >= 10
    assert len(corpus.LONG_TEXT_NOTES) >= 5
    # pick 不连续重复（人味儿 = 不重样）
    picks = [corpus.pick(corpus.BUBBLE_LINES, "test_no_repeat") for _ in range(30)]
    assert all(a != b for a, b in zip(picks, picks[1:])), \
        f"出现连续重复：{picks}"
    # 空列表/单条兜底
    assert corpus.pick([], "empty") == ""
    assert corpus.pick(["唯一"], "single") == "唯一"
    print("ok 固定语料库（统一管理/别名兼容/丰富度/不连续重复）")


def test_bubble_fixed_corpus():
    """冒泡固定语料：BUBBLE_FIXED_PROB=1 时冒泡直接抽语料，不调 LLM（省 token）。"""
    import tempfile
    import corpus
    import db as _db
    import llm as _llm
    from core import Engine

    class DummyChannel:
        def __init__(self):
            self.sent = []
        def send(self, text):
            self.sent.append(text)

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    saved_prob = config.BUBBLE_FIXED_PROB
    config.BUBBLE_FIXED_PROB = 1.0
    real_chat = _llm.chat
    try:
        _db.init()
        ch = DummyChannel()
        engine = Engine(ch)

        def boom(*a, **k):
            raise AssertionError("固定语料冒泡不应调 LLM")

        _llm.chat = boom
        assert engine._proactive("bubble") is True
        assert ch.sent and ch.sent[0] in corpus.BUBBLE_LINES, \
            f"冒泡没走固定语料：{ch.sent}"
        print("ok 冒泡固定语料（不走 LLM，省 token）")
    finally:
        engine.stop()  # 停掉后台回复线程
        _llm.chat = real_chat
        config.BUBBLE_FIXED_PROB = saved_prob
        config.BASE_DIR, config.DB_PATH = real_base, real_db


def test_diary_daily_review():
    """P2 日记系统：每日回顾复盘 + 写日记 + 补记/修正记忆 + 幂等 + 命令。"""
    import tempfile
    import db as _db
    import diary as _diary

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    try:
        _db.init()
        # 让 add_message 的时间戳落在"生活日"上：否则跨午夜（00:00~WAKE_HOUR）时
        # add_message 按墙钟写进"明天"，而 daily_review 按生活日读昨天 → 读到空，
        # 测试在深夜会误挂（"今天还没怎么聊"）。
        from datetime import datetime as _dt
        day = _diary._life_day_str()

        class _Clock:
            @classmethod
            def now(cls):
                return _dt.fromisoformat(day + "T12:00:00")

        real_dt = _db.datetime
        _db.datetime = _Clock
        try:
            _db.add_message("user", "今天好累啊")
            _db.add_message("assistant", "辛苦啦，早点休息")
        finally:
            _db.datetime = real_dt
        _db.add_memory("小徐在准备考研", "事件")

        real_chat = _diary.llm.chat
        _diary.llm.chat = lambda *a, **k: (
            '{"reflection": "今天有点没接住你的累",'
            '"diary": "今天小徐说好累，我让他早点休息。有点心疼。",'
            '"mood": "心疼",'
            '"memory_add": ["小徐今天很累"],'
            '"memory_delete": ["考研"]}')
        try:
            out = _diary.daily_review(force=True)
            assert "日记写好啦" in out, f"返回：{out}"
            entry = _db.get_diary(day)
            assert entry and "心疼" in entry["content"], f"日记没落库：{entry}"
            assert "没接住" in entry["reflection"], f"复盘没落库：{entry}"
            assert entry["mood"] == "心疼"
            assert _db.find_memory_by_keyword("小徐今天很累"), "memory_add 没补记"
            pend = _db.pending_delete_memories()
            assert any("考研" in m["content"] for m in pend), "memory_delete 没标记待确认"
        finally:
            _diary.llm.chat = real_chat

        # 幂等：第二次不重写（也不该再调 LLM）
        _diary.llm.chat = lambda *a, **k: "第二次不该调用"
        out2 = _diary.daily_review()
        assert "已经写过" in out2, f"应该跳过：{out2}"
        _diary.llm.chat = real_chat

        # /diary /diaries 命令
        assert "心疼" in commands.handle("/diary"), "/diary 没显示日记"
        assert day in commands.handle("/diaries"), "/diaries 没列出日期"
        print("ok 日记系统（复盘/写日记/补记/提议删/幂等/命令）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_diary_purge_reviewed_messages():
    """回顾线彻底删除：PURGE_REVIEWED_MESSAGES=on 时复盘后清掉当日原始对话。"""
    import tempfile
    import db as _db
    import diary as _diary

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    real_purge = config.PURGE_REVIEWED_MESSAGES
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    config.PURGE_REVIEWED_MESSAGES = True
    try:
        _db.init()
        # 同 test_diary_daily_review：让消息时间戳落在"生活日"，跨午夜不误挂
        from datetime import datetime as _dt
        day = _diary._life_day_str()

        class _Clock:
            @classmethod
            def now(cls):
                return _dt.fromisoformat(day + "T12:00:00")

        real_dt = _db.datetime
        _db.datetime = _Clock
        try:
            _db.add_message("user", "今天聊点什么")
            _db.add_message("assistant", "随便聊呀")
        finally:
            _db.datetime = real_dt
        assert len(_db.today_messages(day)) == 2
        _diary.llm.chat = lambda *a, **k: '{"diary": "聊了聊", "memory_add": [], "memory_delete": []}'
        _diary.daily_review(force=True)
        assert len(_db.today_messages(day)) == 0, "回顾线没删干净"
        assert _db.get_diary(_diary._life_day_str()) is not None, "日记应保留"
        print("ok 回顾线彻底删除（PURGE_REVIEWED_MESSAGES=on）")
    finally:
        config.BASE_DIR, config.DB_PATH, config.PURGE_REVIEWED_MESSAGES = \
            real_base, real_db, real_purge
        _db.init()


def test_corpus_consolidation():
    """语料统一收进 corpus.py：固定回复 + 匹配关键词/正则都在，业务模块不再各自硬编码。"""
    import corpus
    import game as _game
    import core as _core
    import guard as _guard
    import memory as _mem

    # 固定回复语料
    assert "拜拜" in corpus.ENDING_SIGNALS and "晚安" in corpus.ENDING_SIGNALS
    assert "不玩了" in corpus.GAME_STOP_WORDS
    assert "朋友啦" in corpus.AUTO_FAMILIAR_NOTE
    assert corpus.LLM_STUB
    assert corpus.LLM_ERROR.format("xx") == "（脑子卡住了：xx）"
    assert corpus.INTRO_FALLBACK.format("阿燃") == "你好啊，我叫阿燃，我们认识一下吧！"

    # 匹配关键词/正则（信号词 + 意图词 + 提取红线）都在 corpus，且业务模块不再各自硬编码
    assert "在忙" in corpus.BUSY_SIGNALS
    assert "是谁" in corpus.IDENTITY_QUERY
    assert "可能" in corpus.SPECULATION
    assert "我不记得" in corpus.SELF_AMNESIA
    assert not hasattr(_core, "ENDING_SIGNALS"), "core 还留着 ENDING_SIGNALS"
    assert not hasattr(_game, "STOP_WORDS"), "game 还留着 STOP_WORDS"
    assert not hasattr(_guard, "BUSY_SIGNALS"), "guard 还留着 BUSY_SIGNALS"
    # memory 的红线/意图正则编译自 corpus 词表（源词表不在 memory 硬编码）
    assert _mem._SPECULATION.pattern == corpus.SPECULATION
    assert _mem._SELF_AMNESIA.pattern == corpus.SELF_AMNESIA
    assert _mem._IDENTITY_QUERY.pattern == corpus.IDENTITY_QUERY
    assert _mem._MEMORY_SEARCH_QUERY.pattern == corpus.MEMORY_SEARCH_QUERY
    # 工具 description/返回句这类"不会扩充"的文案不属语料，回到 memory 就近维护
    assert hasattr(_mem, "_TOOL_SEARCH_DESC"), "工具 description 应就近在 memory 维护"
    assert not hasattr(corpus, "TOOL_NO_QUERY"), "工具返回句不该进 corpus 语料"

    # 识别仍工作（走 corpus）
    assert _game.detect_stop("不玩了") is True
    assert _game.detect_stop("今天天气不错") is False
    assert any(s in "我在忙" for s in corpus.BUSY_SIGNALS)
    assert _mem.wants_memory_search("你还记得小葵吗") is True
    print("ok 语料统一收进 corpus（固定回复 + 匹配关键词/正则，业务模块不再硬编码）")


def test_identity_recall():
    """身份询问（"记得XX吗/XX是谁"）时，人际/介绍类记忆应优先于琐事。"""
    import tempfile
    import db as _db
    import memory as _mem

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    try:
        _db.init()
        _db.add_memory("小徐拉了一个群，把初遇朋友和小葵拉进来互相认识",
                       "人际", person="小葵、初遇朋友")
        for i in range(12):
            _db.add_memory(f"小葵琐事{i}号：拿快递晾衣服", "事件", person="小葵")

        hits = _mem.recall("你还记得小葵吗", top_k=3)
        contents = [m["content"] for m in hits]
        assert any("拉进来互相认识" in c for c in contents), \
            f"身份记忆没进 top：{contents}"
        assert "拉进来互相认识" in "".join(contents[:2]), \
            f"身份记忆未排在琐事前面：{contents}"
        print("ok 身份询问召回（人际/介绍类优先于琐事）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_long_memory():
    """A/B 两级库：长文存 B 库（全文+摘要）、A 库挂 long_id 伪链接；追问细节时展开。"""
    import tempfile
    import db as _db
    import memory as _mem
    import context as _ctx

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    try:
        _db.init()

        # 1) 长文：_apply 分流——摘要进 A 库、全文进 B 库、long_id 关联
        full_text = "小徐从头讲了奇异值分解怎么压缩图像，先把图切成 8x8 小块，再对每块做分解，" * 8
        ok = _mem._apply({"content": "小徐给我详细讲了SVD图像压缩的完整思路",
                          "category": "事件", "long": True, "full": full_text,
                          "theme": "技术"}, assistant_tail=False)
        assert ok, "长文落库失败"
        assert _db.memory_count() == 1, "A 库应有 1 条摘要记忆"
        assert _db.long_memory_count() == 1, "B 库应有 1 条长文"
        m = _db.all_memories(limit=1)[0]
        assert m["long_id"] > 0, f"A 库记忆应带 long_id 伪链接：{m}"
        long = _db.get_long_memory(m["long_id"])
        assert long and long["content"] == full_text, "B 库全文应与原文一致"

        # 2) 短记忆照旧：无 long_id
        _db.add_memory("小徐爱吃辣", "偏好")
        short = [x for x in _db.all_memories(limit=5) if x["content"] == "小徐爱吃辣"][0]
        assert short["long_id"] == 0, "短记忆不应挂 long_id"

        # 3) 召回默认不展开（只给摘要，不附全文）
        hits = _mem.recall("SVD图像压缩", top_k=3)
        hit = [h for h in hits if h.get("long_id")][0]
        assert "long_content" not in hit, "默认召回不该展开全文"

        # 4) 追问细节时展开全文
        hits2 = _mem.recall("你详细讲讲SVD那事", top_k=3, expand_long=True)
        hit2 = [h for h in hits2 if h.get("long_id")][0]
        assert "long_content" in hit2, "追问细节应展开全文"
        assert hit2["long_content"] == full_text, "展开的全文应与原文一致"

        # 5) expand_long_memory 接口
        assert _mem.expand_long_memory(m) == full_text
        assert _mem.expand_long_memory(short) is None, "短记忆展开应返回 None"

        # 6) 渲染：_memory_line 带长文标记；build_chat_messages 追问细节注入全文
        line = _ctx._memory_line(m)
        assert "有长文" in line, f"长记忆缺展开提示：{line}"
        _db.add_message("user", "你详细讲讲SVD那事")
        msgs = _ctx.build_chat_messages("AWAKE", ["你详细讲讲SVD那事"])
        joined = "\n".join(x["content"] for x in msgs)
        assert "详情全文" in joined, "追问细节时上下文应含长文全文"
        assert "先把图切成" in joined, "全文应真正注入上下文"
        print("ok A/B 两级库（长文落库 + 伪链接 + 追问展开 + 渲染标记）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_memory_tool_calling():
    """回忆/追问信号下 AI 主动调记忆检索工具（function calling），普通聊天走快速路径。"""
    import tempfile
    import db as _db
    import llm as _llm
    import memory as _mem
    from core import Engine

    class DummyChannel:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    real_ct = _llm.chat_with_tools
    real_chat = _llm.chat
    saved_enabled = config.MEMORY_TOOLS_ENABLED
    try:
        config.MEMORY_TOOLS_ENABLED = True  # 本测试显式开启工具调用
        _db.init()
        _db.add_memory("小徐拉了一个群，把初遇朋友和小葵拉进来互相认识", "人际", person="小葵")
        ch = DummyChannel()
        engine = Engine(ch)

        # 1) 信号判断：含"记得"→ 启用工具；普通闲聊 → 不启用
        assert _mem.wants_memory_search("你还记得小葵吗") is True
        assert _mem.wants_memory_search("今天天气不错") is False

        # 2) 回忆信号下，_generate_reply_text 走 tool loop（mock chat_with_tools）
        calls = []

        def fake_ct(messages, tools, temperature=0.8):
            if not any(m.get("role") == "tool" for m in messages):
                calls.append("tool_round")
                return {"content": "", "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "search_memories",
                                  "arguments": '{"query": "小葵"}'}}
                ]}
            calls.append("final_round")
            return {"content": "哦对，小葵就是群里拉进来认识的那位", "tool_calls": []}

        _llm.chat_with_tools = fake_ct
        reply = engine._generate_reply_text(
            [{"role": "system", "content": "人设"},
             {"role": "user", "content": "你还记得小葵吗"}],
            ["你还记得小葵吗"])
        assert "小葵" in reply and "拉进来" in reply, f"工具结果没被 AI 采用：{reply}"
        assert calls == ["tool_round", "final_round"], f"工具循环轮次不对：{calls}"

        # 3) 普通聊天（无回忆信号）→ 走 llm.chat，不碰 chat_with_tools
        _llm.chat = lambda *a, **k: "普通回复"
        _llm.chat_with_tools = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("普通聊天不该调工具"))
        assert engine._generate_reply_text([{"role": "user", "content": "在吗"}],
                                           ["在吗"]) == "普通回复"

        # 4) 开关关闭 → 即使有回忆信号也走 chat
        config.MEMORY_TOOLS_ENABLED = False
        _llm.chat = lambda *a, **k: "普通回复2"
        assert engine._generate_reply_text([{"role": "user", "content": "你记得吗"}],
                                           ["你记得吗"]) == "普通回复2"
        config.MEMORY_TOOLS_ENABLED = True

        print("ok 记忆工具调用（回忆信号分流 + tool loop 回填 + 普通聊天快速路径）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        config.MEMORY_TOOLS_ENABLED = saved_enabled
        _llm.chat_with_tools = real_ct
        _llm.chat = real_chat
        _db.init()


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


def test_cost_blame_redline():
    """H 模块反愧疚红线：代价只呈现不归因。词表在 corpus，检测在 persona，prompt 有约束。"""
    import corpus
    import persona as _persona

    # 红线词表必须是语料（会扩充），不能散在业务模块
    assert hasattr(corpus, "COST_BLAME") and corpus.COST_BLAME
    assert _persona.has_cost_blame("你打断我，我刚想说的全忘了")
    assert _persona.has_cost_blame("都是因为你，我今天什么都没干成")
    assert _persona.has_cost_blame("又被你打断了")
    # 只说自己的状态（呈现不归因）→ 不违规
    assert not _persona.has_cost_blame("我今天读得晚了，有点困")
    assert not _persona.has_cost_blame("刚在忙，回你晚了")
    assert not _persona.has_cost_blame("没事，我正好也想歇会儿")
    # 人设 prompt 里必须有这条约束（否则模型不知道）
    assert "不归因" in _persona.COMMON_RULES and "你打断我" in _persona.COMMON_RULES
    print("ok 反愧疚措辞红线（归因命中 / 呈现不命中 / 人设有约束）")


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
    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        assert "来有回" in _persona.core_identity_prompt(), \
            "延续引导没进 system prompt"
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()
    print("ok 对话延续引导（追问/延伸 + 分寸约束 + 进 system prompt）")


def test_busy_signals_not_merged():
    """回归：BUSY_SIGNALS 曾漏一个逗号，Python 隐式拼接把"不要说""安静"粘成一个词。"""
    import corpus
    import guard as _guard
    import tempfile
    import db as _db

    for word in ("不要说", "别说", "安静", "在忙", "开会"):
        assert word in corpus.BUSY_SIGNALS, f"忙碌信号丢了：{word}"
    assert not any(len(w) > 6 for w in corpus.BUSY_SIGNALS), \
        f"有信号词被粘成一坨：{corpus.BUSY_SIGNALS}"

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    try:
        _db.init()
        assert _guard.check_busy_signal("安静") is True, "'安静'没被识别成忙碌信号"
        assert _guard.check_busy_signal("不要说了") is True
        assert _db.get_kv("quiet_until"), "忙碌信号没触发静默期"
        _db.set_kv("quiet_until", "")
        assert _guard.check_busy_signal("今天天气不错") is False
        assert not _db.get_kv("quiet_until"), "普通消息不该进静默期"
        print("ok 忙碌信号词表完整（不粘词 + 安静/不要说各自生效 + 普通消息不误伤）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_random_chat_hours_follow_persona():
    """待确认 #8 结论：闲聊时段按人格作息自适应，全局固定会与人格作息打架。"""
    import scheduler as _sched
    from apscheduler.triggers.cron import CronTrigger

    # 同一天内的作息
    assert _sched._awake_hours(7, 23) == "7-22"
    assert _sched._awake_hours(6, 22) == "6-21"
    assert _sched._awake_hours(8, 8) == "*"
    # 跨午夜（夜猫子 10 点起、凌晨 2 点睡）必须拆成两段，否则 cron 拿到倒序区间
    assert _sched._awake_hours(10, 2) == "10-23,0-1"
    # 0 点睡 = 24 点，不是"睡 0 小时"（中二人格 9 点起、0 点睡）
    assert _sched._awake_hours(9, 0) == "9-23"

    # 生成的表达式 APScheduler 真能解析（跨午夜两段也能）
    for hours in ("7-22", "10-23,0-1", "9-23"):
        assert CronTrigger(hour=hours, minute=0) is not None
    print("ok 闲聊时段按人格作息自适应（跨午夜拆段 + cron 可解析）")


def test_work_system():
    """S3 工作系统：排班跟人格作息、惰性结算、心流期、下班、跨天重排、素材不重复取。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import config as _cfg
    import db as _db
    import persona as _persona
    import work as _work

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    saved_share = config.WORK_SHAREABLE_ENABLED
    config.WORK_SHAREABLE_ENABLED = False      # 素材要调 LLM，这里只测机制本身
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        # 夜猫子人格：10 点起、凌晨 2 点睡（作息必须跟着人格走）
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "drawn_at": "2026-10-09T00:00:00"}))

        morning = _dt(2026, 10, 9, 10, 0)
        units = _work.plan(morning)
        assert units, "今天没排出班"
        first_start = _dt.fromisoformat(units[0]["start"])
        assert first_start.hour >= 10, f"夜猫子 10 点才起，排班却从 {first_start} 开始"
        assert _dt.fromisoformat(units[-1]["end"]) <= _dt(2026, 10, 10, 2, 0), \
            f"排班越过了睡觉时间：{units[-1]['end']}"
        # 同一天幂等：再读一次不能换计划（否则它手头的事会一直乱跳）
        assert _work.plan(morning) == units, "同一天重排出了新计划"

        # 心流期 = 单元前 WORK_FLOW_RATIO 比例（这段打断代价最高）
        flow = _work.status(first_start + _td(minutes=2))
        assert flow["phase"] == "flow" and flow["flow"], f"单元刚开工应是心流期：{flow}"
        working = _work.status(first_start + _td(minutes=30))
        assert working["phase"] == "working" and not working["flow"], f"过了心流期：{working}"

        # HUD 与 prompt 注入（要趁它还在忙的时候测：状态是持久的，
        # 一旦结算推进到"下班"，再把时间调回去也回不到心流期）
        assert "刚进入状态" in _work.hud(first_start + _td(minutes=2))
        assert _work.prompt_hint(first_start + _td(minutes=2)), "忙的时候没注入手头的事"
        # 红线：工作状态注入自己不能踩「代价归因」（它只说自己的状态）
        for hint in _work.PHASE_HINTS.values():
            assert not _persona.has_cost_blame(hint.format(done=1, quota=6)), \
                f"工作提示踩了反愧疚红线：{hint}"

        # 单元结束 → 自动结算（精力降、余额涨、进度 +1）
        done1 = _work.status(first_start + _td(minutes=60))
        assert done1["done"] == 1, f"单元结束了却没结算：{done1}"
        assert done1["money"] == _cfg.WORK_PAY_PER_UNIT
        assert done1["energy"] == _cfg.WORK_ENERGY_MAX - _cfg.WORK_ENERGY_COST

        # 休眠/关机的关键回归：错过好几个单元，睡醒回来要能一次性补上
        late = _work.status(_dt(2026, 10, 10, 1, 0))   # 夜猫子的"今天深夜"
        assert late["done"] == len(units), f"错过的单元没补账：{late}"
        assert late["phase"] == "off", f"活都干完了还在忙：{late}"

        # 新的一天（睡醒）→ 重新排班、进度归零
        _work._state(_dt(2026, 10, 10, 12, 0))
        st = _work._load()
        assert st["day"] == "2026-10-10" and st["settled"] == 0, f"跨天没重排：{st}"

        # 素材池：取过的不再取（一条素材只分享一次）
        _db.set_kv("work_shareables", json.dumps(
            [{"day": "2026-10-09", "seq": 1, "text": "刚发现楼下那只猫会自己开门", "used": 0}]))
        assert _work.take_shareable() == "刚发现楼下那只猫会自己开门"
        assert _work.take_shareable() == "", "同一条素材被取了两次"
        print("ok 工作系统（排班跟人格作息 + 心流期 + 惰性补账 + 跨天重排 + 素材不重复）")
    finally:
        config.WORK_SHAREABLE_ENABLED = saved_share
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_offline_inbox_and_rhythm():
    """S3 休眠完整版：离线判定（睡/工作专注=离线）+ 离线留言 inbox + 作息自主浮动与幂等。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import config as _cfg
    import corpus as _corpus
    import db as _db
    import offline as _off
    import work as _work

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        # 老干部人格：6 点起、22 点睡（作息锚点明确，好断言浮动范围）
        _db.set_kv("persona_json", json.dumps({
            "archetype": "oldcadre", "label": "老干部", "name": "稳稳", "quirk": "",
            "text": "你作息规律。", "wake_hour": 6, "sleep_hour": 22,
            "bubble_mult": 0.8, "drawn_at": "2026-10-09T00:00:00"}))

        morning = _dt(2026, 10, 9, 6, 0)
        _work.plan(morning)
        first_start = _dt.fromisoformat(_work.plan(morning)[0]["start"])
        # 工作单元进行中（flow / working）= 离线专注；开工前 = 在线
        assert _off.is_offline(first_start + _td(minutes=2)), "心流期应算离线专注"
        assert _off.is_offline(first_start + _td(minutes=30)), "working 应算离线专注"
        assert not _off.is_offline(first_start - _td(minutes=5)), "开工前应在线"

        # 睡觉 → 离线；reason 区分"睡"和"专注"
        _db.set_state("SLEEPING")
        assert _off.is_offline(), "睡觉应离线"
        assert _off.offline_reason() == "sleep"
        _db.set_state("AWAKE")

        # 离线留言 inbox：攒 → 取 → 清空
        assert _off.push_inbox("在吗") == 1
        assert _off.push_inbox("明天一起吃饭") == 2
        assert _off.pending_inbox_count() == 2
        items = _off.take_inbox()
        assert [m["text"] for m in items] == ["在吗", "明天一起吃饭"]
        assert _off.pending_inbox_count() == 0, "取走留言后应清空"

        # 作息自主：入睡/起床围绕人格锚点浮动，同一生活日幂等
        evening = _dt(2026, 10, 9, 20, 0)
        bed_anchor = _dt(2026, 10, 9, 22, 0)
        sleep_at = _off.tonight_sleep_at(evening)
        lo = bed_anchor - _td(minutes=_cfg.REST_SLEEP_EARLY_MAX)      # 早睡最多提前
        hi = bed_anchor + _td(minutes=_cfg.REST_SLEEP_LATE_MAX)       # 熬夜最多延后
        assert lo <= sleep_at <= hi, f"入睡浮动越界：{sleep_at}"
        assert _off.tonight_sleep_at(evening) == sleep_at, "同一晚入睡时间应幂等"

        wake_base = _dt(2026, 10, 10, 6, 0)
        wake_at = _off.next_wake_at(evening)
        wlo = wake_base - _td(minutes=_cfg.REST_WAKE_EARLY_MAX)       # 早醒最多提前
        whi = wake_base + _td(minutes=_cfg.REST_WAKE_LIE_IN_MAX * 2)  # 熬夜可叠加赖床
        assert wlo <= wake_at <= whi, f"起床浮动越界：{wake_at}"
        assert _off.next_wake_at(evening) == wake_at, "同一晚起床时间应幂等"

        # 语料：工作专注时的第一人称轻提示存在
        assert len(_corpus.OFFLINE_FOCUS_NOTICES) >= 3
        print("ok 离线留言 + 作息自主（离线判定/inbox 攒取/浮动与幂等）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_reading_system():
    """S3 工作=读书：素材库非空/分类齐全、阅读顺序偏好优先、进度持久化、一轮循环、金句可摘。"""
    import json
    import tempfile

    import config as _cfg
    import db as _db
    import persona as _persona
    import reading as _reading

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        # 夜猫子人格：爱读科幻
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "reading_pref": "科幻",
            "drawn_at": "2026-10-09T00:00:00"}))

        # 素材库：非空、六大类齐全、每条都有正文 + 金句（摘抄）可摘
        assert len(_reading.LIBRARY) >= 18, "素材库太薄"
        genres = sorted({a["genre"] for a in _reading.LIBRARY})
        assert genres == ["历史", "散文", "杂文", "热血", "生活随笔", "科幻"], genres
        for a in _reading.LIBRARY:
            assert a.get("quote") and a.get("text"), f"素材缺字段：{a['title']}"

        # 人格卡行为参数带阅读偏好（persona 卡已扩字段）
        assert _persona.behavior_params()["reading_pref"] == "科幻", \
            f"阅读偏好没跟人格走：{_persona.behavior_params()}"

        # 阅读顺序：偏好分类（科幻）排最前
        order = _reading._order()
        first_genres = [_reading.LIBRARY[i]["genre"] for i in order[:4]]
        assert first_genres == ["科幻"] * 4, f"偏好分类没排前面：{first_genres}"

        # 顺序推进 + 进度持久化
        a1 = _reading.next_article()
        assert a1["genre"] == "科幻", f"第一篇应是科幻：{a1}"
        assert _reading._load()["pos"] == 1, "进度没推进"

        # 一轮读完再循环：把进度推到最后一篇，再取一篇应回到第一篇
        p = _reading._load()
        p["pos"] = len(_reading.LIBRARY) - 1
        _reading._save(p)
        _reading.next_article()          # 读完最后一篇
        loop = _reading.next_article()   # 循环回第一篇
        assert loop["title"] == _reading.LIBRARY[order[0]]["title"], \
            f"读满一轮后应循环回第一篇：{loop['title']}"
        print("ok 阅读素材库（分类齐全/偏好优先/进度持久化/一轮循环/金句可摘）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_user_library():
    """S4 用户书库：本地 txt（含 gbk）/ epub 解析、偏好选书、段级进度、结构与内置同构。"""
    import json
    import tempfile
    import zipfile

    import config as _cfg
    import db as _db
    import library as _lib

    def _make_epub(path, title, author, chapters):
        container = ('<?xml version="1.0" encoding="UTF-8"?>'
                     '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                     '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                     'media-type="application/oebps-package+xml"/></rootfiles></container>')
        manifest = "".join(f'<item id="c{i}" href="chapter{i}.xhtml" '
                           f'media-type="application/xhtml+xml"/>' for i in range(len(chapters)))
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
        opf = (f'<?xml version="1.0" encoding="UTF-8"?>'
               f'<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
               f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
               f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator></metadata>'
               f'<manifest>{manifest}</manifest><spine>{spine}</spine></package>')
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", container)
            z.writestr("OEBPS/content.opf", opf)
            for i, ch in enumerate(chapters):
                z.writestr(f"OEBPS/chapter{i}.xhtml",
                           f"<html><body><p>{ch}</p></body></html>")

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    real_books = config.BOOKS_DIR
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        books_dir = tmp / "books"
        (books_dir / "科幻").mkdir(parents=True)
        (books_dir / "散文").mkdir(parents=True)
        # txt（gbk 编码，模拟中文书常见编码）
        (books_dir / "科幻" / "三体.txt").write_bytes(
            "给时光以生命，给岁月以文明。\n\n给岁月以文明，而不是给文明以岁月。\n\n"
            "弱小和无知不是生存的障碍，傲慢才是。".encode("gbk"))
        # epub（带书名/作者元数据）
        _make_epub(books_dir / "散文" / "背影.epub", "背影", "朱自清",
                   ["我与父亲不相见已二年余了。", "我最不能忘记的是他的背影。"])
        (books_dir / "README.txt").write_text("说明文件，不该被当成书", encoding="utf-8")
        config.BOOKS_DIR = books_dir

        # 夜猫子人格：爱读科幻
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "q",
            "text": "夜猫子", "wake_hour": 10, "sleep_hour": 2, "bubble_mult": 1.2,
            "reading_pref": "科幻", "drawn_at": "2026-10-10T00:00:00"}, ensure_ascii=False))

        items = _lib.books()
        assert len(items) == 2, f"应解析出 2 本书：{items}"
        assert {b["title"] for b in items} == {"三体", "背影"}, items

        # 偏好选书：科幻（三体）优先读；返回结构与 reading.next_article 同构
        first = _lib.next_passage()
        assert first["genre"] == "科幻" and first["title"] == "三体", first
        assert set(("genre", "title", "source", "quote", "text")) <= set(first), \
            f"返回结构要对齐内置书摘：{first.keys()}"
        assert first["quote"] == "给时光以生命，给岁月以文明。", first["quote"]

        # 段级进度：三体 3 段读完后换散文（背影）
        _lib.next_passage()
        _lib.next_passage()
        fourth = _lib.next_passage()
        assert fourth["title"] == "背影" and fourth["source"] == "朱自清", fourth

        # 英文句号摘抄（用户可能导入英文书），小数点不误切
        assert _lib._pick_quote("This is a good sentence. Next one here.") == \
            "This is a good sentence.", _lib._pick_quote("This is a good sentence. Next one here.")
        assert "3.14" in _lib._pick_quote("圆周率是 3.14 左右的常数。这是一句完整的话。")

        # 一次读 n 段：进度按 n 推进（读 2 段 → 进度 idx=2）
        _lib._save_progress("", 0)   # 空 book_id → 从偏好排序的第一本（科幻·三体）开始
        art = _lib.next_passage(2)
        assert _lib.progress()["idx"] == 2, f"读 2 段后进度应为 2：{_lib.progress()}"
        assert art["title"] == "三体" and art["quote"], f"读 2 段应返回三体摘抄：{art}"

        # 书架空 → None（work 层回退内置书摘）
        config.BOOKS_DIR = tmp / "empty"
        assert _lib.next_passage() is None, "空书架应返回 None"

        print("ok 用户书库（txt/gbk + epub 解析 + 偏好选书 + 段级进度 + 结构与内置同构）")
    finally:
        config.BOOKS_DIR = real_books
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_legacy_persona_card_compat():
    """旧人格卡（缺 quirk/reading_pref/archetype）补全：/persona 不崩、阅读标签能显示。"""
    import json
    import tempfile

    import db as _db
    import persona_pool as _pool

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        # 旧卡：只有 name/label/text/作息/bubble_mult/drawn_at，缺 quirk/reading_pref/archetype
        _db.set_kv("persona_json", json.dumps({
            "name": "小葵", "label": "阳光话痨", "text": "话痨",
            "wake_hour": 8, "sleep_hour": 23, "bubble_mult": 1.0,
            "drawn_at": "2026-10-06",
        }, ensure_ascii=False))

        card = _pool.load()
        assert card["quirk"] == "", "旧卡 quirk 应补空"
        assert card["archetype"] == "sunshine", "旧卡 archetype 应按 label 反推"
        assert card["reading_pref"] == "生活随笔", "旧卡 reading_pref 应按 label 反推"

        # /persona 不崩、带阅读偏好、空怪癖有兜底
        out = commands.handle("/persona")
        assert "阅读偏好" in out and "生活随笔" in out, f"/persona 缺阅读偏好：{out}"
        assert "没特别怪癖" in out, f"旧卡空怪癖应兜底：{out}"

        # 阅读偏好标签能显示（爱读X）
        import webui as _webui
        assert _webui._persona_tags(card) == ["阳光话痨", "爱读生活随笔"], \
            f"旧卡补全后标签不对：{_webui._persona_tags(card)}"

        # 新卡（已含 reading_pref）不被 normalize 覆盖
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "q",
            "text": "夜猫子", "wake_hour": 10, "sleep_hour": 2, "bubble_mult": 1.2,
            "reading_pref": "科幻", "drawn_at": "2026-10-10T00:00:00",
        }, ensure_ascii=False))
        assert _pool.load()["reading_pref"] == "科幻", "新卡 reading_pref 不应被覆盖"

        print("ok 旧人格卡兼容（缺字段补全 / /persona 不崩 / 阅读标签显示 / 新卡不覆盖）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_care_events():
    """S4 事件记忆与关怀触发：承诺/约定提取 → 到点提醒（醒着发/睡着醒来补/过期错过）。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import db as _db
    import events as _ev
    import memory as _mem

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()

        # 未来才存 / 幂等
        assert _ev.add_care("提醒睡觉", "2020-01-01 12:00") is None, "过期的不该存"
        when = _ev.add_care("提醒睡觉", (_dt.now() + _td(hours=1)).strftime("%Y-%m-%d %H:%M"))
        assert when, "未来事件该存"
        assert _ev.add_care("提醒睡觉", when) is None, "同 what+when 应幂等"
        assert len(_ev.pending_cares()) == 1

        # 醒着到点 → care_remind 触发（missed=False），标记 done
        calls = []
        engine = types.SimpleNamespace(
            care_remind=lambda ev, missed=False: (calls.append((ev, missed)) or True))
        _db.set_kv("care_events", json.dumps([{
            "what": "提醒睡觉",
            "when": (_dt.now() - _td(minutes=5)).strftime("%Y-%m-%d %H:%M"),
            "kind": "提醒", "status": "pending",
            "created_at": _dt.now().isoformat(timespec="seconds"),
        }], ensure_ascii=False))
        _ev.tick(engine)
        assert len(calls) == 1 and calls[0][1] is False, "醒着到点该提醒"
        assert all(c["status"] == "done" for c in _ev.cares()), "提醒后应 done"

        # 睡着 → tick 跳过；醒来 flush_missed 补发（missed=True）
        calls.clear()
        _db.set_kv("care_events", json.dumps([{
            "what": "提醒睡觉",
            "when": (_dt.now() - _td(minutes=5)).strftime("%Y-%m-%d %H:%M"),
            "kind": "提醒", "status": "pending",
            "created_at": _dt.now().isoformat(timespec="seconds"),
        }], ensure_ascii=False))
        _db.set_state("SLEEPING")
        _ev.tick(engine)
        assert not calls, "睡着不该 tick 提醒"
        _db.set_state("AWAKE")
        _ev.flush_missed(engine)
        assert len(calls) == 1 and calls[0][1] is True, "醒来该补发错过"
        assert all(c["status"] == "done" for c in _ev.cares())

        # 过期超 24h → 标记 missed，不再提
        calls.clear()
        _db.set_kv("care_events", json.dumps([{
            "what": "提醒睡觉",
            "when": (_dt.now() - _td(hours=25)).strftime("%Y-%m-%d %H:%M"),
            "kind": "提醒", "status": "pending",
            "created_at": _dt.now().isoformat(timespec="seconds"),
        }], ensure_ascii=False))
        _ev.flush_missed(engine)
        assert not calls, "过期超24h不该再提醒"
        assert all(c["status"] == "missed" for c in _ev.cares())

        # memory 提取带 care 字段 → 落一条"我答应过"记忆 + 落一条关怀事件
        _db.set_kv("care_events", "[]")
        future = (_dt.now() + _td(hours=2)).strftime("%Y-%m-%d %H:%M")
        ok = _mem._apply({
            "content": "计划：提醒对方睡觉", "category": "计划",
            "time": future[:10], "theme": "健康作息",
            "care": {"what": "提醒对方睡觉", "when": future, "kind": "提醒"},
        })
        assert ok, "带 care 的记忆该落库"
        assert any("答应过" in m["content"] for m in _db.all_memories()), \
            "承诺应落一条'我答应过'计划记忆"
        assert len(_ev.pending_cares()) == 1, "care 应落一条关怀事件"

        print("ok 关怀事件（承诺提取/幂等/醒着提醒/睡着醒来补/过期错过）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_interruption_cost():
    """S3 招牌：打断成本 = R(phase 阻力) × (1+F(精力疲劳)) → welcomed/tolerated/costly。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import config as _cfg
    import cost as _cost
    import db as _db
    import persona as _persona
    import work as _work

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    saved_share = config.WORK_SHAREABLE_ENABLED
    config.WORK_SHAREABLE_ENABLED = False      # 素材要调 LLM，这里只测机制本身
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "drawn_at": "2026-10-09T00:00:00"}))

        morning = _dt(2026, 10, 9, 10, 0)
        units = _work.plan(morning)
        first_start = _dt.fromisoformat(units[0]["start"])

        # 阻力系数映射：心流最高、收工为零
        assert _cost.resistance("flow") == 1.0
        assert _cost.resistance("working") == 0.7
        assert _cost.resistance("off") == 0.0

        # 疲劳系数：精力满 = 0，精力空 = 1
        assert _cost.fatigue(100) == 0.0
        assert _cost.fatigue(0) == 1.0

        # 心流期（刚进入状态）恒为 costly——打断代价最高（精力满时 costIndex=1.0）
        flow = _cost.state(first_start + _td(minutes=2))
        assert flow["level"] == "costly" and flow["cost_index"] == 1.0, flow

        # 忙自己的事（精力还满）→ tolerated
        working = _cost.state(first_start + _td(minutes=30))
        assert working["level"] == "tolerated", working

        # 忙完了 → welcomed（完全欢迎，无论精力）
        off = _cost.state(_dt(2026, 10, 10, 1, 0))   # 夜猫子深夜，活都干完
        assert off["level"] == "welcomed" and off["cost_index"] == 0.0, off

        # 精力耗尽时，working 也应升到 costly（累 + 忙 = 双重代价）
        _db.set_kv("work_state", json.dumps({
            "day": "2026-10-09", "quota": 6,
            "units": [{"start": first_start.isoformat(timespec="seconds"),
                       "end": (first_start + _td(minutes=50)).isoformat(timespec="seconds")}],
            "settled": 0, "energy": 20, "money": 0}))
        tired = _cost.state(first_start + _td(minutes=30))
        assert tired["level"] == "costly", tired

        # 语态提示：三态都非空、且不踩反愧疚红线（只说自己的状态）
        for lv in ("welcomed", "tolerated", "costly"):
            assert _cost.LEVEL_HINTS[lv], lv
            assert not _persona.has_cost_blame(_cost.LEVEL_HINTS[lv]), \
                f"打断成本语态踩了反愧疚红线：{_cost.LEVEL_HINTS[lv]}"
        # HUD 徽章文案齐全
        for lv in ("welcomed", "tolerated", "costly", "sleep"):
            assert _cost.LEVEL_BADGE[lv], lv

        print("ok 打断成本（R×(1+F) 三态 + 语态不踩红线 + 徽章文案）")
    finally:
        config.WORK_SHAREABLE_ENABLED = saved_share
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


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

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
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
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_mood():
    """情绪状态（10-09 解冻）：心情由它自己的生活推，不围绕任何人转，语态不踩红线。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import config as _cfg
    import db as _db
    import mood as _mood
    import persona as _persona
    import work as _work

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    saved_share = config.WORK_SHAREABLE_ENABLED
    saved_mood = config.MOOD_ENABLED
    config.WORK_SHAREABLE_ENABLED = False      # 素材要调 LLM，这里只测心情本身
    config.MOOD_ENABLED = True
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "drawn_at": "2026-10-09T00:00:00"}))

        morning = _dt(2026, 10, 9, 10, 0)
        units = _work.plan(morning)
        first_start = _dt.fromisoformat(units[0]["start"])

        # 工作状态 → 心情（心流=投入积极 / 忙=专注中性）
        assert _mood.mood(first_start + _td(minutes=2)) == \
            {"label": "投入", "tone": "positive"}, "心流期应是投入"
        assert _mood.mood(first_start + _td(minutes=30)) == \
            {"label": "专注", "tone": "neutral"}, "忙自己的事应是专注"

        # 还没开工（idle）→ 慵懒
        idle = _mood.mood(_dt(2026, 10, 9, 10, 0))
        assert idle == {"label": "慵懒", "tone": "neutral"}, idle

        # 精力低 → 疲惫（累是最强烈的感受，盖过 phase）
        _db.set_kv("work_state", json.dumps({
            "day": "2026-10-09", "quota": 1,
            "units": [{"start": first_start.isoformat(timespec="seconds"),
                       "end": (first_start + _td(minutes=50)).isoformat(timespec="seconds")}],
            "settled": 0, "energy": 20, "money": 0}))
        tired = _mood.mood(first_start + _td(minutes=30))
        assert tired == {"label": "疲惫", "tone": "tired"}, tired

        # 忙完（off）白天 → 轻松；夜里 → 平静（时段情绪，独立于 phase）
        _db.set_kv("work_state", json.dumps({
            "day": "2026-10-09", "quota": 1,
            "units": [{"start": first_start.isoformat(timespec="seconds"),
                       "end": (first_start + _td(minutes=50)).isoformat(timespec="seconds")}],
            "settled": 1, "energy": 100, "money": 20}))
        assert _mood.mood(_dt(2026, 10, 9, 14, 0)) == {"label": "轻松", "tone": "positive"}
        assert _mood.mood(_dt(2026, 10, 9, 22, 0)) == {"label": "平静", "tone": "neutral"}

        # 语态提示：全部非空、且不踩反愧疚红线（只说自己状态）
        for label in ("投入", "专注", "慵懒", "放松", "轻松", "疲惫", "平静"):
            hint = _mood.MOOD_HINTS[label]
            assert hint, f"缺心情语态：{label}"
            assert not _persona.has_cost_blame(hint), \
                f"心情语态踩了反愧疚红线：{hint}"

        # 关掉开关 → 空心情、不注入语态
        config.MOOD_ENABLED = False
        assert _mood.mood(first_start) == {"label": "", "tone": ""}
        assert _mood.prompt_hint(first_start) == ""

        print("ok 情绪状态（心情由生活推 + 时段独立 + 精力疲惫 + 语态不踩红线）")
    finally:
        config.MOOD_ENABLED = saved_mood
        config.WORK_SHAREABLE_ENABLED = saved_share
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_webui():
    """G 模块网页端（自用阶段主载体）：HUD 页面 + 消息/命令 API + 紧急通道。"""
    import tempfile
    import threading
    from http.server import ThreadingHTTPServer

    import commands as _cmds
    import db as _db
    import guard as _guard
    import httpx
    import webui as _webui

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    real_web_dir = _webui.WEB_DIR
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        _webui.WEB_DIR = Path(real_base) / "web"     # 页面文件仍留在项目里

        def fake_engine(text):
            """替身引擎：命令返回输出，普通消息入库（真引擎会异步生成回复）。"""
            if text.startswith("/"):
                return _cmds.handle(text)
            _db.add_message("user", text)
            return None

        ch = _webui.WebChannel()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _webui._make_handler(ch, fake_engine))
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            # ⚠️ 沙箱里有 HTTP_PROXY，127.0.0.1 会被代理劫持，必须 trust_env=False
            c = httpx.Client(trust_env=False, timeout=10)
            base = f"http://127.0.0.1:{port}"

            html = c.get(base + "/").text
            assert 'class="hud"' in html and "barE" in html and "barP" in html, \
                "页面缺 HUD 结构（状态条/精力条/进度条）"

            st = c.get(base + "/api/state").json()
            assert st["name"] and st["clock"], f"状态快照缺基本信息：{st}"
            assert st["work"]["phase"] in _webui.work.PHASE_LABELS, f"phase 不合法：{st}"

            r = c.post(base + "/api/send", json={"text": "你好呀"}).json()
            assert r["echo"] is True, f"普通消息应回显用户输入：{r}"
            r2 = c.post(base + "/api/send", json={"text": "/work"}).json()
            assert r2["echo"] is False and r2["reply"], f"命令输出该回给浏览器：{r2}"

            msgs = c.get(base + "/api/messages?after=0").json()["messages"]
            assert any(m["role"] == "user" and m["content"] == "你好呀" for m in msgs), \
                f"消息没进库（网页端会看不到自己发的）：{msgs}"

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
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


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


if __name__ == "__main__":
    test_cli_timestamps()
    test_refresh_reloads_config_and_modules()
    test_refresh_reschedules_jobs()
    test_startup_pick_friend()
    test_stable_archive()
    test_game_mode()
    test_session_end_and_outbox()
    test_memory_append_timeline()
    test_memory_v2()
    test_typing_indicator()
    test_profile_autofill()
    test_interrupt_detection()
    test_db_path_lock()
    test_corpus_pick()
    test_corpus_consolidation()
    test_bubble_fixed_corpus()
    test_identity_recall()
    test_long_memory()
    test_memory_tool_calling()
    test_diary_daily_review()
    test_diary_purge_reviewed_messages()
    test_no_groupchat_leftovers()
    test_cost_blame_redline()
    test_conversation_continuation()
    test_busy_signals_not_merged()
    test_random_chat_hours_follow_persona()
    test_work_system()
    test_offline_inbox_and_rhythm()
    test_reading_system()
    test_user_library()
    test_legacy_persona_card_compat()
    test_care_events()
    test_interruption_cost()
    test_urgent_channel()
    test_mood()
    test_webui()
    test_autostart()
    print("\n全部通过")
