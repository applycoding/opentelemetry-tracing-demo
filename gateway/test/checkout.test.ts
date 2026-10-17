import { SpanKind, SpanStatusCode } from "@opentelemetry/api";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { afterEach, describe, expect, it } from "vitest";

import { checkout, parseCheckout, type CheckoutDependencies } from "../src/checkout";
import { createTelemetry, type Telemetry } from "../src/telemetry";
import type { CheckoutRequest, OrdersClient } from "../src/types";

const baseRequest: CheckoutRequest = {
  sku: "field-notes",
  amount: 12,
  quantity: 1,
  injectError: false,
  errorAt: "none",
  injectLatencyMs: 0,
  latencyAt: "none",
};

function spanNamed(spans: ReadableSpan[], name: string): ReadableSpan {
  const found = spans.find((span) => span.name === name);
  if (found === undefined) {
    throw new Error(`missing span ${name}`);
  }
  return found;
}

function traceParentParts(value: string): { traceId: string; spanId: string } {
  const parts = value.split("-");
  if (parts.length !== 4) {
    throw new Error(`bad traceparent ${value}`);
  }
  return { traceId: parts[1] ?? "", spanId: parts[2] ?? "" };
}

describe("checkout", () => {
  let telemetry: Telemetry | undefined;

  afterEach(async () => {
    if (telemetry !== undefined) {
      await telemetry.shutdown();
      telemetry = undefined;
    }
  });

  function deps(client: OrdersClient, overrides?: Partial<CheckoutDependencies>): CheckoutDependencies {
    telemetry = createTelemetry({ inMemory: true });
    return {
      tracer: telemetry.tracer,
      logger: telemetry.logger,
      checkoutRequests: telemetry.checkoutRequests,
      checkoutDuration: telemetry.checkoutDuration,
      ordersClient: client,
      ordersUrl: "http://orders:8080",
      sleep: async () => undefined,
      now: () => 1_000,
      ...overrides,
    };
  }

  it("records spans, attributes, and the outgoing traceparent", async () => {
    const seen: { headers?: Record<string, string>; body?: CheckoutRequest } = {};
    const result = await checkout(
      { ...baseRequest, injectError: true, errorAt: "payments" },
      deps(async (_url, body, headers) => {
        seen.headers = headers;
        seen.body = body;
        return {
          status: 200,
          body: { orderId: "ord_test", status: "payment_queued", traceId: "downstream" },
        };
      }),
    );

    expect(result.ok).toBe(true);
    if (!result.ok) {
      return;
    }
    expect(result.orderId).toBe("ord_test");
    expect(seen.body?.errorAt).toBe("payments");
    expect(seen.body?.injectError).toBe(true);

    const spans = telemetry.spanExporter?.getFinishedSpans() ?? [];
    const server = spanNamed(spans, "gateway.checkout");
    const client = spanNamed(spans, "gateway.call_orders");
    expect(server.kind).toBe(SpanKind.SERVER);
    expect(client.kind).toBe(SpanKind.CLIENT);
    expect(server.attributes["order.sku"]).toBe("field-notes");
    expect(server.attributes["order.amount"]).toBe(12);
    expect(server.attributes["order.quantity"]).toBe(1);
    expect(server.attributes["order.id"]).toBe("ord_test");
    expect(server.attributes["app.injected_error"]).toBe(false);
    expect(client.parentSpanContext?.spanId).toBe(server.spanContext().spanId);
    expect(client.spanContext().traceId).toBe(server.spanContext().traceId);

    const parent = traceParentParts(seen.headers?.traceparent ?? "");
    expect(parent.traceId).toBe(client.spanContext().traceId);
    expect(parent.spanId).toBe(client.spanContext().spanId);
    expect(result.traceId).toBe(server.spanContext().traceId);

    const logs = telemetry.logExporter?.getFinishedLogRecords() ?? [];
    expect(logs.length).toBeGreaterThan(0);
    expect(logs[0]?.spanContext?.traceId).toBe(server.spanContext().traceId);
  });

  it("marks the gateway span as an error and does not call orders", async () => {
    let called = false;
    const slept: number[] = [];
    const result = await checkout(
      {
        ...baseRequest,
        injectError: true,
        errorAt: "gateway",
        injectLatencyMs: 250,
        latencyAt: "gateway",
      },
      deps(
        async () => {
          called = true;
          return { status: 200, body: { orderId: "ord_unused" } };
        },
        {
          sleep: async (ms) => {
            slept.push(ms);
          },
        },
      ),
    );

    expect(called).toBe(false);
    expect(slept).toEqual([250]);
    expect(result.ok).toBe(false);
    if (result.ok) {
      return;
    }
    expect(result.status).toBe(500);

    const spans = telemetry.spanExporter?.getFinishedSpans() ?? [];
    expect(spans.map((span) => span.name)).toEqual(["gateway.checkout"]);
    const server = spans[0];
    expect(server?.status.code).toBe(SpanStatusCode.ERROR);
    expect(server?.attributes["app.injected_error"]).toBe(true);
    expect(server?.attributes["app.injected_latency_ms"]).toBe(250);
    expect(telemetry.logExporter?.getFinishedLogRecords()[0]?.spanContext?.traceId).toBe(
      server?.spanContext().traceId,
    );
  });

  it("records an error span when orders returns an error", async () => {
    const result = await checkout(
      { ...baseRequest, injectError: true, errorAt: "orders" },
      deps(async () => ({ status: 500, body: { error: "injected error at orders" } })),
    );
    expect(result).toMatchObject({ ok: false, status: 500, error: "injected error at orders" });
    const spans = telemetry.spanExporter?.getFinishedSpans() ?? [];
    expect(spanNamed(spans, "gateway.checkout").status.code).toBe(SpanStatusCode.ERROR);
    expect(spanNamed(spans, "gateway.call_orders").status.code).toBe(SpanStatusCode.ERROR);
    expect(spanNamed(spans, "gateway.call_orders").attributes["http.response.status_code"]).toBe(500);
  });

  it("rejects a checkout body without a sku", () => {
    expect(parseCheckout({ amount: 1, quantity: 1 })).toEqual({
      ok: false,
      error: "sku is required",
    });
  });
});
