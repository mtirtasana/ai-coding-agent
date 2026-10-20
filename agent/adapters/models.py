from collections.abc import Sequence

from agent.core.types import Message, ToolCall, ToolSpec


class OpenAIProvider:
    """OpenAI Responses API; SDK wird nur beim Erstellen dieses Adapters geladen."""
    def __init__(self, model: str, client=None):
        if client is None:
            from openai import OpenAI
            client = OpenAI()
        self.client, self.model = client, model

    def generate(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Message:
        items = []
        for message in messages:
            if message.role == "tool":
                items.append({"type": "function_call_output", "call_id": message.call_id,
                              "output": message.content})
            else:
                if message.content:
                    items.append({"role": message.role, "content": message.content})
                items.extend({"type": "function_call", "call_id": c.id,
                              "name": c.name, "arguments": c.arguments}
                             for c in message.tool_calls)
        response = self.client.responses.create(
            model=self.model, input=items, store=False,
            tools=[{"type": "function", "name": t.name, "description": t.description,
                    "parameters": t.parameters, "strict": True} for t in tools])
        calls = tuple(ToolCall(c.call_id, c.name, c.arguments)
                      for c in response.output if c.type == "function_call")
        return Message("assistant", response.output_text, calls)


class OllamaProvider:
    """Lokale Ollama Chat API mit Werkzeugaufrufen; nur Standardbibliothek."""
    def __init__(self, model: str, base_url: str = "http://localhost:11434",
                 timeout: float = 120):
        self.model, self.base_url, self.timeout = model, base_url.rstrip("/"), timeout

    def generate(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Message:
        import json
        from urllib.request import Request, urlopen
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
        request = Request(self.base_url + "/api/chat", data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=self.timeout) as response:
            result = json.load(response)["message"]
        calls = tuple(ToolCall(f"ollama-{len(messages)}-{i}", c["function"]["name"],
                              json.dumps(c["function"]["arguments"]))
                      for i, c in enumerate(result.get("tool_calls") or []))
        return Message("assistant", result.get("content") or "", calls)


class DemoProvider:
    """Deterministischer Offline-Durchlauf, kein Sprachmodell."""
    def generate(self, messages, tools):
        if messages[-1].role == "user":
            return Message("assistant", tool_calls=(ToolCall("demo-1", "list_files",
                                                           '{"path":"."}'),))
        return Message("assistant", "Offline-Demo abgeschlossen: " + messages[-1].content)
