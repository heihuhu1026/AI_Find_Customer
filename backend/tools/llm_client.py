"""LLM tool — unified multi-provider interface via litellm.

Supports: OpenAI, Anthropic, OpenRouter, Groq, GLM (智谱), Kimi (Moonshot),
MiniMax, and 100+ other providers through litellm.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
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
    "ollama/",  # 本地 Ollama 不支持 OpenAI 风格的 response_format，靠 parse_json 兜底
)

# 本地模型前缀（无需 API Key，走 OLLAMA_API_BASE）
_LOCAL_MODEL_PREFIXES = ("ollama/",)

_RATE_LIMIT_BACKOFF_SECONDS = (2, 5, 10)

_DASHSCOPE_PREFIX = "dashscope/"

# 百炼聚合平台模型（通义/DeepSeek/Kimi/GLM 等）统一走 dashscope/ 前缀。
# 用户在 fallback 列表里可写裸名（如 "glm-5.2"），此处自动补前缀。
def _prefix_model(model: str) -> str:
    model = (model or "").strip()
    if not model:
        return model
    if "/" in model:
        return model
    return _DASHSCOPE_PREFIX + model


# ---------------------------------------------------------------------------
# Per-model unsupported-parameter handling (self-healing fallback)
#
# Some providers/models reject certain sampling parameters — e.g. DashScope's
# kimi-k3 rejects `temperature` and returns HTTP 400
# ("Parameter 'temperature' is not supported for kimi-k3 model").
#
# Rather than hard-code a model→param matrix (which would rot as the fallback
# chain changes), we discover rejections lazily at runtime: the first HTTP-400
# from a model records the offending parameter in `_MODEL_UNSUPPORTED_PARAMS`,
# and every subsequent call to that model scrubs it before hitting the API.
# This keeps the fallback chain resilient without manual upkeep, and also makes
# the error switchable so the chain tries the next model instead of aborting.
# ---------------------------------------------------------------------------
_MODEL_UNSUPPORTED_PARAMS: dict[str, set[str]] = {}

# Sampling/penalty params that litellm forwards and that some models reject.
_UNSUPPORTED_PARAM_KEYWORDS = (
    "temperature", "top_p", "top_k", "presence_penalty",
    "frequency_penalty", "repetition_penalty",
)


def _is_param_unsupported_error(exc: Exception) -> bool:
    """True if the error is a model rejecting a request parameter (HTTP 400).

    Covers messages like "Parameter 'temperature' is not supported" /
    "InvalidParameter" / "unsupported parameter".
    """
    msg = (str(exc) or "").lower()
    if not ("not supported" in msg or "invalid parameter" in msg or "unsupported" in msg):
        return False
    # Only treat it as a param error when it actually names a known param,
    # or mentions "parameter" generically (avoids false positives on unrelated 400s).
    return any(p in msg for p in _UNSUPPORTED_PARAM_KEYWORDS) or "parameter" in msg


def _extract_unsupported_params(exc: Exception) -> set[str]:
    """Return the set of sampling params the error says are unsupported."""
    msg = (str(exc) or "").lower()
    return {p for p in _UNSUPPORTED_PARAM_KEYWORDS if p in msg}


def _record_unsupported_param(model: str, param: str) -> None:
    """Remember that `model` rejects `param` so future calls can scrub it."""
    if not model or not param:
        return
    _MODEL_UNSUPPORTED_PARAMS.setdefault(model, set()).add(param)


def _scrub_unsupported_params(model: str, kwargs: dict) -> dict:
    """Return a copy of kwargs with params known to be rejected by `model` removed."""
    banned = _MODEL_UNSUPPORTED_PARAMS.get(model)
    if not banned:
        return kwargs
    return {k: v for k, v in kwargs.items() if k not in banned}


def _is_switchable_error(exc: Exception) -> bool:
    """Return True if the error warrants switching to the next fallback model.

    Covers quota/rate-limit exhaustion, model-access errors (403/401, a
    specific model being unavailable or deprecated), and transient upstream
    failures. The fallback chain shares one provider account for most models,
    so a per-model access error is best handled by trying the next model
    rather than aborting the whole request.

    The ONLY genuinely hard errors are a bad/missing API key — those affect
    every model on the same account, so switching is futile.
    """
    msg = str(exc or "").lower()
    # Hard stop: the key itself is invalid/missing — switching models won't help.
    fatal = any(
        k in msg
        for k in (
            "invalid api key", "incorrect api key", "missing api key",
            "api key is required", "api_key is required", "authentication failed",
        )
    )
    if fatal:
        return False
    # Everything else is treated as switchable: 429/quota, 5xx, timeouts,
    # connection errors, AND model-access errors (403/401/forbidden,
    # model_not_found / does not exist / invalid model) so the chain can try
    # the next model instead of aborting the request.
    switchable = any(
        k in msg
        for k in (
            "rate_limit", "429", "quota", "额度", "exceeded", "余额",
            "balance", "insufficient", "401", "403", "forbidden",
            "model_not_found", "does not exist", "invalid model",
            "502", "503", "504", "timeout", "timed out", "connection",
            "upstream", "server error", "internal error", "too many requests",
            "not supported", "invalid parameter", "unsupported",
        )
    )
    return switchable


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
    message = str(exc or "").lower()
    return (
        "rate_limit" in message
        or '"http_code":"429"' in message
        or '"http_code": "429"' in message
        or "上游返回 429" in message
        or "too many requests" in message
    )


def _parse_rate_limit_reset(message: str) -> float | None:
    """Parse a DashScope 429 reset timestamp.

    DashScope returns messages like
    ``将在 2026-09-01 20:49:55 UTC+8 重置``. When present we can sleep until
    that instant instead of blindly retrying and re-hitting the limit.
    Returns epoch seconds, or ``None`` if no usable timestamp is found.
    """
    m = re.search(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\s*UTC\+8", message)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone(timedelta(hours=8))
        )
        return dt.timestamp()
    except Exception:
        return None


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
            "DASHSCOPE_API_KEY": settings.email_dashscope_api_key or settings.dashscope_api_key,
            "DEEPSEEK_API_KEY": settings.email_deepseek_api_key or settings.deepseek_api_key,
            "VOLCENGINE_API_KEY": settings.email_volcengine_api_key or settings.volcengine_api_key,
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
        "DEEPSEEK_API_KEY": settings.deepseek_api_key,
        "VOLCENGINE_API_KEY": settings.volcengine_api_key,
    }


def _inject_api_keys(settings: Settings, scope: str = "default") -> None:
    """Push provider API keys from Settings into env vars for litellm."""
    _key_map = _provider_key_map(settings, scope)
    for env_var, value in _key_map.items():
        if value:
            os.environ[env_var] = value

    # Local model endpoint (Ollama) — no API key required.
    if getattr(settings, "ollama_api_base", ""):
        os.environ["OLLAMA_API_BASE"] = settings.ollama_api_base

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

    def _supports_response_format(self) -> bool:
        """Return whether the current provider accepts OpenAI-style response_format."""
        return not self.model.startswith(_RESPONSE_FORMAT_UNSUPPORTED_PREFIXES)

    @property
    def _model_chain(self) -> list[str]:
        """Ordered model fallback list: primary model first, then fallbacks.

        All fallback entries are normalized (bare 百炼 model names get the
        ``dashscope/`` prefix). Duplicates are dropped while preserving order.
        """
        primary = normalize_model_name(_select_model(self._settings, self._model_type))
        fb_env = self._settings.llm_model_fallback
        if self._model_type == "reasoning":
            fb_env = self._settings.reasoning_model_fallback or self._settings.llm_model_fallback
        elif self._model_type == "email":
            fb_env = self._settings.email_llm_model_fallback or self._settings.llm_model_fallback
        elif self._model_type == "email_reasoning":
            fb_env = self._settings.email_reasoning_model_fallback or self._settings.llm_model_fallback
        raw = [m.strip() for m in (fb_env or "").split(",") if m.strip()]
        chain = [primary] + [_prefix_model(m) for m in raw]
        seen: set[str] = set()
        out: list[str] = []
        for m in chain:
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    def _supports_response_format_for(self, model: str) -> bool:
        return not model.startswith(_RESPONSE_FORMAT_UNSUPPORTED_PREFIXES)

    async def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> str:
        """Generate a completion, transparently failing over across models.

        Iterates the model fallback chain (primary → fallbacks). Within a single
        model it retries rate-limit errors with backoff; if the model is
        exhausted/unavailable (quota, 5xx, timeout) it switches to the next model
        in the chain instead of failing the whole request.
        """
        temp = temperature if temperature is not None else self._default_temperature
        tokens = max_tokens or self._default_max_tokens

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        limiter = get_llm_rate_limiter(self._model_type, self._requests_per_minute)
        chain = self._model_chain
        last_exc: Exception | None = None

        # Precompute the index of the first provider-independent (local) model.
        # Under a shared-quota rate limit — e.g. all DashScope fallbacks share
        # one account — switching to another same-provider model is futile: it
        # will be rate limited too. The only genuinely useful fallback is a
        # local model that needs no API key (Ollama).
        local_idx = next(
            (i for i, m in enumerate(chain) if m.startswith(_LOCAL_MODEL_PREFIXES)),
            None,
        )

        idx = 0
        while idx < len(chain):
            model = chain[idx]
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temp,
                "max_tokens": tokens,
            }
            # Drop params this specific model is known to reject (e.g. kimi-k3
            # rejects temperature). First occurrence records it; subsequent calls
            # scrub automatically so we never waste a 400 on a known-bad param.
            kwargs = _scrub_unsupported_params(model, kwargs)
            if response_format and self._supports_response_format_for(model):
                kwargs["response_format"] = response_format

            if model.startswith(_LOCAL_MODEL_PREFIXES):
                # Local models: point at the Ollama endpoint and allow a much
                # longer timeout (CPU/7B inference is slow, but unlimited & free).
                api_base = getattr(self._settings, "ollama_api_base", "") or "http://127.0.0.1:11434"
                kwargs["api_base"] = api_base
                kwargs["timeout"] = getattr(self._settings, "ollama_request_timeout", 300) or 300

            rate_limited = False
            for attempt in range(len(_RATE_LIMIT_BACKOFF_SECONDS) + 1):
                try:
                    await limiter.acquire()
                    response = await litellm.acompletion(**kwargs)
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
                    return response.choices[0].message.content
                except Exception as exc:
                    last_exc = exc
                    if _is_param_unsupported_error(exc):
                        # Model rejected a param (e.g. kimi-k3 rejects temperature):
                        # remember it so future calls to this model scrub it. The
                        # error is also switchable, so we fall back to the next model.
                        for _p in _extract_unsupported_params(exc):
                            _record_unsupported_param(model, _p)
                    if not _is_switchable_error(exc):
                        # Hard error (bad key / unknown model / bad request) — no point retrying or switching.
                        raise RuntimeError(format_llm_error(exc)) from exc
                    if _is_retryable_rate_limit_error(exc):
                        # 429: most fallbacks share one provider quota, so
                        # switching models just re-hits the limit. Retry the SAME
                        # model — ideally after the stated reset window — and only
                        # fall back to a local model when the current one is clearly
                        # exhausted. This stops a 13-model chain from multiplying the
                        # wait time by 13 under a single account's rate limit.
                        rate_limited = True
                        reset_ts = _parse_rate_limit_reset(str(exc))
                        if reset_ts is not None:
                            wait = max(0.0, reset_ts - time.time()) + 2.0
                            wait = min(wait, 3600.0)
                            logger.warning(
                                "LLM rate limited on %s; resets in %.0fs — waiting then retrying same model",
                                model, wait,
                            )
                            await asyncio.sleep(wait)
                            continue
                        delay = _RATE_LIMIT_BACKOFF_SECONDS[attempt % len(_RATE_LIMIT_BACKOFF_SECONDS)]
                        logger.warning(
                            "LLM rate limited on %s; retrying in %ss (attempt %s/%s)",
                            model, delay, attempt + 1, len(_RATE_LIMIT_BACKOFF_SECONDS) + 1,
                        )
                        await asyncio.sleep(delay)
                        continue
                    # Non-429 transient (5xx/timeout/connection): switch to next model.
                    logger.warning(
                        "[LLM failover] %s transient error; trying next model in chain",
                        model,
                    )
                    break
            else:
                # Inner loop exhausted all retries — every attempt was a 429.
                if rate_limited and local_idx is not None and local_idx > idx:
                    logger.warning(
                        "[LLM failover] %s rate-limited; switching to local model %s",
                        model, chain[local_idx],
                    )
                    idx = local_idx
                    continue
                # No useful fallback — give up rather than thrash endlessly.
                logger.error("All models exhausted for request (last error: %s)", str(last_exc)[:160])
                break
            idx += 1

        raise RuntimeError(format_llm_error(last_exc)) from last_exc

    async def close(self) -> None:
        """No-op — litellm manages its own connections."""
        pass
