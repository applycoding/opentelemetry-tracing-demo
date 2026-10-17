package main

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	otellog "go.opentelemetry.io/otel/log"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
)

const paymentsQueue = "payments.requests"

type PaymentMessage struct {
	OrderID         string  `json:"orderId"`
	SKU             string  `json:"sku"`
	Amount          float64 `json:"amount"`
	Quantity        int     `json:"quantity"`
	InjectError     bool    `json:"injectError"`
	ErrorAt         string  `json:"errorAt"`
	InjectLatencyMs int     `json:"injectLatencyMs"`
	LatencyAt       string  `json:"latencyAt"`
	TraceParent     string  `json:"traceparent"`
	TraceState      string  `json:"tracestate"`
}

type Dependencies struct {
	Tracer     trace.Tracer
	Propagator propagation.TextMapPropagator
	Logger     otellog.Logger
	Processed  metric.Int64Counter
	Duration   metric.Float64Histogram
	Sleep      func(time.Duration)
	Queue      string
}

type ProcessResult struct {
	OrderID string
	Status  string
	TraceID string
}

func Process(ctx context.Context, msg PaymentMessage, deps Dependencies) (ProcessResult, error) {
	carrier := propagation.MapCarrier{
		"traceparent": msg.TraceParent,
		"tracestate":  msg.TraceState,
	}
	ctx = deps.Propagator.Extract(ctx, carrier)
	ctx, span := deps.Tracer.Start(ctx, "payments.process",
		trace.WithSpanKind(trace.SpanKindConsumer),
		trace.WithAttributes(
			attribute.String("messaging.system", "redis"),
			attribute.String("messaging.destination.name", deps.Queue),
			attribute.String("messaging.operation.name", "process"),
			attribute.String("order.id", msg.OrderID),
			attribute.String("order.sku", msg.SKU),
			attribute.Float64("order.amount", msg.Amount),
			attribute.Int("order.quantity", msg.Quantity),
			attribute.Bool("app.injected_error", msg.InjectError && msg.ErrorAt == "payments"),
		),
	)
	defer span.End()

	started := time.Now()
	metricStatus := "error"
	defer func() {
		elapsed := float64(time.Since(started).Milliseconds())
		attrs := metric.WithAttributes(attribute.String("status", metricStatus))
		deps.Duration.Record(ctx, elapsed, attrs)
		deps.Processed.Add(ctx, 1, attrs)
	}()

	if msg.LatencyAt == "payments" && msg.InjectLatencyMs > 0 {
		span.SetAttributes(attribute.Int("app.injected_latency_ms", msg.InjectLatencyMs))
		deps.Sleep(time.Duration(msg.InjectLatencyMs) * time.Millisecond)
	}

	if msg.Amount < 0 || msg.Quantity < 1 || msg.OrderID == "" {
		err := fmt.Errorf("invalid payment message")
		fail(ctx, span, deps, err)
		return ProcessResult{}, err
	}

	if msg.InjectError && msg.ErrorAt == "payments" {
		err := fmt.Errorf("injected error at payments")
		fail(ctx, span, deps, err)
		return ProcessResult{OrderID: msg.OrderID, TraceID: span.SpanContext().TraceID().String()}, err
	}

	metricStatus = "ok"
	span.SetStatus(codes.Ok, "")
	result := ProcessResult{
		OrderID: msg.OrderID,
		Status:  "approved",
		TraceID: span.SpanContext().TraceID().String(),
	}
	emit(ctx, deps.Logger, otellog.SeverityInfo, "INFO", "payment approved",
		attribute.String("order.id", msg.OrderID),
		attribute.String("payment.status", "approved"),
	)
	return result, nil
}

func fail(ctx context.Context, span trace.Span, deps Dependencies, err error) {
	span.RecordError(err)
	span.SetStatus(codes.Error, err.Error())
	emit(ctx, deps.Logger, otellog.SeverityError, "ERROR", err.Error(),
		attribute.String("error", err.Error()),
	)
}

func emit(ctx context.Context, logger otellog.Logger, severity otellog.Severity, severityText, body string, attrs ...attribute.KeyValue) {
	var record otellog.Record
	record.SetSeverity(severity)
	record.SetSeverityText(severityText)
	record.SetBody(attribute.StringValue(body))
	record.AddAttributes(attrs...)
	logger.Emit(ctx, record)

	spanContext := trace.SpanContextFromContext(ctx)
	payload := map[string]any{
		"service":  "payments",
		"severity": severityText,
		"msg":      body,
		"trace_id": "",
		"span_id":  "",
	}
	if spanContext.HasTraceID() {
		payload["trace_id"] = spanContext.TraceID().String()
	}
	if spanContext.HasSpanID() {
		payload["span_id"] = spanContext.SpanID().String()
	}
	for _, attr := range attrs {
		payload[string(attr.Key)] = attr.Value.AsInterface()
	}
	line, err := json.Marshal(payload)
	if err == nil {
		fmt.Println(string(line))
	}
}
