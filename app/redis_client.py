import redis

from app.config import settings

redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)


def is_rate_limited(username: str) -> bool:
    key = f"rate_limit:{username}"
    current = redis_client.incr(key)
    if current == 1:
        redis_client.expire(key, 60)  # first request in this window starts a 60-second countdown
    return current > settings.rate_limit_per_minute


import hashlib
import json

def _cache_key(username: str, question: str) -> str:
    digest = hashlib.sha256(question.strip().lower().encode()).hexdigest()
    return f"cache:{username}:{digest}"


def get_cached_answer(username: str, question: str) -> dict | None:
    key = _cache_key(username, question)
    cached = redis_client.get(key)
    return json.loads(cached) if cached else None


def set_cached_answer(username: str, question: str, answer_data: dict, ttl_seconds: int) -> None:
    key = _cache_key(username, question)
    redis_client.set(key, json.dumps(answer_data), ex=ttl_seconds)