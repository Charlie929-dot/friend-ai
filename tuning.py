"""调优参数面板（G 模块 S4）：全项目「实测优化型」数值参数的清单 + 读/写。

对应 docs/方案-整体设计-v0.84.md §10（2026-10-13 用户拍板）：

- 这些参数**全部已经是 config.py 里的环境变量**（.env 是唯一真相），这里只做一份
  「元数据清单」——分组 / 层级（L1 首屏高频调 / L2 高级折叠）/ 类型 / 默认值 / 说明，
  供网页调试面板渲染 + 校验。**不改任何业务逻辑**。
- 写回复用「模型设置」同款机制：`auth.write_env` + `config.load_dotenv(force=True)`
  + `importlib.reload(config)`。业务模块都是 `import config` 后 `config.XXX` 动态取
  属性，reload 即覆盖，所以全进程热生效、不用重启。
- 不收录：`tps`（推导值，随当日字数÷时长自动算，不是可调项）、
  `WORK_READ_PER_UNIT`（改造后废弃，改按字数圈定）、§10.9 待定项（常驻层字数上限等，
  待实测续写再进面板）。
"""
import importlib
import re

import auth
import config

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

# 层级：L1 = 首屏高频调；L2 = 高级折叠。类型：int / float / time（HH:MM）。
# min/max/step 只给有明确语义边界的（概率/比例/相似度/温度等），其余只做类型校验。

_GROUPS: list[dict] = [
    {
        "name": "阅读真读 + 上课作息",
        "items": [
            dict(k="READING_DAILY_CHARS", l="当日字数上限", v="L1", t="int", d="10000",
                 h="每日圈定读书字数（非空白），宁少勿超", min=0, step=1),
            dict(k="SCHED_WORK_START", l="上班", v="L1", t="time", d="08:30",
                 h="开始读书（24 小时制）"),
            dict(k="SCHED_LUNCH_START", l="午休开始", v="L1", t="time", d="12:00",
                 h="午休起点"),
            dict(k="SCHED_LUNCH_END", l="午休结束", v="L1", t="time", d="14:00",
                 h="午休终点"),
            dict(k="SCHED_WORK_END", l="下班", v="L1", t="time", d="17:30",
                 h="结束读书"),
            dict(k="SCHED_HALF_PERIOD_MIN", l="半节时长", v="L1", t="int", d="40",
                 h="每半节几分钟", min=1, step=1),
            dict(k="SCHED_INTRA_BREAK_MIN", l="节内休", v="L1", t="int", d="15",
                 h="上半节和下半节之间歇多久", min=0, step=1),
            dict(k="SCHED_INTER_BREAK_MIN", l="节间休", v="L1", t="int", d="20",
                 h="两节课之间歇多久", min=0, step=1),
            dict(k="SCHED_MORNING_PERIODS", l="上午节数", v="L1", t="int", d="2",
                 h="上午上几节课", min=1, step=1),
            dict(k="SCHED_AFTERNOON_PERIODS", l="下午节数", v="L1", t="int", d="2",
                 h="下午上几节课", min=1, step=1),
            dict(k="READING_TXT_FALLBACK_MIN", l="txt 切章下限", v="L2", t="int", d="4000",
                 h="txt 没有章节标题时，按累计字数切章的下限", min=0, step=100),
            dict(k="READING_TXT_FALLBACK_MAX", l="txt 切章上限", v="L2", t="int", d="6000",
                 h="按累计字数切章的上限", min=0, step=100),
            dict(k="READING_SUMMARY_THRESHOLD", l="阶段大总结阈值", v="L2", t="int", d="15000",
                 h="累计读满这么多字触发一次阶段性大总结", min=0, step=500),
            dict(k="READING_CROSS_BOOK_COUNT", l="跨书梳理触发", v="L2", t="int", d="3",
                 h="读满几本触发跨书梳理", min=1, step=1),
        ],
    },
    {
        "name": "主动频率 + 防打扰",
        "items": [
            dict(k="DAILY_PROACTIVE_LIMIT", l="每日主动上限", v="L1", t="int", d="6",
                 h="一天最多主动发起几条消息", min=0, step=1),
            dict(k="BUBBLE_IDLE_MIN_MINUTES", l="冒泡空闲下限", v="L1", t="int", d="30",
                 h="空闲多久开始想冒泡（区间下限）", min=0, step=1),
            dict(k="BUBBLE_IDLE_MAX_MINUTES", l="冒泡空闲上限", v="L1", t="int", d="60",
                 h="空闲多久开始想冒泡（区间上限）", min=0, step=1),
            dict(k="BUBBLE_DAILY_LIMIT", l="冒泡日额", v="L1", t="int", d="12",
                 h="冒泡独立额度，不占主动消息上限", min=0, step=1),
            dict(k="BUBBLE_FIXED_PROB", l="冒泡抽固定语料概率", v="L2", t="float", d="0.35",
                 h="冒泡直接抽固定语料的概率（省 token）", min=0, max=1, step=0.05),
            dict(k="NUDGE_IDLE_MIN_MINUTES", l="轻催空闲下限", v="L2", t="int", d="10",
                 h="聊到一半对方没声了，多久开始想轻催（下限）", min=0, step=1),
            dict(k="NUDGE_IDLE_MAX_MINUTES", l="轻催空闲上限", v="L2", t="int", d="20",
                 h="轻催区间上限", min=0, step=1),
            dict(k="NUDGE_PROBABILITY", l="轻催概率", v="L2", t="float", d="0.6",
                 h="到点轻催的概率，剩下比例安静等待", min=0, max=1, step=0.05),
            dict(k="QUIET_AFTER_BUSY_HOURS", l="忙碌静默", v="L2", t="int", d="3",
                 h="对方说在忙后静默几小时", min=0, step=1),
            dict(k="MAX_UNANSWERED_PROACTIVE", l="连续未回复停发", v="L2", t="int", d="2",
                 h="连续几条主动消息没回就停发", min=0, step=1),
            dict(k="AUTO_FAMILIAR_INFO_THRESHOLD", l="转熟悉阈值", v="L2", t="int", d="5",
                 h="profile 已填项 + 便签数达到即自动转熟悉", min=0, step=1),
        ],
    },
    {
        "name": "打断成本 + 情绪 + 紧急",
        "items": [
            dict(k="COST_R_FLOW", l="阻力·心流", v="L1", t="float", d="1.0",
                 h="心流期阻力（打断最伤）", min=0, max=1, step=0.05),
            dict(k="COST_R_WORKING", l="阻力·工作", v="L1", t="float", d="0.7",
                 h="忙自己的事时的阻力", min=0, max=1, step=0.05),
            dict(k="COST_R_IDLE", l="阻力·空闲", v="L1", t="float", d="0.3",
                 h="还没开工时的阻力", min=0, max=1, step=0.05),
            dict(k="COST_R_BREAK", l="阻力·休息", v="L1", t="float", d="0.3",
                 h="歇会儿时的阻力", min=0, max=1, step=0.05),
            dict(k="COST_R_OFF", l="阻力·收工", v="L1", t="float", d="0.0",
                 h="忙完了的阻力（完全欢迎）", min=0, max=1, step=0.05),
            dict(k="COST_WELCOMED_MAX", l="welcomed 阈值", v="L1", t="float", d="0.4",
                 h="costIndex 低于此值 = 欢迎", min=0, max=1, step=0.05),
            dict(k="COST_TOLERATED_MAX", l="tolerated 阈值", v="L1", t="float", d="0.9",
                 h="costIndex 低于此值 = 可以，否则 = 有代价", min=0, max=1, step=0.05),
            dict(k="URGENT_WINDOW_MINUTES", l="紧急窗口", v="L2", t="int", d="10",
                 h="识别到有急事后的随叫随到窗口", min=0, step=1),
            dict(k="MOOD_TIRED_ENERGY", l="心情疲惫阈值", v="L2", t="int", d="30",
                 h="精力低于此值 = 疲惫", min=0, step=1),
            dict(k="MOOD_EVENING_HOUR", l="晚间时点", v="L2", t="int", d="21",
                 h="这之后忙完 = 平静", min=0, max=23, step=1),
        ],
    },
    {
        "name": "记忆召回 + 衰减",
        "items": [
            dict(k="EMBEDDING_SEMANTIC_WEIGHT", l="语义权重", v="L1", t="float", d="4.0",
                 h="语义相似分在混合打分里的权重", min=0, max=20, step=0.5),
            dict(k="EMBEDDING_MIN_SIMILARITY", l="相似度下限", v="L1", t="float", d="0.35",
                 h="余弦相似度低于此值不算语义命中", min=0, max=1, step=0.05),
            dict(k="MEMORY_DECAY_IDLE_DAYS", l="衰减基准", v="L2", t="int", d="30",
                 h="多久没被想起 → 遗忘一级（基准天数）", min=1, step=1),
            dict(k="MEMORY_DECAY_RECALL_BONUS", l="召回加成", v="L2", t="float", d="0.5",
                 h="每被想起一次的有效期加成", min=0, max=10, step=0.1),
            dict(k="MEMORY_DECAY_MAX_LEVEL", l="压级", v="L2", t="int", d="3",
                 h="压到第几级彻底遗忘（0=原文）", min=0, step=1),
            dict(k="MEMORY_DECAY_PIN_RECALLS", l="永久阈值", v="L2", t="int", d="8",
                 h="被想起够多次 → 永久记住", min=1, step=1),
            dict(k="MEMORY_DECAY_BATCH", l="批量", v="L2", t="int", d="6",
                 h="一次遗忘最多处理几条", min=1, step=1),
            dict(k="TODAY_NOTES_MAX", l="今日素材条数", v="L2", t="int", d="3",
                 h="最多注入几条今日素材", min=0, step=1),
            dict(k="TODAY_NOTES_CHARS", l="今日素材截断", v="L2", t="int", d="200",
                 h="每条正文注入截断字数", min=0, step=10),
            dict(k="MEMORY_TOOL_MAX_ROUNDS", l="检索轮数", v="L2", t="int", d="3",
                 h="一轮回复里工具调用循环的最大轮数", min=1, step=1),
        ],
    },
    {
        "name": "工作系统",
        "items": [
            dict(k="WORK_UNIT_MINUTES", l="单元时长", v="L1", t="int", d="50",
                 h="一个工作单元多长（改造后由上课作息接管）", min=1, step=1),
            dict(k="WORK_BREAK_MINUTES", l="休息", v="L1", t="int", d="15",
                 h="单元之间歇多久", min=0, step=1),
            dict(k="WORK_FLOW_RATIO", l="心流比", v="L1", t="float", d="0.2",
                 h="单元前多少比例是心流期（打断代价最高）", min=0, max=1, step=0.05),
            dict(k="WORK_DAILY_QUOTA", l="配额", v="L1", t="int", d="6",
                 h="今日工作单元配额（做满才下班）", min=1, step=1),
            dict(k="WORK_ENERGY_MAX", l="精力上限", v="L2", t="int", d="100",
                 h="精力上限 E", min=1, step=1),
            dict(k="WORK_ENERGY_COST", l="精力消耗", v="L2", t="int", d="12",
                 h="每个单元消耗精力", min=0, step=1),
            dict(k="WORK_ENERGY_RECOVER", l="精力恢复", v="L2", t="int", d="8",
                 h="每段休息恢复精力", min=0, step=1),
            dict(k="WORK_PAY_PER_UNIT", l="单件产出", v="L2", t="int", d="20",
                 h="每个单元产出（纯叙事余额）", min=0, step=1),
        ],
    },
    {
        "name": "人格 + 双层生成 + 兴趣 + 表情",
        "items": [
            dict(k="INNER_MONOLOGUE_TEMPERATURE", l="独白温度", v="L2", t="float", d="0.75",
                 h="独白层温度（比说话层低，要判断不要表演）", min=0, max=2, step=0.05),
            dict(k="INNER_MONOLOGUE_MAX_TOKENS", l="独白 token", v="L2", t="int", d="400",
                 h="独白输出上限（很短，额外成本有限）", min=1, step=10),
            dict(k="UTTERANCE_TEMPERATURE", l="说话温度", v="L2", t="float", d="0.95",
                 h="说话层温度（要高，口语的随机性）", min=0, max=2, step=0.05),
            dict(k="SELF_MEMORY_MAX", l="自我记忆注入条数", v="L2", t="int", d="5",
                 h="每轮最多注入几条关于自己的记忆", min=0, step=1),
            dict(k="INTEREST_PROMOTE_THRESHOLD", l="兴趣转正阈值", v="L2", t="int", d="3",
                 h="同一兴趣主题累计几次自动转正", min=1, step=1),
            dict(k="EMOJI_POOL_RECENT_N", l="表情防重样窗口", v="L2", t="int", d="8",
                 h="扫最近几条消息提取已用表情，防重复", min=0, step=1),
        ],
    },
    {
        "name": "聊天节奏",
        "items": [
            dict(k="READING_SECONDS_PER_CHAR", l="每字阅读时长", v="L2", t="float", d="0.12",
                 h="每字假装阅读多久（秒）", min=0, step=0.01),
            dict(k="READING_MAX_SECONDS", l="阅读上限", v="L2", t="float", d="20",
                 h="阅读等待上限（秒）", min=0, step=1),
            dict(k="TYPING_SECONDS_PER_CHAR", l="每字输入时长", v="L2", t="float", d="0.12",
                 h="每字假装输入多久（秒）", min=0, step=0.01),
            dict(k="TYPING_MIN_SECONDS", l="输入下限", v="L2", t="float", d="1.2",
                 h="输入最短多久（秒）", min=0, step=0.1),
            dict(k="TYPING_MAX_SECONDS", l="输入上限", v="L2", t="float", d="8",
                 h="输入最长多久（秒）", min=0, step=1),
            dict(k="SEND_SECONDS_PER_CHAR", l="分段间隔", v="L2", t="float", d="0.08",
                 h="分段连发的间隔（按上一条长度，秒/字）", min=0, step=0.01),
            dict(k="SEND_MIN_SECONDS", l="分段下限", v="L2", t="float", d="0.6",
                 h="分段最短间隔（秒）", min=0, step=0.1),
            dict(k="SEND_MAX_SECONDS", l="分段上限", v="L2", t="float", d="5",
                 h="分段最长间隔（秒）", min=0, step=1),
        ],
    },
    {
        "name": "意识流 + 上下文 + 离线作息",
        "items": [
            dict(k="CONSCIOUS_STREAM_RECENT", l="意识流注入条数", v="L2", t="int", d="1000",
                 h="每轮注入最近几条自我意识流", min=0, step=10),
            dict(k="CONSCIOUS_STREAM_LINE_CHARS", l="意识流行截断", v="L2", t="int", d="0",
                 h="每行截断字数（0=不截断，全文注入）", min=0, step=10),
            dict(k="CONTEXT_MAX_MESSAGES", l="会话消息上限", v="L2", t="int", d="1000",
                 h="一次对话最多读多少条（防刷 token）", min=1, step=10),
            dict(k="PROACTIVE_CONTEXT_MAX_MESSAGES", l="主动会话上限", v="L2", t="int", d="1000",
                 h="主动消息同理", min=1, step=10),
            dict(k="REST_SLEEP_EARLY_MAX", l="早睡浮动", v="L2", t="int", d="45",
                 h="早睡最多提前多少分钟", min=0, step=1),
            dict(k="REST_SLEEP_LATE_MAX", l="熬夜浮动", v="L2", t="int", d="90",
                 h="熬夜最多延后多少分钟", min=0, step=1),
            dict(k="REST_WAKE_LIE_IN_MAX", l="赖床浮动", v="L2", t="int", d="45",
                 h="赖床最多延后多少分钟", min=0, step=1),
            dict(k="REST_WAKE_EARLY_MAX", l="早醒浮动", v="L2", t="int", d="30",
                 h="早醒最多提前多少分钟", min=0, step=1),
            dict(k="REST_TIRED_ENERGY", l="累阈值", v="L2", t="int", d="20",
                 h="精力低于此值 = 累，倾向早睡", min=0, step=1),
            dict(k="REST_ENERGETIC_ENERGY", l="精神阈值", v="L2", t="int", d="60",
                 h="精力高于此值 = 精力足，倾向熬夜", min=0, step=1),
        ],
    },
]


def catalog() -> list[dict]:
    """完整参数清单（含当前值 + 默认值），给网页面板一次拉取渲染。

    对外用语义化字段名（key/label/level/type/default/desc + value/min/max/step），
    内部短名（k/l/v/t/d/h）只在本模块用、不外漏。"""
    out: list[dict] = []
    for g in _GROUPS:
        items = []
        for it in g["items"]:
            items.append({
                "key": it["k"], "label": it["l"], "level": it["v"], "type": it["t"],
                "default": it["d"], "desc": it["h"], "value": _read_raw(it),
                "min": it.get("min"), "max": it.get("max"), "step": it.get("step"),
            })
        out.append({"name": g["name"], "items": items})
    return out


def current_values() -> dict:
    """当前生效值（读 config 常量，非 .env 原文）。"""
    return {it["k"]: _read_raw(it) for g in _GROUPS for it in g["items"]}


def _by_key() -> dict:
    return {it["k"]: it for g in _GROUPS for it in g["items"]}


def _read_raw(item: dict):
    return getattr(config, item["k"])


def save_values(updates: dict) -> dict:
    """保存一批参数：校验 + 写回 .env + 热生效，返回新的当前值。

    updates: {env_key: value}。value 可传字符串或数字（前端 input 值）。
    只接受 catalog 里登记过的 key；类型/范围非法抛 ValueError（含 key 名）。
    """
    meta = _by_key()
    clean: dict[str, str] = {}
    for key, raw in updates.items():
        item = meta.get(key)
        if item is None:
            raise ValueError(f"未知参数：{key}")
        clean[key] = _coerce(item, raw)
    if not clean:
        return current_values()
    auth.write_env(clean)
    config.load_dotenv(force=True)   # .env 是唯一真相，改完强制覆盖环境变量
    importlib.reload(config)         # 刷新本进程里的配置常量（同 /refresh、模型设置做法）
    return current_values()


def _coerce(item: dict, raw) -> str:
    """把输入值按元数据类型校验并规范化，返回写 .env 用的字符串。"""
    t = item["t"]
    key = item["k"]
    label = item["l"]
    if t == "int":
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{label}（{key}）得是整数") from None
    elif t == "float":
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{label}（{key}）得是数字") from None
    elif t == "time":
        s = str(raw or "").strip()
        m = _TIME_RE.match(s)
        if not m:
            raise ValueError(f"{label}（{key}）得是 HH:MM 格式的 24 小时制时间")
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            raise ValueError(f"{label}（{key}）时间不合法（小时 0–23、分钟 0–59）")
        return f"{hh:02d}:{mm:02d}"
    else:  # 理论到不了
        raise ValueError(f"{label}（{key}）类型未知：{t}")
    # int/float 的范围检查
    lo = item.get("min")
    hi = item.get("max")
    if lo is not None and v < lo:
        raise ValueError(f"{label}（{key}）不能小于 {lo}")
    if hi is not None and v > hi:
        raise ValueError(f"{label}（{key}）不能大于 {hi}")
    return str(v)
