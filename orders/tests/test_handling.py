from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from opentelemetry.propagate import inject
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from app.api import create_app
from app.handling import Dependencies, MemoryPublisher, OrderRequest, handle_order
from app.telemetry import Telemetry, build_telemetry


def _request(**overrides: object) -> OrderRequest:
    payload: dict[str, object] = {
        "sku": "ceramic-mug",
        "amount": 18.5,
        "quantity": 2,
        "inject_error": False,
        "error_at": "none",
        "inject_latency_ms": 0,
        "latency_at": "none",
    }
    payload.update(overrides)
    return OrderRequest(
        sku=str(payload["sku"]),
        amount=float(payload["amount"]),
        quantity=int(payload["quantity"]),
        inject_error=bool(payload["inject_error"]),
        error_at=payload["error_at"],  # type: ignore[arg-type]
        inject_latency_ms=int(payload["inject_latency_ms"]),
        latency_at=payload["latency_at"],  # type: ignore[arg-type]
    )


def _traceparent(value: str) -> tuple[str, str]:
    _version, trace_id, span_id, _flags = value.split("-")
    return trace_id, span_id


def test_enqueue_propagates_the_incoming_trace_and_correlates_the_log() -> None:
    upstream_exporter = InMemorySpanExporter()
    upstream = TracerProvider()
    upstream.add_span_processor(SimpleSpanProcessor(upstream_exporter))
    carrier: dict[str, str] = {}
    with upstream.get_tracer("upstream").start_as_current_span("gateway.call_orders") as parent:
        inject(carrier)
        parent_context = parent.get_span_context()

    telemetry = build_telemetry(in_memory=True)
    publisher = MemoryPublisher()
    try:
        result = handle_order(
            _request(inject_error=True, error_at="payments", latency_at="payments", inject_latency_ms=80),
            carrier,
            telemetry_deps(telemetry, publisher),
        )
    finally:
        telemetry.shutdown()

    assert result.status_code == 200
    assert publisher.messages[0][0] == "payments.requests"
    message = publisher.messages[0][1]
    assert message["injectError"] is True
    assert message["errorAt"] == "payments"
    trace_id, span_id = _traceparent(str(message["traceparent"]))

    assert telemetry.span_exporter is not None
    spans = {span.name: span for span in telemetry.span_exporter.get_finished_spans()}
    created = spans["orders.create"]
    enqueued = spans["orders.enqueue_payment"]
    assert created.kind == SpanKind.SERVER
    assert enqueued.kind == SpanKind.PRODUCER
    assert created.parent is not None
    assert format(created.parent.span_id, "016x") == format(parent_context.span_id, "016x")
    assert format(created.context.trace_id, "032x") == format(parent_context.trace_id, "032x")
    assert created.attributes["order.sku"] == "ceramic-mug"
    assert created.attributes["order.amount"] == 18.5
    assert created.attributes["order.id"] == message["orderId"]
    assert enqueued.attributes["messaging.system"] == "redis"
    assert enqueued.attributes["messaging.destination.name"] == "payments.requests"
    assert enqueued.attributes["messaging.operation.name"] == "publish"
    assert trace_id == format(enqueued.context.trace_id, "032x")
    assert span_id == format(enqueued.context.span_id, "016x")
    assert result.body["traceId"] == format(created.context.trace_id, "032x")

    assert telemetry.log_exporter is not None
    finished = telemetry.log_exporter.get_finished_logs()
    assert finished
    assert format(finished[-1].log_record.trace_id, "032x") == format(created.context.trace_id, "032x")


def test_injected_order_error_is_a_failed_span_and_does_not_publish() -> None:
    telemetry = build_telemetry(in_memory=True)
    publisher = MemoryPublisher()
    slept: list[int] = []
    try:
        result = handle_order(
            _request(inject_error=True, error_at="orders", latency_at="orders", inject_latency_ms=40),
            {},
            telemetry_deps(telemetry, publisher, sleep=slept.append),
        )
    finally:
        telemetry.shutdown()

    assert result.status_code == 500
    assert result.body["error"] == "injected error at orders"
    assert publisher.messages == []
    assert slept == [40]
    assert telemetry.span_exporter is not None
    spans = telemetry.span_exporter.get_finished_spans()
    assert [span.name for span in spans] == ["orders.create"]
    assert spans[0].status.status_code == StatusCode.ERROR
    assert spans[0].attributes["app.injected_error"] is True
    assert spans[0].attributes["app.injected_latency_ms"] == 40


def test_http_health_and_checkout_round_trip() -> None:
    telemetry = build_telemetry(in_memory=True)
    publisher = MemoryPublisher()
    app = create_app(telemetry_deps(telemetry, publisher), telemetry)
    try:
        client = TestClient(app)
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        created = client.post(
            "/orders",
            headers={"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"},
            json={"sku": "linen-tote", "amount": 24, "quantity": 1},
        )
        assert created.status_code == 200
        assert created.json()["status"] == "payment_queued"
        assert publisher.messages[0][1]["sku"] == "linen-tote"
        trace_id, _span_id = _traceparent(str(publisher.messages[0][1]["traceparent"]))
        assert trace_id == "0123456789abcdef0123456789abcdef"
    finally:
        telemetry.shutdown()


def telemetry_deps(
    telemetry: Telemetry,
    publisher: MemoryPublisher,
    sleep: Callable[[int], None] = lambda _ms: None,
) -> Dependencies:
    return Dependencies(
        tracer=telemetry.tracer,
        logger=telemetry.logger,
        publisher=publisher,
        requests=telemetry.requests,
        duration=telemetry.duration,
        sleep=sleep,
    )
