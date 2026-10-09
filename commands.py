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
import corpus
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
  /profile why <字段>   看TA某条清单的依据（当时对话原文）
  /profile pending     看和旧记忆冲突、待你确认的新说法
  /profile accept|reject <字段>  接受/否决待确认的新说法
  /note <内容>     记一条事实便签（如 /note 叫我小徐），AI 会长期记住
  /notes           查看所有事实便签
  /forget <编号>   删除一条事实便签
  /memories        查看 AI 自动记住的事（分类分组，含待确认删除区）
  /memories time   按时间（新→旧）平铺看全部记忆
  /delmem <编号>   删掉一条自动记忆（也用于确认 AI 提议的删除）
  /keepmem <编号>  保留一条 AI 提议删除的记忆（取消待删除标记）
  /memfill         历史回填：把旧聊天记录补进记忆（历史多会跑一会儿）
  /work            看它今天手头的事（进度/精力/余额/攒了多少素材）
  /autostart       查开机自启状态；/autostart on|off 开/关（网页端开机自己回来）
  /diary           看今天的日记（含复盘）；/diary <日期> 看指定那天
  /diaries         列出所有写过日记的日期
  /review          现在立刻复盘今天并写日记（睡前会自动写）
  /guard           查看防打扰开关状态
  /guard on|off    开/关防打扰（默认关；睡觉拦截是作息，不受影响）
  /refresh         重新加载所有文件（.env/配置/人设/prompt/语料），不用重启
  /restart         重启网页服务（改了后端代码后点；CLI 里无效，Ctrl+C 重开即可）
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


def list_friends() -> list[dict]:
    """归档朋友的结构化列表（网页端 `/api/friends` 用，`/friends` 也复用它）。

    排序和编号必须跟 `/friend <编号>` 对得上——唯一真相是 `_archives()`，
    别在别处再排一遍序，否则点列表第 2 个切到的可能是第 3 个。
    """
    out = []
    for i, p in enumerate(_archives()):
        name, at = _archive_display(p)
        out.append({"idx": i + 1, "name": name, "at": at, "file": p.name})
    return out


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
        return corpus.INTRO_FALLBACK.format(card['name'])


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
        import work as _work
        hud = _work.hud()   # 它手头的事（工作系统关着时为空）
        import offline as _off
        off_txt = ""
        if _off.is_offline():
            off_txt = "（离线：" + ("睡着" if _off.offline_reason() == "sleep" else "专注做事") + "）"
        sleep_at = _off.tonight_sleep_at(datetime.now())
        wake_at = _off.next_wake_at(datetime.now())
        sched_txt = f" | 今晚 {sleep_at:%H:%M} 睡 · 下次 {wake_at:%m-%d %H:%M} 起"
        return (f"状态：{db.get_state()}{off_txt} | 关系阶段：{stage} | 模型：{model}{playing}"
                + (f" | {hud}" if hud else "") + sched_txt + "\n"
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
        import memory as _mem
        sub = arg.split(maxsplit=1)
        action = sub[0] if sub else ""
        value = sub[1].strip() if len(sub) > 1 else ""
        if not action:
            current = persona._read_profile()
            if not current:
                return "TA 现在对你一无所知。/profile add <一行> 告诉TA，或 /profile reuse 沿用旧清单。"
            ev = _mem.profile_evidence()
            lines = []
            for l in current.splitlines():
                l = l.strip()
                if not l:
                    continue
                key = l.split("：", 1)[0].split(":", 1)[0].strip()
                lid = ev.get(key, {}).get("long_id", 0) if isinstance(ev, dict) else 0
                marker = f"　〔证据#{lid}〕" if lid else ""
                lines.append(l + marker)
            out = f"TA 知道的关于你的事（预填清单）：\n" + "\n".join(lines)
            if ev:
                out += "\n（〔证据#N〕是TA得出这条的依据，/profile why <字段> 看当时对话原文）"
            pending = _mem.profile_pending()
            if pending:
                plines = [f"  {k} → {p['value']}"
                          + (f"〔证据#{p['long_id']}〕" if p.get("long_id") else "")
                          for k, p in pending.items()]
                out += ("\n\n⚠ 有几处新说法跟旧的不一致，等你确认：\n" + "\n".join(plines)
                        + "\n/profile accept <字段> 接受新说法 · /profile reject <字段> 保留旧的")
            return out
        if action == "why" and value:
            text = _mem.profile_evidence_text(value)
            if not text:
                return f"「{value}」没有证据记录（可能是你手动填的，或还没自动提取过）。"
            return f"「{value}」这条的依据（当时对话原文）：\n{text}"
        if action == "pending":
            pending = _mem.profile_pending()
            if not pending:
                return "没有待确认的更新。"
            plines = [f"  {k} → {p['value']}" for k, p in pending.items()]
            return ("待你确认的说法（新的和旧的冲突，TA 没擅自改）：\n" + "\n".join(plines)
                    + "\n/profile accept <字段> 接受 · /profile reject <字段> 保留旧的")
        if action == "accept" and value:
            v = _mem.accept_pending_profile(value)
            return f"好，记下啦：{value}：{v}" if v else f"「{value}」没有待确认的更新"
        if action == "reject" and value:
            v = _mem.reject_pending_profile(value)
            return f"好，保留原来的说法，这条新说法不记了（{value}）。" if v else f"「{value}」没有待确认的更新"
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
        return "用法：/profile | /profile add <一行> | /profile reuse | /profile clear | /profile why <字段> | /profile pending | /profile accept|reject <字段>"

    if cmd == "/persona":
        card = persona_pool.load()
        if not card:
            return "这位是默认老朋友，没有人格卡。/new-friend 可以抽一位有专属人格的新朋友。"
        return (f"人格卡：{card['name']} · {card['label']}\n"
                f"  作息：{card['wake_hour']}点起 / {card['sleep_hour']}点睡\n"
                f"  冒泡频率：x{card['bubble_mult']}\n"
                f"  阅读偏好：{card.get('reading_pref') or '没特别偏好'}\n"
                f"  怪癖：{card.get('quirk') or '没特别怪癖'}\n"
                f"  相识于：{card.get('drawn_at', '')}")

    if cmd == "/friends":
        friends = list_friends()
        if not friends:
            return "还没有归档的老朋友。/new-friend 之后这里就会有啦。"
        lines = [f"  [{f['idx']}] {f['name']}（最后归档 {f['at']}）" if f["at"]
                 else f"  [{f['idx']}] {f['name']}" for f in friends]
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
            db.init()  # 覆盖回旧库文件后重新迁移（补齐后加的列，如五维标签/long_id）
        name, _ = _archive_display(chosen)
        msg = f"欢迎回来～已切换到 {name}，接着聊吧。"
        import core  # 切过去就顺手看看TA有没有留言攒着
        pending = core.pending_outbox_text()
        return msg + (f"\n\n{pending}" if pending else "")

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
        sub = arg.strip().lower()
        if sub in ("time", "时间", "-t", "--time"):
            # 按时间平铺（新→旧）：不分组，每条带存储时间 + 事发时间标签，方便看时间线
            mems = [m for m in db.all_memories(limit=200)
                    if m.get("status", "ok") == "ok"]
            if not mems:
                return "我还没自动记住什么。聊着聊着就会有啦，也可以 /note 直接告诉我。"
            lines = []
            for m in mems:  # all_memories 已是 id 倒序（新→旧）
                stored = m.get("created_at", "")[:16].replace("T", " ")  # MM-DD HH:MM
                anchor = f"@{m['time_label']}" if m.get("time_label") else ""
                lines.append(f"[{m['id']}] {m['content']}"
                             + (f"  {anchor}" if anchor else "")
                             + f"（记于{stored}）")
            return f"我记住的 {len(mems)} 件事（按时间，新→旧）：\n" + "\n".join(lines)
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
        out += "\n（想按时间看全部，用 /memories time）"
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

    if cmd == "/work":
        # 工作系统（S3）：它今天手头的事。看得见它的日子，才知道什么时候找它会打扰
        import work as _work
        s = _work.status()
        if not s["quota"]:
            return "工作系统关着（WORK_ENABLED=off）——它没有「手头的事」，打断成本也无从算起。"
        fresh = len([x for x in _work._shareables() if not x.get("used")])
        return (f"{_work.hud()}\n"
                f"今日进度 {s['done']}/{s['quota']} 件 · 精力 {s['energy']}/{config.WORK_ENERGY_MAX}"
                f" · 余额 {s['money']} · 攒了 {fresh} 条没说过的素材\n"
                f"（余额是纯叙事资源，不绑任何真实付费）")

    if cmd == "/autostart":
        # 开机自启与常驻（G 模块）：查状态 / 开 / 关。start、stop 只在终端做——
        # 在网页里把自己停掉就再也起不来了
        import autostart as _as
        a = (arg or "status").split()[0].lower()
        if a in ("on", "install"):
            return _as.install()
        if a in ("off", "uninstall"):
            return _as.uninstall()
        return _as.status()

    if cmd == "/diary":
        import diary as _diary
        if arg:
            return _diary.read_diary(arg)
        return _diary.read_diary()

    if cmd == "/diaries":
        import diary as _diary
        dates = _diary.list_diaries()
        if not dates:
            return "还没写过日记。睡前会自动写，也可以 /review 现在写。"
        return "写过日记的日子：\n" + "\n".join(f"  {d}" for d in dates)

    if cmd == "/review":
        import diary as _diary
        return _diary.daily_review(force=True)

    if cmd == "/refresh":
        # 热重载：不重启进程让文件改动生效，聊天界面只回一句"刷新成功"。
        # 范围：.env/config 参数、人格池、人设 prompt、LLM、记忆、防打扰、游戏规则。
        # 不重载 core/db/scheduler/通道（引擎实例已创建，结构改动仍需重启）。
        import importlib
        import context as _context
        import corpus as _corpus
        import diary as _diary
        import game as _game
        import guard as _guard
        import llm as _llm
        import memory as _memory
        import persona as _persona
        import persona_pool as _pool
        config.load_dotenv(force=True)   # .env 改过的值强制生效
        importlib.reload(config)         # 重读全部配置参数
        for m in (_corpus, _pool, _persona, _llm, _memory, _diary, _guard, _game, _context):
            importlib.reload(m)
        return "刷新成功"

    if cmd == "/restart":
        # 真正的退出逻辑在 webui 里拦（有守护才敢退）；CLI 走到这里只给个说明
        return "重启是网页端的行为：改完后端点一下，守护会自动拉起新进程。\nCLI 请直接 Ctrl+C 退出后重跑。"

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
