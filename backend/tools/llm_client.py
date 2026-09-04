"""LLM tool — unified multi-provider interface via litellm.

Supports: OpenAI, Anthropic, OpenRouter, Groq, GLM (智谱), Kimi (Moonshot),
MiniMax, and 100+ other providers through litellm.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any

import litellm

from config.settings import Settings, get_settings
from tools.llm_errors import format_llm_error
from tools.llm_rate_limiter import get_llm_rate_limiter


# Suppress litellm's verbose logging by default
litellm.suppress_debug_info = True
logger = logging.getLogger(__name__)


_RESPONSE_FORMAT_UNSUPPORTED_PREFIXES = (
    "zai/",
)

_RATE_LIMIT_BACKOFF_SECONDS = (2, 5, 10)


# ── Input truncation ────────────────────────────────────────────────────────
def truncate_for_llm(text: str, max_chars: int) -> str:
    """Trim ``text`` to at most ``max_chars`` characters for LLM input.

    Keeps the head (instructions/context) and the tail (most recent content),
    dropping the middle — the least useful region for most tasks. Returns the
    original text when it is already short enough or ``max_chars <= 0``.
    """
    if not text or max_chars <= 0 or len(text) <= max_chars:
        return text
    keep = max_chars - 40
    if keep <= 0:
        return text[:max_chars]
    half = keep // 2
    head = text[:half]
    tail = text[-(keep - half):]
    return f"{head}\n...[{len(text) - keep} chars omitted]...\n{tail}"


# ── Deterministic response cache (in-memory, process-wide) ─────────────────
# Identical prompts with temperature==0 are reproducible, so we can skip the
# LLM call entirely on a cache hit. This is the single biggest token saver for
# repeated/duplicate work (e.g. parsing the same website across hunts).
_CACHE_LOCK = threading.Lock()
_RESPONSE_CACHE: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_CACHE_MAX_ENTRIES = 4096


def _cache_key(model: str, system: str, prompt: str, temperature: float,
               max_tokens: int, response_format: dict | None) -> str:
    payload = [model, system or "", prompt or "", temperature, max_tokens, response_format or {}]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str, ttl: int) -> str | None:
    if ttl <= 0:
        return None
    with _CACHE_LOCK:
        item = _RESPONSE_CACHE.get(key)
        if not item:
            return None
        expires, content = item
        if expires and time.time() > expires:
            _RESPONSE_CACHE.pop(key, None)
            return None
        _RESPONSE_CACHE.move_to_end(key)
        return content


def _cache_put(key: str, content: str, ttl: int) -> None:
    if ttl <= 0:
        return
    with _CACHE_LOCK:
        _RESPONSE_CACHE[key] = (time.time() + ttl, content)
        _RESPONSE_CACHE.move_to_end(key)
        while len(_RESPONSE_CACHE) > _CACHE_MAX_ENTRIES:
            _RESPONSE_CACHE.popitem(last=False)


def normalize_minimax_api_base(api_base: str) -> str:
    """Normalize MiniMax API base URLs to the OpenAI-compatible `/v1` form."""
    base = (api_base or "").strip().rstrip("/")
    if not base:
        return api_base
    if base.endswith("/anthropic"):
        logger.warning("Normalizing legacy MiniMax API base from %s to OpenAI-compatible /v1", api_base)
        return base[: -len("/anthropic")] + "/v1"
    return base


def normalize_model_name(model: str) -> str:
    """Normalize legacy provider/model aliases before sending to LiteLLM.

    MiniMax exposes an Anthropic-compatible endpoint, so older config may use
    `anthropic/MiniMax-*`. LiteLLM treats that as the real Anthropic provider
    and expects `ANTHROPIC_API_KEY`, which breaks auth when only
    `MINIMAX_API_KEY` is configured.
    """
    if model.startswith("anthropic/") and "minimax" in model.lower():
        normalized = "minimax/" + model.split("/", 1)[1]
        logger.warning("Normalizing legacy model alias from %s to %s", model, normalized)
        return normalized
    return model


def _is_retryable_rate_limit_error(exc: Exception) -> bool:
    """Return True when the error is transient enough to retry / fall back.

    Covers 429 rate-limits AND quota / balance exhaustion ("缺少流量"), since
    both should trigger a jump to the next candidate model instead of failing.
    """
    message = str(exc or "").lower()
    return (
        "rate_limit" in message
        or '"http_code":"429"' in message
        or '"http_code": "429"' in message
        or "上游返回 429" in message
        or "too many requests" in message
        or "quota" in message
        or "insufficient_balance" in message
        or "insufficient balance" in message
        or "余额不足" in message
        or "额度" in message
        or "exceeded" in message
    )


class _ModelExhausted(Exception):
    """Raised when a single model is exhausted (rate-limited / out of quota) after
    all retry attempts. The caller should jump to the next candidate model.
    """

    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


def _split_fallback_models(raw: str) -> list[str]:
    """Parse a comma-separated fallback list into normalized litellm model names.

    Bare names (no provider '/') are auto-prefixed with ``dashscope/`` so the
    user can write ``kimi-k3`` instead of ``dashscope/kimi-k3``.
    """
    out: list[str] = []
    for part in (raw or "").split(","):
        m = part.strip()
        if not m:
            continue
        if "/" not in m:
            m = "dashscope/" + m
        out.append(m)
    return out


def _provider_key_map(settings: Settings, scope: str) -> dict[str, str]:
    if scope in {"email", "email_reasoning"}:
        return {
            "OPENAI_API_KEY": settings.email_openai_api_key or settings.openai_api_key,
            "ANTHROPIC_API_KEY": settings.email_anthropic_api_key or settings.anthropic_api_key,
            "OPENROUTER_API_KEY": settings.email_openrouter_api_key or settings.openrouter_api_key,
            "GROQ_API_KEY": settings.email_groq_api_key or settings.groq_api_key,
            "ZAI_API_KEY": settings.email_zai_api_key or settings.zai_api_key,
            "MOONSHOT_API_KEY": settings.email_moonshot_api_key or settings.moonshot_api_key,
            "MINIMAX_API_KEY": settings.email_minimax_api_key or settings.minimax_api_key,
            "MINIMAX_API_BASE": normalize_minimax_api_base(settings.minimax_api_base),
            "ZHIPUAI_API_KEY": settings.email_zai_api_key or settings.zai_api_key,
            "DASHSCOPE_API_KEY": settings.dashscope_api_key,
            "OLLAMA_API_BASE": settings.ollama_api_base,
        }
    return {
        "OPENAI_API_KEY": settings.openai_api_key,
        "ANTHROPIC_API_KEY": settings.anthropic_api_key,
        "OPENROUTER_API_KEY": settings.openrouter_api_key,
        "GROQ_API_KEY": settings.groq_api_key,
        "ZAI_API_KEY": settings.zai_api_key,
        "MOONSHOT_API_KEY": settings.moonshot_api_key,
        "MINIMAX_API_KEY": settings.minimax_api_key,
        "MINIMAX_API_BASE": normalize_minimax_api_base(settings.minimax_api_base),
        "ZHIPUAI_API_KEY": settings.zai_api_key,
        "DASHSCOPE_API_KEY": settings.dashscope_api_key,
        "OLLAMA_API_BASE": settings.ollama_api_base,
    }


def _inject_api_keys(settings: Settings, scope: str = "default") -> None:
    """Push provider API keys from Settings into env vars for litellm."""
    _key_map = _provider_key_map(settings, scope)
    for env_var, value in _key_map.items():
        if value:
            os.environ[env_var] = value

    # Native workaround: If using anthropic/ prefix for MiniMax, inject ANTHROPIC_API_BASE
    llm_models = [
        settings.llm_model,
        settings.reasoning_model,
        settings.email_llm_model,
        settings.email_reasoning_model,
    ]
    if any(model.startswith("anthropic/") and "minimax" in model.lower() for model in llm_models if model):
        os.environ["ANTHROPIC_API_BASE"] = normalize_minimax_api_base(settings.minimax_api_base)


def _select_model(settings: Settings, model_type: str) -> str:
    if model_type == "reasoning":
        return settings.reasoning_model
    if model_type == "email":
        return settings.email_llm_model or settings.llm_model
    if model_type == "email_reasoning":
        return settings.email_reasoning_model or settings.reasoning_model
    if model_type == "cheap":
        # Simple tasks (classification, field extraction) route to a small/fast
        # model. Empty => fall back to the default model (no behaviour change).
        return settings.cheap_model or settings.llm_model
    return settings.llm_model


class LLMTool:
    """Unified LLM client powered by litellm.

    All calls go through a single interface so agents don't care about the
    provider.  Just set ``llm_model`` in Settings (or ``LLM_MODEL`` in .env)
    using litellm model naming, e.g.:
      - ``gpt-4o``
      - ``anthropic/claude-3-5-sonnet-20241022``
      - ``openrouter/google/gemini-pro``
      - ``groq/llama-3.3-70b-versatile``
      - ``zai/glm-4.7``
      - ``moonshot/moonshot-v1-128k``
      - ``minimax/MiniMax-Text-01``

    Args:
        model_type: ``"default"`` uses ``llm_model`` (fast, cheap — data extraction).
                    ``"reasoning"`` uses ``reasoning_model`` (strong reasoning — ReAct decisions).
        settings: Optional Settings override.
        hunt_id: Optional hunt ID for cost tracking.
        agent: Agent name label for cost tracking (e.g. "keyword_gen", "email_craft").
        hunt_round: Current hunt round for per-round cost breakdown.
    """

    def __init__(
        self,
        model_type: str = "default",
        settings: Settings | None = None,
        hunt_id: str = "",
        agent: str = "unknown",
        hunt_round: int = 0,
    ) -> None:
        self._settings = settings or get_settings()
        self._model_type = model_type
        self._hunt_id = hunt_id
        self._agent = agent
        self._hunt_round = hunt_round
        _inject_api_keys(self._settings, self._model_type)

    @property
    def model(self) -> str:
        return normalize_model_name(_select_model(self._settings, self._model_type))

    @property
    def _default_temperature(self) -> float:
        if self._model_type in {"reasoning", "email_reasoning"}:
            return self._settings.reasoning_temperature
        return self._settings.llm_temperature

    @property
    def _default_max_tokens(self) -> int:
        if self._model_type in {"reasoning", "email_reasoning"}:
            return self._settings.reasoning_max_tokens
        return self._settings.llm_max_tokens

    @property
    def _requests_per_minute(self) -> int:
        if self._model_type == "reasoning":
            return self._settings.reasoning_requests_per_minute or self._settings.llm_requests_per_minute
        if self._model_type == "email":
            return self._settings.email_llm_requests_per_minute or self._settings.llm_requests_per_minute
        if self._model_type == "email_reasoning":
            return self._settings.email_reasoning_requests_per_minute or self._settings.reasoning_requests_per_minute or self._settings.llm_requests_per_minute
        return self._settings.llm_requests_per_minute

    def _candidate_models(self) -> list[str]:
        """Ordered model list for this tool: primary -> fallback chain -> cheap (兜底).

        The local small model (``cheap_model`` / Ollama) is always appended last
        as the ultimate backstop when every cloud model is rate-limited or out of
        quota ("缺少流量").
        """
        s = self._settings
        if self._model_type == "reasoning":
            primary, fb = s.reasoning_model, s.reasoning_model_fallback
        elif self._model_type == "email":
            primary, fb = (s.email_llm_model or s.llm_model), s.llm_model_fallback
        elif self._model_type == "email_reasoning":
            primary, fb = (s.email_reasoning_model or s.reasoning_model), s.reasoning_model_fallback
        elif self._model_type == "cheap":
            primary, fb = (s.cheap_model or s.llm_model), ""
        else:
            primary, fb = s.llm_model, s.llm_model_fallback

        cands: list[str] = []
        if primary:
            cands.append(normalize_model_name(primary))
        for m in _split_fallback_models(fb):
            m = normalize_model_name(m)
            if m not in cands:
                cands.append(m)
        cheap = normalize_model_name(s.cheap_model) if s.cheap_model else ""
        if cheap and cheap not in cands:
            cands.append(cheap)
        return cands

    async def _completion_with_retry(self, kwargs: dict, limiter) -> Any:
        """Call litellm once per attempt, retrying on transient rate-limit/quota errors.

        Raises ``_ModelExhausted`` after all attempts are exhausted (caller jumps
        to the next candidate). Raises ``RuntimeError`` immediately for non-retryable
        errors.
        """
        last_exc: Exception | None = None
        for attempt in range(len(_RATE_LIMIT_BACKOFF_SECONDS) + 1):
            try:
                await limiter.acquire()
                return await litellm.acompletion(**kwargs)
            except _ModelExhausted:
                raise
            except Exception as exc:
                last_exc = exc
                if not _is_retryable_rate_limit_error(exc):
                    raise RuntimeError(format_llm_error(exc)) from exc
                if attempt < len(_RATE_LIMIT_BACKOFF_SECONDS):
                    delay = _RATE_LIMIT_BACKOFF_SECONDS[attempt]
                    logger.warning(
                        "[LLMTool] model %s rate-limited/quota; retry in %ss (attempt %s/%s)",
                        kwargs.get("model"), delay, attempt + 1,
                        len(_RATE_LIMIT_BACKOFF_SECONDS) + 1,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise _ModelExhausted(exc) from exc
        if last_exc is not None:
            raise _ModelExhausted(last_exc)
        raise _ModelExhausted(RuntimeError("no completion attempt executed"))

    async def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        cache: bool | None = None,
    ) -> str:
        """Generate a completion from the LLM.

        Attempts models in order: primary -> ``llm_model_fallback`` /
        ``reasoning_model_fallback`` chain -> ``cheap_model`` (local Ollama). If a
        model is rate-limited or runs out of quota, it auto-jumps to the next one
        instead of failing (兜底).

        Args:
            prompt: User message / main prompt.
            system: Optional system message.
            temperature: Override default temperature.
            max_tokens: Override default max_tokens.
            response_format: Optional JSON mode config.
            cache: Cache control. ``None`` (default) caches only when the call is
                deterministic (temperature == 0). ``True`` forces caching,
                ``False`` disables it. Cache hits skip the LLM call entirely.

        Returns:
            The generated text content.
        """
        temp = temperature if temperature is not None else self._default_temperature
        tokens = max_tokens or self._default_max_tokens

        # Cap input length to avoid runaway token spend on huge contexts.
        max_in = self._settings.llm_max_input_chars
        if max_in > 0:
            if system:
                system = truncate_for_llm(system, max_in)
            prompt = truncate_for_llm(prompt, max_in)

        # Deterministic cache: only safe when output is reproducible (temp == 0).
        ttl = self._settings.llm_cache_ttl_seconds if self._settings.llm_cache_enabled else 0
        use_cache = ttl > 0 and (cache if cache is not None else (temp == 0))

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "messages": messages,
            "temperature": temp,
            "max_tokens": tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format

        limiter = get_llm_rate_limiter(self._model_type, self._requests_per_minute)
        last_exc: Exception | None = None
        unsupported_fmt = _RESPONSE_FORMAT_UNSUPPORTED_PREFIXES

        # Try each candidate in order; jump to the next on rate-limit / quota.
        for model in self._candidate_models():
            kwargs["model"] = model

            # JSON mode only when the provider supports it.
            if model.startswith(unsupported_fmt):
                kwargs.pop("response_format", None)
            elif response_format and "response_format" not in kwargs:
                kwargs["response_format"] = response_format

            key = None
            if use_cache:
                key = _cache_key(model, system, prompt, temp, tokens, response_format)
                cached = _cache_get(key, ttl)
                if cached is not None:
                    logger.debug("[LLMTool] cache hit for agent=%s model=%s", self._agent, model)
                    return cached

            try:
                response = await self._completion_with_retry(kwargs, limiter)
            except _ModelExhausted as exc:
                last_exc = exc.original
                logger.warning(
                    "[LLMTool] model %s exhausted; jumping to next candidate", model
                )
                continue

            content = response.choices[0].message.content

            # Record cost to tracker if hunt_id is set
            if self._hunt_id:
                try:
                    from observability.cost_tracker import get_tracker
                    usage = getattr(response, "usage", None)
                    if usage:
                        cost = getattr(response, "_hidden_params", {}).get("response_cost") or 0.0
                        get_tracker(self._hunt_id).record_llm_call(
                            agent=self._agent,
                            model=model,
                            prompt_tokens=getattr(usage, "prompt_tokens", 0),
                            completion_tokens=getattr(usage, "completion_tokens", 0),
                            cost_usd=float(cost),
                            hunt_round=self._hunt_round,
                        )
                except Exception:
                    pass  # Never let tracking break the main flow

            if key:
                _cache_put(key, content or "", ttl)
            return content

        if last_exc is not None:
            raise RuntimeError(format_llm_error(last_exc)) from last_exc
        raise RuntimeError(
            f"No LLM model configured for model_type={self._model_type!r}"
        )

    async def close(self) -> None:
        """No-op — litellm manages its own connections."""
        pass
