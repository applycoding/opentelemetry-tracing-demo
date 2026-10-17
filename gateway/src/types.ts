export const stages = ["gateway", "orders", "payments", "none"] as const;

export type Stage = (typeof stages)[number];

export type CheckoutRequest = {
  sku: string;
  amount: number;
  quantity: number;
  injectError: boolean;
  errorAt: Stage;
  injectLatencyMs: number;
  latencyAt: Stage;
};

export type ParseResult =
  | { ok: true; value: CheckoutRequest }
  | { ok: false; error: string };

export type OrdersResponse = {
  status: number;
  body: unknown;
};

export type OrdersClient = (
  url: string,
  body: CheckoutRequest,
  headers: Record<string, string>,
) => Promise<OrdersResponse>;

export type CheckoutResult =
  | { ok: true; status: 200; orderId: string; traceId: string }
  | { ok: false; status: number; error: string; traceId: string };

export function isStage(value: unknown): value is Stage {
  return typeof value === "string" && (stages as readonly string[]).includes(value);
}

export function matchesStage(stage: Stage, here: Stage): boolean {
  switch (stage) {
    case "gateway":
    case "orders":
    case "payments":
    case "none":
      return stage === here;
    default: {
      const unexpected: never = stage;
      throw new Error(`unexpected stage ${String(unexpected)}`);
    }
  }
}
