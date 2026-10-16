"""调优参数面板（G 模块 S4）：全项目「实测优化型」数值参数的清单 + 读/写。

对应 docs/方案-整体设计-v0.84.md §10（2026-10-13 用户拍板）：

- 这些参数**全部已经是 config.py 里的环境变量**（.env 是唯一真相），这里只做一份
  「元数据清单」——分组 / 层级（L1 首屏高频调 / L2 高级折叠）/ 类型 / 说明 / 取值范围，
  供网页调试面板渲染 + 校验。**不改任何业务逻辑**。
- **默认值单头真相**（v0.88 E1）：清单里不再手写默认值，`default` 一律
  `getattr(config, k)` 取——改默认值只动 config.py 一处。
- 写回复用「模型设置」同款机制：`auth.write_env` + `config.load_dotenv(force=True)`
  + `importlib.reload(config)`。业务模块都是 `import config` 后 `config.XXX` 动态取
  属性，reload 即覆盖，所以全进程热生效、不用重启。
- 不收录：`tps` 之类推导值（v0.89 起已无此项：阅读速度 = 当日字数 ÷ 320min，
  由 `READING_DAILY_CHARS` 推导）、§10.9 待定项（常驻层字数上限等，待实测续写再进面板）。
- v0.89 批 A：删「工作系统」「上课作息」两组、换一组「生活与任务」；`WORK_READ_PER_UNIT`
  等已退役参数不再登记。
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
        # v0.89：SCHED_*（上课作息 8 半节）整族退役——节奏改由章节边界决定，
        # 面板两组（阅读真读 / 工作系统）合并为「生活与任务」。
        "name": "阅读真读",
        "items": [
            dict(k="READING_DAILY_CHARS", l="当日字数上限", v="L1", t="int",
                 h="每日圈定读书字数（非空白），宁少勿超；阅读速度由它推导（÷320min）", min=0, step=1),
            dict(k="READING_TXT_FALLBACK_MIN", l="txt 切章下限", v="L2", t="int",
                 h="txt 没有章节标题时，按累计字数切章的下限", min=0, step=100),
            dict(k="READING_TXT_FALLBACK_MAX", l="txt 切章上限", v="L2", t="int",
                 h="按累计字数切章的上限", min=0, step=100),
            dict(k="READING_SUMMARY_THRESHOLD", l="阶段大总结阈值", v="L2", t="int",
                 h="累计读满这么多字触发一次阶段性大总结", min=0, step=500),
            dict(k="READING_CROSS_BOOK_COUNT", l="跨书梳理触发", v="L2", t="int",
                 h="读满几本触发跨书梳理", min=1, step=1),
        ],
    },
    {
        "name": "主动频率 + 防打扰",
        "items": [
            dict(k="DAILY_PROACTIVE_LIMIT", l="每日主动上限", v="L1", t="int",
                 h="一天最多主动发起几条消息", min=0, step=1),
            dict(k="BUBBLE_IDLE_MIN_MINUTES", l="冒泡空闲下限", v="L1", t="int",
                 h="空闲多久开始想冒泡（区间下限）", min=0, step=1),
            dict(k="BUBBLE_IDLE_MAX_MINUTES", l="冒泡空闲上限", v="L1", t="int",
                 h="空闲多久开始想冒泡（区间上限）", min=0, step=1),
            dict(k="BUBBLE_DAILY_LIMIT", l="冒泡日额", v="L1", t="int",
                 h="冒泡独立额度，不占主动消息上限", min=0, step=1),
            dict(k="BUBBLE_FIXED_PROB", l="冒泡抽固定语料概率", v="L2", t="float",
                 h="冒泡直接抽固定语料的概率（省 token）", min=0, max=1, step=0.05),
            dict(k="SHARE_IMPULSE_DAILY_LIMIT", l="分享冲动日预算", v="L1", t="int",
                 h="每天主动开口的分享预算（读到触动/刷到好东西时想聊，防刷屏）", min=0, step=1),
            dict(k="QUIET_AFTER_BUSY_HOURS", l="忙碌静默", v="L2", t="int",
                 h="对方说在忙后静默几小时", min=0, step=1),
            dict(k="MAX_UNANSWERED_PROACTIVE", l="连续未回复停发", v="L2", t="int",
                 h="连续几条主动消息没回就停发", min=0, step=1),
            dict(k="AUTO_FAMILIAR_INFO_THRESHOLD", l="转熟悉阈值", v="L2", t="int",
                 h="profile 已填项 + 便签数达到即自动转熟悉", min=0, step=1),
        ],
    },
    {
        "name": "打断成本 + 情绪 + 紧急",
        "items": [
            dict(k="COST_R_FLOW", l="阻力·心流", v="L1", t="float",
                 h="心流期阻力（打断最伤）", min=0, max=1, step=0.05),
            dict(k="COST_R_WORKING", l="阻力·工作", v="L1", t="float",
                 h="忙自己的事时的阻力", min=0, max=1, step=0.05),
            dict(k="COST_R_IDLE", l="阻力·空闲", v="L1", t="float",
                 h="还没开工时的阻力", min=0, max=1, step=0.05),
            dict(k="COST_R_BREAK", l="阻力·休息", v="L1", t="float",
                 h="歇会儿时的阻力", min=0, max=1, step=0.05),
            dict(k="COST_R_OFF", l="阻力·收工", v="L1", t="float",
                 h="忙完了的阻力（完全欢迎）", min=0, max=1, step=0.05),
            dict(k="COST_WELCOMED_MAX", l="welcomed 阈值", v="L1", t="float",
                 h="costIndex 低于此值 = 欢迎", min=0, max=1, step=0.05),
            dict(k="COST_TOLERATED_MAX", l="tolerated 阈值", v="L1", t="float",
                 h="costIndex 低于此值 = 可以，否则 = 有代价", min=0, max=1, step=0.05),
            dict(k="URGENT_WINDOW_MINUTES", l="紧急窗口", v="L2", t="int",
                 h="识别到有急事后的随叫随到窗口", min=0, step=1),
            dict(k="MOOD_TIRED_ENERGY", l="心情疲惫阈值", v="L2", t="int",
                 h="精力低于此值 = 疲惫", min=0, step=1),
            dict(k="MOOD_EVENING_HOUR", l="晚间时点", v="L2", t="int",
                 h="这之后忙完 = 平静", min=0, max=23, step=1),
        ],
    },
    {
        "name": "记忆召回 + 衰减",
        "items": [
            dict(k="EMBEDDING_SEMANTIC_WEIGHT", l="语义权重", v="L1", t="float",
                 h="语义相似分在混合打分里的权重", min=0, max=20, step=0.5),
            dict(k="EMBEDDING_MIN_SIMILARITY", l="相似度下限", v="L1", t="float",
                 h="余弦相似度低于此值不算语义命中", min=0, max=1, step=0.05),
            dict(k="MEMORY_DECAY_IDLE_DAYS", l="衰减基准", v="L2", t="int",
                 h="多久没被想起 → 遗忘一级（基准天数）", min=1, step=1),
            dict(k="MEMORY_DECAY_RECALL_BONUS", l="召回加成", v="L2", t="float",
                 h="每被想起一次的有效期加成", min=0, max=10, step=0.1),
            dict(k="MEMORY_DECAY_MAX_LEVEL", l="压级", v="L2", t="int",
                 h="压到第几级彻底遗忘（0=原文）", min=0, step=1),
            dict(k="MEMORY_DECAY_PIN_RECALLS", l="永久阈值", v="L2", t="int",
                 h="被想起够多次 → 永久记住", min=1, step=1),
            dict(k="MEMORY_DECAY_BATCH", l="批量", v="L2", t="int",
                 h="一次遗忘最多处理几条", min=1, step=1),
            dict(k="TODAY_NOTES_MAX", l="今日素材条数", v="L2", t="int",
                 h="最多注入几条今日素材", min=0, step=1),
            dict(k="MEMORY_RECALL_POOL", l="召回窗口", v="L2", t="int",
                 h="字面/标签召回的候选窗口条数（语义召回走全量向量池，不受此限）", min=50, step=50),
            dict(k="TODAY_NOTES_CHARS", l="今日素材截断", v="L2", t="int",
                 h="每条正文注入截断字数", min=0, step=10),
            dict(k="MEMORY_TOOL_MAX_ROUNDS", l="检索轮数", v="L2", t="int",
                 h="一轮回复里工具调用循环的最大轮数", min=1, step=1),
        ],
    },
    {
        # v0.89：原「工作系统」组（单元/配额/余额）整体退役，换成生活引擎的节奏参数。
        "name": "生活与任务",
        "items": [
            dict(k="WORK_WARMUP_MIN", l="起床磨蹭", v="L1", t="int",
                 h="醒来后多久才开始读（带抖动）", min=0, step=1),
            dict(k="LUNCH_MIN", l="午休下限", v="L1", t="int",
                 h="跨过中午的那次休息至少多长（午休是涌现的，不排班）", min=0, step=5),
            dict(k="LATE_READ_HOUR", l="读得晚阈值", v="L1", t="int",
                 h="这之后还在读 = 今天读得晚（叙事事件）", min=0, max=23, step=1),
            dict(k="WORK_FLOW_RATIO", l="心流比", v="L1", t="float",
                 h="一段会话前多少比例是心流期（打断代价最高）", min=0, max=1, step=0.05),
            dict(k="WORK_ENERGY_MAX", l="精力上限", v="L2", t="int",
                 h="精力上限 E", min=1, step=1),
            dict(k="WORK_ENERGY_COST", l="精力消耗", v="L2", t="int",
                 h="每 40 分钟消耗多少精力（换算成每分钟率）", min=0, step=1),
            dict(k="WORK_ENERGY_RECOVER", l="精力恢复", v="L2", t="int",
                 h="每 20 分钟恢复多少精力（换算成每分钟率）", min=0, step=1),
            dict(k="WORK_ENERGY_REST_BELOW", l="精力歇点", v="L2", t="int",
                 h="精力低于此值，下次休息延长到回血再开工", min=0, step=1),
        ],
    },
    {
        "name": "人格 + 双层生成 + 兴趣 + 表情",
        "items": [
            dict(k="INNER_MONOLOGUE_TEMPERATURE", l="独白温度", v="L2", t="float",
                 h="独白层温度（比说话层低，要判断不要表演）", min=0, max=2, step=0.05),
            dict(k="INNER_MONOLOGUE_MAX_TOKENS", l="独白 token", v="L2", t="int",
                 h="独白输出上限（很短，额外成本有限）", min=1, step=10),
            dict(k="UTTERANCE_TEMPERATURE", l="说话温度", v="L2", t="float",
                 h="说话层温度（要高，口语的随机性）", min=0, max=2, step=0.05),
            dict(k="SELF_MEMORY_MAX", l="自我记忆注入条数", v="L2", t="int",
                 h="每轮最多注入几条关于自己的记忆", min=0, step=1),
            dict(k="INTEREST_PROMOTE_THRESHOLD", l="兴趣转正阈值", v="L2", t="int",
                 h="同一兴趣主题累计几次自动转正", min=1, step=1),
            dict(k="INTEREST_CONVO_DAYS", l="刷源兴趣回溯天数", v="L2", t="int",
                 h="刷源兴趣判据：回溯最近几个生活日的双方消息（命中的主题算「交流过的」）",
                 min=1, step=1),
            dict(k="EMOJI_POOL_RECENT_N", l="表情防重样窗口", v="L2", t="int",
                 h="扫最近几条消息提取已用表情，防重复", min=0, step=1),
        ],
    },
    {
        "name": "聊天节奏",
        "items": [
            dict(k="READING_SECONDS_PER_CHAR", l="每字阅读时长", v="L2", t="float",
                 h="每字假装阅读多久（秒）", min=0, step=0.01),
            dict(k="READING_MAX_SECONDS", l="阅读上限", v="L2", t="float",
                 h="阅读等待上限（秒）", min=0, step=1),
            dict(k="TYPING_SECONDS_PER_CHAR", l="每字输入时长", v="L2", t="float",
                 h="每字假装输入多久（秒）", min=0, step=0.01),
            dict(k="TYPING_MIN_SECONDS", l="输入下限", v="L2", t="float",
                 h="输入最短多久（秒）", min=0, step=0.1),
            dict(k="TYPING_MAX_SECONDS", l="输入上限", v="L2", t="float",
                 h="输入最长多久（秒）", min=0, step=1),
            dict(k="SEND_SECONDS_PER_CHAR", l="分段间隔", v="L2", t="float",
                 h="分段连发的间隔（按上一条长度，秒/字）", min=0, step=0.01),
            dict(k="SEND_MIN_SECONDS", l="分段下限", v="L2", t="float",
                 h="分段最短间隔（秒）", min=0, step=0.1),
            dict(k="SEND_MAX_SECONDS", l="分段上限", v="L2", t="float",
                 h="分段最长间隔（秒）", min=0, step=1),
        ],
    },
    {
        "name": "意识流 + 上下文 + 离线作息",
        "items": [
            dict(k="CONSCIOUS_STREAM_RECENT", l="意识流注入条数", v="L2", t="int",
                 h="每轮注入最近几条自我意识流", min=0, step=10),
            dict(k="CONSCIOUS_STREAM_LINE_CHARS", l="意识流行截断", v="L2", t="int",
                 h="每行截断字数（0=不截断，全文注入）", min=0, step=10),
            dict(k="CONTEXT_MAX_MESSAGES", l="会话消息上限", v="L2", t="int",
                 h="一次对话最多读多少条（防刷 token）", min=1, step=10),
            dict(k="PROACTIVE_CONTEXT_MAX_MESSAGES", l="主动会话上限", v="L2", t="int",
                 h="主动消息同理", min=1, step=10),
            dict(k="REST_SLEEP_EARLY_MAX", l="早睡浮动", v="L2", t="int",
                 h="早睡最多提前多少分钟", min=0, step=1),
            dict(k="REST_SLEEP_LATE_MAX", l="熬夜浮动", v="L2", t="int",
                 h="熬夜最多延后多少分钟", min=0, step=1),
            dict(k="REST_WAKE_LIE_IN_MAX", l="赖床浮动", v="L2", t="int",
                 h="赖床最多延后多少分钟", min=0, step=1),
            dict(k="REST_WAKE_EARLY_MAX", l="早醒浮动", v="L2", t="int",
                 h="早醒最多提前多少分钟", min=0, step=1),
            dict(k="REST_TIRED_ENERGY", l="累阈值", v="L2", t="int",
                 h="精力低于此值 = 累，倾向早睡", min=0, step=1),
            dict(k="REST_ENERGETIC_ENERGY", l="精神阈值", v="L2", t="int",
                 h="精力高于此值 = 精力足，倾向熬夜", min=0, step=1),
        ],
    },
    {
        # v0.94 信息获取：把人设第三件事「刷手机看信息」从名词变成动词（方案 §11）。
        # 开关类（总开关/次数/上限）走 L1；截断/窗口这类调优项走 L2。
        "name": "信息获取",
        "items": [
            dict(k="BROWSE_ENABLED", l="刷信息总开关", v="L1", t="bool",
                 h="关掉 = 回归旧行为（不抓外部源、注入块也不出现）"),
            dict(k="BROWSE_DAILY_PASSES", l="一天刷几次", v="L1", t="int",
                 h="每天最多自主刷几轮（接作息后生效）", min=0, step=1),
            dict(k="BROWSE_ITEMS_PER_PASS", l="单源取几条", v="L1", t="int",
                 h="每个源一次最多取几条", min=1, step=1),
            dict(k="HTTP_TIMEOUT_SEC", l="取数超时", v="L1", t="int",
                 h="外部请求超时（秒），超时当没刷到", min=1, step=1),
            dict(k="LOOKUP_ENABLED", l="按需查证开关", v="L1", t="bool",
                 h="对话里允许它自己上网看一眼（含贴链接让它看、按需看一眼）"),
            dict(k="LOOKUP_MAX_PER_REPLY", l="每轮取数上限", v="L1", t="int",
                 h="一轮回复最多几次网络取数", min=0, step=1),
            dict(k="LOOKUP_DAILY_LIMIT", l="每日取数上限", v="L1", t="int",
                 h="每天最多几次网络取数", min=0, step=1),
            dict(k="SEARCH_PROVIDER", l="搜索适配器", v="L1", t="str",
                 h="搜索引擎适配器；none = 只用 RSS/网页"),
            dict(k="BROWSE_CONTEXT_MAX", l="注入几条", v="L2", t="int",
                 h="每轮最多注入几条刷到的", min=0, step=1),
            dict(k="BROWSE_ITEM_CHARS", l="注入截断", v="L2", t="int",
                 h="每条注入截断字数", min=0, step=10),
            dict(k="SOURCE_MAX_CHARS", l="正文截断", v="L2", t="int",
                 h="抓到的正文参与摘要前的截断", min=200, step=500),
            dict(k="HTTP_CACHE_TTL_MIN", l="去重窗口", v="L2", t="int",
                 h="同一 URL 多久内不重复抓（分钟）", min=0, step=60),
            dict(k="INFO_ITEMS_KEEP_DAYS", l="刷到记录保留", v="L2", t="int",
                 h="刷到的东西保留多少天", min=1, step=10),
            dict(k="TOOL_RESULT_CHARS", l="工具返回上限", v="L2", t="int",
                 h="工具返回给模型的文本上限（防烧 token）", min=100, step=100),
            dict(k="SOURCES_FILE", l="订阅源文件", v="L2", t="str",
                 h="订阅源列表文件路径（一行一条 URL；不存在时用出厂种子）"),
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
                "default": _default_of(it), "desc": it["h"], "value": _read_raw(it),
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
    v = getattr(config, item["k"])
    if item["t"] == "bool":          # config 里是 Python bool，面板要的是 on/off
        return "on" if v else "off"
    if item["t"] == "str":           # 可能不是 str（如 SOURCES_FILE 是 Path）→ 统一转字符串
        return str(v)
    return v


def _default_of(item: dict) -> str:
    """默认值**单头真相**：直接取 config 的当前常量（v0.88 E1，搭 v0.89 批 A）。

    旧实现在清单里手写 `d="100"`，与 config.py 双头维护、靠一条测试防漂移——
    需要测试盯着本身就是设计错了。现在 tuning 只留纯元数据
    （分组 / 标签 / 层级 / 类型 / 范围），默认值一律 `getattr(config, k)` + 类型适配。
    """
    v = getattr(config, item["k"], "")
    if item["t"] == "bool":
        return "on" if v else "off"
    if item["t"] == "int":
        try:
            return str(int(v))
        except (TypeError, ValueError):
            return str(v)
    if item["t"] == "float":
        try:
            return f"{float(v):g}"
        except (TypeError, ValueError):
            return str(v)
    return str(v)


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
    elif t == "bool":
        s = str(raw).strip().lower()
        if s in ("on", "true", "1", "yes"):
            return "on"
        if s in ("off", "false", "0", "no"):
            return "off"
        raise ValueError(f"{label}（{key}）得是 on / off")
    elif t == "str":
        return str(raw or "").strip()
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
