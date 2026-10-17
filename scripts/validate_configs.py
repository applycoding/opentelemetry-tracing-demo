#!/usr/bin/env python3
"""Check the collector, Prometheus, Grafana, and compose files for this demo."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    errors: list[str] = []
    errors.extend(check_collector(ROOT / "deploy" / "otel-collector.yaml"))
    errors.extend(check_prometheus(ROOT / "deploy" / "prometheus.yml", ROOT / "deploy" / "alerts.yml"))
    errors.extend(check_grafana(ROOT / "deploy" / "grafana"))
    errors.extend(check_compose(ROOT / "docker-compose.yml"))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("config validation passed")
    return 0


def load_yaml(path: Path) -> object:
    return yaml.safe_load(path.read_text())


def check_collector(path: Path) -> list[str]:
    errors: list[str] = []
    data = load_yaml(path)
    if not isinstance(data, dict):
        return [f"{path} must be a mapping"]
    protocols = data.get("receivers", {}).get("otlp", {}).get("protocols", {})
    if "grpc" not in protocols or "http" not in protocols:
        errors.append("collector otlp receiver needs grpc and http")
    exporters = data.get("exporters", {})
    if "otlp/jaeger" not in exporters:
        errors.append("collector is missing the otlp/jaeger exporter")
    if "prometheus" not in exporters:
        errors.append("collector is missing the prometheus exporter")
    if "debug" not in exporters:
        errors.append("collector is missing the debug log exporter")
    pipelines = data.get("service", {}).get("pipelines", {})
    expected = {
        "traces": {"otlp/jaeger"},
        "metrics": {"prometheus"},
        "logs": {"debug"},
    }
    for name, required in expected.items():
        pipeline = pipelines.get(name, {})
        if pipeline.get("receivers") != ["otlp"]:
            errors.append(f"{name} pipeline must receive otlp")
        exporters_for_pipeline = set(pipeline.get("exporters", []))
        if not required.issubset(exporters_for_pipeline):
            errors.append(f"{name} pipeline must export to {sorted(required)}")
    return errors


def check_prometheus(config_path: Path, alerts_path: Path) -> list[str]:
    errors: list[str] = []
    config = load_yaml(config_path)
    alerts = load_yaml(alerts_path)
    if not isinstance(config, dict) or not isinstance(alerts, dict):
        return ["prometheus config files must be mappings"]
    rule_files = config.get("rule_files", [])
    if "/etc/prometheus/alerts.yml" not in rule_files:
        errors.append("prometheus config must load /etc/prometheus/alerts.yml")
    jobs = [job.get("job_name") for job in config.get("scrape_configs", [])]
    if "otel-collector" not in jobs:
        errors.append("prometheus must scrape the otel-collector job")
    rules = []
    for group in alerts.get("groups", []):
        rules.extend(group.get("rules", []))
    if len(rules) < 2:
        errors.append("prometheus needs at least two alert rules")
    for rule in rules:
        description = str(rule.get("annotations", {}).get("description", ""))
        if "PLACEHOLDER" not in description:
            errors.append(f"alert {rule.get('alert')} must mark its threshold as a placeholder")
    return errors


def check_grafana(root: Path) -> list[str]:
    errors: list[str] = []
    datasources = load_yaml(root / "provisioning" / "datasources" / "datasources.yml")
    provider = load_yaml(root / "provisioning" / "dashboards" / "dashboards.yml")
    rules = load_yaml(root / "provisioning" / "alerting" / "rules.yml")
    dashboard = json.loads((root / "dashboards" / "tracing.json").read_text())
    if not isinstance(datasources, dict) or not isinstance(provider, dict) or not isinstance(rules, dict):
        return ["grafana provisioning files must be mappings"]
    kinds = {item.get("type") for item in datasources.get("datasources", [])}
    if kinds != {"prometheus", "jaeger"} and not {"prometheus", "jaeger"}.issubset(kinds):
        errors.append("grafana needs prometheus and jaeger datasources")
    path = provider.get("providers", [{}])[0].get("options", {}).get("path")
    if path != "/etc/grafana/dashboards":
        errors.append("grafana dashboard provider path must be /etc/grafana/dashboards")
    grafana_rules = []
    for group in rules.get("groups", []):
        grafana_rules.extend(group.get("rules", []))
    if len(grafana_rules) < 2:
        errors.append("grafana needs at least two provisioned alert rules")
    for rule in grafana_rules:
        description = str(rule.get("annotations", {}).get("description", ""))
        if "PLACEHOLDER" not in description:
            errors.append(f"grafana rule {rule.get('uid')} must mark its threshold as a placeholder")
    if dashboard.get("uid") != "otel-tracing-demo":
        errors.append("dashboard uid must be otel-tracing-demo")
    if len(dashboard.get("panels", [])) < 4:
        errors.append("dashboard needs the checkout, orders, and payments panels")
    return errors


def check_compose(path: Path) -> list[str]:
    errors: list[str] = []
    data = load_yaml(path)
    if not isinstance(data, dict):
        return ["compose file must be a mapping"]
    services = data.get("services", {})
    required = ["redis", "jaeger", "otel-collector", "prometheus", "grafana", "gateway", "orders", "payments"]
    for name in required:
        if name not in services:
            errors.append(f"compose is missing service {name}")
    for name, service in services.items():
        image = service.get("image", "")
        if isinstance(image, str) and (image.endswith(":latest") or image == ""):
            if "build" not in service:
                errors.append(f"service {name} must pin an image tag")
    collector = services.get("otel-collector", {})
    volumes = collector.get("volumes", [])
    if not any("otel-collector.yaml" in item for item in volumes):
        errors.append("collector must mount deploy/otel-collector.yaml")
    prometheus = services.get("prometheus", {})
    if not any("alerts.yml" in item for item in prometheus.get("volumes", [])):
        errors.append("prometheus must mount deploy/alerts.yml")
    grafana = services.get("grafana", {})
    if not any("provisioning" in item for item in grafana.get("volumes", [])):
        errors.append("grafana must mount its provisioning directory")
    gateway = services.get("gateway", {}).get("environment", {})
    orders = services.get("orders", {}).get("environment", {})
    payments = services.get("payments", {}).get("environment", {})
    if "ORDERS_URL" not in gateway or "OTEL_EXPORTER_OTLP_ENDPOINT" not in gateway:
        errors.append("gateway needs ORDERS_URL and OTEL_EXPORTER_OTLP_ENDPOINT")
    if "REDIS_URL" not in orders:
        errors.append("orders needs REDIS_URL")
    if "REDIS_ADDR" not in payments:
        errors.append("payments needs REDIS_ADDR")
    return errors


if __name__ == "__main__":
    sys.exit(main())
