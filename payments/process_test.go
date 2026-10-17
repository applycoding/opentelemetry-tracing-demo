package main

import (
	"context"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

type logCapture struct {
	records []sdklog.Record
}

func (c *logCapture) Enabled(context.Context, sdklog.EnabledParameters) bool { return true }

func (c *logCapture) OnEmit(_ context.Context, record *sdklog.Record) error {
	c.records = append(c.records, record.Clone())
	return nil
}

func (c *logCapture) Shutdown(context.Context) error { return nil }

func (c *logCapture) ForceFlush(context.Context) error { return nil }

func testDeps(t *testing.T, sleep func(time.Duration)) (Dependencies, *tracetest.SpanRecorder, *logCapture) {
	t.Helper()
	recorder := tracetest.NewSpanRecorder()
	tracerProvider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder))
	capture := &logCapture{}
	loggerProvider := sdklog.NewLoggerProvider(sdklog.WithProcessor(capture))
	meterProvider := sdkmetric.NewMeterProvider()
	meter := meterProvider.Meter("payments-test")
	processed, err := meter.Int64Counter("payments_processed")
	if err != nil {
		t.Fatalf("counter: %v", err)
	}
	duration, err := meter.Float64Histogram("payment_duration_ms")
	if err != nil {
		t.Fatalf("histogram: %v", err)
	}
	if sleep == nil {
		sleep = func(time.Duration) {}
	}
	return Dependencies{
		Tracer:     tracerProvider.Tracer("payments-test"),
		Propagator: propagation.TraceContext{},
		Logger:     loggerProvider.Logger("payments-test"),
		Processed:  processed,
		Duration:   duration,
		Sleep:      sleep,
		Queue:      paymentsQueue,
	}, recorder, capture
}

func spanByName(t *testing.T, recorder *tracetest.SpanRecorder, name string) sdktrace.ReadOnlySpan {
	t.Helper()
	for _, span := range recorder.Ended() {
		if span.Name() == name {
			return span
		}
	}
	t.Fatalf("missing span %s", name)
	return nil
}

func attrString(span sdktrace.ReadOnlySpan, key string) string {
	for _, kv := range span.Attributes() {
		if string(kv.Key) == key {
			return kv.Value.AsString()
		}
	}
	return ""
}

func TestProcessContinuesTheQueuedTrace(t *testing.T) {
	deps, recorder, logs := testDeps(t, nil)
	ctx, parent := deps.Tracer.Start(context.Background(), "orders.enqueue_payment")
	carrier := propagation.MapCarrier{}
	deps.Propagator.Inject(ctx, carrier)
	parent.End()

	msg := PaymentMessage{
		OrderID:         "ord_123",
		SKU:             "field-notes",
		Amount:          12,
		Quantity:        1,
		InjectError:     false,
		ErrorAt:         "none",
		InjectLatencyMs: 0,
		LatencyAt:       "none",
		TraceParent:     carrier["traceparent"],
		TraceState:      carrier["tracestate"],
	}
	result, err := Process(context.Background(), msg, deps)
	if err != nil {
		t.Fatalf("process: %v", err)
	}
	if result.Status != "approved" {
		t.Fatalf("status = %s", result.Status)
	}

	span := spanByName(t, recorder, "payments.process")
	if span.SpanContext().TraceID() != parent.SpanContext().TraceID() {
		t.Fatalf("trace id mismatch")
	}
	if span.Parent().SpanID() != parent.SpanContext().SpanID() {
		t.Fatalf("parent span id = %s, want %s", span.Parent().SpanID(), parent.SpanContext().SpanID())
	}
	if span.SpanKind() != trace.SpanKindConsumer {
		t.Fatalf("span kind = %s", span.SpanKind())
	}
	if attrString(span, "order.id") != "ord_123" {
		t.Fatalf("order.id = %s", attrString(span, "order.id"))
	}
	if attrString(span, "messaging.system") != "redis" {
		t.Fatalf("messaging.system = %s", attrString(span, "messaging.system"))
	}
	if attrString(span, "messaging.operation.name") != "process" {
		t.Fatalf("operation = %s", attrString(span, "messaging.operation.name"))
	}
	if got := span.Attributes(); !hasBool(got, "app.injected_error", false) {
		t.Fatalf("injected error attribute = %#v", got)
	}
	if result.TraceID != span.SpanContext().TraceID().String() {
		t.Fatalf("result trace id = %s", result.TraceID)
	}
	if len(logs.records) == 0 {
		t.Fatal("expected a correlated log record")
	}
	if logs.records[0].TraceID() != span.SpanContext().TraceID() {
		t.Fatalf("log trace id = %s", logs.records[0].TraceID())
	}
}

func TestProcessRecordsAnInjectedError(t *testing.T) {
	slept := []time.Duration{}
	deps, recorder, logs := testDeps(t, func(delay time.Duration) {
		slept = append(slept, delay)
	})
	_, err := Process(context.Background(), PaymentMessage{
		OrderID:         "ord_err",
		SKU:             "linen-tote",
		Amount:          24,
		Quantity:        1,
		InjectError:     true,
		ErrorAt:         "payments",
		InjectLatencyMs: 30,
		LatencyAt:       "payments",
	}, deps)
	if err == nil {
		t.Fatal("expected an injected error")
	}
	if len(slept) != 1 || slept[0] != 30*time.Millisecond {
		t.Fatalf("sleeps = %v", slept)
	}
	span := spanByName(t, recorder, "payments.process")
	if span.Status().Code != codes.Error {
		t.Fatalf("status = %s", span.Status().Code)
	}
	if !hasInt(span.Attributes(), "app.injected_latency_ms", 30) {
		t.Fatalf("latency attribute missing: %#v", span.Attributes())
	}
	if span.Parent().IsValid() {
		t.Fatal("missing traceparent should start a new trace")
	}
	if len(logs.records) == 0 || logs.records[0].TraceID() != span.SpanContext().TraceID() {
		t.Fatal("error log was not correlated with the payment span")
	}
}

func hasBool(attrs []attribute.KeyValue, key string, want bool) bool {
	for _, kv := range attrs {
		if string(kv.Key) == key {
			return kv.Value.AsBool() == want
		}
	}
	return false
}

func hasInt(attrs []attribute.KeyValue, key string, want int) bool {
	for _, kv := range attrs {
		if string(kv.Key) == key {
			return int(kv.Value.AsInt64()) == want
		}
	}
	return false
}
