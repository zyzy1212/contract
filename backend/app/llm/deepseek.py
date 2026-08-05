from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Sequence
from typing import Any, TypeVar
from weakref import WeakKeyDictionary

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.common.errors import ConfigurationError, DomainError, InfrastructureError
from app.config import Settings, get_settings


ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)

MODEL_REQUEST_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS = 1.0


def _parse_api_keys(value: str) -> list[str]:
    return [key.strip() for key in value.split(",") if key.strip()]


class ModelUnavailable(InfrastructureError):
    """The configured model provider could not complete a request."""


class InvalidModelOutput(DomainError, ValueError):
    """Model output cannot be parsed into the required structured schema."""


class DeepSeekClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client_factory: Callable[[], AsyncOpenAI] | None = None,
        max_attempts: int | None = None,
        base_delay_seconds: float | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        if max_attempts is None:
            max_attempts = self._settings.deepseek_max_retries
        if base_delay_seconds is None:
            base_delay_seconds = (
                self._settings.deepseek_retry_base_delay_seconds
            )
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be positive")
        self._api_keys = tuple(_parse_api_keys(self._settings.deepseek_api_key))
        self._client_factory = client_factory
        self._clients_by_loop: WeakKeyDictionary[
            asyncio.AbstractEventLoop,
            tuple[AsyncOpenAI, ...],
        ] = WeakKeyDictionary()
        self._clients_lock = threading.Lock()
        self._next_key_index = 0
        self._max_attempts = max_attempts
        self._base_delay_seconds = base_delay_seconds

    @property
    def base_url(self) -> str:
        return self._settings.deepseek_base_url

    @property
    def generation_model(self) -> str:
        return self._settings.deepseek_generation_model

    @property
    def review_model(self) -> str:
        return self._settings.deepseek_review_model

    @property
    def api_key_count(self) -> int:
        return len(self._api_keys)

    async def complete_json(
        self,
        model: str,
        messages: Sequence[dict[str, str]],
        response_model: type[ResponseModelT],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ResponseModelT:
        if not model.strip():
            raise ValueError("model must not be empty")
        if not messages:
            raise ValueError("messages must not be empty")
        clients = self._clients_for_current_loop()
        with self._clients_lock:
            start = self._next_key_index % len(clients)
            self._next_key_index += 1
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            client = clients[(start + attempt - 1) % len(clients)]
            try:
                completion = await client.chat.completions.create(
                    model=model,
                    messages=[dict(item) for item in messages],
                    response_format={"type": "json_object"},
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except (
                openai.APITimeoutError,
                openai.APIConnectionError,
                openai.RateLimitError,
                openai.InternalServerError,
            ) as exc:
                last_error = exc
                if attempt == self._max_attempts:
                    break
                if len(clients) > 1:
                    continue
                await asyncio.sleep(
                    self._base_delay_seconds * (2 ** (attempt - 1))
                )
                continue
            except openai.APIStatusError as exc:
                raise ModelUnavailable(
                    f"model provider rejected the request with status {exc.status_code}"
                ) from exc
            return _parse_json_completion(completion, response_model)
        raise ModelUnavailable(
            f"model unavailable after {self._max_attempts} attempts"
        ) from last_error

    def _clients_for_current_loop(self) -> tuple[AsyncOpenAI, ...]:
        if not self._api_keys:
            raise ConfigurationError("deepseek_api_key is not configured")
        loop = asyncio.get_running_loop()
        with self._clients_lock:
            clients = self._clients_by_loop.get(loop)
            if clients is None:
                clients = tuple(
                    self._build_client(api_key) for api_key in self._api_keys
                )
                self._clients_by_loop[loop] = clients
            return clients

    def _build_client(self, api_key: str) -> AsyncOpenAI:
        if self._client_factory is not None:
            try:
                return self._client_factory(api_key)
            except TypeError:
                return self._client_factory()
        return AsyncOpenAI(
            base_url=self._settings.deepseek_base_url,
            api_key=api_key,
            timeout=self._settings.deepseek_timeout_seconds,
            max_retries=0,
        )


def _message_content(completion: Any) -> str:
    try:
        message = completion.choices[0].message
    except (AttributeError, IndexError, TypeError) as exc:
        raise InvalidModelOutput("model returned no message") from exc
    content = getattr(message, "content", None)
    if content is None:
        raise InvalidModelOutput("model returned empty content")
    content = str(content).strip()
    if not content:
        raise InvalidModelOutput("model returned empty content")
    return content


def _parse_json_completion(
    completion: Any,
    response_model: type[ResponseModelT],
) -> ResponseModelT:
    content = _message_content(completion)
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise InvalidModelOutput("model output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidModelOutput("model output must be a JSON object")
    try:
        return response_model.model_validate(payload)
    except ValidationError as exc:
        detail = exc.errors()[0]
        location = ".".join(str(part) for part in detail["loc"])
        raise InvalidModelOutput(
            f"model output violates {response_model.__name__} at {location}: {detail['msg']}"
        ) from exc
