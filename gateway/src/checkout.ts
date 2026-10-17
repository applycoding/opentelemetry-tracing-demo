import { context, propagation, SpanKind, SpanStatusCode, type Tracer } from "@opentelemetry/api";
import type { Counter, Histogram } from "@opentelemetry/api";
import type { Logger } from "@opentelemetry/api-logs";

import { emitServiceLog } from "./telemetry";
import {
  isStage,
  matchesStage,
  type CheckoutRequest,
  type CheckoutResult,
  type OrdersClient,
  type ParseResult,
} from "./types";

export type CheckoutDependencies = {
  tracer: Tracer;
  logger: Logger;
  checkoutRequests: Counter;
  checkoutDuration: Histogram;
  ordersClient: OrdersClient;
  ordersUrl: string;
  sleep: (ms: number) => Promise<void>;
  now: () => number;
};

type OrderBody = {
  orderId?: unknown;
  error?: unknown;
};

export function parseCheckout(input: unknown): ParseResult {
  if (typeof input !== "object" || input === null) {
    return { ok: false, error: "request body must be a JSON object" };
  }
  const body = input as Record<string, unknown>;
  if (typeof body.sku !== "string" || body.sku.trim() === "") {
    return { ok: false, error: "sku is required" };
  }
  if (typeof body.amount !== "number" || !Number.isFinite(body.amount) || body.amount < 0) {
    return { ok: false, error: "amount must be a non-negative number" };
  }
  if (typeof body.quantity !== "number" || !Number.isInteger(body.quantity) || body.quantity < 1) {
    return { ok: false, error: "quantity must be an integer greater than zero" };
  }
  const injectError = body.injectError === undefined ? false : body.injectError;
  if (typeof injectError !== "boolean") {
    return { ok: false, error: "injectError must be a boolean" };
  }
  const errorAt = body.errorAt === undefined ? "none" : body.errorAt;
  if (!isStage(errorAt)) {
    return { ok: false, error: "errorAt must be gateway, orders, payments, or none" };
  }
  const injectLatencyMs = body.injectLatencyMs === undefined ? 0 : body.injectLatencyMs;
  if (
    typeof injectLatencyMs !== "number" ||
    !Number.isInteger(injectLatencyMs) ||
    injectLatencyMs < 0
  ) {
    return { ok: false, error: "injectLatencyMs must be a non-negative integer" };
  }
  const latencyAt = body.latencyAt === undefined ? "none" : body.latencyAt;
  if (!isStage(latencyAt)) {
    return { ok: false, error: "latencyAt must be gateway, orders, payments, or none" };
  }
  return {
    ok: true,
    value: {
      sku: body.sku,
      amount: body.amount,
      quantity: body.quantity,
      injectError,
      errorAt,
      injectLatencyMs,
      latencyAt,
    },
  };
}

function errorMessage(body: unknown, fallback: string): string {
  if (typeof body === "object" && body !== null) {
    const record = body as OrderBody;
    if (typeof record.error === "string" && record.error !== "") {
      return record.error;
    }
  }
  return fallback;
}

function orderIdFrom(body: unknown): string | undefined {
  if (typeof body === "object" && body !== null) {
    const record = body as OrderBody;
    if (typeof record.orderId === "string" && record.orderId !== "") {
      return record.orderId;
    }
  }
  return undefined;
}

export async function checkout(
  request: CheckoutRequest,
  deps: CheckoutDependencies,
): Promise<CheckoutResult> {
  const started = deps.now();
  return deps.tracer.startActiveSpan(
    "gateway.checkout",
    {
      kind: SpanKind.SERVER,
      attributes: {
        "http.request.method": "POST",
        "http.route": "/checkout",
        "order.sku": request.sku,
        "order.amount": request.amount,
        "order.quantity": request.quantity,
        "app.injected_error": request.injectError && matchesStage(request.errorAt, "gateway"),
      },
    },
    async (span): Promise<CheckoutResult> => {
      let metricStatus: "ok" | "error" = "error";
      const traceId = span.spanContext().traceId;
      try {
        if (matchesStage(request.latencyAt, "gateway") && request.injectLatencyMs > 0) {
          span.setAttribute("app.injected_latency_ms", request.injectLatencyMs);
          await deps.sleep(request.injectLatencyMs);
        }

        if (request.injectError && matchesStage(request.errorAt, "gateway")) {
          const error = new Error("injected error at gateway");
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          emitServiceLog(deps.logger, "ERROR", error.message, {
            "order.sku": request.sku,
            "app.injected_error": true,
          });
          span.setAttribute("http.response.status_code", 500);
          return { ok: false, status: 500, error: error.message, traceId };
        }

        const response = await deps.tracer.startActiveSpan(
          "gateway.call_orders",
          {
            kind: SpanKind.CLIENT,
            attributes: {
              "http.request.method": "POST",
              "http.route": "/orders",
              "server.address": "orders",
            },
          },
          async (clientSpan) => {
            try {
              const headers: Record<string, string> = {};
              propagation.inject(context.active(), headers);
              const downstream = await deps.ordersClient(deps.ordersUrl, request, headers);
              clientSpan.setAttribute("http.response.status_code", downstream.status);
              if (downstream.status >= 400) {
                clientSpan.setStatus({
                  code: SpanStatusCode.ERROR,
                  message: `orders returned ${downstream.status}`,
                });
              }
              return downstream;
            } catch (caught) {
              const error = caught instanceof Error ? caught : new Error(String(caught));
              clientSpan.recordException(error);
              clientSpan.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
              throw error;
            } finally {
              clientSpan.end();
            }
          },
        );

        if (response.status >= 400) {
          const message = errorMessage(response.body, `orders returned ${response.status}`);
          span.setStatus({ code: SpanStatusCode.ERROR, message });
          span.setAttribute("http.response.status_code", response.status);
          emitServiceLog(deps.logger, "ERROR", message, { "order.sku": request.sku });
          return { ok: false, status: response.status, error: message, traceId };
        }

        const orderId = orderIdFrom(response.body);
        if (orderId === undefined) {
          const message = "orders response did not include orderId";
          span.setStatus({ code: SpanStatusCode.ERROR, message });
          span.setAttribute("http.response.status_code", 502);
          emitServiceLog(deps.logger, "ERROR", message, { "order.sku": request.sku });
          return { ok: false, status: 502, error: message, traceId };
        }

        span.setAttribute("order.id", orderId);
        span.setAttribute("http.response.status_code", 200);
        span.setStatus({ code: SpanStatusCode.OK });
        metricStatus = "ok";
        emitServiceLog(deps.logger, "INFO", "checkout accepted", {
          "order.id": orderId,
          "order.sku": request.sku,
        });
        return { ok: true, status: 200, orderId, traceId };
      } catch (caught) {
        const error = caught instanceof Error ? caught : new Error(String(caught));
        span.recordException(error);
        span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
        span.setAttribute("http.response.status_code", 502);
        emitServiceLog(deps.logger, "ERROR", error.message, { "order.sku": request.sku });
        return { ok: false, status: 502, error: error.message, traceId };
      } finally {
        const elapsed = deps.now() - started;
        deps.checkoutDuration.record(elapsed, { status: metricStatus });
        deps.checkoutRequests.add(1, { status: metricStatus });
        span.end();
      }
    },
  );
}

export async function postOrders(
  url: string,
  body: CheckoutRequest,
  headers: Record<string, string>,
): Promise<{ status: number; body: unknown }> {
  const response = await fetch(`${url.replace(/\/$/, "")}/orders`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...headers,
    },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  if (text === "") {
    return { status: response.status, body: null };
  }
  try {
    return { status: response.status, body: JSON.parse(text) as unknown };
  } catch {
    return { status: response.status, body: { raw: text } };
  }
}
