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

# 群聊时间模拟（阅读/打字/接话间隔）的测试用快档：压到毫秒级，别把测试拖慢
_FAST_TIMING = {
    "READING_SECONDS_PER_CHAR": 0.0, "READING_MAX_SECONDS": 5.0,
    "TYPING_SECONDS_PER_CHAR": 0.0, "TYPING_MIN_SECONDS": 0.0,
    "TYPING_MAX_SECONDS": 0.01,
    "SEND_SECONDS_PER_CHAR": 0.0, "SEND_MIN_SECONDS": 0.0, "SEND_MAX_SECONDS": 0.01,
    "GROUP_SPEAKER_GAP_MIN_SECONDS": 0.0, "GROUP_SPEAKER_GAP_MAX_SECONDS": 0.01,
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
        check_nudge=lambda: None)
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
        assert "这条用户还没回应" in _mem.EXTRACT_PROMPT   # 结构化标注的说明保留
        # 身份清洗（2026-10-07 用户拍板）：prompt 全项目不出现 AI 身份——TA 就是抽到的角色本人
        assert "AI" not in _mem.EXTRACT_PROMPT
        import persona as _persona
        import groupchat as _gc2
        assert "AI" not in _persona.COMMON_RULES
        assert "AI朋友" not in _gc2.GROUP_RULES

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
        assert "我（这条用户还没回应）：吃完跟我说吃了啥" in convo
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


def test_groupchat():
    """朋友联动群聊：拉群/名字显示/轮次上限防循环/散群写回+各自提取/群聊暂停冒泡。"""
    import tempfile
    import commands as _cmd
    import db as _db
    import groupchat as _gc
    import guard as _guard

    class FakeChannel:
        def __init__(self):
            self.sent = []  # (sender, text)

        def send(self, text, sender=""):
            self.sent.append((sender, text))

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    real_chat = _gc.llm.chat
    saved_timing = _set_timing(_FAST_TIMING)
    try:
        # 当前朋友小葵 + 归档老朋友稳稳
        _db.init()
        _db.set_kv("persona_json", '{"name": "小葵", "label": "温柔治愈", '
                                   '"text": "温柔", "wake_hour": 8, "sleep_hour": 23, '
                                   '"bubble_mult": 1.0, "drawn_at": "2026-10-06"}')
        config.DB_PATH = tmp / "friend-abc12345.db"
        _db.init()
        _db.set_kv("persona_json", '{"name": "稳稳", "label": "毒舌卷王", '
                                   '"text": "毒舌", "wake_hour": 7, "sleep_hour": 24, '
                                   '"bubble_mult": 1.0, "drawn_at": "2026-10-05"}')
        _db.set_kv("alias", "稳稳")
        _db.set_kv("archived_at", "2026-10-05T23:00:00")
        config.DB_PATH = tmp / "friend.db"

        # 拉群：当前朋友 + 归档 [1]
        out = _cmd.handle("/group 1")
        assert "小葵" in out and "稳稳" in out, f"拉群失败：{out}"
        assert _gc.is_active()
        # 群聊期间主动消息暂停
        ok, reason = _guard.can_send_proactive()
        assert not ok and reason == "群聊中", f"群聊中冒泡未拦截：{reason}"

        # 群聊对话：mock LLM——小葵的话里点名稳稳（提及接力）
        def fake_chat(messages, temperature=0.7):
            sys_msg = messages[0]["content"]
            if "你是小葵" in sys_msg:
                return "小徐你来啦\n稳稳你也说两句呗"
            return "哟，都在呢"

        _gc.llm.chat = fake_chat
        # 强制串话不停，验证 MAX_AI_TURNS 硬顶
        real_cross, real_stop = _gc.CROSS_TALK_PROB, _gc.STOP_TALK_PROB
        _gc.CROSS_TALK_PROB, _gc.STOP_TALK_PROB = 1.0, 0.0
        ch = FakeChannel()
        _gc.on_user_text("大家好呀", ch)
        _gc.CROSS_TALK_PROB, _gc.STOP_TALK_PROB = real_cross, real_stop

        ai_msgs = [x for x in ch.sent if x[0]]
        assert all(s in ("小葵", "稳稳") for s, _ in ai_msgs), f"发言人名字不对：{ai_msgs[:3]}"
        s = _gc.current()
        # transcript 一条 = 一次发言（send 会按换行拆段，不能拿消息数当轮次）
        ai_turns = sum(1 for sp, _ in s.transcript if sp != "小徐")
        assert ai_turns == _gc.MAX_AI_TURNS, f"轮次硬顶失效：{ai_turns}"
        assert s.transcript[0] == ("小徐", "大家好呀")
        # 小葵第一段没点名，第二段点名稳稳——都在 transcript 里
        assert any(sp == "小葵" and "稳稳" in tx for sp, tx in s.transcript)

        # 散群：记录写回两位各自的库 + 各自提取记忆
        _gc.llm.chat = lambda messages, temperature=0.7: "[]"  # 提取阶段不新记
        out = _cmd.handle("/endgroup")
        assert "散啦" in out
        assert not _gc.is_active()
        for f, name in ((tmp / "friend.db", "小葵"), (tmp / "friend-abc12345.db", "稳稳")):
            config.DB_PATH = f
            _db.init()
            with __import__("sqlite3").connect(str(f)) as c:
                n = c.execute("SELECT COUNT(*) FROM messages WHERE kind='group'").fetchone()[0]
            assert n == len(s.transcript), f"{name} 库里群聊记录不全：{n}"
        config.DB_PATH = tmp / "friend.db"
        # 散群后冒泡恢复
        ok, _ = _guard.can_send_proactive()
        assert ok, "散群后冒泡未恢复"
        print("ok 群聊（拉群/名字/轮次上限/写回各自库/暂停恢复冒泡）")
    finally:
        _set_timing(saved_timing)
        _gc.llm.chat = real_chat
        _gc._session = None
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
            def send(self, text, sender=""):
                self.events.append(("send", text))
            def typing(self, sender=""):
                self.events.append(("typing",))

        ch = Ch()
        eng = Engine(ch)
        eng._judge_session_end = lambda *a, **k: None
        real_chat = _mem.llm.chat
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
        kinds = [e[0] for e in ch.events]
        assert "typing" in kinds, f"没出现正在输入：{ch.events}"
        assert kinds.index("typing") < kinds.index("send"), \
            f"typing 应在正文之前：{ch.events}"
        sends = [e[1] for e in ch.events if e[0] == "send"]
        assert sends == ["好呀好呀", "就这么说定了"], f"分段发送不对：{sends}"
        print("ok 正在输入（typing 先于正文 + 分段发送）")
    finally:
        (config.TYPING_ENABLED, config.TYPING_SECONDS_PER_CHAR,
         config.TYPING_MIN_SECONDS, config.TYPING_MAX_SECONDS,
         config.READING_MAX_SECONDS, config.READING_SECONDS_PER_CHAR) = saved
        config.BASE_DIR, config.DB_PATH = real_base, real_db


def test_profile_autofill():
    """AI 自动维护 profile：profile 字段进清单、自动字段可更新、手动字段不覆盖。"""
    import tempfile
    import db as _db
    import memory as _mem

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    try:
        _db.init()
        # 1) 提取结果带 profile 字段 → 写进清单并锁定
        _mem._apply({"content": "小徐是夜猫子作息", "category": "画像",
                     "profile": "作息习惯：夜猫子，凌晨才睡"})
        assert _db.get_kv("profile_locked") == "1", "自动填写应锁定清单"
        assert "作息习惯：夜猫子，凌晨才睡" in _db.get_kv("profile_text"), \
            f"清单没写进去：{_db.get_kv('profile_text')!r}"
        # 2) 同一字段新值 → AI 写过的字段可更新
        _mem._apply({"content": "小徐最近开始早睡了", "category": "画像",
                     "profile": "作息习惯：最近在调整，十一点前就睡"})
        assert "十一点前就睡" in _db.get_kv("profile_text") \
            and "凌晨才睡" not in _db.get_kv("profile_text"), \
            f"自动字段未更新：{_db.get_kv('profile_text')!r}"
        # 3) 用户手动加的字段 → AI 不覆盖
        _db.set_kv("profile_text", _db.get_kv("profile_text") + "\n称呼：小徐")
        _mem._apply({"content": "小徐让大家叫他徐哥", "category": "画像",
                     "profile": "称呼：徐哥"})
        assert "称呼：小徐" in _db.get_kv("profile_text"), \
            f"手动字段被覆盖：{_db.get_kv('profile_text')!r}"
        # 4) 无"字段：值"结构的 profile 不写入
        _mem._apply_profile("没有结构的一整句话")
        assert "没有结构" not in _db.get_kv("profile_text")
        # 5) 记忆本身照常入库（profile 是附加动作）
        assert any(m["content"] == "小徐是夜猫子作息" for m in _db.all_memories())
        print("ok AI 自动维护 profile（写入/更新/不覆盖手动/结构校验）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db


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
            def send(self, text, sender=""):
                self.events.append(("send", text))
            def typing(self, sender=""):
                self.events.append(("typing",))

        real_chat = _mem.llm.chat
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
        _mem.llm.chat = real_chat
        (config.TYPING_ENABLED, config.TYPING_SECONDS_PER_CHAR,
         config.TYPING_MIN_SECONDS, config.TYPING_MAX_SECONDS,
         config.READING_MAX_SECONDS, config.READING_SECONDS_PER_CHAR,
         config.SEND_MIN_SECONDS, config.SEND_MAX_SECONDS,
         config.SEND_SECONDS_PER_CHAR) = saved
        config.BASE_DIR, config.DB_PATH = real_base, real_db


def test_group_persist_and_friend_name():
    """群聊持久化（/refresh 自动散场 + 重启恢复）+ 老朋友名字回退到别名。"""
    import tempfile
    import db as _db
    import groupchat as _gc
    import commands as _cmd
    from channels.cli import _friend_name

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    real_chat = _gc.llm.chat
    saved_timing = _set_timing(_FAST_TIMING)

    class Ch:
        def __init__(self):
            self.sent = []
        def send(self, text, sender=""):
            self.sent.append((sender, text))

    try:
        # 两个朋友库：小葵（当前，有人格卡）、初遇朋友（归档，无卡只有别名）
        _db.init()
        _db.set_kv("persona_json", '{"name": "小葵", "label": "阳光话痨", '
                                   '"text": "话痨", "wake_hour": 8, "sleep_hour": 23, '
                                   '"bubble_mult": 1.0, "drawn_at": "2026-10-06"}')
        config.DB_PATH = tmp / "friend-cc111111.db"
        _db.init()
        _db.set_kv("alias", "初遇朋友")
        _db.set_kv("created_at", "2026-10-05T23:00:00")
        config.DB_PATH = tmp / "friend.db"

        # 1) 名字回退：无卡有别名 → 别名；无卡无别名 → 老朋友
        config.DB_PATH = tmp / "friend-cc111111.db"
        assert _friend_name() == "初遇朋友", f"别名回退失败：{_friend_name()}"
        config.DB_PATH = tmp / "friend-dd222222.db"
        _db.init()
        assert _friend_name() == "老朋友", f"无名回退失败：{_friend_name()}"
        config.DB_PATH = tmp / "friend.db"
        assert _friend_name() == "小葵", f"人格卡名字失败：{_friend_name()}"

        # 2) 拉群说话后，transcript 落盘存在
        parts = [_gc.Participant(tmp / "friend.db", "小葵"),
                 _gc.Participant(tmp / "friend-cc111111.db", "初遇朋友")]
        _gc.start(parts)
        _gc.llm.chat = lambda messages, temperature=0.7: "嗯嗯我在听"
        _gc.on_user_text("我吃完了", Ch())
        persist = tmp / "group_session.json"
        assert persist.exists(), "群聊 transcript 未落盘"

        # 3) /refresh：先自动散场沉淀，再重载——群聊内容不丢
        out = _cmd.handle("/refresh")
        config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"  # reload 会重置，拉回来
        assert "散场" in out, f"refresh 未提示散场：{out}"
        assert not _gc.is_active(), "refresh 后群还应处于散场状态"
        assert not persist.exists(), "散场后落盘文件应删除"
        with __import__("sqlite3").connect(str(tmp / "friend-cc111111.db")) as c:
            n = c.execute("SELECT COUNT(*) FROM messages WHERE kind='group'").fetchone()[0]
        assert n > 0, "refresh 散场后群聊记录没写回初遇朋友的库"

        # 4) 重启恢复：模拟进程带着未散场的群退出——recover_pending 补写回
        _gc.start(parts)
        _gc.llm.chat = lambda messages, temperature=0.7: "好嘞"
        _gc.on_user_text("群里再说点啥", Ch())
        assert persist.exists()
        _gc._session.active = False  # 停掉闲聊线程再模拟重启
        _gc._session = None  # 模拟进程重启（内存没了，落盘还在）
        _gc.llm.chat = lambda messages, temperature=0.7: "[]"  # 提取不新记
        note = _gc.recover_pending()
        assert "群聊恢复" in note, f"恢复提示不对：{note}"
        assert not persist.exists(), "恢复后落盘文件应删除"
        with __import__("sqlite3").connect(str(tmp / "friend.db")) as c:
            rows = [r[0] for r in c.execute(
                "SELECT content FROM messages WHERE kind='group' AND content LIKE '%群里再说点啥%'")]
        assert rows, "恢复后群聊记录没补写回小葵的库"
        print("ok 群聊持久化（落盘/refresh散场/重启恢复）+ 老朋友名字回退")
    finally:
        _set_timing(saved_timing)
        _gc.llm.chat = real_chat
        if _gc._session is not None:
            _gc._session.active = False
        _gc._session = None
        (tmp / "group_session.json").unlink(missing_ok=True)
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


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
        _t.sleep(0.3)                 # 确保提取线程先拿到锁
        t = threading.Thread(target=do_switch)
        t.start()
        _t.sleep(0.3)
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
        def send(self, text, sender=""):
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
        _llm.chat = real_chat
        config.BUBBLE_FIXED_PROB = saved_prob
        config.BASE_DIR, config.DB_PATH = real_base, real_db


def _make_two_friend_dbs(tmp):
    """测试工具：当前朋友小葵 + 归档老朋友稳稳两份库。返回两个 Participant。"""
    import db as _db
    import groupchat as _gc
    config.DB_PATH = tmp / "friend.db"
    _db.init()
    _db.set_kv("persona_json", '{"name": "小葵", "label": "阳光话痨", '
                               '"text": "话痨", "wake_hour": 8, "sleep_hour": 23, '
                               '"bubble_mult": 1.0, "drawn_at": "2026-10-06"}')
    config.DB_PATH = tmp / "friend-abc12345.db"
    _db.init()
    _db.set_kv("persona_json", '{"name": "稳稳", "label": "老干部", '
                               '"text": "稳", "wake_hour": 7, "sleep_hour": 22, '
                               '"bubble_mult": 1.0, "drawn_at": "2026-10-05"}')
    config.DB_PATH = tmp / "friend.db"
    return [_gc.Participant(tmp / "friend.db", "小葵"),
            _gc.Participant(tmp / "friend-abc12345.db", "稳稳")]


def test_group_timing():
    """群聊时间感与私聊同步：阅读等待生效；发言人"正在输入"在其正文之前。"""
    import tempfile
    import time as _t
    import db as _db
    import groupchat as _gc

    class Ch:
        def __init__(self):
            self.events = []  # ("typing", sender) / ("send", sender, text)
        def send(self, text, sender=""):
            self.events.append(("send", sender, text))
        def typing(self, sender=""):
            self.events.append(("typing", sender))

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    saved_timing = _set_timing(_FAST_TIMING)
    # 阅读时长拉开一点，验证"读完才回"（首位回应者：read + 0.3s 起步）
    config.READING_SECONDS_PER_CHAR = 0.05
    real_chat = _gc.llm.chat
    try:
        parts = _make_two_friend_dbs(tmp)
        ch = Ch()
        _gc.llm.chat = lambda messages, temperature=0.7: "嗯嗯"
        _gc.start(parts)
        t0 = _t.time()
        _gc.on_user_text("大家好呀", ch)
        elapsed = _t.time() - t0
        # 阅读模拟：5 个字 * 0.05s + 最少 0.3s 反应 ≈ 0.55s 起步
        assert elapsed >= 0.5, f"阅读等待没生效，秒回了：{elapsed:.2f}s"
        # 每位发言人都先 typing 再发正文，且 typing 带发言人自己的名字
        for name in ("小葵", "稳稳"):
            ti = next((i for i, e in enumerate(ch.events)
                       if e == ("typing", name)), None)
            si = next((i for i, e in enumerate(ch.events)
                       if e[0] == "send" and e[1] == name), None)
            assert ti is not None and si is not None and ti < si, \
                f"{name} 的 typing/正文时序不对：{ch.events}"
        print("ok 群聊时间感（阅读等待 + 发言人 typing 先于正文）")
    finally:
        _gc.llm.chat = real_chat
        if _gc._session is not None:
            _gc._session.active = False
        _gc._session = None
        _set_timing(saved_timing)
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_group_idle_chatter():
    """群聊自主闲聊：用户沉默后 AI 们自己开聊（间隔拉长、轮数有上限）；
    用户一开口立刻打断、话题回归用户；睡觉的成员不参与闲聊。"""
    import tempfile
    import time as _t
    import db as _db
    import groupchat as _gc
    import commands as _cmd

    class Ch:
        def __init__(self):
            self.sent = []  # (sender, text)
        def send(self, text, sender=""):
            self.sent.append((sender, text))

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    saved_timing = _set_timing(_FAST_TIMING)
    saved_idle = _set_timing({
        "GROUP_IDLE_AFTER_MIN_MINUTES": 0.003,   # ≈0.2s 空闲就开聊
        "GROUP_IDLE_AFTER_MAX_MINUTES": 0.003,
        "GROUP_IDLE_GAP_MIN_SECONDS": 0.05,
        "GROUP_IDLE_GAP_MAX_SECONDS": 0.08,
        "GROUP_IDLE_MAX_TURNS": 3,
        "TYPING_ENABLED": False,                  # 闲聊速度优先
    })
    real_chat = _gc.llm.chat
    try:
        parts = _make_two_friend_dbs(tmp)
        ch = Ch()
        _gc.llm.chat = lambda messages, temperature=0.7: "随便聊聊"
        _gc.start(parts)
        _gc.attach_channel(ch)

        def wait_for(pred, timeout=8):
            deadline = _t.time() + timeout
            while _t.time() < deadline:
                if pred():
                    return True
                _t.sleep(0.03)
            return False

        # 1) 用户没说话，AI 们自己聊起来了
        assert wait_for(lambda: len(ch.sent) >= 2), \
            f"自主闲聊没触发：{ch.sent}"
        assert all(s in ("小葵", "稳稳") for s, _ in ch.sent), \
            f"闲聊发言人不对：{ch.sent[:3]}"
        s = _gc.current()
        assert s is not None
        # 自主闲聊提示词确实注入了（冷场指引）
        seen_prompts = []

        def spy_chat(messages, temperature=0.7):
            seen_prompts.append(messages[0]["content"])
            return "随便聊聊"

        _gc.llm.chat = spy_chat
        assert wait_for(lambda: any("冷下来了" in p for p in seen_prompts)), \
            "闲聊没注入冷场指引"

        # 2) 用户一开口：闲聊被打断，话题回归用户（全员回应用户的新消息）
        ch.sent.clear()
        _gc.llm.chat = lambda messages, temperature=0.7: "回应小徐"
        _gc.on_user_text("我回来了", ch)
        idx_user = next(i for i, (sp, _) in enumerate(s.transcript) if sp == "小徐"
                        and _ == "我回来了")
        after = [sp for sp, _ in s.transcript[idx_user + 1:]]
        assert "小葵" in after and "稳稳" in after, \
            f"用户回来后没人接话：{s.transcript}"
        assert all(tx == "回应小徐" for sp, tx in s.transcript[idx_user + 1:]
                   if sp != "小徐"), "话题没回到用户"

        # 3) 稳稳睡觉了：新一轮闲聊只有小葵发言
        _cmd._write_kv_file(tmp / "friend-abc12345.db", "state", "SLEEPING")
        ch.sent.clear()
        with _db.PATH_LOCK:
            s.last_user_ts = 0.0
            s.last_idle_ts = 0.0
            s.idle_after = 0.15  # 立刻再触发一轮
        assert wait_for(lambda: len(ch.sent) >= 2), \
            f"睡觉判定后闲聊没继续：{ch.sent}"
        speakers = {sp for sp, _ in ch.sent}
        assert speakers == {"小葵"}, f"睡觉的稳稳不该发言：{ch.sent}"
        _gc.llm.chat = lambda messages, temperature=0.7: "[]"  # 散群提取不新记
        _gc.end()
        print("ok 群聊自主闲聊（冷场开聊/用户打断回归/睡觉成员跳过）")
    finally:
        _gc.llm.chat = real_chat
        if _gc._session is not None:
            _gc._session.active = False
        _gc._session = None
        _set_timing(saved_timing)
        _set_timing(saved_idle)
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_groupidle_command():
    """/groupidle：查看/开/关自主闲聊，kv 运行时覆盖优先于 config。"""
    import tempfile
    import db as _db
    import commands as _cmd
    import groupchat as _gc

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    try:
        _db.init()
        assert _gc.idle_enabled() is True  # config 默认开
        assert "开" in _cmd.handle("/groupidle")
        assert "已关闭" in _cmd.handle("/groupidle off")
        assert _gc.idle_enabled() is False
        assert "关" in _cmd.handle("/groupidle")
        assert "已开启" in _cmd.handle("/groupidle on")
        assert _gc.idle_enabled() is True
        assert "用法" in _cmd.handle("/groupidle xyz")
        print("ok /groupidle 开关（查看/开/关/运行时覆盖）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


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
        _db.add_message("user", "今天好累啊")
        _db.add_message("assistant", "辛苦啦，早点休息")
        day = _diary._life_day_str()
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
        _db.add_message("user", "今天聊点什么")
        _db.add_message("assistant", "随便聊呀")
        assert len(_db.today_messages()) == 2
        _diary.llm.chat = lambda *a, **k: '{"diary": "聊了聊", "memory_add": [], "memory_delete": []}'
        _diary.daily_review(force=True)
        assert len(_db.today_messages()) == 0, "回顾线没删干净"
        assert _db.get_diary(_diary._life_day_str()) is not None, "日记应保留"
        print("ok 回顾线彻底删除（PURGE_REVIEWED_MESSAGES=on）")
    finally:
        config.BASE_DIR, config.DB_PATH, config.PURGE_REVIEWED_MESSAGES = \
            real_base, real_db, real_purge
        _db.init()


def test_group_handoff_free_chat():
    """拉群后用户把焦点交给朋友们（"你们认识一下"），应很快开自由闲聊，不等长空闲。"""
    import tempfile
    import time as _t
    import db as _db
    import groupchat as _gc

    class Ch:
        def __init__(self):
            self.sent = []  # (sender, text)
        def send(self, text, sender=""):
            self.sent.append((sender, text))

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    # 正常空闲阈值拉到 60 分钟（测试期间绝不触发），焦点交接阈值压到 0.2s
    saved = _set_timing({
        "GROUP_IDLE_AFTER_MIN_MINUTES": 60.0,
        "GROUP_IDLE_AFTER_MAX_MINUTES": 60.0,
        "GROUP_IDLE_GAP_MIN_SECONDS": 0.05,
        "GROUP_IDLE_GAP_MAX_SECONDS": 0.08,
        "GROUP_IDLE_MAX_TURNS": 3,
        "GROUP_HANDOFF_IDLE_SECONDS": 0.2,
        "TYPING_ENABLED": False,
    })
    real_chat = _gc.llm.chat
    try:
        parts = _make_two_friend_dbs(tmp)
        ch = Ch()
        _gc.llm.chat = lambda messages, temperature=0.7: "随便聊聊"
        _gc.start(parts)
        _gc.attach_channel(ch)

        # 用户还没说话：60 分钟阈值下不该有闲聊
        _t.sleep(0.6)
        assert ch.sent == [], f"用户没说话不该闲聊：{ch.sent}"

        # 用户把焦点交给朋友 → 回应流程同步完成后，很快出现自主闲聊
        _gc.on_user_text("哈喽朋友们，你们认识一下新朋友", ch)
        after_respond = len(ch.sent)
        deadline = _t.time() + 3
        while _t.time() < deadline and len(ch.sent) <= after_respond:
            _t.sleep(0.03)
        assert len(ch.sent) > after_respond, \
            f"焦点交接后没开自由闲聊：{ch.sent}"

        # 非交接消息则不受影响：仍走正常长空闲（这里只验证不立即触发）
        ch.sent.clear()
        _gc.on_user_text("在吗", ch)
        after2 = len(ch.sent)
        _t.sleep(0.5)
        assert len(ch.sent) == after2, f"普通消息不该立即触发闲聊：{ch.sent}"
        _gc.llm.chat = lambda messages, temperature=0.7: "[]"  # 散群提取不新记
        _gc.end()
        print("ok 群聊焦点交接（你们认识一下→立刻自由闲聊；普通消息不受影响）")
    finally:
        _gc.llm.chat = real_chat
        if _gc._session is not None:
            _gc._session.active = False
        _gc._session = None
        _set_timing(saved)
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_corpus_consolidation():
    """固定语料统一收进 corpus.py：信号词/固定单句/模板都在，业务模块不再各自硬编码。"""
    import corpus
    import game as _game
    import core as _core

    assert "拜拜" in corpus.ENDING_SIGNALS and "晚安" in corpus.ENDING_SIGNALS
    assert "不玩了" in corpus.GAME_STOP_WORDS
    assert "朋友啦" in corpus.AUTO_FAMILIAR_NOTE
    assert corpus.LLM_STUB
    assert corpus.LLM_ERROR.format("xx") == "（脑子卡住了：xx）"
    assert corpus.INTRO_FALLBACK.format("阿燃") == "你好啊，我叫阿燃，我们认识一下吧！"
    # 业务模块不再各自硬编码
    assert not hasattr(_core, "ENDING_SIGNALS"), "core 还留着 ENDING_SIGNALS"
    assert not hasattr(_game, "STOP_WORDS"), "game 还留着 STOP_WORDS"
    # 游戏结束检测仍工作（走 corpus）
    assert _game.detect_stop("不玩了") is True
    assert _game.detect_stop("今天天气不错") is False
    print("ok 固定语料统一收进 corpus（信号词/单句/模板，业务模块不再硬编码）")


def test_group_llm_fallback():
    """群聊与私聊同步：LLM 报错时那位朋友说兜底语，不卡死整轮群聊。"""
    import tempfile
    import db as _db
    import groupchat as _gc

    class Ch:
        def __init__(self):
            self.sent = []
        def send(self, text, sender=""):
            self.sent.append((sender, text))

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    saved = _set_timing({
        "TYPING_ENABLED": False,
        "READING_SECONDS_PER_CHAR": 0.0,
        "GROUP_SPEAKER_GAP_MIN_SECONDS": 0.0,
        "GROUP_SPEAKER_GAP_MAX_SECONDS": 0.01,
    })
    real_chat = _gc.llm.chat
    try:
        parts = _make_two_friend_dbs(tmp)
        ch = Ch()
        _gc.llm.chat = lambda messages, temperature=0.7: (_ for _ in ()).throw(
            _gc.llm.LLMUnavailable("boom"))
        _gc.start(parts)
        _gc.attach_channel(ch)
        _gc.on_user_text("在吗", ch)  # 不应抛异常
        assert ch.sent and all("脑子卡住了" in t for _, t in ch.sent), \
            f"群聊 LLM 兜底不对：{ch.sent}"
        _gc.llm.chat = lambda messages, temperature=0.7: "[]"
        _gc.end()
        print("ok 群聊 LLM 兜底（报错说'脑子卡住了'，不卡死整轮）")
    finally:
        _gc.llm.chat = real_chat
        if _gc._session is not None:
            _gc._session.active = False
        _gc._session = None
        _set_timing(saved)
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


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
    test_groupchat()
    test_typing_indicator()
    test_profile_autofill()
    test_interrupt_detection()
    test_group_persist_and_friend_name()
    test_db_path_lock()
    test_corpus_pick()
    test_corpus_consolidation()
    test_bubble_fixed_corpus()
    test_group_timing()
    test_group_idle_chatter()
    test_groupidle_command()
    test_group_handoff_free_chat()
    test_group_llm_fallback()
    test_diary_daily_review()
    test_diary_purge_reviewed_messages()
    print("\n全部通过")
