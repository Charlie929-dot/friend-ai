"""DeepSeek 对话引擎。无 API key 时降级为 stub，保证本地可调。"""
import httpx

import config
import corpus


class LLMUnavailable(Exception):
    pass


def chat(messages: list[dict], temperature: float = 0.8) -> str:
    """messages: OpenAI 格式 [{"role": ..., "content": ...}]，返回回复文本。"""
    if not config.DEEPSEEK_API_KEY:
        return corpus.LLM_STUB
    import db  # 延迟导入，/model 命令的运行时覆盖
    model = db.get_kv("model_override", config.DEEPSEEK_MODEL)
    try:
        resp = httpx.post(
            f"{config.DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except (httpx.HTTPError, KeyError, IndexError) as e:
        raise LLMUnavailable(str(e)) from e


def chat_with_tools(messages: list[dict], tools: list[dict],
                    temperature: float = 0.8) -> dict:
    """带工具定义调用（function calling）。返回 OpenAI 格式的 message：
    {"content": str, "tool_calls": [{"id", "type", "function": {"name", "arguments"}}]}。

    无 API key 时降级为普通 stub 回复（tool_calls 空），保证本地可调。"""
    if not config.DEEPSEEK_API_KEY:
        return {"content": corpus.LLM_STUB, "tool_calls": []}
    import db  # 延迟导入，/model 命令的运行时覆盖
    model = db.get_kv("model_override", config.DEEPSEEK_MODEL)
    try:
        resp = httpx.post(
            f"{config.DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "tools": tools,
            },
            timeout=60,
        )
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        return {
            "content": (msg.get("content") or "").strip(),
            "tool_calls": msg.get("tool_calls") or [],
        }
    except (httpx.HTTPError, KeyError, IndexError) as e:
        raise LLMUnavailable(str(e)) from e
