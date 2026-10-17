#!/usr/bin/env python3
"""Send checkout traffic, including injected errors and latency, to the gateway."""

from __future__ import annotations

import argparse
import json
import random
import urllib.error
import urllib.request
from collections import Counter


CATALOG = (
    ("field-notes", 12.0),
    ("ceramic-mug", 18.5),
    ("linen-tote", 24.0),
)
STAGES = ("gateway", "orders", "payments")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:18081", help="Gateway base URL")
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--error-rate", type=float, default=0.2)
    parser.add_argument("--latency-rate", type=float, default=0.2)
    parser.add_argument("--latency-ms", type=int, default=300)
    args = parser.parse_args()
    if args.count < 1:
        print("count must be at least 1")
        return 2

    statuses: Counter[int] = Counter()
    failures = 0
    for index in range(args.count):
        sku, amount = CATALOG[index % len(CATALOG)]
        body: dict[str, object] = {
            "sku": sku,
            "amount": amount,
            "quantity": 1,
            "injectError": False,
            "errorAt": "none",
            "injectLatencyMs": 0,
            "latencyAt": "none",
        }
        if random.random() < args.error_rate:
            body["injectError"] = True
            body["errorAt"] = random.choice(STAGES)
        if random.random() < args.latency_rate:
            body["injectLatencyMs"] = args.latency_ms
            body["latencyAt"] = random.choice(STAGES)
        status, payload = post_json(f"{args.url.rstrip('/')}/checkout", body)
        statuses[status] += 1
        trace_id = payload.get("traceId", "")
        if status >= 400:
            failures += 1
        print(
            json.dumps(
                {
                    "status": status,
                    "traceId": trace_id,
                    "errorAt": body["errorAt"],
                    "latencyAt": body["latencyAt"],
                    "error": payload.get("error", ""),
                    "orderId": payload.get("orderId", ""),
                }
            )
        )

    print(
        json.dumps(
            {
                "requests": args.count,
                "http_status_counts": dict(sorted(statuses.items())),
                "http_errors": failures,
                "note": "Counts are what this process observed. They are not a benchmark.",
            }
        )
    )
    return 0


def post_json(url: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode()
            parsed = json.loads(raw) if raw else {}
            if not isinstance(parsed, dict):
                parsed = {"raw": parsed}
            return response.status, parsed
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"error": raw}
        if not isinstance(parsed, dict):
            parsed = {"raw": parsed}
        return error.code, parsed


if __name__ == "__main__":
    raise SystemExit(main())
