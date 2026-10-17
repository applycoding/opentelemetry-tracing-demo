import {
  context,
  metrics,
  propagation,
  trace,
  type Counter,
  type Histogram,
  type Tracer,
} from "@opentelemetry/api";
import { logs, SeverityNumber, type Logger } from "@opentelemetry/api-logs";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { CompositePropagator, W3CBaggagePropagator, W3CTraceContextPropagator } from "@opentelemetry/core";
import { OTLPLogExporter } from "@opentelemetry/exporter-logs-otlp-http";
import { OTLPMetricExporter } from "@opentelemetry/exporter-metrics-otlp-http";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { resourceFromAttributes } from "@opentelemetry/resources";
import {
  BatchLogRecordProcessor,
  InMemoryLogRecordExporter,
  LoggerProvider,
  SimpleLogRecordProcessor,
} from "@opentelemetry/sdk-logs";
import { MeterProvider, PeriodicExportingMetricReader } from "@opentelemetry/sdk-metrics";
import {
  BatchSpanProcessor,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

const SERVICE_NAME = "gateway";

let globalsReady = false;

function ensureContextAndPropagator(): void {
  if (globalsReady) {
    return;
  }
  const manager = new AsyncLocalStorageContextManager();
  manager.enable();
  context.setGlobalContextManager(manager);
  propagation.setGlobalPropagator(
    new CompositePropagator({
      propagators: [new W3CTraceContextPropagator(), new W3CBaggagePropagator()],
    }),
  );
  globalsReady = true;
}

export type Telemetry = {
  tracer: Tracer;
  logger: Logger;
  checkoutRequests: Counter;
  checkoutDuration: Histogram;
  spanExporter: InMemorySpanExporter | undefined;
  logExporter: InMemoryLogRecordExporter | undefined;
  shutdown: () => Promise<void>;
};

export function createTelemetry(options?: { inMemory?: boolean }): Telemetry {
  ensureContextAndPropagator();
  const inMemory = options?.inMemory === true;
  const resource = resourceFromAttributes({
    "service.name": SERVICE_NAME,
    "service.version": "0.1.0",
    "deployment.environment.name": "demo",
  });

  const spanExporter = inMemory ? new InMemorySpanExporter() : undefined;
  const logExporter = inMemory ? new InMemoryLogRecordExporter() : undefined;

  const tracerProvider = new NodeTracerProvider({
    resource,
    spanProcessors: [
      inMemory
        ? new SimpleSpanProcessor(spanExporter as InMemorySpanExporter)
        : new BatchSpanProcessor(new OTLPTraceExporter()),
    ],
  });

  const exportInterval = Number(process.env.OTEL_METRIC_EXPORT_INTERVAL ?? "5000");
  const meterProvider = new MeterProvider({
    resource,
    readers: inMemory
      ? []
      : [
          new PeriodicExportingMetricReader({
            exporter: new OTLPMetricExporter(),
            exportIntervalMillis: Number.isFinite(exportInterval) ? exportInterval : 5000,
          }),
        ],
  });

  const loggerProvider = new LoggerProvider({
    resource,
    processors: [
      inMemory
        ? new SimpleLogRecordProcessor({ exporter: logExporter as InMemoryLogRecordExporter })
        : new BatchLogRecordProcessor({ exporter: new OTLPLogExporter() }),
    ],
  });

  if (!inMemory) {
    trace.setGlobalTracerProvider(tracerProvider);
    metrics.setGlobalMeterProvider(meterProvider);
    logs.setGlobalLoggerProvider(loggerProvider);
  }

  const meter = meterProvider.getMeter(SERVICE_NAME);
  return {
    tracer: tracerProvider.getTracer(SERVICE_NAME),
    logger: loggerProvider.getLogger(SERVICE_NAME),
    checkoutRequests: meter.createCounter("checkout_requests", {
      description: "Checkout requests handled by the gateway",
    }),
    checkoutDuration: meter.createHistogram("checkout_duration_ms", {
      description: "Checkout handler duration in milliseconds",
    }),
    spanExporter,
    logExporter,
    shutdown: async () => {
      await Promise.all([
        tracerProvider.shutdown(),
        meterProvider.shutdown(),
        loggerProvider.shutdown(),
      ]);
    },
  };
}

export function emitServiceLog(
  logger: Logger,
  severity: "INFO" | "ERROR",
  body: string,
  attributes: Record<string, string | number | boolean>,
): void {
  const severityNumber = severity === "INFO" ? SeverityNumber.INFO : SeverityNumber.ERROR;
  logger.emit({
    severityNumber,
    severityText: severity,
    body,
    attributes,
    context: context.active(),
  });
  const spanContext = trace.getActiveSpan()?.spanContext();
  console.log(
    JSON.stringify({
      service: SERVICE_NAME,
      severity,
      msg: body,
      trace_id: spanContext?.traceId ?? "",
      span_id: spanContext?.spanId ?? "",
      ...attributes,
    }),
  );
}
