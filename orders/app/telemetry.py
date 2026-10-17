from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Counter, Histogram
from opentelemetry.sdk._logs import Logger, LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Tracer, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SERVICE_NAME = "orders"


@dataclass
class Telemetry:
    tracer: Tracer
    logger: Logger
    requests: Counter
    duration: Histogram
    span_exporter: InMemorySpanExporter | None
    log_exporter: InMemoryLogRecordExporter | None
    shutdown: Callable[[], None]


def build_telemetry(*, in_memory: bool) -> Telemetry:
    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": "0.1.0",
            "deployment.environment.name": "demo",
        }
    )
    span_exporter = InMemorySpanExporter() if in_memory else None
    log_exporter = InMemoryLogRecordExporter() if in_memory else None

    tracer_provider = TracerProvider(resource=resource)
    if span_exporter is not None:
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    else:
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    if in_memory:
        meter_provider = MeterProvider(resource=resource, metric_readers=[InMemoryMetricReader()])
    else:
        interval = int(os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "5000"))
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(),
                    export_interval_millis=interval,
                )
            ],
        )

    logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=not in_memory)
    if log_exporter is not None:
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    else:
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))

    meter = meter_provider.get_meter(SERVICE_NAME)

    def shutdown() -> None:
        tracer_provider.shutdown()
        meter_provider.shutdown()
        logger_provider.shutdown()

    return Telemetry(
        tracer=tracer_provider.get_tracer(SERVICE_NAME),
        logger=logger_provider.get_logger(SERVICE_NAME),
        requests=meter.create_counter(
            "orders_requests",
            description="Order requests handled by the orders service",
        ),
        duration=meter.create_histogram(
            "orders_duration_ms",
            description="Orders handler duration in milliseconds",
        ),
        span_exporter=span_exporter,
        log_exporter=log_exporter,
        shutdown=shutdown,
    )


def emit_log(
    logger: Logger,
    severity: str,
    body: str,
    attributes: dict[str, str | int | float | bool],
) -> None:
    severity_number = SeverityNumber.INFO if severity == "INFO" else SeverityNumber.ERROR
    logger.emit(
        body=body,
        severity_number=severity_number,
        severity_text=severity,
        attributes=attributes,
    )
    span_context = trace.get_current_span().get_span_context()
    logging.getLogger(SERVICE_NAME).info(
        json.dumps(
            {
                "service": SERVICE_NAME,
                "severity": severity,
                "msg": body,
                "trace_id": format(span_context.trace_id, "032x") if span_context.trace_id else "",
                "span_id": format(span_context.span_id, "016x") if span_context.span_id else "",
                **attributes,
            }
        )
    )
