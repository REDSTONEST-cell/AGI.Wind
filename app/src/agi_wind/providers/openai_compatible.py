"""Explicitly configured vendor Chat Completions dialect; no implicit API key discovery."""
import json
import httpx
from ..core.errors import AppError

class OpenAICompatibleProvider:
    def __init__(self, config, credential, client=None):
        self.config, self.credential = config, credential
        self.client = client or httpx.AsyncClient(timeout=config.timeout_seconds)

    def payload(self, model, messages, stream):
        if set(model.parameters) - set(self.config.supported_parameters):
            raise AppError("UNSUPPORTED_PARAMETER", "供应商不支持指定参数", 422)
        params = dict(model.parameters)
        if "max_output_tokens" in params: params["max_tokens"] = params.pop("max_output_tokens")
        return {"model": model.model_id, "messages": messages, "stream": stream, **params}

    async def stream(self, model, messages):
        body = self.payload(model, messages, True)
        try:
            async with self.client.stream("POST", self.config.base_url.rstrip("/") + "/chat/completions", headers={"Authorization": "Bearer " + self.credential}, json=body) as response:
                if response.status_code != 200:
                    raise AppError("PROVIDER_UNAVAILABLE", "厂商请求失败", 503)
                complete = False
                async for line in response.aiter_lines():
                    if not line.startswith("data:"): continue
                    value = line[5:].strip()
                    if value == "[DONE]":
                        complete = True
                        break
                    data = json.loads(value)
                    if "error" in data: raise AppError("PROVIDER_UNAVAILABLE", "厂商返回错误", 503)
                    choices = data.get("choices", [])
                    if choices:
                        text = choices[0].get("delta", {}).get("content")
                        if isinstance(text, str) and text: yield text
                if not complete:
                    raise AppError("PROVIDER_STREAM_INTERRUPTED", "厂商输出中途断开", 503)
        except AppError: raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise AppError("PROVIDER_UNAVAILABLE", "厂商连接或响应不可用", 503) from None

    async def complete(self, model, messages):
        body = self.payload(model, messages, False)
        try:
            response = await self.client.post(self.config.base_url.rstrip("/") + "/chat/completions", headers={"Authorization": "Bearer " + self.credential}, json=body)
            response.raise_for_status()
            value = response.json()["choices"][0]["message"]["content"]
            if not isinstance(value, str): raise ValueError("missing text")
            return value
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
            raise AppError("PROVIDER_UNAVAILABLE", "厂商请求或响应不可用", 503) from None

    async def aclose(self): await self.client.aclose()
