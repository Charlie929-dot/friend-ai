"""DeepSeek 对话引擎。无 API key 时降级为 stub，保证本地可调。

v1.0（整体设计方案 §10）接入要点：
- 模型标识符 pin `deepseek-flash`（旧名 `deepseek-v4-flash` 已被静默改路由，防再次
  静默换模型破坏回归）。
- 思考内容走 `reasoning_content` 字段，与正文 `content` 分开返回——阅读"思考即笔记"
  依赖它（`chat_with_reasoning` 取两者）。
- thinking 开启时 temperature / presence_penalty / frequency_penalty 被静默忽略、
  top_p 只接受 0.95–1.0——所以 `thinking=True` 时不传这些采样参数，避免"以为生效其实没"。
- 关思考走 `extra_body.chat_template_kwargs.enable_thinking=false`（打勾、状态写入等
  短回执关思考省 token）。
- 结构化输出（JSON）+ thinking 可能返回 `content: null`——要 JSON 的调用要么关 thinking，
  要么放大 max_tokens；本模块在 `thinking=False` 时照常返回 content。
"""
import httpx

import config
import corpus

# V4 系模型才认 effort 字段（low/high/max）；老模型传了会报错，所以按 id 判断。
_V4_MODEL_IDS = {"deepseek-flash", "deepseek-v4-pro"}


class LLMUnavailable(Exception):
    pass


def _current_model() -> str:
    """当前模型（全局配置）。网页端「模型设置」改完写回 .env 并热生效。"""
    return config.DEEPSEEK_MODEL


def _current_effort() -> str:
    return (config.DEEPSEEK_EFFORT or "").strip().lower()


def _is_v4(model: str) -> bool:
    m = (model or "").strip().lower()
    return m in _V4_MODEL_IDS or "v4" in m


def _payload(messages: list[dict], temperature: float, max_tokens: int,
             tools: list[dict] | None = None,
             thinking: bool | None = None) -> dict:
    """拼请求体。

    thinking：
      None —— 不设 enable_thinking（跟模型默认走，保持旧行为）
      True —— 显式开思考（阅读"思考即笔记"用）；此时**不传** temperature 等采样参数
              （DeepSeek 会静默忽略它们，传了徒增"以为生效"的错觉）
      False—— 显式关思考（JSON 结构化输出 / 短回执省 token 用）；此时 temperature 照常生效
    """
    payload: dict = {
        "model": _current_model(),
        "messages": messages,
    }
    if thinking is not True:
        # 开思考时采样参数被静默忽略，只在关思考 / 未指定时传，避免误导
        payload["temperature"] = temperature
    if max_tokens > 0:
        payload["max_tokens"] = max_tokens
    if tools:
        payload["tools"] = tools
    # effort 只在 V4 上发（默认 low：日常聊天要快和省，不是深度推理）
    if _is_v4(payload["model"]) and _current_effort() in ("low", "high", "max"):
        payload["effort"] = _current_effort()
    if thinking is not None:
        # 关/开思考开关（DeepSeek V4.1 Flash）：官方走 extra_body.chat_template_kwargs
        payload["extra_body"] = {
            "chat_template_kwargs": {"enable_thinking": bool(thinking)}
        }
    return payload


def _post(payload: dict) -> dict:
    """发一次请求，返回完整响应 json。顺带把 token 用量（含缓存命中）打进日志。"""
    resp = httpx.post(
        f"{config.DEEPSEEK_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    _log_usage(data)
    return data


def _log_usage(data: dict) -> None:
    """把 token 用量打进日志——**缓存命中率必须可观测**。

    v0.80 全量读之后每轮输入 1.5–2 万 token：命中 90% 约 1.2 元/天，命中率掉到 50%
    就是 2.2 元/天。不看 `prompt_cache_hit_tokens` 根本不知道钱花在哪。"""
    try:
        u = data.get("usage") or {}
        hit = int(u.get("prompt_cache_hit_tokens") or 0)
        miss = int(u.get("prompt_cache_miss_tokens") or 0)
        total = hit + miss
        rate = f"{hit * 100 // total}%" if total else "-"
        print(f"[token] 输入 {total}（缓存命中 {hit} / 未命中 {miss}，命中率 {rate}）"
              f" 输出 {u.get('completion_tokens', '-')}")
    except Exception:
        pass


def _message(data: dict) -> dict:
    """从完整响应里抽出 message 对象（content + reasoning_content + tool_calls）。"""
    return data["choices"][0]["message"]


def current_config() -> dict:
    """当前 LLM 配置（给网页端「模型设置」面板回填用）。"""
    return {
        "model": _current_model(),
        "effort": _current_effort(),
        "temperature": config.CHAT_TEMPERATURE,
    }


def save_config(model: str = "", effort: str = "",
                temperature: float | None = None) -> dict:
    """保存模型配置：写回 .env + 热生效（**全局**，不按朋友库隔离）。

    为什么是全局：模型/effort/温度是"这台部署怎么跑"的事，不是"这个朋友是谁"的事
    ——放朋友库里换个人就变了，跟网页端"全局配置"的预期对不上（实测 #42 附带的坑：
    /model 原先把 override 存在各库 kv 里）。"""
    updates: dict = {}
    if model:
        updates["DEEPSEEK_MODEL"] = str(model).strip()
    if effort:
        updates["DEEPSEEK_EFFORT"] = str(effort).strip().lower()
    if temperature is not None:
        updates["CHAT_TEMPERATURE"] = str(temperature)
    if not updates:
        return current_config()
    import importlib
    import auth
    auth.write_env(updates)
    config.load_dotenv(force=True)   # .env 是唯一真相，改完强制覆盖环境变量
    importlib.reload(config)         # 刷新本进程里的配置常量（同 /refresh 的做法）
    return current_config()


def list_models() -> list[dict]:
    """拉取可用模型列表（服务端有 key 就能调）。给网页端「模型设置」下拉用——
    官方上新模型会自动出现在列表里，不用改代码。失败返回空列表。"""
    if not config.DEEPSEEK_API_KEY:
        return []
    try:
        resp = httpx.get(
            f"{config.DEEPSEEK_BASE_URL}/models",
            headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
            timeout=20,
        )
        resp.raise_for_status()
        items = resp.json().get("data") or []
        return [x for x in items if isinstance(x, dict) and x.get("id")]
    except (httpx.HTTPError, ValueError, KeyError, AttributeError):
        return []


def chat_with_reasoning(messages: list[dict], temperature: float | None = None,
                        max_tokens: int = 0,
                        thinking: bool | None = None) -> tuple[str, str]:
    """返回 (正文, 思考过程)。阅读"思考即笔记"要拿 reasoning_content 就用这个。

    thinking 语义见 _payload：None=跟默认、True=开思考（采样参数失效）、
    False=关思考（JSON/短回执）。"""
    if not config.DEEPSEEK_API_KEY:
        return corpus.LLM_STUB, ""
    temp = config.CHAT_TEMPERATURE if temperature is None else temperature
    payload = _payload(messages, temp, max_tokens, thinking=thinking)
    try:
        data = _post(payload)
        msg = _message(data)
        return (msg.get("content") or "").strip(), (msg.get("reasoning_content") or "")
    except (httpx.HTTPError, KeyError, IndexError) as e:
        raise LLMUnavailable(str(e)) from e


def chat(messages: list[dict], temperature: float | None = None,
         max_tokens: int = 0, thinking: bool | None = None) -> str:
    """messages: OpenAI 格式 [{"role": ..., "content": ...}]，返回回复文本。

    temperature 不传时用全局 `config.CHAT_TEMPERATURE`（网页端「模型设置」可调）。
    max_tokens > 0 时限制输出长度（给内心独白这种"很短的结构化输出"省 token 用）。
    thinking：None=跟默认 / True=开思考 / False=关思考（见 _payload）。"""
    content, _ = chat_with_reasoning(messages, temperature, max_tokens, thinking)
    return content


def chat_with_tools(messages: list[dict], tools: list[dict],
                    temperature: float | None = None,
                    thinking: bool | None = None) -> dict:
    """带工具定义调用（function calling）。返回 OpenAI 格式的 message：
    {"content": str, "reasoning_content": str, "tool_calls": [{"id","type","function":{...}}]}。

    ⚠️ 多轮 tools：调用方把 assistant 的 tool_calls 消息回填进 messages 时，**必须连
    reasoning_content 一起带上**（否则工具调用质量下降、部分端点直接 400）——所以这里
    把 reasoning_content 一并返回，供上层透传。

    无 API key 时降级为普通 stub 回复（tool_calls 空），保证本地可调。"""
    if not config.DEEPSEEK_API_KEY:
        return {"content": corpus.LLM_STUB, "reasoning_content": "", "tool_calls": []}
    temp = config.CHAT_TEMPERATURE if temperature is None else temperature
    payload = _payload(messages, temp, 0, tools, thinking=thinking)
    try:
        data = _post(payload)
        msg = _message(data)
        return {
            "content": (msg.get("content") or "").strip(),
            "reasoning_content": (msg.get("reasoning_content") or ""),
            "tool_calls": msg.get("tool_calls") or [],
        }
    except (httpx.HTTPError, KeyError, IndexError) as e:
        raise LLMUnavailable(str(e)) from e
