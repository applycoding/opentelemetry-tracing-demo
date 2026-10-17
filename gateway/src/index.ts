import { postOrders } from "./checkout";
import { createServer } from "./server";
import { createTelemetry } from "./telemetry";
import type { CheckoutDependencies } from "./checkout";

const telemetry = createTelemetry();
const port = Number(process.env.PORT ?? "8080");
const ordersUrl = process.env.ORDERS_URL ?? "http://orders:8080";

const deps: CheckoutDependencies = {
  tracer: telemetry.tracer,
  logger: telemetry.logger,
  checkoutRequests: telemetry.checkoutRequests,
  checkoutDuration: telemetry.checkoutDuration,
  ordersClient: postOrders,
  ordersUrl,
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  now: () => Date.now(),
};

const app = createServer(deps);
const server = app.listen(port, "0.0.0.0", () => {
  console.log(JSON.stringify({ service: "gateway", msg: "listening", port }));
});

async function shutdown(): Promise<void> {
  await new Promise<void>((resolve) => {
    server.close(() => resolve());
  });
  await telemetry.shutdown();
}

process.on("SIGTERM", () => {
  void shutdown().then(() => process.exit(0));
});
process.on("SIGINT", () => {
  void shutdown().then(() => process.exit(0));
});
