from __future__ import annotations

import json
import os
import time
from typing import Literal

import redis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.handling import QUEUE_NAME, Dependencies, OrderRequest, handle_order
from app.telemetry import Telemetry, build_telemetry

StageName = Literal["gateway", "orders", "payments", "none"]


class OrderIn(BaseModel):
    sku: str
    amount: float
    quantity: int
    injectError: bool = False
    errorAt: StageName = "none"
    injectLatencyMs: int = 0
    latencyAt: StageName = "none"


class RedisPublisher:
    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    def publish(self, queue: str, message: dict[str, object]) -> None:
        self._client.lpush(queue, json.dumps(message))


def build_runtime() -> tuple[Dependencies, Telemetry]:
    telemetry = build_telemetry(in_memory=False)
    client = redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    deps = Dependencies(
        tracer=telemetry.tracer,
        logger=telemetry.logger,
        publisher=RedisPublisher(client),
        requests=telemetry.requests,
        duration=telemetry.duration,
        sleep=_sleep_ms,
        queue=os.environ.get("QUEUE", QUEUE_NAME),
    )
    return deps, telemetry


def _sleep_ms(milliseconds: int) -> None:
    if milliseconds > 0:
        time.sleep(milliseconds / 1000)


def create_app(deps: Dependencies | None = None, telemetry: Telemetry | None = None) -> FastAPI:
    app = FastAPI(title="orders")
    if deps is None:
        deps, telemetry = build_runtime()
    app.state.deps = deps
    app.state.telemetry = telemetry

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/orders")
    def create_order(body: OrderIn, request: Request) -> JSONResponse:
        result = handle_order(
            OrderRequest(
                sku=body.sku,
                amount=body.amount,
                quantity=body.quantity,
                inject_error=body.injectError,
                error_at=body.errorAt,
                inject_latency_ms=body.injectLatencyMs,
                latency_at=body.latencyAt,
            ),
            request.headers,
            request.app.state.deps,
        )
        return JSONResponse(status_code=result.status_code, content=result.body)

    return app
