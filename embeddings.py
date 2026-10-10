"""本地向量化（语义召回用）：bge-small-zh-v1.5 的 ONNX 推理。

为什么本地：DeepSeek 没有 embedding API；云端 embedding 会把记忆文本上传第三方，
违背「数据永远只在本地」的卖点。所以用 onnxruntime 在本地 CPU 上跑 bge-small-zh-v1.5
——不需要 torch / GPU / sentence-transformers，只靠 onnxruntime + tokenizers 两个轻依赖。

模型文件放 `models/bge-small-zh-v1.5/`（model.onnx + tokenizer.json，不进 git——
100MB 单文件超 GitHub/Gitee 限制；服务器需单独下载，见 sync 脚本说明）。

降级约定：模型文件缺失 / onnxruntime 没装时，`embed()` 返回 None，
`memory.py` 的 recall() 自动退回纯字面召回——语义召回是增强，不是必须。
"""
import os
from pathlib import Path

import numpy as np

import config

MODEL_DIR = Path(config.EMBEDDING_MODEL_DIR)

_session = None
_tokenizer = None
_load_error = None
_loaded = False


def available() -> bool:
    """模型与依赖是否就绪（就绪才启用语义召回）。首次调用会尝试惰性加载。"""
    global _loaded
    if not _loaded:
        _try_load()
    return _session is not None


def load_error() -> str:
    """加载失败原因（调试/日志用），未失败返回空串。"""
    if not _loaded:
        _try_load()
    return _load_error or ""


def _try_load() -> None:
    global _loaded, _session, _tokenizer, _load_error
    if _loaded:
        return
    _loaded = True
    model_path = MODEL_DIR / "model.onnx"
    tokenizer_path = MODEL_DIR / "tokenizer.json"
    if not model_path.exists() or not tokenizer_path.exists():
        _load_error = f"embedding 模型文件缺失：{model_path}（语义召回退回纯字面）"
        return
    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer
    except Exception as e:  # 依赖没装（如精简部署）
        _load_error = f"缺少 onnxruntime/tokenizers 依赖：{e}"
        return
    try:
        _session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"])
        _tokenizer = Tokenizer.from_file(str(tokenizer_path))
        _tokenizer.enable_truncation(max_length=512)
        _tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
    except Exception as e:
        _load_error = f"加载 embedding 模型失败：{e}"
        _session = None
        _tokenizer = None


def embed(texts: list[str]) -> np.ndarray | None:
    """把一批文本编码成 L2 归一化的句向量（CLS pooling）。

    返回形状 (len(texts), 512) 的 float32 数组；不可用时返回 None。
    """
    if not texts:
        return np.zeros((0, 512), dtype=np.float32)
    if not available():
        return None
    enc = _tokenizer.encode_batch([t or " " for t in texts])
    input_ids = np.array([e.ids for e in enc], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
    token_type_ids = np.array([e.type_ids for e in enc], dtype=np.int64)

    # 动态取输入名（不同 ONNX 导出版本的命名可能略不同），输出取第一个
    in_names = [i.name for i in _session.get_inputs()]
    out_name = _session.get_outputs()[0].name
    feed = {}
    for n in in_names:
        if n == "input_ids":
            feed[n] = input_ids
        elif n == "attention_mask":
            feed[n] = attention_mask
        elif n == "token_type_ids":
            feed[n] = token_type_ids

    last_hidden = _session.run([out_name], feed)[0]          # (batch, seq, hidden)
    cls = last_hidden[:, 0, :].astype(np.float32)            # CLS pooling
    norms = np.linalg.norm(cls, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return cls / norms


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """两个已归一化向量的余弦相似度（标量）。"""
    return float(np.dot(a, b))
