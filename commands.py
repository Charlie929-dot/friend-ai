"""斜杠命令：/help /status /model /new-friend /friends /friend /persona ...
所有通道通用（CLI 直接打印，IM 通道作为回复发回）。

归档机制（V2）：每位朋友有稳定身份 friend_id（存库内 kv，随库走），
归档文件 = friend-<friend_id>.db，一个好朋友永远只有一个文件，重复归档覆盖更新；
归档时间记在库内 kv.archived_at + 根目录 archive.log，文件名不再带时间戳。
"""
import json
import shutil
import sqlite3
import uuid
from datetime import datetime

import config
import db
import persona_pool

HELP = """可用命令：
  /help            显示本帮助
  /status          当前状态（作息/关系阶段/今日计数/模型）
  /model           查看当前模型
  /model <名字>    切换模型，如 /model deepseek-reasoner
  /persona         查看当前朋友的人格卡
  /familiar        进入熟悉阶段（主动消息只由触发源发起，停止冒泡）
  /new             回到初见阶段（重新开始高频冒泡）
  /new-friend      抽一位"新朋友"：随机人格，归档当前数据库，从零开始
  /friends         列出所有归档的"老朋友"（名字+最后归档时间）
  /friend <编号>   换回某个老朋友继续聊（当前朋友自动归档，覆盖更新TA那份）
  /alias <编号> <别名>  给归档的老朋友改别名（只改库内记录，文件名不变）
  /profile         查看当前朋友持有的你的信息清单
  /profile add <一行>   往清单里加一条（如 /profile add 年级：大二）
  /profile reuse   让当前朋友沿用之前填过的清单
  /profile clear   清空清单
  /note <内容>     记一条事实便签（如 /note 叫我小徐），AI 会长期记住
  /notes           查看所有事实便签
  /forget <编号>   删除一条事实便签
  /memories        查看 AI 自动记住的事（记忆系统，含待确认删除区）
  /delmem <编号>   删掉一条自动记忆（也用于确认 AI 提议的删除）
  /keepmem <编号>  保留一条 AI 提议删除的记忆（取消待删除标记）
  /memfill         历史回填：把旧聊天记录补进记忆（历史多会跑一会儿）
  /group <编号...> 拉群：当前朋友和指定的归档老朋友一起聊天（编号见 /friends）
  /endgroup        散群：群聊记录写回每位朋友的库并各自记进记忆
  /guard           查看防打扰开关状态
  /guard on|off    开/关防打扰（默认关；睡觉拦截是作息，不受影响）
  /refresh         重新加载所有文件（.env/配置/人设/prompt/语料），不用重启
"""


def _read_kv_file(path) -> dict:
    """直接读某个数据库文件的 kv 表（不切当前库）。"""
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT key, value FROM kv").fetchall()
        conn.close()
        return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {}


def _write_kv_file(path, key: str, value: str) -> None:
    """直接写某个数据库文件的 kv（/alias 用，不切当前库）。"""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
    conn.close()


def _archives() -> list:
    """所有归档数据库（friend-<friend_id>.db），按相识时间（首次归档）升序。
    排序必须稳定：切换朋友会刷新 archived_at，若按它排序列表每次都在变。"""
    files = [p for p in config.BASE_DIR.glob("friend-*.db")]
    return sorted(files, key=lambda p: (
        _read_kv_file(p).get("created_at") or _read_kv_file(p).get("archived_at", ""),
        p.name))


def _current_friend_name() -> str:
    """当前朋友的显示名：人格卡名 > 别名 > 老朋友。"""
    card = persona_pool.load()
    if card:
        return card["name"]
    return db.get_kv("alias") or "老朋友"


def _friend_name_in(path) -> str:
    """读某个归档库里的朋友显示名：人格卡名 > 别名 > 老朋友。"""
    kv = _read_kv_file(path)
    pj = kv.get("persona_json")
    if pj:
        try:
            return json.loads(pj)["name"]
        except (ValueError, KeyError):
            pass
    return kv.get("alias") or "老朋友"


def _archive_display(p) -> tuple[str, str]:
    """(显示名, 最后归档时间短格式)。名字优先级：别名 > 人格名 > 老朋友。"""
    kv = _read_kv_file(p)
    name = kv.get("alias", "")
    if not name and kv.get("persona_json"):
        try:
            name = json.loads(kv["persona_json"])["name"]
        except (ValueError, KeyError):
            pass
    name = name or "老朋友"
    at = kv.get("archived_at", "")
    short = ""
    if at:
        try:
            short = datetime.fromisoformat(at).strftime("%m-%d %H:%M")
        except ValueError:
            short = at
    return name, short


def _friend_id() -> str:
    """当前朋友的稳定身份 ID：首次归档时生成，存 kv 随库走（切朋友=切 id）。"""
    fid = db.get_kv("friend_id")
    if not fid:
        fid = uuid.uuid4().hex[:8]
        db.set_kv("friend_id", fid)
    return fid


def _archive_current() -> str:
    """把当前 friend.db 归档到 friend-<friend_id>.db（覆盖更新，一个好朋友一份）。

    返回朋友显示名；没有数据（空库）则返回空。"""
    if not config.DB_PATH.exists():
        return ""
    # 空库（没聊过）不归档
    if not db.get_kv("relationship_stage") and db.last_user_message_ts() is None:
        return ""
    fid = _friend_id()
    now = datetime.now().isoformat(timespec="seconds")
    if not db.get_kv("created_at"):
        db.set_kv("created_at", now)  # 相识时间：首次归档写入，之后不变（/friends 排序用）
    db.set_kv("archived_at", now)
    target = config.BASE_DIR / f"friend-{fid}.db"
    shutil.copyfile(str(config.DB_PATH), str(target))
    name, _ = _archive_display(target)
    # 归档事件日志：专门记录每次归档时间（文件名不再带时间戳）
    with open(config.BASE_DIR / "archive.log", "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 归档 {name}（{fid}）\n")
    return name


def pick_friend_on_startup(input_fn=input, print_fn=print) -> str | None:
    """CLI 启动时的朋友选择菜单：先让人知道现在对面是谁。

    返回要执行的命令（"/friend 2" / "/new-friend"），None 表示继续当前朋友。
    在引擎和调度器启动前调用，选完谁的作息就按谁排。
    """
    card = persona_pool.load()
    current = f"{card['name']}·{card['label']}" if card else "默认老朋友（无人格卡）"
    archives = _archives()

    print_fn(f"当前朋友：{current}")
    if archives:
        print_fn("归档的老朋友：")
        for i, p in enumerate(archives):
            name, at = _archive_display(p)
            print_fn(f"  [{i + 1}] {name}（最后归档 {at}）" if at else f"  [{i + 1}] {name}")
    options = ["回车继续和TA聊"]
    if archives:
        options.append("输入编号换老朋友")
    options.append("输入 n 抽一位新朋友")
    print_fn("；".join(options))

    choice = input_fn("> ").strip().lower()
    if not choice:
        return None
    if choice == "n":
        return "/new-friend"
    if choice.isdigit() and 1 <= int(choice) <= len(archives):
        return f"/friend {choice}"
    print_fn("没看懂，继续和当前朋友聊。")
    return None


def _intro_greeting(card: dict) -> str:
    """新朋友按自己的人格打招呼自我介绍。"""
    import llm
    import persona
    try:
        return llm.chat([
            {"role": "system", "content": persona.build_system_prompt("AWAKE")},
            {"role": "user", "content": (
                "（内心活动，不用回复这句话本身）你刚和TA认识，这是你们说的第一句话。"
                "用你自己的风格打个招呼、简单自我介绍（说出你的名字），"
                "表达想认识TA的意思。两三句话以内。")},
        ])
    except Exception:
        return f"你好啊，我叫{card['name']}，我们认识一下吧！"


def handle(cmd_line: str) -> str:
    parts = cmd_line.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/help":
        return HELP

    if cmd == "/status":
        import game
        stage = db.get_kv("relationship_stage", "new")
        model = db.get_kv("model_override", config.DEEPSEEK_MODEL)
        g = game.current()
        playing = f" | 游戏中：{game.GAMES[g['game']]['label']}" if g else ""
        return (f"状态：{db.get_state()} | 关系阶段：{stage} | 模型：{model}{playing}\n"
                f"今日主动：{db.get_counter('proactive_count')}/{config.DAILY_PROACTIVE_LIMIT} "
                f"| 冒泡：{db.get_counter('bubble_count')}/{config.BUBBLE_DAILY_LIMIT} "
                f"| 连续未回复：{db.get_counter('unanswered_proactive')}")

    if cmd == "/model":
        if not arg:
            return f"当前模型：{db.get_kv('model_override', config.DEEPSEEK_MODEL)}"
        db.set_kv("model_override", arg)
        return f"模型已切换为 {arg}"

    if cmd == "/familiar":
        db.set_kv("relationship_stage", "familiar")
        return "好，我们已经是老朋友了。以后我不打扰你，有事才来冒泡。"

    if cmd == "/new":
        db.set_kv("relationship_stage", "new")
        return "回到初见阶段，我会经常来冒泡的～"

    if cmd == "/new-friend":
        with db.PATH_LOCK:  # 与后台回复/提取互斥：归档+清库期间旧库不再被写
            archived = _archive_current()
            db.clear_all()  # 清空而非删文件：避免沙箱安全删除拦截，也无文件占用问题
            db.set_kv("profile_locked", "1")  # 新朋友零了解开局，信息清单自己攒
            card = persona_pool.draw()  # P1 核心乐趣：开盲盒抽人格
            persona_pool.save(card)
            # 让这位新朋友用人格做自我介绍（LLM 不可用就兜底）
            greeting = _intro_greeting(card)
        head = f"（抽到了新朋友：{card['name']}·{card['label']}）"
        tail = ("\n\n（想让TA先了解你一点？/profile add <一行> 逐条告诉TA，"
                "/profile reuse 沿用之前填过的清单，或跳过直接聊）")
        if archived:
            return f"{head} {archived} 的回忆已归档，随时可以用 /friend 换回来。\n\n{greeting}{tail}"
        return f"{head}\n\n{greeting}{tail}"

    if cmd == "/profile":
        import persona
        sub = arg.split(maxsplit=1)
        action = sub[0] if sub else ""
        value = sub[1].strip() if len(sub) > 1 else ""
        if not action:
            current = persona._read_profile()
            if not current:
                return "TA 现在对你一无所知。/profile add <一行> 告诉TA，或 /profile reuse 沿用旧清单。"
            return f"TA 知道的关于你的事（预填清单）：\n{current}"
        if action == "add" and value:
            db.set_kv("profile_locked", "1")
            old = db.get_kv("profile_text")
            db.set_kv("profile_text", f"{old}\n{value}".strip())
            return f"告诉TA啦：{value}"
        if action == "reuse":
            # 把全局 profile.md（老朋友时代的清单）复制给当前朋友
            path = config.BASE_DIR / "profile.md"
            if not path.exists():
                return "还没有可沿用的清单（profile.md 不存在）。"
            lines = [l for l in path.read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.strip().startswith("#")
                     and "：" in l and l.split("：", 1)[1].strip()]
            if not lines:
                return "profile.md 里没有已填写的项。"
            db.set_kv("profile_locked", "1")
            db.set_kv("profile_text", "\n".join(lines))
            return f"已把旧清单交给TA（{len(lines)} 项）。"
        if action == "clear":
            db.set_kv("profile_locked", "1")
            db.set_kv("profile_text", "")
            return "已清空，TA 又对你一无所知了。"
        return "用法：/profile | /profile add <一行> | /profile reuse | /profile clear"

    if cmd == "/persona":
        card = persona_pool.load()
        if not card:
            return "这位是默认老朋友，没有人格卡。/new-friend 可以抽一位有专属人格的新朋友。"
        return (f"人格卡：{card['name']} · {card['label']}\n"
                f"  作息：{card['wake_hour']}点起 / {card['sleep_hour']}点睡\n"
                f"  冒泡频率：x{card['bubble_mult']}\n"
                f"  怪癖：{card['quirk']}\n"
                f"  相识于：{card['drawn_at']}")

    if cmd == "/friends":
        archives = _archives()
        if not archives:
            return "还没有归档的老朋友。/new-friend 之后这里就会有啦。"
        lines = []
        for i, p in enumerate(archives):
            name, at = _archive_display(p)
            lines.append(f"  [{i + 1}] {name}（最后归档 {at}）" if at
                         else f"  [{i + 1}] {name}")
        return ("归档的老朋友们：\n" + "\n".join(lines) +
                "\n用 /friend <编号> 换回TA继续聊，/alias <编号> <别名> 给TA改名")

    if cmd == "/alias":
        parts2 = arg.split(maxsplit=1)
        archives = _archives()
        if len(parts2) != 2 or not parts2[0].isdigit() \
                or not (1 <= int(parts2[0]) <= len(archives)):
            return f"用法：/alias <编号> <别名>（编号 1-{len(archives)}，先用 /friends 查看）"
        new_alias = "".join(ch for ch in parts2[1] if ch not in r'\/:*?"<>|')[:20]
        if not new_alias:
            return "别名不能为空或全是非法字符。"
        target = archives[int(parts2[0]) - 1]
        _write_kv_file(target, "alias", new_alias)  # 改库内别名，文件名不动
        return f"改好啦：{new_alias}"

    if cmd == "/friend":
        archives = _archives()
        if not arg.isdigit() or not (1 <= int(arg) <= len(archives)):
            return f"用法：/friend <编号>（1-{len(archives)}），先用 /friends 查看列表"
        chosen = archives[int(arg) - 1]
        with db.PATH_LOCK:  # 与后台回复/提取互斥：归档+覆盖期间库不被写
            _archive_current()  # 当前朋友也归档（覆盖更新TA自己的那份），不丢
            shutil.copyfile(str(chosen), str(config.DB_PATH))
        name, _ = _archive_display(chosen)
        msg = f"欢迎回来～已切换到 {name}，接着聊吧。"
        import core  # 切过去就顺手看看TA有没有留言攒着
        pending = core.pending_outbox_text()
        return msg + (f"\n\n{pending}" if pending else "")

    if cmd == "/group":
        import groupchat
        if groupchat.is_active():
            return "已经在群里啦，/endgroup 散群后再拉新的。"
        archives = _archives()
        idxs = [int(x) for x in arg.split() if x.isdigit()]
        if not idxs or any(not (1 <= i <= len(archives)) for i in idxs):
            return (f"用法：/group <编号...>（1-{len(archives)}，先用 /friends 查看），"
                    f"把当前朋友和TA们拉一个群")
        # 参与者：当前朋友 + 指定编号的归档老朋友（去重保序）
        parts = [groupchat.Participant(config.DB_PATH, _current_friend_name())]
        for i in dict.fromkeys(idxs):
            p = archives[i - 1]
            parts.append(groupchat.Participant(p, _friend_name_in(p)))
        return groupchat.start(parts)

    if cmd == "/endgroup":
        import groupchat
        return groupchat.end()

    if cmd == "/note":
        if not arg:
            return "用法：/note <要记住的事>，如 /note 叫我小徐"
        n = db.add_fact(arg)
        return f"记下啦（便签 #{n}）：{arg}"

    if cmd == "/notes":
        facts = db.get_facts()
        if not facts:
            return "还没有事实便签。用 /note <内容> 记一条。"
        return "事实便签：\n" + "\n".join(
            f"  [{i + 1}] {f}" for i, f in enumerate(facts))

    if cmd == "/forget":
        if not arg.isdigit():
            return "用法：/forget <编号>，先用 /notes 查看"
        removed = db.delete_fact(int(arg))
        return f"已忘掉：{removed}" if removed else "没有这个编号的便签"

    if cmd == "/memories":
        mems = [m for m in db.all_memories(limit=50)
                if m.get("status", "ok") == "ok"]
        pendings = db.pending_delete_memories()
        if not mems and not pendings:
            return "我还没自动记住什么。聊着聊着就会有啦，也可以 /note 直接告诉我。"
        # 按 (category, theme) 分组：同主题的记忆排成时间线，新的追加在组后面
        groups = {}
        for m in mems:
            groups.setdefault((m["category"], m.get("theme") or ""), []).append(m)
        # 组按最新一条的 id 倒序（最近活跃的组在前），组内按时间正序
        ordered = sorted(groups.items(), key=lambda kv: -max(x["id"] for x in kv[1]))
        lines = []
        for (cat, theme), items in ordered:
            items.sort(key=lambda x: x["id"])
            lines.append(f"▸ {cat}" + (f"·{theme}" if theme else ""))
            for m in items:
                dims = " ".join(f"@{x}" for x in
                                (m.get("time_label"), m.get("place"), m.get("person")) if x)
                stored = m.get("created_at", "")[5:16].replace("T", " ")  # MM-DD HH:MM
                lines.append(f"  [{m['id']}] {m['content']}"
                             + (f"  {dims}" if dims else "")
                             + (f"（记于{stored}）" if stored else ""))
        out = f"我记住的 {len(mems)} 件事：\n" + "\n".join(lines)
        if pendings:
            plines = [f"  [{m['id']}] {m['content']}" for m in pendings]
            out += ("\n\n🗑 我想忘掉这些（待你确认）：\n" + "\n".join(plines)
                    + "\n/delmem <编号> 确认忘掉，/keepmem <编号> 保留")
        return out

    if cmd == "/delmem":
        if not arg.isdigit():
            return "用法：/delmem <编号>，先用 /memories 查看"
        removed = db.delete_memory(int(arg))
        return f"已忘掉：{removed}" if removed else "没有这个编号的记忆"

    if cmd == "/keepmem":
        if not arg.isdigit():
            return "用法：/keepmem <编号>，先用 /memories 查看待确认列表"
        target = next((m for m in db.pending_delete_memories()
                       if m["id"] == int(arg)), None)
        if not target:
            return "这条记忆不在待确认删除列表里"
        db.set_memory_status(int(arg), "ok")
        return f"好，这条我留着：{target['content'][:30]}"

    if cmd == "/memfill":
        # 历史回填：把旧聊天记录分批提取进记忆（每批一次 LLM 调用，断点续跑）
        import memory as _memory
        added = _memory.backfill_history()
        if added == 0:
            return "历史里没有新东西可记（或已经全部回填过了）。"
        return f"回填完成：从旧聊天里新记住 {added} 条。用 /memories 看看吧。"

    if cmd == "/refresh":
        # 热重载：不重启进程让文件改动生效，聊天界面只回一句"刷新成功"。
        # 范围：.env/config 参数、人格池、人设 prompt、LLM、记忆、防打扰、游戏规则。
        # 不重载 core/db/scheduler/通道（引擎实例已创建，结构改动仍需重启）。
        import importlib
        import game as _game
        import groupchat as _groupchat
        import guard as _guard
        import llm as _llm
        import memory as _memory
        import persona as _persona
        import persona_pool as _pool
        # 群聊会话在 groupchat 模块内存里，reload 会把它冲掉——先散群沉淀再重载
        group_note = ""
        if _groupchat.is_active():
            _groupchat.end()
            group_note = "（群聊已先散场，大家都记下了）"
        config.load_dotenv(force=True)   # .env 改过的值强制生效
        importlib.reload(config)         # 重读全部配置参数
        for m in (_pool, _persona, _llm, _memory, _guard, _game, _groupchat):
            importlib.reload(m)
        return "刷新成功" + group_note

    if cmd == "/guard":
        import guard as g
        if not arg:
            return f"防打扰：{'开' if g.guard_enabled() else '关（默认）'}。" \
                   "开着时会限制频率：每日上限、忙碌静默、连续未回复熔断。"
        if arg == "on":
            db.set_kv("guard_enabled", "on")
            return "防打扰已开启：每日上限 + 忙碌静默 + 连续未回复熔断都生效了。"
        if arg == "off":
            db.set_kv("guard_enabled", "off")
            return "防打扰已关闭。我会随意冒泡的，别嫌我烦。"
        return "用法：/guard on 或 /guard off"

    return f"未知命令：{cmd}，输入 /help 查看可用命令"
