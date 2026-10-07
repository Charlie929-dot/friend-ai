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
