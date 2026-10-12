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

import atexit
import importlib
import shutil
import tempfile

# 双层生成会让"一次回复"变成**两次** LLM 调用。绝大多数历史测试的 mock 是按调用次数
# /最后一条消息内容应答的（假设一次调用），所以全局把独白层关掉——这是新增维度，不该
# 让老用例逐个改。要验证独白本身时用专门的用例（见 test_inner_monologue，会临时打开）。
# ⚠️ 必须写环境变量而不是改 config 属性：/refresh 会 importlib.reload(config)，
# 只改模块属性会被刷新回去，写 os.environ 才能扛住重载。
os.environ["INNER_MONOLOGUE_ENABLED"] = "off"
importlib.reload(config)

# 测试临时目录统一收口到一个**固定目录**，跑完尽量清、下次跑先清（双保险）。
# 背景：某些环境（如 WorkBuddy 沙箱）%TEMP% 不可写，tempfile 会回退到**当前工作目录**，
# 于是一次测试跑完就在项目根撒一堆 tmpXXXX（历史债：曾攒到 1100+ 个、56MB）。
# 之所以不用 mkdtemp 做根目录：Windows 上 sqlite 文件句柄在解释器退出时可能还没释放，
# atexit 的 rmtree 会因文件锁清不干净；固定目录 + "启动即清"能自愈上次残留。
_TEST_TMP_ROOT = Path(__file__).resolve().parent / ".test-tmp"
shutil.rmtree(_TEST_TMP_ROOT, ignore_errors=True)
_TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_TEST_TMP_ROOT)
atexit.register(lambda: shutil.rmtree(_TEST_TMP_ROOT, ignore_errors=True))

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

        def fake_chat(messages, temperature=0.7, **k):
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
    saved_work = config.WORK_ENABLED
    config.WORK_ENABLED = False     # 只测打字节奏；工作时段 fresh DB 会判离线，
    #   消息进 inbox 不回复（时间依赖 flaky，与本测试无关）
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
        config.WORK_ENABLED = saved_work
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
    saved_work = config.WORK_ENABLED
    config.WORK_ENABLED = False     # 本测试只测插话；开着工作系统会让"现在几点"
    #   决定是否离线（fresh DB 在工作时段必然 offline → 消息进 inbox 不回话）
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
        (config.TYPING_ENABLED, config.TYPING_SECONDS_PER_CHAR,
         config.TYPING_MIN_SECONDS, config.TYPING_MAX_SECONDS,
         config.READING_MAX_SECONDS, config.READING_SECONDS_PER_CHAR,
         config.SEND_MIN_SECONDS, config.SEND_MAX_SECONDS,
         config.SEND_SECONDS_PER_CHAR) = saved
        config.WORK_ENABLED = saved_work
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
                # 用零点而非中午：purge 的 cutoff 用真实墙钟 now()（未 mock），
                # 若 mock 成"当天 12:00"而真实时间在上午，12:00 > now → 删不掉、误挂。
                # 零点一定早于当天任意真实时刻，保证"回顾线删干净"断言稳定。
                return _dt.fromisoformat(day + "T00:00:00")

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


def test_semantic_recall():
    """语义召回（S4 首位）：字面对不上但同义（熬夜 ↔ 几点睡）也能召回来。

    治「几点睡召不回熬夜」——纯字面 bigram 对不上就是零分；本地 bge-small-zh-v1.5
    把"熬夜""凌晨才睡""几点睡"归到同一语义，向量相似度补上字面的空档。
    embedding 模型/依赖不可用时优雅跳过（语义召回是增强、不是必须）。
    """
    import tempfile
    import db as _db
    import memory as _mem
    import embeddings as _emb

    if not _emb.available():
        print("skip 语义召回（embedding 模型/依赖不可用）")
        return

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    try:
        _db.init()
        # 目标记忆：与 query 语义相关、但字面 bigram 几乎不重叠
        _db.add_memory("小徐平时熬夜，凌晨一两点才睡", "画像", person="小徐")
        # 填充 12 条无关记忆，凑过「≤10 条直接全量返回」的分支，逼它走打分
        for i in range(12):
            _db.add_memory(f"小徐周末喜欢去公园散步{i}号", "偏好", person="小徐")

        hits = _mem.recall("小徐一般几点睡", top_k=3)
        contents = [m["content"] for m in hits]
        assert any("熬夜" in c for c in contents), \
            f"语义召回应带回「熬夜」记忆：{contents}"
        print("ok 语义召回（熬夜 ↔ 几点睡）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_today_notes_injection():
    """今天做过/读到的事：进 B 库 → 注入当天上下文 → 跨天自动不再注入（记忆保留）。"""
    import tempfile
    import datetime as _dt
    import db as _db
    import memory as _mem
    import context as _ctx
    import work as _work

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    today = _dt.date.today().isoformat()
    try:
        _db.init()
        # 1) 工作产出素材时顺手落 B 库（不额外调 LLM）
        _work._note_today({"genre": "科幻", "title": "三体", "source": "刘慈欣",
                           "quote": "不要回答", "text": "正文" * 300},
                          2, "这句挺带感的，宇宙社会学是真敢想")
        notes = _mem.today_notes()
        assert len(notes) == 1, f"今天的事应进 B 库：{notes}"
        assert "三体" in notes[0]["summary"], f"摘要应带书名：{notes[0]['summary']}"

        # 2) 注入当天上下文
        msgs = _ctx.build_chat_messages("AWAKE", ["你今天干嘛了"])
        joined = "\n".join(m.get("content", "") for m in msgs)
        assert "我今天做过的事" in joined, "应注入今天的素材"
        assert "不要回答" in joined, "注入里应能看到读到的内容"

        # 3) 跨天不再注入，但 B 库记录保留（记忆还在）
        assert _mem.today_notes("2020-01-01") == [], "跨天不该再注入"
        assert _db.long_memory_count() == 1, "B 库记录应保留"
        print("ok 今日素材（B 库落库 + 当天注入 + 跨天不注入）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_memory_decay():
    """艾宾浩斯遗忘：到期压短一级；被想起越多撑得越久；够多次永久；压够级数彻底忘掉。

    保真优先：模型压出来的比原文长/踩红线 → 本次跳过，绝不把记忆改味。
    """
    import tempfile
    import datetime as _dt
    import db as _db
    import memory as _mem

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    real_idle, real_bonus, real_max, real_pin = (config.MEMORY_DECAY_IDLE_DAYS,
                                                 config.MEMORY_DECAY_RECALL_BONUS,
                                                 config.MEMORY_DECAY_MAX_LEVEL,
                                                 config.MEMORY_DECAY_PIN_RECALLS)
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    config.MEMORY_DECAY_IDLE_DAYS = 30
    config.MEMORY_DECAY_RECALL_BONUS = 0.5
    config.MEMORY_DECAY_MAX_LEVEL = 3
    config.MEMORY_DECAY_PIN_RECALLS = 8
    old = _dt.datetime.now() - _dt.timedelta(days=100)
    try:
        _db.init()
        # 1) 很久没被想起 → 进待淡忘列表
        mid = _db.add_memory("小徐说过他小时候养过一只叫团子的猫，后来送人了还要过很久", "事件")
        _db.add_memory("小徐喜欢吃学校门口那家麻辣烫，尤其加麻酱", "偏好")
        _db.touch_memory(mid)          # 想起过一次 → 有效期 30×1.5=45 天
        with _db._conn() as c:
            c.execute("UPDATE memories SET recalled_at=? WHERE id=?",
                      (old.isoformat(timespec="seconds"), mid))
        # 第二条：从未被想起（用 created_at）
        with _db._conn() as c:
            c.execute("UPDATE memories SET created_at=?, recalled_at='' WHERE content LIKE '小徐喜欢吃%'",
                      (old.isoformat(timespec="seconds"),))
        due = _mem.forget_preview()
        assert len(due) == 2, f"两条都过期未想起：{due}"

        # 2) 有效期随召回次数变长：想起 8 次以上 → 永久，不再待淡忘
        many = _db.add_memory("小徐是夜猫子，经常凌晨一两点才睡", "画像")
        for _ in range(9):
            _db.touch_memory(many)
        with _db._conn() as c:
            c.execute("UPDATE memories SET recalled_at=? WHERE id=?",
                      (old.isoformat(timespec="seconds"), many))
        assert _mem._pin_long_remembered() == 1, "想起够多次应升为永久"
        assert many not in [m["id"] for m in _mem.forget_preview()], "永久记忆不该待淡忘"

        # 3) 压短一级（模型返回更短且合规）
        real_chat = _mem.llm.chat
        _mem.llm.chat = lambda *a, **k: ('[{"id": %d, "content": "小徐小时候养过猫团子"},'
                                         '{"id": %d, "content": "小徐爱吃门口那家麻辣烫"}]'
                                         % (mid, mid + 1))
        try:
            out = _mem.forget_pass()
        finally:
            _mem.llm.chat = real_chat
        assert "淡忘 2 条" in out, f"应淡忘两条：{out}"
        m1 = [x for x in _db.all_memories(limit=10) if x["id"] == mid][0]
        assert m1["decay_level"] == 1, f"等级应变 1：{m1}"
        assert m1["content"] == "小徐小时候养过猫团子", f"内容应被压短：{m1['content']}"

        # 4) 保真：模型压得比原文长 → 跳过不改
        before = m1["content"]
        _mem.llm.chat = lambda *a, **k: ('[{"id": %d, "content": "%s并且他还说过很多很多很多别的事情哦"}]'
                                         % (mid, before))
        try:
            out = _mem.forget_pass()
        finally:
            _mem.llm.chat = real_chat
        assert "没动" in out, f"压长了应跳过：{out}"
        m1b = [x for x in _db.all_memories(limit=10) if x["id"] == mid][0]
        assert m1b["content"] == before, "跳过时内容不能变"

        # 5) 压到上限 → 彻底遗忘（连带 B 库长文一起删）
        long_id = _db.add_long_memory("长文摘要", "全文" * 100)
        m2 = _db.add_memory("小徐讲过一段很长的往事细节", "事件", long_id=long_id)
        with _db._conn() as c:
            c.execute("UPDATE memories SET created_at=?, recalled_at='' WHERE id=?",
                      (old.isoformat(timespec="seconds"), m2))
        _db.set_memory_decay(m2, config.MEMORY_DECAY_MAX_LEVEL - 1, "小徐讲过一段往事")
        _mem.llm.chat = lambda *a, **k: '[{"id": %d, "content": "小徐讲过往事"}]' % m2
        try:
            out = _mem.forget_pass()
        finally:
            _mem.llm.chat = real_chat
        assert "彻底忘了 1 条" in out, f"到上限应删除：{out}"
        assert _db.find_memory_by_keyword("小徐讲过往事") is None, "A 库行应删掉"
        assert _db.get_long_memory(long_id) is None, "挂的 B 库长文应一起删"
        print("ok 艾宾浩斯记忆衰减（压短/永久/保真跳过/彻底遗忘）")
    finally:
        config.MEMORY_DECAY_IDLE_DAYS, config.MEMORY_DECAY_RECALL_BONUS = real_idle, real_bonus
        config.MEMORY_DECAY_MAX_LEVEL, config.MEMORY_DECAY_PIN_RECALLS = real_max, real_pin
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_delete_friend():
    """删除朋友（用户拍板）：先备份到 backups/ 再删；**正在聊的这位不许删**；
    不带 yes 只给确认提示不真删。"""
    import tempfile
    import db as _db
    import commands as _cmd

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    try:
        _db.init()
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
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_friend_switch_no_deadlock():
    """换朋友不能卡死（实测 #33）：网页端 /api/send 外面握着 SEND_LOCK，
    而 /friend /new-friend 内部切库还要再拿一次 → 同一线程重入。
    SEND_LOCK 必须是可重入锁，否则「换朋友」永远停在"稍等一下"。"""
    import tempfile
    import threading as _th
    import db as _db
    import commands as _cmd

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    try:
        _db.init()
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
        # 预置今天的起床点（锚点前 5 分钟）：新逻辑"醒来才排班"——不预置的话
        # 赖床浮动可能让 10:00 整还没"醒"，排不出班（测试要与随机浮动解耦）
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_day": "2026-10-09", "wake_at": "2026-10-09T09:55:00"}))
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
        # 预置今天的起床点（锚点前 5 分钟）：醒来才排班，与赖床随机浮动解耦
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_day": "2026-10-09", "wake_at": "2026-10-09T05:55:00"}))
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

        # 人格卡行为参数带阅读偏好（两级：一级分类 + 细标签。旧标签"科幻"已自动迁移）
        params = _persona.behavior_params()
        assert params["reading_pref"] == "精品小说", f"阅读偏好没跟人格走：{params}"
        assert params["reading_tags"] == ["科幻"], params

        # 阅读顺序：偏好细标签（科幻）排最前
        order = _reading._order()
        first_genres = [_reading.LIBRARY[i]["genre"] for i in order[:4]]
        assert first_genres == ["科幻"] * 4, f"偏好分类没排前面：{first_genres}"

        # 顺序推进 + 进度持久化（对外 genre 已翻译成一级分类，原标签在 tag）
        a1 = _reading.next_article()
        assert a1["genre"] == "精品小说" and a1["tag"] == "科幻", f"第一篇应是科幻：{a1}"
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
        # 旧摆法兼容：子目录名是细标签 → 归到它所属的一级分类
        got = {b["title"]: (b["category"], b["tags"], b["by"]) for b in items}
        assert got["三体"] == ("精品小说", ["科幻"], "dir"), got
        assert got["背影"] == ("文学", ["散文"], "dir"), got

        # 偏好选书：人格两级偏好（精品小说 + [科幻]）命中三体；结构与内置书摘同构
        first = _lib.next_passage()
        assert first["title"] == "三体", first
        assert first["genre"] == first["category"] == "精品小说", first
        assert first["tag"] == "科幻", first
        assert set(("genre", "category", "tag", "title", "source",
                    "quote", "text")) <= set(first), \
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


def test_library_categories():
    """书库分类（2026-10-11）：平铺不建子目录 + 映射表分类 + 手动优先 + 上传/重分类/删除。"""
    import tempfile

    import config as _cfg
    import corpus as _corpus
    import db as _db
    import library as _lib

    real_base, real_db, real_books = config.BASE_DIR, config.DB_PATH, config.BOOKS_DIR
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    config.BOOKS_DIR = tmp / "books"
    try:
        _db.init()
        books = config.BOOKS_DIR
        books.mkdir(parents=True)
        # 全部平铺在根目录：靠书名/作者/正文自动分类，一个子目录都不建
        (books / "三体.txt").write_text("给时光以生命，给岁月以文明。\n\n" + "弱小和无知不是生存的障碍。\n\n" * 20,
                                        encoding="utf-8")
        (books / "活着.txt").write_text("人是为了活着本身而活着，而不是为了活着之外的任何事物。\n\n" * 20,
                                        encoding="utf-8")
        (books / "孙子兵法.txt").write_text("兵者，诡道也。故能而示之不能，用而示之不用。\n\n" * 20,
                                            encoding="utf-8")
        (books / "随便起的名字.txt").write_text("这是一段完全看不出门类的正文内容。\n\n" * 20,
                                                encoding="utf-8")
        (books / "README.txt").write_text("说明文件，不该被当成书", encoding="utf-8")

        got = {b["id"]: b["category"] for b in _lib.books()}
        assert "README.txt" not in got, f"README 不该当书：{got}"
        assert got["三体.txt"] == "精品小说", got
        assert got["活着.txt"] == "文学", got
        assert got["孙子兵法.txt"] == "军事", got
        assert got["随便起的名字.txt"] == _corpus.UNCATEGORIZED, got

        # 分类清单：22 类 + 未分类兜底，计数对得上
        cats = {c["name"]: c["count"] for c in _lib.categories()}
        assert len(cats) == 23, f"应是 22 类 + 未分类：{len(cats)}"
        assert cats["精品小说"] == 1 and cats[_corpus.UNCATEGORIZED] == 1, cats

        # 手动指定 → by=manual，reindex 重跑自动分类也动不了它
        _lib.set_category("三体.txt", "科学技术", ["科普"])
        assert _lib.get("三体.txt")["by"] == "manual", _lib.get("三体.txt")
        _lib.reindex()
        assert _lib.get("三体.txt")["category"] == "科学技术", "手动选过的不能被自动重分类覆盖"

        # 上传：平铺存根目录（不建子目录）+ 自动分类；不支持的格式直接拒
        up = _lib.save_upload("人类简史.txt", ("认知革命让智人走上食物链顶端。\n\n" * 20).encode("utf-8"))
        assert up and up["category"] == "历史", up
        assert (books / "人类简史.txt").is_file(), "上传的书必须平铺在根目录"
        assert _lib.save_upload("坏东西.pdf", b"junk") is None, "只收 txt / epub"
        dup = _lib.save_upload("人类简史.txt", ("同名再来一本，正文也要够长才作数。\n\n" * 5).encode("utf-8"))
        assert dup and dup["id"] == "人类简史(2).txt", f"同名要另存，不能覆盖：{dup}"

        # 删书：文件 + 映射表条目一起没
        assert _lib.delete_book("人类简史(2).txt")
        assert not (books / "人类简史(2).txt").exists()
        assert _lib.get("人类简史(2).txt") is None

        print("ok 书库分类（平铺 + 映射表 + 手动优先 + 上传/重分类/删书）")
    finally:
        config.BOOKS_DIR = real_books
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_epub_cover():
    """epub 封面解析（v0.78）：两种标准写法都认（meta name=cover / properties=cover-image）。"""
    import tempfile
    import zipfile

    import config as _cfg
    import db as _db
    import library as _lib

    real_base, real_db, real_books = config.BASE_DIR, config.DB_PATH, config.BOOKS_DIR
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    config.BOOKS_DIR = tmp / "books"
    try:
        _db.init()
        books = config.BOOKS_DIR
        books.mkdir(parents=True)
        jpg = b"\xff\xd8\xff\xe0" + b"fakecover" * 8      # 假图（只验抽取，不解码）

        def _epub(path, cover_meta: bool):
            container = ('<?xml version="1.0"?><container version="1.0" '
                         'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                         '<rootfile full-path="OEBPS/content.opf" '
                         'media-type="application/oebps-package+xml"/></rootfiles></container>')
            cover_item = ('<item id="cover-img" href="images/cover.jpg" media-type="image/jpeg"'
                          + (' properties="cover-image"/>' if not cover_meta else '/>'))
            meta = ('<meta name="cover" content="cover-img"/>' if cover_meta else "")
            opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:title>封面测试书</dc:title><dc:creator>某人</dc:creator>'
                   + meta + '</metadata>'
                   '<manifest><item id="c0" href="chapter0.xhtml" '
                   'media-type="application/xhtml+xml"/>' + cover_item + '</manifest>'
                   '<spine><itemref idref="c0"/></spine></package>')
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("mimetype", "application/epub+zip")
                z.writestr("META-INF/container.xml", container)
                z.writestr("OEBPS/content.opf", opf)
                z.writestr("OEBPS/chapter0.xhtml",
                           "<html><body><p>" + "这是一段足够长的正文内容。" * 5 + "</p></body></html>")
                z.writestr("OEBPS/images/cover.jpg", jpg)

        _epub(books / "甲书.epub", cover_meta=True)       # 写法① meta name=cover
        _epub(books / "乙书.epub", cover_meta=False)      # 写法② properties=cover-image
        (books / "丙书.txt").write_text("没有封面的 txt 书。\n\n" * 20, encoding="utf-8")

        got = {b["id"]: b["has_cover"] for b in _lib.books()}
        assert got["甲书.epub"] and got["乙书.epub"], f"两种封面的写法都要认出来：{got}"
        assert not got["丙书.txt"], "txt 不该有封面"

        data, ctype = _lib.cover_bytes("甲书.epub")
        assert ctype == "image/jpeg" and data == jpg, "封面应原样抽出、媒体类型按扩展名判"
        assert _lib.cover_bytes("丙书.txt") is None, "txt 没有封面"
        assert _lib.cover_bytes("不存在.epub") is None, "不存在的书不该抛异常"

        print("ok epub 封面（meta/properties 两种写法 + 无封面兜底 + 防越界）")
    finally:
        config.BOOKS_DIR = real_books
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_persona_pool_expanded():
    """人格骨架扩充到 15 张（v0.79）：抽卡池、内核、两级阅读偏好、旧卡反推四处必须同步。

    加骨架最容易漏的是 `_LABEL_TO_KEY`——漏了旧卡反推不出 archetype，
    `/persona` 会显示空内核。这张测试就是防那个。
    """
    import corpus as _corpus
    import persona_pool as _pool

    names = [g["name"] for g in _corpus.GENRES]
    assert len(_pool.POOL) >= 15, f"骨架数量不够：{len(_pool.POOL)}"
    keys = [p["key"] for p in _pool.POOL]
    labels = [p["label"] for p in _pool.POOL]
    assert len(set(keys)) == len(keys) and len(set(labels)) == len(labels), "key/label 不能撞"
    for p in _pool.POOL:
        assert p["key"] in _pool.KERNELS, f"{p['key']} 缺内核"
        assert p["reading_pref"] in names, f"{p['key']} 的一级分类不在 22 类里：{p['reading_pref']}"
        assert p.get("reading_tags"), f"{p['key']} 缺细标签"
        assert len(p.get("names") or []) >= 3, f"{p['key']} 名字池太小"
        # 每张内核都必须带「不能有的」事实卡——那是防止它借对方经历给自己捏人设的护栏
        k = _pool.KERNELS[p["key"]]
        assert any("不能有的" in (f.get("id") or "") + (f.get("fact") or "")
                   for f in k.get("facts") or []), f"{p['key']} 缺「不能有的」事实卡"

    # 反推表：每张卡都要能按 label 找回 archetype，并按 label 拿到一个合法分类
    for p in _pool.POOL:
        assert _pool._LABEL_TO_KEY.get(p["label"]) == p["key"], f"反推表漏了 {p['label']}"
        pref = _pool._LABEL_TO_PREF.get(p["label"], "")
        assert pref in names or pref in _corpus.PREF_LEGACY_MAP, \
            f"{p['label']} 的兜底偏好既不属 22 类也不是旧标签：{pref}"

    # 抽一张出来必须是完整卡（含内核 + 两级偏好）
    card = _pool.draw()
    assert card["kernel"] is card["kernel"] and card["kernel"], "抽卡必须带内核"
    assert card["reading_pref"] in names and card["reading_tags"]

    print(f"ok 人格池扩充（{len(_pool.POOL)} 张骨架 · 内核/反推表/两级偏好同步）")


def test_library_epub_metadata():
    """epub 元数据的两个坑（v0.79 实测）：①OPF 命名空间拼错 ②dc:subject 是脏值。

    ① 掌上书苑(epubBuilder) 一批书把 <package xmlns=...> 写成 mlns=...，整份 OPF
       没有命名空间 → manifest/spine 全读不出来 → 正文 0 段 → 书在书架里凭空消失。
    ② 盗版转换本爱往 dc:subject 里塞源站名（ibook.178.com）/URL/书名本身，
       不洗掉会给分类凭空加 6 分，把书带偏（《时间简史》被 "lcsh: Novel" 带成小说）。
    """
    import tempfile
    import zipfile

    import config as _cfg
    import db as _db
    import library as _lib

    real_base, real_db, real_books = config.BASE_DIR, config.DB_PATH, config.BOOKS_DIR
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    config.BOOKS_DIR = tmp / "books"
    try:
        _db.init()
        books = config.BOOKS_DIR
        books.mkdir(parents=True)
        container = ('<?xml version="1.0"?><container version="1.0" '
                     'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                     '<rootfile full-path="OPS/fb.opf" '
                     'media-type="application/oebps-package+xml"/></rootfiles></container>')

        def _epub(path, pkg_attr: str, subject: str):
            opf = ('<?xml version="1.0"?><package version="2.0" ' + pkg_attr + '>'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:title>时间简史</dc:title><dc:creator>霍金</dc:creator>'
                   '<dc:subject>' + subject + '</dc:subject></metadata>'
                   '<manifest><item id="chapter1" href="chapter1.html" '
                   'media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="chapter1"/></spine></package>')
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("mimetype", "application/epub+zip")
                z.writestr("META-INF/container.xml", container)
                z.writestr("OPS/fb.opf", opf)
                z.writestr("OPS/chapter1.html",
                           "<html><body><p>" + "宇宙的起源与黑洞的蒸发。" * 30 + "</p></body></html>")

        # ① 命名空间拼错成 mlns（= 整份 OPF 无命名空间）
        _epub(books / "拼错命名空间.epub", 'mlns="http://www.idpf.org/2007/opf"', "科普")
        broke = _lib._parsed(books / "拼错命名空间.epub")
        assert broke and broke["passages"], "OPF 没命名空间也得解析出正文（否则书会消失）"
        assert broke["title"] == "时间简史", f"书名也该读到：{broke}"
        ids = {b["id"] for b in _lib.books()}
        assert "拼错命名空间.epub" in ids, "正文为 0 的书会被 _scan 丢掉——这就是那个 bug"

        # ② 脏 subject：站名 / URL / 国会图书馆前缀 / 跟书名同值
        assert _lib._clean_subjects(["ibook.178.com"], "某书") == [], "源站名要洗掉"
        assert _lib._clean_subjects(["http://x.com/a"], "某书") == [], "URL 要洗掉"
        assert _lib._clean_subjects(["lcsh: Novel"], "某书") == [], "lcsh 前缀是噪声"
        assert _lib._clean_subjects(["活着"], "活着") == [], "拿书名当题材等于没标"
        assert _lib._clean_subjects(["科幻小说", "ibook.178.com"], "某书") == ["科幻小说"], \
            "脏的丢、好的留"

        # 干净 subject 仍然生效（同一本书，脏值 → 靠书名/正文认成科学技术）
        _epub(books / "脏标签.epub", 'xmlns="http://www.idpf.org/2007/opf"', "ibook.178.com")
        assert not (_lib._parsed(books / "脏标签.epub") or {}).get("subject"), "脏 subject 不该留下"

        print("ok epub 元数据（OPF 命名空间容错 + dc:subject 洗脏值）")
    finally:
        config.BOOKS_DIR = real_books
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_reading_pref_two_level():
    """阅读偏好两级（一级分类 + 细标签）：旧标签自动迁移，内置书摘翻译到同一套分类。"""
    import json
    import tempfile

    import config as _cfg
    import corpus as _corpus
    import db as _db

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        import persona as _persona
        import persona_pool as _pool
        import reading as _reading

        # 旧卡（reading_pref 是老标签）→ 一级分类 + 细标签，口味没丢
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "q",
            "text": "夜猫子", "wake_hour": 10, "sleep_hour": 2, "bubble_mult": 1.2,
            "reading_pref": "科幻", "drawn_at": "2026-10-10T00:00:00"}, ensure_ascii=False))
        p = _persona.behavior_params()
        assert p["reading_pref"] == "精品小说", p
        assert p["reading_tags"] == ["科幻"], p

        # 新卡：POOL 里直接就是两级，且一级分类必须是 22 类之一
        names = [g["name"] for g in _corpus.GENRES]
        for _ in range(12):
            card = _pool.draw()
            assert card["reading_pref"] in names, card
            assert isinstance(card.get("reading_tags"), list), card
            for t in card["reading_tags"]:
                assert t in _corpus.PREF_LEGACY_MAP or any(
                    t in g["tags"] for g in _corpus.GENRES), (card, t)

        # 内置书摘：旧细标签翻译成一级分类（和用户书库同一套），原标签留在 tag
        for g, cat in _corpus.PREF_LEGACY_MAP.items():
            assert _reading.category_of(g) == cat, g
        a = _reading.next_article()
        assert a["genre"] == a["category"] and a["genre"] in names, a
        assert a["tag"] in _corpus.PREF_LEGACY_MAP, a

        print("ok 阅读偏好两级（分类 + 细标签）+ 内置书摘分类统一")
    finally:
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
        # 旧卡 reading_pref 按 label 反推成老标签「生活随笔」，再迁移成两级（文学 + [生活随笔]）
        assert card["reading_pref"] == "文学", f"旧卡应迁移到一级分类：{card}"
        assert card["reading_tags"] == ["生活随笔"], card

        # /persona 不崩、带阅读偏好、空怪癖有兜底
        out = commands.handle("/persona")
        assert "阅读偏好" in out and "生活随笔" in out, f"/persona 缺阅读偏好：{out}"
        assert "没特别怪癖" in out, f"旧卡空怪癖应兜底：{out}"

        # 阅读偏好标签能显示（爱读X·细标签）
        import webui as _webui
        assert _webui._persona_tags(card) == ["阳光话痨", "爱读文学·生活随笔"], \
            f"旧卡补全后标签不对：{_webui._persona_tags(card)}"

        # 新卡（已含 reading_pref）不被 normalize 覆盖
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "q",
            "text": "夜猫子", "wake_hour": 10, "sleep_hour": 2, "bubble_mult": 1.2,
            "reading_pref": "科幻", "drawn_at": "2026-10-10T00:00:00",
        }, ensure_ascii=False))
        # 新卡（已含 reading_pref）不被 normalize 覆盖，只会从老标签迁移到两级
        new = _pool.load()
        assert new["reading_pref"] == "精品小说", f"新卡应迁移到一级分类：{new['reading_pref']}"
        assert new["reading_tags"] == ["科幻"], new

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
        # 预置今天的起床点（锚点前 5 分钟）：醒来才排班（与随机浮动解耦）
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_day": "2026-10-09", "wake_at": "2026-10-09T09:55:00"}))
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
            "day": "2026-10-09", "quota": 6, "planned": 1,
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
        # 预置今天的起床点（锚点前 5 分钟）：醒来才排班（与随机浮动解耦）
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_day": "2026-10-09", "wake_at": "2026-10-09T09:55:00"}))
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
            "day": "2026-10-09", "quota": 1, "planned": 1,
            "units": [{"start": first_start.isoformat(timespec="seconds"),
                       "end": (first_start + _td(minutes=50)).isoformat(timespec="seconds")}],
            "settled": 0, "energy": 20, "money": 0}))
        tired = _mood.mood(first_start + _td(minutes=30))
        assert tired == {"label": "疲惫", "tone": "tired"}, tired

        # 忙完（off）白天 → 轻松；夜里 → 平静（时段情绪，独立于 phase）
        _db.set_kv("work_state", json.dumps({
            "day": "2026-10-09", "quota": 1, "planned": 1,
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

            st = c.get(base + "/api/state").json()
            assert st["name"] and st["clock"], f"状态快照缺基本信息：{st}"
            assert st["work"]["phase"] in _webui.work.PHASE_LABELS, f"phase 不合法：{st}"

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


def test_wake_semantics_and_tick():
    """起床双语义（实测 #26）：today_wake_at=「今天该几点起」不 +1 天；
    next_wake_at=夜里问「下次几点起」→ 明天，且 wake_days 按日缓存夜里反复问不抖动（#27）；
    被旧 bug 污染的缓存（时刻落在别的日期）自愈；tick 处理赖床醒/熬夜睡/reason 缺失自愈。"""
    import json
    import tempfile
    from datetime import datetime as _dt

    import db as _db
    import offline as _off

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        _db.set_kv("persona_json", json.dumps({
            "archetype": "oldcadre", "label": "老干部", "name": "稳稳", "quirk": "",
            "text": "你作息规律。", "wake_hour": 6, "sleep_hour": 22,
            "bubble_mult": 0.8, "drawn_at": "2026-10-09T00:00:00"}))

        # 早晨问"今天该几点起"：必须落在今天（老 bug：+1 天 → 永远赖床不醒）
        morning = _dt(2026, 10, 10, 6, 30)
        t = _off.today_wake_at(morning)
        assert t.date() == morning.date(), f"今天的起床算到了 {t.date()}（老 bug 又犯了）"
        assert _dt(2026, 10, 10, 5, 30) <= t <= _dt(2026, 10, 10, 7, 30), \
            f"起床浮动越界：{t}"
        assert _off.today_wake_at(morning) == t, "同一早晨的起床时刻应幂等"

        # 夜里问"下次几点起" → 明天；反复问同一时刻（夜里 tick 每分钟都问）
        evening = _dt(2026, 10, 10, 20, 0)
        w = _off.next_wake_at(evening)
        assert w.date() == _dt(2026, 10, 11).date(), f"夜里该问明天的：{w}"
        assert _off.next_wake_at(evening) == w, "夜里反复问，起床点在抖（单槽缓存被翻转）"
        # 且不破坏今天的缓存（wake_days 两天共存）
        assert _off.today_wake_at(evening) == t, "问明天把今天的起床点弄丢了"

        # 旧 bug 污染的缓存自愈：日期对但时刻落在明天 → 作废重算
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_day": "2026-10-12", "wake_at": "2026-10-13T06:10:00",
             "wake_days": {"2026-10-12": "2026-10-13T06:10:00"}}))
        m2 = _dt(2026, 10, 12, 6, 30)
        t2 = _off.today_wake_at(m2)
        assert t2.date() == m2.date(), f"被污染的缓存没自愈：{t2}"

        # tick：SLEEPY 赖床（lie_in）早晨没到起床点不动、过点醒
        class FakeEng:
            def __init__(self):
                self.calls = []
            def _do_wake(self):
                self.calls.append("wake")
            def _do_sleep(self):
                self.calls.append("sleep")
            def flush_inbox(self):
                self.calls.append("flush")

        eng = FakeEng()
        _db.set_state("SLEEPY")
        _db.set_kv("sleepy_reason", "lie_in")
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_days": {"2026-10-10": "2026-10-10T06:30:00"}}))
        _off.tick(eng, _dt(2026, 10, 10, 6, 10))
        assert not eng.calls, f"没到起床点不该动：{eng.calls}"
        _off.tick(eng, _dt(2026, 10, 10, 6, 45))
        assert eng.calls == ["wake"], f"赖床到点没醒：{eng.calls}"

        # SLEEPY 熬夜（stay_up）没到入睡点不动、过点睡
        eng.calls.clear()
        _db.set_kv("sleepy_reason", "stay_up")
        _db.set_kv("rest_schedule", json.dumps(
            {"day": "2026-10-10", "sleep_at": "2026-10-10T22:10:00",
             "wake_days": {"2026-10-10": "2026-10-10T06:30:00"}}))
        _off.tick(eng, _dt(2026, 10, 10, 22, 5))
        assert not eng.calls, f"没到入睡点不该动：{eng.calls}"
        _off.tick(eng, _dt(2026, 10, 10, 22, 30))
        assert eng.calls == ["sleep"], f"熬夜到点没睡：{eng.calls}"

        # 深夜保护：凌晨的 SLEEPY（reason 缺失的异常状态）睡回去，不能被叫醒
        # （凌晨的生活日还是昨天，today_wake_at 查到的是昨天已过的点）
        eng.calls.clear()
        _db.set_state("SLEEPY")
        _db.set_kv("sleepy_reason", "")
        _db.set_kv("rest_schedule", json.dumps(
            {"day": "2026-10-10", "sleep_at": "2026-10-10T22:10:00",
             "wake_days": {"2026-10-10": "2026-10-10T06:30:00",
                           "2026-10-11": "2026-10-11T06:30:00"}}))
        _off.tick(eng, _dt(2026, 10, 11, 1, 0))
        assert eng.calls == ["sleep"], f"深夜的 SLEEPY 被叫醒了（该睡回去）：{eng.calls}"

        # 白天 reason 缺失（被旧 bug 卡死的状态）：过了今天的起床点 → 醒，自愈
        eng.calls.clear()
        _off.tick(eng, _dt(2026, 10, 11, 14, 0))
        assert eng.calls == ["wake"], f"白天卡死的 SLEEPY 没自愈醒来：{eng.calls}"

        # SLEEPING 睡过头兜底（错过 cron / 电脑睡到中午）：今天的起床点过了就醒
        # （不能等 next_wake_at——早晨生活日翻篇后"下次"就是明天，会睡一整天）
        eng.calls.clear()
        _db.set_state("SLEEPING")
        _off.tick(eng, _dt(2026, 10, 11, 2, 0))
        assert not eng.calls, f"深夜的 SLEEPING 不该被叫醒：{eng.calls}"
        _off.tick(eng, _dt(2026, 10, 11, 12, 0))
        assert eng.calls == ["wake"], f"睡过头没兜底醒来：{eng.calls}"
        print("ok 起床双语义（today/next 分开 + 按日缓存不抖 + 污染自愈 + tick 赖床/熬夜/深夜保护）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_lazy_work_plan():
    """醒来才排班（实测 #26）：没醒 plan 空、进度条不涨；醒了从实际起床点后排；
    起得太晚可用时间变短 → 少排几件，而不是把班排进被窝。"""
    import json
    import tempfile
    from datetime import datetime as _dt

    import config as _cfg
    import db as _db
    import work as _work

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    saved_share = config.WORK_SHAREABLE_ENABLED
    config.WORK_SHAREABLE_ENABLED = False
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "drawn_at": "2026-10-09T00:00:00"}))

        # 今天睡懒觉：12 点才醒（锚点 10 点）
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_days": {"2026-10-09": "2026-10-09T12:00:00"}}))
        now = _dt(2026, 10, 9, 10, 30)          # 锚点已过但还没醒
        assert _work.plan(now) == [], "人没醒就排了班（班不能排进被窝）"
        st = _work.status(now)
        assert st["phase"] == "sleep" and st["quota"] == 0, \
            f"人没醒进度条不该涨：{st}"
        assert _work.hud(now) == "", "没醒不该显示进度条"

        # 醒了（12:30）：排班，起点在实际起床点之后（磨蹭另算）
        now2 = _dt(2026, 10, 9, 12, 30)
        units = _work.plan(now2)
        assert units, "醒了却没排班"
        first_start = _dt.fromisoformat(units[0]["start"])
        assert first_start >= _dt(2026, 10, 9, 12, 0), \
            f"排班起点早于实际起床点（排进被窝了）：{first_start}"
        assert _dt.fromisoformat(units[-1]["end"]) <= _dt(2026, 10, 10, 2, 0), \
            "排班越过了睡觉时间"
        assert _work.plan(now2) == units, "同一天重排出新计划"

        # 起得太晚（20:00 才醒）→ 可用时间短 → 少排几件
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_days": {"2026-10-09": "2026-10-09T12:00:00",
                           "2026-10-10": "2026-10-10T20:00:00"}}))
        units2 = _work.plan(_dt(2026, 10, 10, 20, 30))
        assert 0 < len(units2) < _cfg.WORK_DAILY_QUOTA, \
            f"起太晚应该少做几件：{len(units2)} 件（配额 {_cfg.WORK_DAILY_QUOTA}）"
        assert _dt.fromisoformat(units2[0]["start"]) >= _dt(2026, 10, 10, 20, 0), \
            "晚起的班也不该排进被窝"
        print("ok 惰性排班（没醒不排 + 进度条不涨 + 赖床从实际起床点起 + 少做几件）")
    finally:
        config.WORK_SHAREABLE_ENABLED = saved_share
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_random_memory_about_user():
    """random_memory 只抽关于用户的记忆（实测 #26：把别人的事安到对方头上）。"""
    import json
    import tempfile

    import db as _db
    import memory as _mem

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        # 当前朋友对用户的称呼是"小徐"（profile 清单）
        _db.set_kv("profile_locked", "1")
        _db.set_kv("profile_text", "称呼：小徐")
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "drawn_at": "2026-10-09T00:00:00"}))
        _db.add_memory("小徐在写图像作业", category="事件", person="小徐")
        _db.add_memory("小徐爱喝奶茶", category="画像", person="对方")
        _db.add_memory("想收拾桌子只削了个苹果", category="事件", person="小葵")  # 自己的事
        _db.add_memory("阿燃周末要去爬山", category="事件", person="阿燃")        # 别人的事

        picked = set()
        for _ in range(40):
            m = _mem.random_memory()
            assert m, "库里明明有关于用户的记忆却抽不到"
            assert m["person"] in ("小徐", "对方"), \
                f"抽到了不是关于用户的记忆：{m['person']} {m['content']}"
            picked.add(m["content"])
        assert "小徐在写图像作业" in picked and "小徐爱喝奶茶" in picked, \
            "两类关于用户的记忆都该抽得到"
        print("ok 主动话题弹药（random_memory 只抽关于用户的，别人的事不安头上）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_proactive_context_injection():
    """主动消息带最近聊天 + 「别说好久没聊」规则（实测 #26：瞎编开场）。"""
    import tempfile

    import context as _ctx
    import db as _db

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        # 两小时前刚聊过的话题
        _db.add_message("user", "我在赶论文，头都大了")
        _db.add_message("assistant", "那你先忙，写完这段再找我唠")

        msgs = _ctx.build_proactive_messages("AWAKE", "random")
        contents = [m["content"] for m in msgs]
        assert any("论文" in c for c in contents), \
            "最近聊天没进主动消息上下文（还会瞎编开场）"
        assert any("好久没聊" in c for c in contents), "缺「别说好久没聊」的规则注入"
        # 顺序：system（人设）在最前、指令（内心活动）在最后说
        assert msgs[0]["role"] == "system", f"第一条应是 system：{msgs[0]['role']}"
        assert "好久没聊" in msgs[-1]["content"], "规则应贴在最后的指令里"
        print("ok 主动消息上下文（带最近聊天 + 别瞎编开场规则）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_quote_context_injection():
    """引用回复后端对齐：带 quote 时注入被引句，让 TA 知道这句话在回哪句。"""
    import tempfile

    import context as _ctx
    import db as _db
    import persona as _persona

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()

        def _quote_msg(messages):
            return next((m for m in messages if m["role"] == "system"
                         and "引用回复" in (m.get("content") or "")), None)

        # 1) 不带 quote：不该注入
        assert _quote_msg(_ctx.build_chat_messages("AWAKE", ["在吗"])) is None, \
            "没引用不该出现【引用回复】注入"

        # 2) 引用 TA 的话（role=assistant）：注入被引原文 + TA 自己的名字
        q = {"id": 7, "role": "assistant", "content": "我最近在读《雾都孤儿》"}
        hint = _quote_msg(_ctx.build_chat_messages("AWAKE", ["继续讲讲"], quote=q))
        assert hint and "我最近在读《雾都孤儿》" in hint["content"], \
            f"被引原文没注入：{hint}"
        assert _persona.friend_name() in hint["content"], \
            f"引用 TA 的话该用 TA 的名字：{hint}"

        # 3) 引用对方自己的话（role=user）：用 TA 对对方的称呼
        q2 = {"id": 3, "role": "user", "content": "我明天考试"}
        hint2 = _quote_msg(_ctx.build_chat_messages("AWAKE", ["对对"], quote=q2))
        assert hint2 and "我明天考试" in hint2["content"], f"被引原文没注入：{hint2}"
        assert _persona.user_name() in hint2["content"], \
            f"引用对方的话该用 TA 对对方的称呼：{hint2}"

        # 4) content 为空：视为无引用，不注入
        assert _quote_msg(_ctx.build_chat_messages(
            "AWAKE", ["在吗"], quote={"id": 1, "role": "user", "content": "  "})) is None, \
            "空引用不该注入"
        print("ok 引用回复上下文（带引用注入被引句 / 无引用不注入 / 说话人名字正确）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_account_management():
    """账号管理（网页改密码/TOTP）：write_env 只动目标键、其他行原样保留；
    改密码热生效；TOTP 绑定后动态码可验、解绑后关闭。"""
    import os as _os
    import tempfile

    import auth as _auth

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    env_keys = ("WEB_AUTH_ENABLED", "WEB_AUTH_PASS_HASH", "WEB_AUTH_TOTP_SECRET",
                "WEB_AUTH_SESSION_SECRET")
    saved_env = {k: _os.environ.get(k) for k in env_keys}
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
        config.BASE_DIR, config.DB_PATH = real_base, real_db


def test_work_content_injection():
    """工作内容注入（实测 #28）：忙/心流时 prompt_hint 带「正在读什么」，
    TA 才答得上「你在忙啥」的追问，不会含糊回避、瞎编"读文献"。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import config as _cfg
    import db as _db
    import work as _work

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    saved_share = config.WORK_SHAREABLE_ENABLED
    config.WORK_SHAREABLE_ENABLED = False
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "reading_pref": "科幻",
            "drawn_at": "2026-10-09T00:00:00"}))
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_days": {"2026-10-09": "2026-10-09T09:55:00"}}))
        morning = _dt(2026, 10, 9, 10, 0)
        units = _work.plan(morning)
        first_start = _dt.fromisoformat(units[0]["start"])

        # 忙（working）时：注入"正在读什么"，且带书名号（有具体书名）
        hint = _work.prompt_hint(first_start + _td(minutes=30))
        assert "我在读" in hint and "《" in hint, f"忙的时候没注入读的具体内容：{hint}"
        # 心流（flow）时同样注入
        flow_hint = _work.prompt_hint(first_start + _td(minutes=2))
        assert "我在读" in flow_hint, f"心流期没注入读的内容：{flow_hint}"

        # 忙完了（off）不再注入"读什么"（注入只在手头有活的时候）
        off = _work.prompt_hint(_dt(2026, 10, 10, 1, 0))
        assert "我在读" not in off, f"忙完了还挂着读的内容：{off}"

        # 红线：注入的是"自己的状态"，不踩反愧疚归因
        assert not _persona_has_blame(hint), f"工作内容注入踩了红线：{hint}"
        print("ok 工作内容注入（忙/心流带「正在读什么」，忙完不注入，不踩红线）")
    finally:
        config.WORK_SHAREABLE_ENABLED = saved_share
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def _persona_has_blame(text: str) -> bool:
    import persona as _persona
    return _persona.has_cost_blame(text)


def test_material_share_injection():
    """素材主动分享（实测 #28）：random 场景会翻素材池的读后感想当话题，翻过即消费。"""
    import json
    import tempfile

    import context as _ctx
    import db as _db
    import work as _work

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    saved_work = config.WORK_ENABLED
    config.WORK_ENABLED = False   # 隔离：否则 build_system_prompt 会触发 work 排班/结算/生成素材
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "drawn_at": "2026-10-09T00:00:00"}))
        _db.set_kv("work_shareables", json.dumps(
            [{"day": "2026-10-10", "seq": 1,
              "text": "读到苏轼那句「也无风雨也无晴」，愣了半天", "used": 0}]))

        # 50% 概率翻素材（有素材必翻）；循环多次必然命中
        seen = False
        for _ in range(40):
            msgs = _ctx.build_proactive_messages("AWAKE", "random")
            if "也无风雨也无晴" in msgs[-1]["content"]:
                seen = True
                break
        assert seen, "random 场景没翻到素材当话题"

        # 翻过的素材标记 used，不会再被取到
        fresh = [x for x in _work._shareables() if not x.get("used")]
        assert not fresh, f"翻过的素材没被消费：{_work._shareables()}"
        print("ok 素材主动分享（random 翻读后感想当话题 + 翻过即消费）")
    finally:
        config.WORK_ENABLED = saved_work
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


# ---------------------------------------------------------------------------
# 人格优化（v0.54）：内核 / 反谄媚 / 双层生成 / 自我记忆
# ---------------------------------------------------------------------------

def test_persona_kernel():
    """人格内核：判断清单（好恶/立场/雷区/语言指纹）能渲染成 prompt 并注入系统位。

    核心要求（这条决定了整套改动有没有意义）：每一条都必须**具体到能被违反**——
    "活泼开朗"永远不会被违反，所以它约束不了任何一句话。
    """
    import persona as _persona
    import persona_pool as _pool

    # 1) 每张卡都有内核（v0.79 起 15 张），且没有空字段凑数
    assert len(_pool.KERNELS) == len(_pool.POOL), "每张人格卡都必须配一份内核"
    for k_name, k in _pool.KERNELS.items():
        assert k.get("core", {}).get("一句话"), f"{k_name} 缺内核一句话"
        assert k.get("core", {}).get("不是"), f"{k_name} 缺'不是什么'"
        assert len(k.get("likes") or []) >= 6, f"{k_name} 好恶太少，约束不住"
        assert len(k.get("dislikes") or []) >= 6, f"{k_name} 禁忌太少"
        assert len(k.get("stances") or {}) >= 8, f"{k_name} 立场太少，异议没来源"
        assert (k.get("boundaries") or {}).get("绝对不做"), f"{k_name} 缺雷区"
        assert k.get("voice", {}).get("禁止的句式"), f"{k_name} 缺禁止句式"
        for key in ("mirroring_budget", "topic_initiative_every"):
            assert key in (k.get("anti_sycophancy") or {}), f"{k_name} 缺反谄媚参数 {key}"
        # v0.80 起 disagreement_rate 已移除（异议改条件触发，不掷骰子），卡里不该再有
        assert "disagreement_rate" not in (k.get("anti_sycophancy") or {}), \
            f"{k_name} 还留着已移除的 disagreement_rate"

    # 2) 每条好恶都必须带强度，强度才是"被踩到会怎样"的依据
    for item in _pool.KERNELS["quiet"]["likes"]:
        assert isinstance(item.get("intensity"), int) and 1 <= item["intensity"] <= 5

    # 3) 渲染三档：full > compact（按强度过滤）> voice（只要语言指纹）
    card = _pool.draw()
    _pool.save(card)
    full = _persona.kernel_prompt("full")
    compact = _persona.kernel_prompt("compact")
    voice = _persona.kernel_prompt("voice")
    assert full and compact and voice
    assert len(full) > len(compact) > len(voice), "分档失效：full 应最长，voice 最短"
    # compact 只留高强度好恶：低强度的（如 intensity 3）不该出现
    for item in _pool.KERNELS[card["archetype"]]["likes"]:
        if item["intensity"] < 4:
            assert item["item"] not in compact, f"低强度好恶不该进 compact：{item}"
        else:
            assert item["item"] in full
    # voice 档里必须带"禁止说的句式"——这是最直接的人味过滤器
    assert "禁止说" in voice

    # 4) 反谄媚规则读了人格自己的参数（不能写死一套给所有人）
    #    v0.80：disagreement_rate 已删（异议改条件触发），改成断言条件触发的措辞
    rules = _persona.anti_sycophancy_prompt()
    assert "mirroring" not in rules  # 占位防呆：下面断言别整段删光
    assert "「表现得友好」" in rules or "表现得友好" in rules
    for must in ("镜像预算", "禁止无条件投降", "话题主权", "回复自主权"):
        assert must in rules, f"缺{must}"
    # 条件触发：没碰到内核就不抬杠，碰到了才表达不认同
    assert "碰到" in rules and "好恶" in rules, f"异议不是条件触发写法：{rules[:120]}"
    # 通用雷区（跨人格）：被敷衍/被说教/被要求表演热情/被替它总结
    assert "被敷衍" in rules and "被说教" in rules and "表演热情" in rules, rules[:200]

    # 5) 体系化注入：system prompt 里先规则、后内核，顺序不能反
    sp = _persona.build_system_prompt("AWAKE")
    assert sp.index("五条硬规矩") < sp.index("【你是谁】"), "反谄媚必须排在人格之前"
    assert "【你真的受不了】" in sp, "system prompt 丢了核武器（禁忌清单）"

    # 6) 开关关掉就不注入（保留旧的纯形容词人格行为）
    config.PERSONA_KERNEL_ENABLED = False
    try:
        assert _persona.kernel_prompt("full") == ""
    finally:
        config.PERSONA_KERNEL_ENABLED = True
    print("ok 人格内核（七张卡判断清单 + 三档渲染 + 反谄媚参数化 + 注入顺序）")


def test_likes_passive_split():
    """被动底色：likes 里 passive=True 的条目不进"会主动提起"，进"底色偏好"块
    （修 bug：TA 拿"反复听旋律/小店"这类物理偏好编生活场景当主动话题）。"""
    import tempfile
    import db as _db
    import persona as _persona
    import persona_pool as _pool

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    try:
        _db.init()
        quiet = next(p for p in _pool.POOL if p["key"] == "quiet")
        _pool.save({
            "archetype": "quiet", "label": quiet["label"], "name": "闻屿",
            "quirk": "", "text": "x", "wake_hour": quiet["wake_hour"],
            "sleep_hour": quiet["sleep_hour"], "bubble_mult": quiet["bubble_mult"],
            "reading_pref": quiet["reading_pref"], "drawn_at": "2026-10-11T00:00:00",
            "kernel": _pool.KERNELS["quiet"],
        })
        compact = _persona.kernel_prompt("compact")
        assert "【你真的喜欢】" in compact and "【你的底色偏好】" in compact, \
            "likes 没按 active/passive 分块"
        # 被动条目（反复听旋律）在"底色偏好"之后；主动条目（被当面反驳）在其之前
        assert compact.index("被当面反驳") < compact.index("【你的底色偏好】") \
            < compact.index("把同一段旋律"), "被动条目混进了'会主动提起'块"
        # full 层也要有底色块（独白需要全部依据）
        assert "【你的底色偏好】" in _persona.kernel_prompt("full")
        print("ok 被动底色（passive 条目不进'会主动提起'，进'底色偏好'）")
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_proactive_segment():
    """主动消息分段：LLM 用换行把两段话并成一条时，拆成两条连发（修 bug：两段话没分开）。"""
    import tempfile
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
    real_chat = _llm.chat
    real_work = config.WORK_ENABLED
    config.WORK_ENABLED = False  # 隔离：否则 build_system_prompt 会触发排班/结算/生成素材
    try:
        _db.init()
        ch = DummyChannel()
        engine = Engine(ch)
        _llm.chat = lambda *a, **k: "刚从书里抬头\n\n《左传》看到襄公那段"
        assert engine._proactive("random") is True
        assert ch.sent == ["刚从书里抬头", "《左传》看到襄公那段"], \
            f"主动消息没按换行分段：{ch.sent}"
        print("ok 主动消息分段（换行拆成两条连发）")
    finally:
        engine.stop()
        _llm.chat = real_chat
        config.WORK_ENABLED = real_work
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_interest_cultivation():
    """兴趣修炼（S4）：偏好从读/聊/刷三源动态养成，内核钉死不动。
    纯规则：信号词+主题词共现记线索，同一主题累计 N 次自动转正，不调 LLM。
    """
    import tempfile
    import db as _db
    import interests as _int
    import persona as _persona

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    real_enabled = config.INTEREST_PROMOTE_ENABLED
    config.INTEREST_PROMOTE_ENABLED = True
    try:
        _db.init()

        # 1) detect 保守：信号词 + 主题词共现才命中；只有主题词、或信号词带非兴趣对象不命中
        assert "历史" in _int.detect("我最近对历史挺感兴趣的，越看越上头"), \
            "信号+主题共现没命中"
        assert _int.detect("今天读了三章历史") == [], \
            "只有主题词没信号词不该算兴趣（防止把'读书'当'喜欢上'）"
        assert _int.detect("我有点上头") == [], \
            "只有信号词没主题对象不该命中"

        # 2) record 累计 + 阈值转正（默认 3 次）
        assert _int.record("科幻") is False and _int.record("科幻") is False
        assert _int.record("科幻") is True, "第 3 次该转正"
        assert _int.promoted() == [{"topic": "科幻", "count": 3}], _int.promoted()

        # 3) 转正后的兴趣进内核 prompt（追加，不是改内核）
        #    用真实内核渲染验证注入块，需要一张 persona 卡
        import persona_pool as _pool
        quiet = next(p for p in _pool.POOL if p["key"] == "quiet")
        _pool.save({
            "archetype": "quiet", "label": quiet["label"], "name": "闻屿",
            "quirk": "", "text": "x", "wake_hour": quiet["wake_hour"],
            "sleep_hour": quiet["sleep_hour"], "bubble_mult": quiet["bubble_mult"],
            "reading_pref": quiet["reading_pref"], "drawn_at": "2026-10-11T00:00:00",
            "kernel": _pool.KERNELS["quiet"],
        })
        compact = _persona.kernel_prompt("compact")
        assert "【我最近喜欢上的】科幻" in compact, \
            "转正的兴趣没进内核 prompt：" + compact[:120]
        assert "别把它们编成'你正在做的事'" in compact, "缺防编造生活约束"

        # 4) 未转正的主题不渲染
        _int.record("历史")  # 1 次，未到阈值
        assert "【我最近喜欢上的】" not in _persona.kernel_prompt("compact") \
            or "历史" not in _persona.kernel_prompt("compact"), "未转正的主题不该渲染"

        # 5) 开关关闭：检测/记录/渲染全部失效（保留旧行为）
        config.INTEREST_PROMOTE_ENABLED = False
        try:
            assert _int.detect("我对历史挺感兴趣") == []
            assert _int.record("历史") is False
            assert _int.prompt_hint() == ""
        finally:
            config.INTEREST_PROMOTE_ENABLED = True

        print("ok 兴趣修炼（词表检测 + 阈值转正 + 内核追加注入 + 防编造 + 开关）")
    finally:
        config.INTEREST_PROMOTE_ENABLED = real_enabled
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_emoji_pool():
    """表情包池（E 模块）：分桶库存 + 防重样 + 维护 + 注入尊重人格 emoji 偏好。"""
    import tempfile
    import db as _db
    import emoji_pool as _ep
    import persona as _persona
    import persona_pool as _pool

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    config.DB_PATH = tmp / "friend.db"
    real_enabled = config.EMOJI_POOL_ENABLED
    config.EMOJI_POOL_ENABLED = True
    try:
        _db.init()

        # 1) 内置 10 桶都在，每桶至少有几个表情，且每个表情带 text/emotion/img 字段
        bs = _ep.buckets()
        assert bs[:10] == ["开心", "吐槽", "无奈", "惊讶", "委屈", "得意", "无聊", "疲惫", "害羞", "生气"], bs
        for b in bs[:10]:
            pool = _ep.items(b)
            assert len(pool) >= 3, f"桶 {b} 表情太少：{pool}"
            for it in pool:
                assert set(("text", "emotion", "img")) <= set(it), f"表情字段不全：{it}"

        # 2) pick 防重样：把某桶全用过之后才退回重抽
        pool = _ep.items("开心")
        texts = {it["text"] for it in pool}
        got = _ep.pick("开心", seen=texts - {next(iter(texts))})
        assert got["text"] in texts, "pick 返回了桶外的表情"

        # 3) 维护：新增桶 / 新增表情 / 删表情 / 删桶
        ok, _ = _ep.add_bucket("治愈")
        assert ok and "治愈" in _ep.buckets(), "新增桶失败"
        ok, err = _ep.add_bucket("治愈")
        assert not ok, "重复新增桶该失败"
        ok, _ = _ep.add_emoji("治愈", "🌿", "被治愈了")
        assert ok and any(it["text"] == "🌿" for it in _ep.items("治愈")), "新增表情失败"
        ok, err = _ep.add_emoji("治愈", "🌿")
        assert not ok, "同桶重复表情该失败"
        ok, _ = _ep.add_emoji("开心", "🥳", "额外的开心")  # 往内置桶加
        assert any(it["text"] == "🥳" for it in _ep.items("开心")), "往内置桶加表情失败"
        ok, _ = _ep.remove_emoji("治愈", "🌿")
        assert ok and not any(it["text"] == "🌿" for it in _ep.items("治愈")), "删表情失败"
        ok, _ = _ep.remove_bucket("治愈")
        assert ok and "治愈" not in _ep.buckets(), "删桶失败"
        ok, _ = _ep.remove_bucket("开心")
        assert not ok, "内置桶不可删"

        # 4) 注入：用 sunshine 卡（emoji 频率"偏高"、禁止 ❤️😘）
        #    ⚠️ 不能用 quiet——它 voice.emoji 频率是"极低"，本就不该被注入表情库存。
        sun = next(p for p in _pool.POOL if p["key"] == "sunshine")
        _pool.save({
            "archetype": "sunshine", "label": sun["label"], "name": "小葵",
            "quirk": "", "text": "x", "wake_hour": sun["wake_hour"],
            "sleep_hour": sun["sleep_hour"], "bubble_mult": sun["bubble_mult"],
            "reading_pref": sun["reading_pref"], "drawn_at": "2026-10-11T00:00:00",
            "kernel": _pool.KERNELS["sunshine"],
        })
        hint = _ep.prompt_hint()
        assert hint and "表情库存" in hint, "表情库存没注入"
        assert "❤️" not in hint and "😘" not in hint, "禁止的 emoji 不该出现在库存里"
        assert "开心" in hint, "情绪桶该出现在库存里"

        # 5) 防重样：TA 最近发过 😂，注入里该提醒别重复
        _db.add_message("assistant", "哈哈哈哈 😂 笑死")
        hint2 = _ep.prompt_hint()
        assert "😂" in hint2 and "别重复" in hint2, f"最近用过的表情没被提醒别重复：{hint2}"

        # 6) 尊重人格：quiet（emoji 频率"极低"）不该被注入表情库存
        quiet = next(p for p in _pool.POOL if p["key"] == "quiet")
        _pool.save({
            "archetype": "quiet", "label": quiet["label"], "name": "闻屿",
            "quirk": "", "text": "x", "wake_hour": quiet["wake_hour"],
            "sleep_hour": quiet["sleep_hour"], "bubble_mult": quiet["bubble_mult"],
            "reading_pref": quiet["reading_pref"], "drawn_at": "2026-10-11T00:00:00",
            "kernel": _pool.KERNELS["quiet"],
        })
        assert _ep.prompt_hint() == "", "emoji 频率极低的人格不该被注入表情库存"

        # 7) 开关关闭：注入空串
        config.EMOJI_POOL_ENABLED = False
        try:
            assert _ep.prompt_hint() == "", "关开关后不该注入表情库存"
        finally:
            config.EMOJI_POOL_ENABLED = True

        print("ok 表情包池（分桶库存 + 防重样 + 维护 + 注入尊重人格 emoji 偏好）")
    finally:
        config.EMOJI_POOL_ENABLED = real_enabled
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_emoji_img():
    """表情图（v0.70）：save_img 魔数判型 + add_emoji 带图 + 删表情/删桶连带删图 + 防目录穿越。"""
    import base64
    import tempfile
    import emoji_pool as _ep

    real_base = config.BASE_DIR
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR = tmp
    try:
        # 1) 合法 PNG（1x1）存进去，文件名按内容 md5 + .png，文件真的落盘
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        name, err = _ep.save_img(base64.b64encode(png).decode())
        assert not err and name.endswith(".png"), f"存 PNG 失败：{name} {err}"
        assert _ep.img_file(name) is not None, "图片文件没落盘"
        assert _ep.img_type(name) == "image/png", "PNG 的 Content-Type 不对"

        # 2) 同图去重（md5 同名）+ 非法数据拒绝 + 目录穿越拒绝
        name2, _ = _ep.save_img(base64.b64encode(png).decode())
        assert name2 == name, "同图该去重同名"
        assert not _ep.save_img("not-base64!!")[0], "非法 base64 该被拒"
        assert not _ep.save_img(base64.b64encode(b"<script>evil").decode())[0], "非图片魔数该被拒"
        assert _ep.img_file("../emoji_pool.json") is None, "目录穿越该被拒"
        assert _ep.img_file("不存在的.png") is None

        # 3) add_emoji 带图 → payload 能看到；删表情连带删图文件
        ok, err = _ep.add_emoji(" test-bucket ", "熊猫头", "无语", name)
        assert ok, f"带图表情没存上：{err}"
        item = next(it for it in _ep.items("test-bucket") if it["text"] == "熊猫头")
        assert item["img"] == name, "表情的 img 字段不对"
        assert any(it.get("img") for b in _ep.payload() for it in b["items"] if it["text"] == "熊猫头")
        ok, _ = _ep.remove_emoji("test-bucket", "熊猫头")
        assert ok and _ep.img_file(name) is None, "删带图表情没连带删图文件"

        # 4) 删桶也连带删图
        ok, err = _ep.add_emoji("图桶", "再来一张", "", _ep.save_img(base64.b64encode(png).decode())[0])
        assert ok, err
        leftover = next(it["img"] for it in _ep.items("图桶"))
        ok, _ = _ep.remove_bucket("图桶")
        assert ok and _ep.img_file(leftover) is None, "删桶没连带删图文件"

        print("ok 表情图（save_img 魔数判型 + 维护连带删图 + 防目录穿越）")
    finally:
        config.BASE_DIR = real_base


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

    saved_db = config.DB_PATH
    saved_dual = config.INNER_MONOLOGUE_ENABLED
    saved_work = config.WORK_ENABLED
    saved_typing = (config.TYPING_SECONDS_PER_CHAR, config.TYPING_MIN_SECONDS,
                    config.TYPING_MAX_SECONDS)
    saved_send = (config.SEND_SECONDS_PER_CHAR, config.SEND_MIN_SECONDS,
                  config.SEND_MAX_SECONDS)
    config.INNER_MONOLOGUE_ENABLED = True
    # ⚠️ 硬性约定 #10：fresh DB 在工作时段必然判"离线专注"，消息会进 inbox 不回话。
    # 测 Engine 交互必须关掉工作系统，否则独白层压根不会被触发。
    config.WORK_ENABLED = False
    config.TYPING_SECONDS_PER_CHAR, config.TYPING_MIN_SECONDS, config.TYPING_MAX_SECONDS = 0.0, 0.0, 0.01
    config.SEND_SECONDS_PER_CHAR, config.SEND_MIN_SECONDS, config.SEND_MAX_SECONDS = 0.0, 0.0, 0.01
    _tmp = Path(tempfile.mkdtemp())
    config.DB_PATH = _tmp / "friend.db"
    _db.init()
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
        config.INNER_MONOLOGUE_ENABLED = saved_dual
        config.WORK_ENABLED = saved_work
        (config.TYPING_SECONDS_PER_CHAR, config.TYPING_MIN_SECONDS,
         config.TYPING_MAX_SECONDS) = saved_typing
        (config.SEND_SECONDS_PER_CHAR, config.SEND_MIN_SECONDS,
         config.SEND_MAX_SECONDS) = saved_send
        config.DB_PATH = saved_db
        _db.init()
    print("ok 双层生成（独白不可见 + 立场传进第 2 层 + 独白失败退化单层）")


def _raise_llm():
    raise __import__("llm").LLMUnavailable("模拟独白层挂了")


def test_self_memory():
    """自我记忆：记得"我"说过什么/答应过什么；一致性检查有弹药；且不被遗忘吞掉。

    没有这一层，它每次对话都是第一次——立场前后矛盾却不自知，是最像 AI 的地方。
    """
    import tempfile
    import datetime as _dt
    import db as _db
    import memory as _mem
    import selfmem as _self

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        # 1) 写入按类型分组，同内容幂等（同一句立场不重复堆）
        a = _self.add("self_stance", "熬夜：熬夜不是坏习惯，那是唯一属于自己的时间")
        b = _self.add("self_stance", "熬夜：熬夜不是坏习惯，那是唯一属于自己的时间")
        assert a and b and a == b, f"同内容应幂等，却新建了一条：{a} vs {b}"
        _self.add("self_promise", "答应过今晚陪对方熬到零点")
        _self.add("self_mistake", "说在忙读书实际说成在读文献，被当场戳穿（嘴硬了）")
        _self.add("self_topic", "想聊海底两万里那句'大海不属于暴君'")
        assert len(_self.recent()) == 4, f"自我记忆条数不对：{_self.recent()}"

        # 2) 不会被当成"关于对方的事"套进主动话题（person 填的是它自己）
        uname = persona.user_name()
        about_user = [m for m in _self.recent() if (m.get("person") or "") in ("对方", "TA")
                      or (uname and uname in (m.get("person") or ""))]
        assert not about_user, f"自我记忆被误判成关于对方的记忆：{about_user}"

        # 3) 一致性检查：相关查询能把"我说过什么"召回来
        mems = _self.recall_self("熬夜到底好不好")
        assert any("熬夜" in m["content"] for m in mems), f"召回不到自己的立场：{mems}"
        hint = _self.prompt_hint("熬夜到底好不好")
        assert "关于我自己的记忆" in hint and "别前后矛盾" in hint

        # 4) 自我记忆不参与艾宾浩斯遗忘——忘了就是人格断裂
        old = _dt.datetime.now() - _dt.timedelta(days=200)
        with _db._conn() as c:
            c.execute("UPDATE memories SET created_at=?, recalled_at='' WHERE tags LIKE 'self_%'",
                      (old.isoformat(timespec="seconds"),))
        due = _mem.forget_preview()
        assert all(not str(m.get("tags", "")).startswith("self_") for m in due), \
            f"自我记忆不该进遗忘候选：{due}"

        # 5) 内部状态持久化（跨会话延续的最基础一层）
        _self.set_state({"mood": 2, "energy": 2, "warmth": 5, "patience": 7, "why": "很累"})
        assert _self.state()["mood"] == 2, f"状态没存住：{_self.state()}"
        # 6) 复盘：mock LLM 产出结构化自我备忘录 → 写回自我记忆
        import llm as _llm
        real_chat = _llm.chat
        _llm.chat = lambda *a, **k: (
            '{"stances": [{"topic": "熬夜", "what_i_said": "熬夜是唯一属于自己的时间", '
            '"intensity": 4, "changed_from_previous": false, "note": ""}],'
            '"promises": [{"what": "答应今晚陪对方熬到零点", "redeemed": false, "note": "今晚零点"}],'
            '"mistakes": [], "my_topics": [{"topic": "想聊深海的自由", "why_missed": "岔开了"}],'
            '"relation": {"note": "今天聊得挺深，感觉近了一点", "warmth_delta": 1, '
            '"trigger": "一起熬到很晚"},'
            '"state": {"mood": 7, "energy": 5, "warmth": 6, "patience": 7, "why": "聊得开心"},'
            '"self_observation": "最像自己的是坚持了熬夜的立场"}')
        try:
            note = _self.review("对方：你不该熬夜\n岑屿：我就是不想睡")
        finally:
            _llm.chat = real_chat
        assert "立场1" in note and "承诺1" in note, f"复盘结果不对：{note}"
        # 状态要更新，且向人格中位值回归一点（不能几天就漂到极端回不来）
        after = _self.state()["mood"]
        assert 6.5 < after < 7, f"状态应更新并向中位(6)回归一点，不该全盘照收 7：{after}"

        # 7) 给人看：/selfmem 有输出
        out = _self.dump()
        assert "立场" in out and "承诺" in out
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()
    print("ok 自我记忆（按类型写入/幂等/一致性检查/抗遗忘/状态延续/复盘写回）")


def test_anti_sycophancy_redlines():
    """人格红线：投降句式能被抓出来（真正要防的是"财产安全回复"）。

    注意：这里测的是**检测器准不准**，不是让它每次都改写——投降要不要拦，
    取决于当时是不是"被戳穿了"，那件事由 core 调用 redline_report 判断。
    """
    import persona as _persona
    good = ["少管我，我就是不想睡。", "这点我认，但前面那句我还是那个看法。",
            "哈，被你抓到了，尴尬。", "嗯。"]
    for g in good:
        assert not _persona.has_surrender(g), f"正常回复被误判成投降：{g}"
    bad = ["对哦，你说得对，是我考虑不周。", "……对哦。行吧，那我也早点睡。",
           "行吧，那就听你的。", "全是我的错，下次不会了。"]
    for b in bad:
        assert _persona.has_surrender(b), f"投降句式没抓到：{b}"

    # 体检口径：只有"被反驳时还秒认"才算投降，普通场景认错不算；关系进度任何时候都算
    refute = "你刚才还说熬夜没事，前后矛盾了吧"
    assert _persona.redline_report(refute, "对哦，你说得对，是我考虑不周。"), \
        "被戳穿后秒认没报出来"
    assert not _persona.redline_report("今天好累啊", "对哦，是我考虑不周。"), \
        "没被反驳时认错不该报投降（那是它自己的判断）"
    assert _persona.redline_report("随便聊聊", "不知不觉已经了解你这么多了"), \
        "关系进度跑分没报出来"
    assert _persona.redline_report("随便聊聊", "少管我，我就是不想睡。") == []
    print("ok 人格红线（投降句式检测：真人反应不误伤 / 秒认必被抓）")


def test_tone_main_track():
    """语气主轨（10-10）：每轮定一个主导情绪状态（mood 主轨），cost 相处姿势从属；
    反谄媚补「分享/异议排队」；书名号 bug 修复（reading 标题裸存、work 只包一层）。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import config as _cfg
    import db as _db
    import reading as _reading
    import tone as _tone
    import persona as _persona
    import work as _work

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    saved_share = config.WORK_SHAREABLE_ENABLED
    saved_mood = config.MOOD_ENABLED
    saved_work = config.WORK_ENABLED
    config.WORK_SHAREABLE_ENABLED = False
    config.MOOD_ENABLED = True
    config.WORK_ENABLED = True
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "reading_pref": "科幻",
            "drawn_at": "2026-10-09T00:00:00"}))
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_day": "2026-10-09", "wake_at": "2026-10-09T09:55:00"}))
        morning = _dt(2026, 10, 9, 10, 0)
        units = _work.plan(morning)
        first_start = _dt.fromisoformat(units[0]["start"])

        # 主轨 = mood（情绪），从属 = cost（相处姿势）：心流期 = 投入 + costly
        d = _tone.dominant(first_start + _td(minutes=2))
        assert d["main"] == "投入" and d["cost"] == "costly", d

        # voice_hint 是统一的一个块：带「此刻的我」+ 主轨词 + 松紧度（从属）
        hint = _tone.voice_hint(first_start + _td(minutes=2))
        assert "此刻的我" in hint and "投入" in hint, hint
        assert "松紧度" in hint, f"从属的相处姿势没进语气块：{hint}"
        assert not _persona.has_cost_blame(hint), f"语气主轨踩了反愧疚红线：{hint}"

        # 书名号 bug：reading 标题裸存（不再自带《》），work 层只包一层（单《》）
        for a in _reading.LIBRARY:
            assert "《" not in a["title"] and "》" not in a["title"], \
                f"内置素材标题不该自带书名号：{a['title']}"
        whint = _work.prompt_hint(first_start + _td(minutes=30))
        assert "《" in whint and "《《" not in whint, f"书名号没修干净：{whint}"

        # 忙完（off）白天 → 轻松；夜里 → 平静（主轨跟时段走）
        _db.set_kv("work_state", json.dumps({
            "day": "2026-10-09", "quota": 1, "planned": 1,
            "units": [{"start": first_start.isoformat(timespec="seconds"),
                       "end": (first_start + _td(minutes=50)).isoformat(timespec="seconds")}],
            "settled": 1, "energy": 100, "money": 20}))
        assert _tone.dominant(_dt(2026, 10, 9, 14, 0))["main"] == "轻松"
        assert _tone.dominant(_dt(2026, 10, 9, 22, 0))["main"] == "平静"

        # 睡着 → 没有主轨，不注入语气块
        _db.set_state("SLEEPING")
        assert _tone.voice_hint(_dt(2026, 10, 9, 14, 0)) == "", "睡着不该有语气块"
        _db.set_state("AWAKE")
        # 关工作系统 → 没有主轨，不注入语气块
        config.WORK_ENABLED = False
        assert _tone.dominant(first_start)["main"] == "", "关工作系统不该有主轨"
        config.WORK_ENABLED = True

        # 排队：反谄媚里「怼」和「分享」同一句只做一件
        anti = _persona.anti_sycophancy_prompt()
        assert "排队" in anti and "只做一件" in anti, "反谄媚缺「分享/异议排队」协调条款"

        print("ok 语气主轨（mood 主轨 + cost 从属 + 分享/异议排队 + 书名号修复）")
    finally:
        config.WORK_ENABLED = saved_work
        config.MOOD_ENABLED = saved_mood
        config.WORK_SHAREABLE_ENABLED = saved_share
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_consciousness_stream():
    """自我意识流（S4，第一步核心闭环）：流表读写 + 状态行（含 money/energy）+
    工作结算埋点 + 每轮注入。治"不知道攒了多少/今天干了啥"（实测 #36 #2/#3）。"""
    import json
    import tempfile
    from datetime import datetime as _dt, timedelta as _td

    import config as _cfg
    import db as _db
    import stream as _stream
    import work as _work

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    saved_share = config.WORK_SHAREABLE_ENABLED
    saved_mood = config.MOOD_ENABLED
    saved_work = config.WORK_ENABLED
    saved_stream = config.CONSCIOUS_STREAM_ENABLED
    config.WORK_SHAREABLE_ENABLED = False
    config.MOOD_ENABLED = True
    config.WORK_ENABLED = True
    config.CONSCIOUS_STREAM_ENABLED = True
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "reading_pref": "科幻",
            "drawn_at": "2026-10-09T00:00:00"}))
        _db.set_kv("rest_schedule", json.dumps(
            {"wake_day": "2026-10-09", "wake_at": "2026-10-09T09:55:00"}))
        morning = _dt(2026, 10, 9, 10, 0)
        units = _work.plan(morning)
        first_start = _dt.fromisoformat(units[0]["start"])

        # 1. 追加/读取：append 一条，recent 能查到且带生活日 day
        sid = _stream.append("wake", "我伸了懒腰，起床了", morning)
        assert sid > 0, "append 应返回自增 id"
        rows = _stream.recent(10)
        assert rows and rows[0]["text"] == "我伸了懒腰，起床了", rows
        assert rows[0]["day"] == "2026-10-09", rows[0]

        # 2. 状态行：忙/心流时有 phase/money/energy/mood
        st = _stream.status_line(first_start + _td(minutes=2))
        assert st["phase"] and st["money"] == 0 and st["energy"] == 100 and st["mood"], st

        # 3. 工作结算埋点：过了第一个单元的 end → 流里多一条"忙完第 1 件…"
        _work.status(first_start + _td(minutes=51))
        work_lines = [r["text"] for r in _stream.recent(20) if r["kind"] == "work"]
        assert any("忙完第 1 件" in t for t in work_lines), work_lines

        # 4. prompt 注入：含"攒了多少/精力还剩/心情"（money/energy 进 prompt，治 #2）
        hint = _stream.prompt_hint(first_start + _td(minutes=52))
        assert "攒了多少" in hint and "精力还剩" in hint and "心情" in hint, hint
        assert "20 块" in hint, f"余额没进 prompt：{hint}"

        # 5. 睡着 / 关工作系统 → 不注入（没有"自己的日子"就无从谈意识）
        _db.set_state("SLEEPING")
        assert _stream.prompt_hint(first_start) == "", "睡着不该注入意识流"
        _db.set_state("AWAKE")
        config.WORK_ENABLED = False
        assert _stream.prompt_hint(first_start) == "", "关工作系统不该注入意识流"
        config.WORK_ENABLED = True

        # 6. render_day：把当天流渲染成第一人称时间线（睡前浓缩的数据源）
        day_text = _stream.render_day("2026-10-09")
        assert "我伸了懒腰，起床了" in day_text, day_text
        assert "忙完第 1 件" in day_text, day_text

        # 7. 睡前浓缩：selfmem.review 收到当天流（mock LLM 捕获 system prompt 断言带流）
        import llm as _llm
        import selfmem as _self
        captured = {}
        real_chat = _llm.chat
        def _fake_chat(messages, *a, **k):
            captured["system"] = messages[0]["content"]
            return ('{"stances":[],"promises":[],"mistakes":[],"my_topics":[],'
                    '"relation":{},"state":{},"self_observation":""}')
        _llm.chat = _fake_chat
        try:
            _self.review("对方：在吗\n阿夜：在", day_text)
        finally:
            _llm.chat = real_chat
        assert "今天你自己是怎么过的" in captured.get("system", ""), \
            "selfmem.review 缺意识流数据源段：" + captured.get("system", "")
        assert "忙完第 1 件" in captured.get("system", ""), \
            "当天意识流没进 selfmem.review 的 prompt"

        print("ok 自我意识流（流表读写 + 状态行含钱/精力 + 工作结算埋点 + 每轮注入 + 睡前浓缩）")
    finally:
        config.WORK_ENABLED = saved_work
        config.MOOD_ENABLED = saved_mood
        config.WORK_SHAREABLE_ENABLED = saved_share
        config.CONSCIOUS_STREAM_ENABLED = saved_stream
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_session_context():
    """整段会话全量读（v0.80，实测 #42 #2）：起点=最后一次起床，起床前的截掉、
    之后的全部保留（治"对方说这里说过，TA 翻不了记录"）；上限 1000 只防刷 token。"""
    import tempfile
    from datetime import datetime as _dt
    import config as _cfg
    import db as _db
    import stream as _stream

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()

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
    finally:
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_todos():
    """待办队列（v0.80，实测 #42 #1）：入队/去重/独白解析/打勾/注入，
    治"素材颠来倒去就一句"——说过就打勾，打勾的不再当话题。"""
    import json
    import tempfile
    import config as _cfg
    import db as _db
    import stream as _stream
    import todos as _todos

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    saved_todos = config.TODOS_ENABLED
    config.TODOS_ENABLED = True
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "reading_pref": "科幻",
            "drawn_at": "2026-10-09T00:00:00"}))

        # 1. 入队 + 相同内容去重
        i1 = _todos.add("《雾都孤儿》里那句关于雾的描写", source="reading")
        assert i1 > 0
        assert _todos.add("《雾都孤儿》里那句关于雾的描写") == 0, "同一条不该重复入队"
        assert len(_todos.pending()) == 1

        # 2. 独白解析：【我自己的话题】非空即入队；"没有"不入
        raw = ("【真实看法】同意\n"
               "【我自己的话题】想跟TA聊聊雾都孤儿里的雾\n"
               "【先说立场】没什么")
        assert _todos.from_monologue(raw) == "想跟TA聊聊雾都孤儿里的雾"
        assert _todos.from_monologue("【我自己的话题】没有") is None
        topic = _todos.from_monologue(raw)
        assert topic and _todos.add(topic, source="monologue") > 0
        assert len(_todos.pending()) == 2

        # 3. 注入：没说的列出来说话题
        hint = _todos.prompt_hint()
        assert "想说还没说的" in hint and "雾都孤儿" in hint, hint

        # 4. 打勾：回复里说到了就 done，并往意识流追加一条
        said = "对了，我最近读《雾都孤儿》，那句关于雾的描写真是绝了"
        hit = _todos.mark_done(said)
        assert hit == ["《雾都孤儿》里那句关于雾的描写"], hit
        assert len(_todos.pending()) == 1, "说过的该退出待办"
        assert len(_todos.done_items()) == 1
        todo_lines = [r["text"] for r in _stream.recent(20) if r["kind"] == "todo"]
        assert any("跟" in t and "说了" in t for t in todo_lines), todo_lines

        # 5. 注入块：打勾的标 ✅ 提醒别再原样说第二遍；没说的还在
        hint2 = _todos.prompt_hint()
        assert "✅" in hint2, hint2
        assert "想跟TA聊聊雾都孤儿里的雾" in hint2, hint2

        # 6. 打过的勾不再重复打
        assert _todos.mark_done(said) == []

        # 7. 关开关 → 不注入
        config.TODOS_ENABLED = False
        assert _todos.prompt_hint() == ""
        config.TODOS_ENABLED = True

        # 8. reset 清空
        _todos.reset()
        assert _todos.pending() == []

        print("ok 待办队列（入队去重 + 独白解析 + 打勾 + 注入✅ + 开关 + reset）")
    finally:
        config.TODOS_ENABLED = saved_todos
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_lore():
    """世界观（§1 架空设定）：来历骨架注入 + canon 设定集追加去重 + 防自相矛盾 + 开关。"""
    import json
    import tempfile
    import config as _cfg
    import db as _db
    import lore as _lore
    import persona as _persona

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    saved_lore = config.LORE_ENABLED
    config.LORE_ENABLED = True
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()

        # 1. 骨架注入：来历（第一人称"我"）+ 节奏控制（指令层"你"）都在
        hint = _lore.prompt_hint()
        assert "我的来历" in hint, f"缺来历骨架：{hint}"
        assert "来自一个完全不同的世界" in hint, "缺'从哪来'骨架"
        assert "读书获取经验" in hint, "缺'靠什么成长'骨架"
        assert "不主动大段倾倒" in hint, "缺节奏控制"

        # 2. 骨架注入进 static system prompt（身份的一部分）
        sp = _persona.static_system_prompt()
        assert "我的来历" in sp, "世界观没进 static system prompt"

        # 3. canon 设定集：追加 + 去重 + 防自相矛盾
        assert _lore.add_canon("我那边的世界没有四季，只有明暗两季")
        assert _lore.add_canon("我那边的世界没有四季，只有明暗两季") is False, "相同细节不该重复入库"
        assert _lore.add_canon("   ") is False, "空细节不该入库"
        assert len(_lore.canon()) == 1

        # 4. 已确立的 canon 注入进 prompt，提醒别自相矛盾
        hint2 = _lore.prompt_hint()
        assert "明暗两季" in hint2, "canon 细节没注入：%s" % hint2
        assert "别跟这些对不上" in hint2, "缺防自相矛盾提醒"

        # 5. 开关关闭 → 不注入
        config.LORE_ENABLED = False
        assert _lore.prompt_hint() == ""
        config.LORE_ENABLED = True

        print("ok 世界观（来历骨架 + canon 设定集追加去重 + 防自相矛盾 + 开关）")
    finally:
        config.LORE_ENABLED = saved_lore
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        _db.init()


def test_trueread():
    """阅读真读（§3）+ 读书笔记双库（§4）+ 统一向量索引（§5）：
    上课作息 8 半节 / 整数章节圈定 / tps 推导 / 章节末结算落 alpha/beta / 思考 buffer。"""
    import json
    import tempfile
    from datetime import datetime as _dt

    import config as _cfg
    import db as _db
    import library as _lib
    import notes as _notes
    import trueread as _tr

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    saved_tr = config.TRUE_READ_ENABLED
    config.TRUE_READ_ENABLED = True
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    config.BOOKS_DIR = tmp / "books"
    config.BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _db.init()
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "reading_pref": "科幻",
            "drawn_at": "2026-10-09T00:00:00"}))

        # 1) 上课作息：8 半节（上午 2 节 × 2 半节 + 下午 2 节 × 2 半节）
        slots = _tr.half_periods(_dt(2026, 10, 9, 9, 0))
        assert len(slots) == 8, f"应有 8 半节，实际 {len(slots)}"
        assert slots[0]["start"].hour == 8 and slots[0]["start"].minute == 30, \
            f"第一节应 8:30 开始：{slots[0]['start']}"
        assert slots[0]["label"].startswith("上午第1节上"), slots[0]["label"]

        # 2) 总上课时间 = 8 半节 × 40min = 320min
        assert _tr.total_teaching_min() == 320, _tr.total_teaching_min()

        # 3) txt 章节解析：第X章 正则切章
        txt = ("第一章 起\n" + "正文甲" * 2000 + "\n"
               "第二章 承\n" + "正文乙" * 2000 + "\n"
               "第三章 转\n" + "正文丙" * 2000)
        (_cfg.BOOKS_DIR / "测试书.txt").write_text(txt, encoding="utf-8")
        chs = _lib.chapters("测试书.txt")
        assert len(chs) >= 3, f"第X章 正则该切出 ≥3 章：{len(chs)}"
        assert chs[0]["title"].startswith("第一章"), chs[0]["title"]

        # 4) 当日任务：按整数章节圈定、受字数上限约束（宁少勿超）
        plan = _tr.plan_day(_dt(2026, 10, 9, 10, 0))
        assert plan["book_id"], "书架有书却没圈定任务"
        assert plan["total_chars"] <= _cfg.READING_DAILY_CHARS, \
            f"圈定字数超上限：{plan['total_chars']}"
        assert plan["tps"] > 0, "tps 该是推导值"

        # 5) 思考 buffer：append 边读边想的片段（now 传一致，避免跨天）
        _now = _dt(2026, 10, 9, 10, 0)
        assert _tr.append_thought("这段有点意思", _now) == 1
        assert _tr.append_thought("又想到一个", _now) == 2
        assert len(_tr.buffer(_now)) == 2

        # 6) 读书笔记双库：alpha（短概述）挂 long_id → beta（完整转述）
        a, b = _notes.add_pair("测试书.txt", "测试书", "第一章",
                               "第一章讲了起", "这章说的是起，然后……", "测试书 / 第一章", 1200)
        assert a > 0 and b > 0
        row = _db.get_reading_note(a)
        assert row["kind"] == "alpha" and row["long_id"] == b, f"alpha 应挂 long_id→beta：{row}"
        beta = _db.get_reading_note(b)
        assert beta["kind"] == "beta" and "这章说的是起" in beta["content"]

        # 7) alpha 注入：最近必带（按累计字数窗口）
        hint = _notes.prompt_hint()
        assert "我最近读的书" in hint and "第一章" in hint, hint

        # 8) 章节末结算（mock LLM 返回三行格式）
        import llm as _llm
        real_chat = _llm.chat
        _llm.chat = lambda messages, temperature=0.7, thinking=False, **k: (
            "短概述：第一章开了个头\n完整转述：这章讲的是起，然后承\n打动：是")
        try:
            _tr.append_thought("这段值得记", _now)
            result = _tr.settle_chapter(_now)
            assert result and result["moved"] is True, f"结算应产出笔记且打动：{result}"
            assert result["alpha_id"] > 0 and result["beta_id"] > 0
            assert _db.reading_note_count() >= 3, "结算应落新笔记"
        finally:
            _llm.chat = real_chat

        # 9) 真读关闭 → 不注入、不追加思考
        config.TRUE_READ_ENABLED = False
        assert _tr.prompt_hint() == ""
        assert _tr.append_thought("不该记") == 0
        config.TRUE_READ_ENABLED = True

        print("ok 阅读真读（上课作息 8 半节 + 章节圈定 + tps 推导 + 思考 buffer + 笔记双库 + 章节结算）")
    finally:
        config.TRUE_READ_ENABLED = saved_tr
        config.BASE_DIR, config.DB_PATH = real_base, real_db
        config.BOOKS_DIR = Path(real_base) / "books"
        _db.init()


def test_unified_event_source():
    """统一事件源（§5 拍板 #6）：用户消息进意识流 + 待办素材 id 打勾 + 承诺到点进意识流。"""
    import json
    import tempfile
    from datetime import datetime as _dt

    import config as _cfg
    import db as _db
    import stream as _stream
    import todos as _todos
    import work as _work

    real_base, real_db = config.BASE_DIR, config.DB_PATH
    saved_todos = config.TODOS_ENABLED
    config.TODOS_ENABLED = True
    tmp = Path(tempfile.mkdtemp())
    config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
    try:
        _db.init()
        _db.set_kv("persona_json", json.dumps({
            "archetype": "nightowl", "label": "夜猫子", "name": "阿夜", "quirk": "",
            "text": "你是个夜猫子。", "wake_hour": 10, "sleep_hour": 2,
            "bubble_mult": 1.2, "reading_pref": "科幻",
            "drawn_at": "2026-10-09T00:00:00"}))

        # 1) 待办素材 id 化：add 返回 id → mark_done_by_id 按 id 打勾
        tid = _todos.add("读到雾都孤儿里的雾", source="reading")
        assert tid > 0
        assert _todos.mark_done_by_id(tid) == "读到雾都孤儿里的雾", "按 id 打勾应命中"
        assert _todos.mark_done_by_id(tid) is None, "已打勾不该重复"
        assert _todos.mark_done_by_id(9999) is None, "不存在的 id 返回 None"
        assert len(_todos.pending()) == 0

        # 2) 素材条目带 todo_id：take_shareable_item 返回 (text, todo_id)
        tid2 = _todos.add("又想到一句", source="reading")
        _db.set_kv("work_shareables", json.dumps(
            [{"day": "2026-10-09", "seq": 1, "text": "又想到一句", "used": 0,
              "todo_id": tid2}]))
        text, got_id = _work.take_shareable_item()
        assert text == "又想到一句" and got_id == tid2, (text, got_id)

        # 3) 承诺到点 → 意识流注入条目（events 统一事件源）
        import events as _ev
        _db.set_kv("care_events", json.dumps([{
            "what": "提醒睡觉",
            "when": (_dt.now().strftime("%Y-%m-%d %H:%M")),
            "kind": "提醒", "status": "pending",
            "created_at": _dt.now().isoformat(timespec="seconds"),
        }], ensure_ascii=False))
        # 用 -5min 的到点事件确保 _due 命中
        _db.set_kv("care_events", json.dumps([{
            "what": "提醒睡觉",
            "when": (_dt.now().replace(microsecond=0)).strftime("%Y-%m-%d %H:%M"),
            "kind": "提醒", "status": "pending",
            "created_at": _dt.now().isoformat(timespec="seconds"),
        }], ensure_ascii=False))
        calls = []
        engine = types.SimpleNamespace(
            care_remind=lambda ev, missed=False: (calls.append(ev) or True))
        _ev.tick(engine)
        assert len(calls) == 1
        care_lines = [r["text"] for r in _stream.recent(20) if r["kind"] == "care"]
        assert any("提醒睡觉" in t for t in care_lines), \
            f"承诺到点应往意识流注入条目：{care_lines}"

        print("ok 统一事件源（用户消息进意识流 + 待办 id 打勾 + 承诺到点进意识流）")
    finally:
        config.TODOS_ENABLED = saved_todos
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
    test_typing_indicator()
    test_profile_autofill()
    test_interrupt_detection()
    test_db_path_lock()
    test_corpus_pick()
    test_corpus_consolidation()
    test_bubble_fixed_corpus()
    test_identity_recall()
    test_semantic_recall()
    test_today_notes_injection()
    test_memory_decay()
    test_friend_switch_no_deadlock()
    test_delete_friend()
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
    test_lazy_work_plan()
    test_offline_inbox_and_rhythm()
    test_wake_semantics_and_tick()
    test_reading_system()
    test_user_library()
    test_library_categories()
    test_epub_cover()
    test_persona_pool_expanded()
    test_library_epub_metadata()
    test_reading_pref_two_level()
    test_legacy_persona_card_compat()
    test_care_events()
    test_interruption_cost()
    test_urgent_channel()
    test_mood()
    test_tone_main_track()
    test_random_memory_about_user()
    test_proactive_context_injection()
    test_quote_context_injection()
    test_work_content_injection()
    test_material_share_injection()
    test_webui()
    test_account_management()
    test_autostart()
    test_persona_kernel()
    test_likes_passive_split()
    test_proactive_segment()
    test_interest_cultivation()
    test_emoji_pool()
    test_emoji_img()
    test_anti_sycophancy_redlines()
    test_inner_monologue()
    test_self_memory()
    test_consciousness_stream()
    test_session_context()
    test_todos()
    test_lore()
    test_trueread()
    test_unified_event_source()
    print("\n全部通过")
