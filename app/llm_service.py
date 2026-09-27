import random
import time
import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import settings

logger = logging.getLogger(__name__)


class LLMUnavailableError(Exception):
    """Raised when the LLM could not produce an answer after retries/fallback."""


def _mock_call(question: str) -> dict:
    time.sleep(0.2)

    if random.random() < 0.2:
        raise TimeoutError("Simulated LLM timeout")

    return {
        "answer": f"[mock answer] Here's a response to: '{question}'",
        "model_used": "mock-model",
        "prompt_tokens": len(question.split()),
        "completion_tokens": 12,
        "total_tokens": len(question.split()) + 12,
    }


GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
)


@retry(
    stop=stop_after_attempt(settings.llm_max_retries),
    wait=wait_exponential(multiplier=0.5, max=4),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.HTTPStatusError)),
)
def _gemini_call(question: str) -> dict:
    url = GEMINI_URL_TEMPLATE.format(model=settings.llm_primary_model, api_key=settings.gemini_api_key)
    body = {"contents": [{"parts": [{"text": question}]}]}

    with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
        response = client.post(url, json=body)
        response.raise_for_status()
        data = response.json()

    answer_text = data["candidates"][0]["content"]["parts"][0]["text"]
    usage = data.get("usageMetadata", {})

    return {
        "answer": answer_text,
        "model_used": settings.llm_primary_model,
        "prompt_tokens": usage.get("promptTokenCount", 0),
        "completion_tokens": usage.get("candidatesTokenCount", 0),
        "total_tokens": usage.get("totalTokenCount", 0),
    }


def get_answer(question: str) -> dict:
    if settings.llm_provider == "mock":
        return _mock_call(question)

    if settings.llm_provider == "gemini":
        try:
            return _gemini_call(question)
        except Exception as exc:
            logger.exception("Gemini call failed")
            raise LLMUnavailableError(f"Gemini call failed after retries: {exc}")

    raise LLMUnavailableError(f"Unknown or unimplemented provider: {settings.llm_provider}")