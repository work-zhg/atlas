"""大模型客户端：OpenAI 兼容接口 + 结果缓存。

★ 裁判调用按（模型, 消息, 参数）的哈希缓存在 SQLite：重新判分、调整报告时不重复花钱。
  生成调用不缓存 —— 生成结果已经落在运行目录的原始结果里，断点续跑靠那份文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

__all__ = ["ChatResult", "LlmClient"]


@dataclass
class ChatResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


class _Cache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS llm (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._db.commit()

    def get(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM llm WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO llm (key, value) VALUES (?, ?)", (key, value))
        self._db.commit()


class LlmClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        cache_path: Path | None = None,
        timeout_s: float = 300.0,
        concurrency: int = 4,
        client: Any = None,
    ) -> None:
        self.base_url = base_url
        self.raw = client or AsyncOpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=3
        )
        self._cache = _Cache(cache_path) if cache_path else None
        self._sem = asyncio.Semaphore(concurrency)

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        json_mode: bool = False,
        cache: bool = False,
    ) -> ChatResult:
        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            params["response_format"] = {"type": "json_object"}
        key = hashlib.sha256(
            json.dumps(params, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if cache and self._cache and (hit := self._cache.get(key)) is not None:
            return ChatResult(**json.loads(hit))
        async with self._sem:
            started = time.monotonic()
            resp = await self.raw.chat.completions.create(**params)
        usage = resp.usage
        result = ChatResult(
            text=(resp.choices[0].message.content or "").strip(),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        if cache and self._cache and result.text:
            self._cache.put(key, json.dumps(result.__dict__, ensure_ascii=False))
        return result

    async def chat_json(
        self, model: str, messages: list[dict[str, str]], **kw: Any
    ) -> dict[str, Any]:
        """要求返回 JSON；解析失败重试一次。"""
        for attempt in range(2):
            res = await self.chat(model, messages, json_mode=True, cache=attempt == 0, **kw)
            text = (
                res.text.strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
            try:
                value = json.loads(text)
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                continue
        raise ValueError(f"裁判没有返回合法的 JSON：{res.text[:200]}")
