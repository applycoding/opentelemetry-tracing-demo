import express, { type ErrorRequestHandler, type Express } from "express";

import { checkout, parseCheckout, type CheckoutDependencies } from "./checkout";

export function createServer(deps: CheckoutDependencies): Express {
  const app = express();
  app.use(express.json());

  app.get("/health", (_req, res) => {
    res.status(200).json({ status: "ok" });
  });

  app.post("/checkout", async (req, res) => {
    const parsed = parseCheckout(req.body);
    if (!parsed.ok) {
      res.status(400).json({ error: parsed.error });
      return;
    }
    const result = await checkout(parsed.value, deps);
    if (result.ok) {
      res.status(result.status).json({
        orderId: result.orderId,
        status: "payment_queued",
        traceId: result.traceId,
      });
      return;
    }
    res.status(result.status).json({ error: result.error, traceId: result.traceId });
  });

  const invalidJson: ErrorRequestHandler = (err, _req, res, next) => {
    if (err instanceof SyntaxError) {
      res.status(400).json({ error: "invalid json" });
      return;
    }
    next(err);
  };
  app.use(invalidJson);
  return app;
}
