from prometheus_client import Counter, Histogram

llm_request_latency_seconds = Histogram(
    "llm_request_latency_seconds", "LLM call latency in seconds", ["provider", "status"]
)
llm_tokens_total = Counter(
    "llm_tokens_total", "Total tokens used", ["provider", "token_type"]
)
chat_requests_total = Counter(
    "chat_requests_total", "Total /chat requests", ["status"]
)
rate_limit_rejections_total = Counter(
    "rate_limit_rejections_total", "Requests rejected for exceeding rate limit"
)