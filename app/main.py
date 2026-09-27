from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from sqlalchemy import text
from prometheus_fastapi_instrumentator import Instrumentator
import time

from app.config import settings
from app.database import Base, engine, get_db
from app import models
from app.schemas import LoginRequest, TokenResponse, BaseModel, ChatRequest, ChatResponse
from app.security import verify_password, create_access_token, decode_access_token
from app.redis_client import is_rate_limited, get_cached_answer, set_cached_answer, redis_client
from app.llm_service import get_answer, LLMUnavailableError
from app.metrics import (
    llm_request_latency_seconds, llm_tokens_total,
    chat_requests_total, rate_limit_rejections_total,
)

app = FastAPI(title=settings.app_name)

bearer_scheme = HTTPBearer()

Instrumentator().instrument(app).expose(app, endpoint="/metrics")


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)


@app.get("/")
def root():
    return {"message": "QA API is running"}


@app.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == payload.username).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    token = create_access_token(username=user.username, role=user.role.value)
    return TokenResponse(access_token=token)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    user = db.query(models.User).filter(models.User.username == payload["sub"]).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")
    return user


def require_role(*allowed_roles: str):
    def role_checker(current_user: models.User = Depends(get_current_user)) -> models.User:
        if current_user.role.value not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current_user.role.value}' is not permitted to access this resource",
            )
        return current_user
    return role_checker


@app.get("/auth/me")
def read_current_user(current_user: models.User = Depends(get_current_user)):
    return {"username": current_user.username, "role": current_user.role.value}


@app.get("/admin-only-test")
def admin_only(current_user: models.User = Depends(require_role("admin"))):
    return {"message": f"Hello admin {current_user.username}"}


@app.post("/chat", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    current_user: models.User = Depends(require_role("admin", "user")),
    db: Session = Depends(get_db),
):
    if is_rate_limited(current_user.username):
        rate_limit_rejections_total.inc()
        chat_requests_total.labels(status="rate_limited").inc()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded. Try again later.")

    cached = get_cached_answer(current_user.username, payload.question)
    if cached:
        log_entry = models.ChatLog(
            user_id=current_user.id, question=payload.question, answer=cached["answer"],
            model_used=cached.get("model_used", "cache"), prompt_tokens=0, completion_tokens=0,
            total_tokens=0, latency_ms=0.0, status="cached",
        )
        db.add(log_entry)
        db.commit()
        chat_requests_total.labels(status="cached").inc()
        return ChatResponse(answer=cached["answer"])

    start = time.perf_counter()

    try:
        result = get_answer(payload.question)
        status_label = "success"
    except Exception:
        latency_ms = (time.perf_counter() - start) * 1000
        llm_request_latency_seconds.labels(provider=settings.llm_provider, status="error").observe(latency_ms / 1000)
        chat_requests_total.labels(status="error").inc()
        db.add(models.ChatLog(
            user_id=current_user.id, question=payload.question, answer=None,
            model_used=None, prompt_tokens=0, completion_tokens=0, total_tokens=0,
            latency_ms=latency_ms, status="error",
        ))
        db.commit()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="LLM service is currently unavailable")

    latency_ms = (time.perf_counter() - start) * 1000

    llm_request_latency_seconds.labels(provider=settings.llm_provider, status="success").observe(latency_ms / 1000)
    llm_tokens_total.labels(provider=settings.llm_provider, token_type="prompt").inc(result["prompt_tokens"])
    llm_tokens_total.labels(provider=settings.llm_provider, token_type="completion").inc(result["completion_tokens"])
    chat_requests_total.labels(status="success").inc()

    log_entry = models.ChatLog(
        user_id=current_user.id, question=payload.question, answer=result["answer"],
        model_used=result["model_used"], prompt_tokens=result["prompt_tokens"],
        completion_tokens=result["completion_tokens"], total_tokens=result["total_tokens"],
        latency_ms=latency_ms, status=status_label,
    )
    db.add(log_entry)
    db.commit()

    set_cached_answer(current_user.username, payload.question, {"answer": result["answer"], "model_used": result["model_used"]}, settings.cache_ttl_seconds)

    return ChatResponse(answer=result["answer"])


@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    health = {"status": "ok", "database": "ok", "redis": "ok"}

    try:
        db.execute(text("SELECT 1"))
    except Exception:
        health["database"] = "unavailable"
        health["status"] = "degraded"

    try:
        redis_client.ping()
    except Exception:
        health["redis"] = "unavailable"
        health["status"] = "degraded"

    return health