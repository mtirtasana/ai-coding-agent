"""Optional model-specific estimation without coupling the engine to a provider."""
import math
import threading
from concurrent.futures import Future, TimeoutError

from agent.core.tokens import ByteTokenEstimator, TokenEstimate, request_text


_encodings = {}
_lock = threading.Lock()


def optional_encoding(provider, model):
    if provider != 'openai':
        return None
    try:
        import tiktoken
        name = tiktoken.encoding_name_for_model(model)
    except (ImportError, KeyError):
        return None
    with _lock:
        if name not in _encodings:
            if len(_encodings) >= 8:
                return None
            pending = Future()
            _encodings[name] = pending
            def load():
                try:
                    pending.set_result(tiktoken.encoding_for_model(model))
                except Exception:
                    # Optional assets must never stall inference or expose raw
                    # network errors. At most one daemon load per encoding.
                    pending.set_result(None)
            threading.Thread(target=load, daemon=True).start()
        return _encodings[name]


class ModelTokenEstimator(ByteTokenEstimator):
    def __init__(self, provider, model):
        self.pending = optional_encoding(provider, model)

    def estimate(self, messages, tools):
        try:
            encoding = self.pending.result(timeout=0.05) if self.pending is not None else None
        except TimeoutError:
            encoding = None
        if encoding is None:
            return super().estimate(messages, tools)
        count = len(encoding.encode(request_text(messages, tools), disallowed_special=()))
        return TokenEstimate(math.ceil(count * 1.15) + 32 * (len(messages) + len(tools)) + 256,
                             'tiktoken:' + encoding.name + '+margin')
