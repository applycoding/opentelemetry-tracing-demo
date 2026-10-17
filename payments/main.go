package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	providers, err := setupTelemetry(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "telemetry setup failed: %v\n", err)
		os.Exit(1)
	}

	addr := getenv("REDIS_ADDR", "redis:6379")
	queue := getenv("QUEUE", paymentsQueue)
	port := getenv("PORT", "8080")
	client := redis.NewClient(&redis.Options{Addr: addr})

	deps := Dependencies{
		Tracer:     providers.Tracer,
		Propagator: providers.Propagator,
		Logger:     providers.Logger,
		Processed:  providers.Processed,
		Duration:   providers.Duration,
		Sleep:      time.Sleep,
		Queue:      queue,
	}

	go consume(ctx, client, queue, deps)

	server := &http.Server{
		Addr:              ":" + port,
		Handler:           routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
	}()

	fmt.Println(mustJSON(map[string]string{"service": "payments", "msg": "listening", "port": port}))
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		fmt.Fprintf(os.Stderr, "http server failed: %v\n", err)
		os.Exit(1)
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = client.Close()
	if err := providers.Shutdown(shutdownCtx); err != nil {
		fmt.Fprintf(os.Stderr, "telemetry shutdown failed: %v\n", err)
	}
}

func routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("content-type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	return mux
}

func consume(ctx context.Context, client *redis.Client, queue string, deps Dependencies) {
	for {
		if ctx.Err() != nil {
			return
		}
		result, err := client.BRPop(ctx, 2*time.Second, queue).Result()
		if err != nil {
			if errors.Is(err, redis.Nil) || errors.Is(err, context.Canceled) || ctx.Err() != nil {
				continue
			}
			fmt.Println(mustJSON(map[string]string{
				"service":  "payments",
				"severity": "ERROR",
				"msg":      "redis pop failed",
				"error":    err.Error(),
			}))
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Second):
			}
			continue
		}
		if len(result) != 2 {
			continue
		}
		var msg PaymentMessage
		if err := json.Unmarshal([]byte(result[1]), &msg); err != nil {
			fmt.Println(mustJSON(map[string]string{
				"service":  "payments",
				"severity": "ERROR",
				"msg":      "payment message was not valid json",
				"error":    err.Error(),
			}))
			continue
		}
		_, _ = Process(ctx, msg, deps)
	}
}

func getenv(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func mustJSON(payload map[string]string) string {
	line, err := json.Marshal(payload)
	if err != nil {
		return "{\"msg\":\"log marshal failed\"}"
	}
	return string(line)
}
