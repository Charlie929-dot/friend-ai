"""测试公共机制（E4 拆分，v0.92）。

任何新的测试文件都 `from _test_common import *`，即可拿到本文件里的
**两段全局机制**（约定 #10/#11 要求）：
  ① 临时目录收口到固定 `.test-tmp/`（启动即清 + 退出清）；某些环境 %TEMP% 不可写会
     让 tempfile 回退到当前工作目录，跑几次就在项目根撒一地 tmpXXXX；
  ② 全局关掉双层生成（INNER_MONOLOGUE_ENABLED=off）——写环境变量而不是改 config 属性，
     因为 /refresh 会 importlib.reload(config)，只改属性会被刷回去。

另外给出造数据助手 `_mk_day`（手造 v2 day_state）/ `_mk_book`（临时书架放一本 txt）。
本文件自身不含用例，run_tests.py 不收集它。
"""
import atexit
import builtins
import contextlib
import importlib
import io
import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import commands
import config
import persona

# 双层生成会让"一次回复"变成**两次** LLM 调用。绝大多数历史测试的 mock 是按调用次数
# /最后一条消息内容应答的（假设一次调用），所以全局把独白层关掉——这是新增维度，不该
# 让老用例逐个改。要验证独白本身时用专门的用例（见 test_inner_monologue，会临时打开）。
# ⚠️ 必须写环境变量而不是改 config 属性：/refresh 会 importlib.reload(config)，
# 只改模块属性会被刷新回去，写 os.environ 才能扛住重载。
os.environ["INNER_MONOLOGUE_ENABLED"] = "off"
# ③ 全局关掉「章节结算」后台线程（v0.109）。`_settle_due` 会起 `_settle_one` 线程，
# 它**先读计划、再调 LLM、最后写 day_state/读书笔记**；测试主线程若在中间
# `_patch_config(_db_init=True)` 切到下一个用例的临时库，线程收尾时就把旧库的
# 计划/笔记写进新库——复现为 test_trueread「不该有重复笔记：4」（多出真实书第 4 课）、
# test_life_engine 固定日期 day_state 被写脏后重排（2026-10-17 抓到现场：
# 失败瞬间线程表里有 `_settle_one`，且笔记 book_id 是真实书库里的书）。
# 需要结算的用例自己用 `_patch_config(WORK_SHAREABLE_ENABLED=True)` 局部打开。
os.environ["WORK_SHAREABLE_ENABLED"] = "off"
importlib.reload(config)

# 测试临时目录统一收口到一个**固定目录**，跑完尽量清、下次跑先清（双保险）。
# 之所以不用 mkdtemp 做根目录：Windows 上 sqlite 文件句柄在解释器退出时可能还没释放，
# atexit 的 rmtree 会因文件锁清不干净；固定目录 + "启动即清"能自愈上次残留。
_TEST_TMP_ROOT = Path(__file__).resolve().parent / ".test-tmp"
shutil.rmtree(_TEST_TMP_ROOT, ignore_errors=True)
_TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_TEST_TMP_ROOT)
atexit.register(lambda: shutil.rmtree(_TEST_TMP_ROOT, ignore_errors=True))

TS_RE = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}")


def _mk_day(day: str, wake, chapters, book_title: str = "测试书", start_ch: int = 0,
            warmup: int = 0, rest_min: int = 20, bedtime=None) -> dict:
    """手造一份 v2 今日生活（day_state）——生活引擎唯一的账。

    ⚠️ 必须带 `v:2` + `planned:1`：否则 `life.today()` 会把它当旧账重排、转头去扫
    真实书架，测试就不再确定。章节字数给整数 → 会话时长 = 字数 ÷ 阅读速度。
    """
    import json as _json
    from datetime import timedelta as _td

    import life as _life
    import db as _db

    chs = []
    for i, c in enumerate(chapters):
        c = {"title": f"第{start_ch + i + 1}章", "char_count": int(c)} if isinstance(c, int) \
            else dict(c)
        c.setdefault("idx", start_ch + i)
        c["char_count"] = int(c.get("char_count") or 0)
        chs.append(c)
    sp = _life.speed()
    sessions, t = [], wake.replace() + _td(minutes=warmup)
    for c in chs:
        end = t + _td(minutes=c["char_count"] / sp)
        sessions.append({"start": t.isoformat(timespec="seconds"),
                         "end": end.isoformat(timespec="seconds"),
                         "chapters": [c], "partial": False})
        t = end + _td(minutes=rest_min)
    state = {"v": 2, "day": day, "planned": 1,
             "wake": wake.isoformat(timespec="seconds"),
             "bedtime": (bedtime or t + _td(hours=8)).isoformat(timespec="seconds"),
             "sessions": sessions, "settled_idx": 0, "overtime": 0,
             "plan": {"done": False, "book_id": "测试书.txt", "book_title": book_title,
                      "start_ch": start_ch, "chapters": chs,
                      "total_chars": sum(c["char_count"] for c in chs)}}
    _db.set_kv("day_state", _json.dumps(state, ensure_ascii=False))
    return state


def _mk_book(books_dir, name: str = "测试书.txt", n_ch: int = 6,
             chars: int | None = None) -> None:
    """在临时书架放一本带「第X章」的 txt（真读/排班要用真书）。"""
    import life as _life
    per = chars if chars is not None else int(_life.NOMINAL_SESSION_MIN * _life.speed())
    body = "正文内容" * (per // 4)
    books_dir.mkdir(parents=True, exist_ok=True)
    (books_dir / name).write_text(
        "\n".join(f"第{i + 1}章 章节标题{i + 1}\n{body}" for i in range(n_ch)),
        encoding="utf-8")


# ── 计时快档：引擎真实运行时按"字"假装耗时（打字 0.12s/字起、发送段间隔 0.6s 起…），
# 测试里照真实值跑要等很久。这族参数**只影响等待时长、不影响逻辑**——用它把等待压到
# 毫秒级。凡是要真实跑一遍「on_user_message → send」流程的用例都该套上（否则整轮跑完
# 要按秒等）。个别场景需要"慢"（如留插话窗口）时，在里面再嵌套 _patch_config 覆盖。
_FAST_TIMING = {
    "READING_SECONDS_PER_CHAR": 0.0, "READING_MAX_SECONDS": 0.05,
    "TYPING_SECONDS_PER_CHAR": 0.001, "TYPING_MIN_SECONDS": 0.01,
    "TYPING_MAX_SECONDS": 0.05,
    "SEND_SECONDS_PER_CHAR": 0.001, "SEND_MIN_SECONDS": 0.01,
    "SEND_MAX_SECONDS": 0.05,
}


@contextlib.contextmanager
def _patch_config(_db_init: bool = False, **overrides):
    """批量改 config 属性，退出自动还原——治「手写 save/restore，漏了哪个参数就
    污染下个用例」。

    `_db_init=True`：改完属性（及还原属性）之后各调一次 `db.init()`，每次调用
    时 import 当前 db 模块。DB 重定向场景用——连接跟着新库走、退出时切回原库，
    跨用例不留"连接还指着上个用例临时库"的隐患。等价旧样板：
        real_base, real_db = config.BASE_DIR, config.DB_PATH
        config.BASE_DIR, config.DB_PATH = tmp, tmp / "friend.db"
        try:  _db.init(); …  finally:  config.BASE_DIR, config.DB_PATH = real_base, real_db; _db.init()
    写法：
        with _patch_config(_db_init=True, BASE_DIR=tmp, DB_PATH=tmp / "friend.db"):
            …
    """
    saved = {k: getattr(config, k) for k in overrides}
    for k, v in overrides.items():
        setattr(config, k, v)
    _db = None
    if _db_init:
        import db as _db
        _db.init()
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(config, k, v)
        if _db:
            _db.init()

# `from _test_common import *` 能带出来的名字（下划线开头的助手要显式列进 __all__，
# 否则 import * 不带下划线名）。测试文件统一 `from _test_common import *`。
__all__ = [
    "atexit", "builtins", "contextlib", "importlib", "io", "os", "re",
    "shutil", "sys", "tempfile", "types", "Path",
    "commands", "config", "persona", "TS_RE", "_mk_day", "_mk_book",
    "_FAST_TIMING", "_patch_config",
]
