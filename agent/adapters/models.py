import json
from collections.abc import Sequence
from uuid import uuid4

from agent.core.types import Continuation, Message, ModelResponse, ModelUsage, ToolCall, ToolSpec


def token_count(value):
    """Keep unavailable/malformed usage unknown; zero is a real reported count."""
    return value if type(value) is int and value >= 0 else None


def responses_items(message: Message):
    """Validate checkpoint data before replaying it as Responses input."""
    continuation = message.continuation
    if (message.role != "assistant" or continuation.format != "openai.responses.v1"
            or not isinstance(continuation.payload, str)):
        raise ValueError("Invalid Responses continuation.")
    items = json.loads(continuation.payload)
    if not isinstance(items, list):
        raise ValueError("Invalid Responses output items.")
    calls = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid Responses output item.")
        kind = item.get("type")
        if kind == "function_call":
            if any(not isinstance(item.get(key), str) for key in ("call_id", "name", "arguments")):
                raise ValueError("Invalid Responses function call.")
            calls.append(ToolCall(item["call_id"], item["name"], item["arguments"]))
        elif kind == "reasoning":
            if (not isinstance(item.get("id"), str) or not isinstance(item.get("summary"), list)
                    or ("encrypted_content" in item and not isinstance(item["encrypted_content"], str))):
                raise ValueError("Invalid Responses reasoning item.")
        elif kind == "message":
            if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
                raise ValueError("Invalid Responses assistant item.")
        else:
            # Only custom function tools are enabled by this adapter.
            raise ValueError("Unsupported Responses output item.")
    if tuple(calls) != message.tool_calls:
        raise ValueError("Responses continuation disagrees with tool calls.")
    return items


class OpenAIProvider:
    """OpenAI Responses API; SDK wird nur beim Erstellen dieses Adapters geladen."""
    def __init__(self, model: str, client=None, *, timeout=120, max_output_tokens=4096, api_key=None):
        if client is None:
            from openai import OpenAI
            options = {} if api_key is None else {'api_key': api_key}
            client = OpenAI(timeout=timeout, max_retries=0, **options)
        self.client, self.model = client, model
        self.max_output_tokens = max_output_tokens

    def generate(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelResponse:
        items = []
        for message in messages:
            if message.continuation is not None:
                items.extend(responses_items(message))
                continue
            if message.role == "tool":
                items.append({"type": "function_call_output", "call_id": message.call_id,
                              "output": message.content})
            else:
                if message.content:
                    items.append({"role": message.role, "content": message.content})
                items.extend({"type": "function_call", "call_id": c.id,
                              "name": c.name, "arguments": c.arguments}
                             for c in message.tool_calls)
        options = {} if self.max_output_tokens is None else {"max_output_tokens": self.max_output_tokens}
        response = self.client.responses.create(
            **options,
            model=self.model, input=items, store=False,
            include=["reasoning.encrypted_content"],
            tools=[{"type": "function", "name": t.name, "description": t.description,
                    "parameters": t.parameters, "strict": True} for t in tools])
        calls = tuple(ToolCall(c.call_id, c.name, c.arguments)
                      for c in response.output if c.type == "function_call")
        raw_usage = getattr(response, "usage", None)
        usage = None
        if raw_usage is not None:
            details = getattr(raw_usage, "input_tokens_details", None)
            usage = ModelUsage(
                input_tokens=token_count(getattr(raw_usage, "input_tokens", None)),
                output_tokens=token_count(getattr(raw_usage, "output_tokens", None)),
                cached_input_tokens=token_count(getattr(details, "cached_tokens", None)))
        continuation = Continuation("openai.responses.v1", json.dumps(
            [item.model_dump(mode="json", exclude_none=True) for item in response.output],
            ensure_ascii=False, separators=(",", ":")))
        message = Message("assistant", response.output_text, calls, continuation=continuation)
        responses_items(message)
        return ModelResponse(message, usage, getattr(response, 'status', None) == 'incomplete')


class OllamaProvider:
    """Lokale Ollama Chat API mit Werkzeugaufrufen; nur Standardbibliothek."""
    def __init__(self, model: str, base_url: str = "http://localhost:11434",
                 timeout: float = 120, *, max_output_tokens=4096):
        from urllib.parse import urlsplit
        url = urlsplit(base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username or url.password or url.query or url.fragment):
            raise ValueError("Base URL must be HTTP(S), without credentials, query or fragment.")
        self.model, self.base_url, self.timeout = model, base_url.rstrip("/"), timeout
        self.max_output_tokens = max_output_tokens

    def generate(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelResponse:
        import json
        from urllib.request import Request, build_opener, HTTPRedirectHandler

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                if fp is not None:
                    fp.close()
                raise ValueError("Ollama endpoint redirects are not supported.")

        items = []
        names = {}
        for message in messages:
            item = {"role": message.role, "content": message.content}
            if message.tool_calls:
                item["tool_calls"] = [{"function": {"name": c.name,
                    "arguments": json.loads(c.arguments)}} for c in message.tool_calls]
                names.update({c.id: c.name for c in message.tool_calls})
            if message.role == "tool":
                item["tool_name"] = names[message.call_id]
            items.append(item)
        payload = {"model": self.model, "stream": False, "messages": items,
                   "tools": [{"type": "function", "function": {
                       "name": t.name, "description": t.description,
                       "parameters": t.parameters}} for t in tools]}
        if self.max_output_tokens is not None:
            payload["options"] = {"num_predict": self.max_output_tokens}
        request = Request(self.base_url + "/api/chat", data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        with build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError('Provider response exceeds 4 MiB.')
            payload = json.loads(raw)
        result = payload["message"]
        # Selected history can have the same length in successive requests.
        response_id = uuid4().hex
        calls = tuple(ToolCall(f"ollama-{response_id}-{i}", c["function"]["name"],
                              json.dumps(c["function"]["arguments"]))
                      for i, c in enumerate(result.get("tool_calls") or []))
        input_tokens = token_count(payload.get("prompt_eval_count"))
        output_tokens = token_count(payload.get("eval_count"))
        usage = (None if input_tokens is None and output_tokens is None else
                 ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens))
        return ModelResponse(Message("assistant", result.get("content") or "", calls), usage, payload.get('done_reason') == 'length')


class OpenAICompatibleProvider:
    """Chat Completions with tool calling; no Responses fallback or SDK retries."""
    def __init__(self, model: str, base_url: str, api_key: str, client=None, *, timeout=120, max_output_tokens=4096):
        from urllib.parse import urlsplit
        url = urlsplit(base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username or url.password or url.query or url.fragment):
            raise ValueError("Base URL must be HTTP(S), without credentials, query or fragment.")
        if not model or not api_key:
            raise ValueError("Compatible provider requires a model and an API key.")
        if client is None:
            from openai import OpenAI
            client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)
        self.client, self.model = client, model
        self.max_output_tokens = max_output_tokens

    def generate(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelResponse:
        items = []
        for message in messages:
            item = {"role": message.role, "content": message.content}
            if message.role == "tool":
                item["tool_call_id"] = message.call_id
            if message.tool_calls:
                item["content"] = message.content or None
                item["tool_calls"] = [{"id": call.id, "type": "function", "function": {
                    "name": call.name, "arguments": call.arguments}} for call in message.tool_calls]
            items.append(item)
        kwargs = {"model": self.model, "messages": items, "stream": False}
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {
                "name": tool.name, "description": tool.description,
                "parameters": tool.parameters}} for tool in tools]
        if self.max_output_tokens is not None:
            kwargs["max_tokens"] = self.max_output_tokens
        response = self.client.chat.completions.create(**kwargs)
        result = response.choices[0].message
        calls = tuple(ToolCall(call.id, call.function.name, call.function.arguments)
                      for call in (result.tool_calls or []))
        raw = getattr(response, "usage", None)
        usage = None
        if raw is not None:
            details = getattr(raw, "prompt_tokens_details", None)
            usage = ModelUsage(token_count(getattr(raw, "prompt_tokens", None)),
                               token_count(getattr(raw, "completion_tokens", None)),
                               token_count(getattr(details, "cached_tokens", None)))
        return ModelResponse(Message("assistant", result.content or "", calls), usage, getattr(response.choices[0], 'finish_reason', None) == 'length')


class DemoProvider:
    """Deterministischer Offline-Durchlauf, kein Sprachmodell."""
    def generate(self, messages, tools):
        if messages[-1].role == "user":
            call = ToolCall("demo-1", "list_files", '{"path":"."}')
            return ModelResponse(Message("assistant", tool_calls=(call,)))
        message = Message("assistant", "Offline-Demo abgeschlossen: " + messages[-1].content)
        return ModelResponse(message)
