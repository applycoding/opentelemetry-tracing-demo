from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from opentelemetry.metrics import Counter, Histogram
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk._logs import Logger
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer, get_current_span

from app.telemetry import emit_log

QUEUE_NAME = "payments.requests"
Stage = Literal["gateway", "orders", "payments", "none"]


class Publisher(Protocol):
    def publish(self, queue: str, message: dict[str, object]) -> None: ...


@dataclass
class OrderRequest:
    sku: str
    amount: float
    quantity: int
    inject_error: bool
    error_at: Stage
    inject_latency_ms: int
    latency_at: Stage


@dataclass
class OrderResult:
    status_code: int
    body: dict[str, object]


@dataclass
class Dependencies:
    tracer: Tracer
    logger: Logger
    publisher: Publisher
    requests: Counter
    duration: Histogram
    sleep: Callable[[int], None]
    queue: str = QUEUE_NAME


class MemoryPublisher:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    def publish(self, queue: str, message: dict[str, object]) -> None:
        self.messages.append((queue, message))


def new_order_id() -> str:
    return f"ord_{secrets.token_hex(8)}"


def _trace_id() -> str:
    span_context = get_current_span().get_span_context()
    if not span_context.trace_id:
        return ""
    return format(span_context.trace_id, "032x")


def handle_order(
    request: OrderRequest,
    headers: Mapping[str, str],
    deps: Dependencies,
) -> OrderResult:
    parent = extract(headers)
    started = time.perf_counter()
    metric_status = "error"
    with deps.tracer.start_as_current_span(
        "orders.create",
        context=parent,
        kind=SpanKind.SERVER,
        attributes={
            "http.request.method": "POST",
            "http.route": "/orders",
            "order.sku": request.sku,
            "order.amount": request.amount,
            "order.quantity": request.quantity,
            "app.injected_error": request.inject_error and request.error_at == "orders",
        },
    ) as span:
        try:
            invalid = _validate(request)
            if invalid is not None:
                span.set_status(Status(StatusCode.ERROR, invalid))
                span.set_attribute("app.validation_error", invalid)
                span.set_attribute("http.response.status_code", 400)
                _log(deps, "ERROR", invalid, {"order.sku": request.sku})
                return OrderResult(400, {"error": invalid, "traceId": _trace_id()})

            if request.latency_at == "orders" and request.inject_latency_ms > 0:
                span.set_attribute("app.injected_latency_ms", request.inject_latency_ms)
                deps.sleep(request.inject_latency_ms)

            if request.inject_error and request.error_at == "orders":
                message = "injected error at orders"
                error = RuntimeError(message)
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, message))
                span.set_attribute("http.response.status_code", 500)
                _log(
                    deps,
                    "ERROR",
                    message,
                    {"order.sku": request.sku, "app.injected_error": True},
                )
                return OrderResult(500, {"error": message, "traceId": _trace_id()})

            order_id = new_order_id()
            span.set_attribute("order.id", order_id)
            _enqueue(deps, request, order_id)
            span.set_attribute("http.response.status_code", 200)
            span.set_status(Status(StatusCode.OK))
            metric_status = "ok"
            _log(deps, "INFO", "order enqueued", {"order.id": order_id, "order.sku": request.sku})
            return OrderResult(
                200,
                {"orderId": order_id, "status": "payment_queued", "traceId": _trace_id()},
            )
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            deps.duration.record(elapsed_ms, {"status": metric_status})
            deps.requests.add(1, {"status": metric_status})


def _validate(request: OrderRequest) -> str | None:
    if request.sku.strip() == "":
        return "sku is required"
    if request.amount < 0:
        return "amount must be a non-negative number"
    if request.quantity < 1:
        return "quantity must be an integer greater than zero"
    if request.inject_latency_ms < 0:
        return "injectLatencyMs must be a non-negative integer"
    return None


def _enqueue(deps: Dependencies, request: OrderRequest, order_id: str) -> None:
    with deps.tracer.start_as_current_span(
        "orders.enqueue_payment",
        kind=SpanKind.PRODUCER,
        attributes={
            "messaging.system": "redis",
            "messaging.destination.name": deps.queue,
            "messaging.operation.name": "publish",
            "order.id": order_id,
        },
    ):
        carrier: dict[str, str] = {}
        inject(carrier)
        deps.publisher.publish(
            deps.queue,
            {
                "orderId": order_id,
                "sku": request.sku,
                "amount": request.amount,
                "quantity": request.quantity,
                "injectError": request.inject_error,
                "errorAt": request.error_at,
                "injectLatencyMs": request.inject_latency_ms,
                "latencyAt": request.latency_at,
                "traceparent": carrier.get("traceparent", ""),
                "tracestate": carrier.get("tracestate", ""),
            },
        )


def _log(
    deps: Dependencies,
    severity: str,
    body: str,
    attributes: dict[str, str | int | float | bool],
) -> None:
    emit_log(deps.logger, severity, body, attributes)
