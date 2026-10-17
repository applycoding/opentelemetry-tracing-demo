package main

import (
	"context"
	"os"
	"strconv"
	"time"

	"go.opentelemetry.io/otel/attribute"
	otlploghttp "go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"
	otlpmetrichttp "go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	otlptracehttp "go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	otellog "go.opentelemetry.io/otel/log"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

const serviceName = "payments"

type Providers struct {
	Tracer     trace.Tracer
	Logger     otellog.Logger
	Processed  metric.Int64Counter
	Duration   metric.Float64Histogram
	Propagator propagation.TextMapPropagator
	shutdown   func(context.Context) error
}

func setupTelemetry(ctx context.Context) (*Providers, error) {
	res, err := resource.New(ctx,
		resource.WithAttributes(
			attribute.String("service.name", serviceName),
			attribute.String("service.version", "0.1.0"),
			attribute.String("deployment.environment.name", "demo"),
		),
	)
	if err != nil {
		return nil, err
	}

	traceExporter, err := otlptracehttp.New(ctx)
	if err != nil {
		return nil, err
	}
	tracerProvider := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(traceExporter),
		sdktrace.WithResource(res),
	)

	metricExporter, err := otlpmetrichttp.New(ctx)
	if err != nil {
		return nil, err
	}
	interval := 5 * time.Second
	if raw := os.Getenv("OTEL_METRIC_EXPORT_INTERVAL"); raw != "" {
		if millis, convErr := strconv.Atoi(raw); convErr == nil && millis > 0 {
			interval = time.Duration(millis) * time.Millisecond
		}
	}
	meterProvider := sdkmetric.NewMeterProvider(
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(metricExporter, sdkmetric.WithInterval(interval))),
		sdkmetric.WithResource(res),
	)

	logExporter, err := otlploghttp.New(ctx)
	if err != nil {
		return nil, err
	}
	loggerProvider := sdklog.NewLoggerProvider(
		sdklog.WithProcessor(sdklog.NewBatchProcessor(logExporter)),
		sdklog.WithResource(res),
	)

	meter := meterProvider.Meter(serviceName)
	processed, err := meter.Int64Counter("payments_processed",
		metric.WithDescription("Payment messages processed by the payments service"),
	)
	if err != nil {
		return nil, err
	}
	duration, err := meter.Float64Histogram("payment_duration_ms",
		metric.WithDescription("Payment processing duration in milliseconds"),
	)
	if err != nil {
		return nil, err
	}

	return &Providers{
		Tracer:     tracerProvider.Tracer(serviceName),
		Logger:     loggerProvider.Logger(serviceName),
		Processed:  processed,
		Duration:   duration,
		Propagator: propagation.TraceContext{},
		shutdown: func(shutdownCtx context.Context) error {
			var first error
			if err := loggerProvider.Shutdown(shutdownCtx); err != nil && first == nil {
				first = err
			}
			if err := meterProvider.Shutdown(shutdownCtx); err != nil && first == nil {
				first = err
			}
			if err := tracerProvider.Shutdown(shutdownCtx); err != nil && first == nil {
				first = err
			}
			return first
		},
	}, nil
}

func (p *Providers) Shutdown(ctx context.Context) error {
	return p.shutdown(ctx)
}
