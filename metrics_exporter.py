# =============================================================================
# tools/metrics_exporter.py — Exportador de Métricas de Tokens
# Fase 4: Monitoreo & Observabilidad
# Destinos: Datadog StatsD | Grafana (via Prometheus pushgateway o JSON API)
# Uso standalone: python metrics_exporter.py --source /tmp/token_metrics.json
# Uso integrado: desde orchestrator.py al final de cada ejecución
# =============================================================================

import json
import os
import sys
import socket
import logging
import argparse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger('metrics_exporter')


# =============================================================================
# DTOs
# =============================================================================

@dataclass
class TokenMetric:
    """Una llamada individual a la API de Claude."""
    timestamp_iso: str
    model: str
    input_tokens: int
    output_tokens: int
    task_type: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_dict(cls, d: dict) -> 'TokenMetric':
        return cls(
            timestamp_iso=d['timestamp_iso'],
            model=d['model'],
            input_tokens=int(d['input_tokens']),
            output_tokens=int(d['output_tokens']),
            task_type=d['task_type'],
        )


@dataclass
class ExportResult:
    destination: str
    success: bool
    records_sent: int
    error: Optional[str] = None


# =============================================================================
# DATADOG (via StatsD UDP — sin dependencias externas)
# =============================================================================

class DatadogExporter:
    """
    Envía métricas a Datadog via StatsD UDP (puerto 8125).
    No requiere instalar datadog SDK — usa sockets UDP puros.

    Métricas enviadas:
      claude.tokens.input       (gauge)
      claude.tokens.output      (gauge)
      claude.tokens.total       (gauge)
      claude.api.requests       (counter)
      claude.kpi.exceeded       (gauge, 0 o 1)
      claude.cost.estimated_usd (gauge)
    """

    # Costo estimado por 1M tokens (actualizar según precios reales)
    COST_PER_M_TOKENS = {
        'claude-opus-4-5':           {'input': 15.0,  'output': 75.0},
        'claude-sonnet-4-5':         {'input': 3.0,   'output': 15.0},
        'claude-haiku-4-5-20251001': {'input': 0.25,  'output': 1.25},
    }
    DEFAULT_COST = {'input': 3.0, 'output': 15.0}
    KPI_LIMIT = 1500

    def __init__(
        self,
        host: str = 'localhost',
        port: int = 8125,
        prefix: str = 'claude',
        tags: Optional[list[str]] = None,
    ):
        self.host = host
        self.port = port
        self.prefix = prefix
        self.base_tags = tags or []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _send(self, metric: str, value: float, metric_type: str, tags: list[str]) -> None:
        """Envía un paquete StatsD UDP."""
        all_tags = self.base_tags + tags
        tag_str = f"|#{','.join(all_tags)}" if all_tags else ""
        payload = f"{self.prefix}.{metric}:{value}|{metric_type}{tag_str}"
        try:
            self._sock.sendto(payload.encode('utf-8'), (self.host, self.port))
        except Exception as e:
            logger.debug(f"StatsD send error: {e}")

    def _gauge(self, metric: str, value: float, tags: list[str] = []) -> None:
        self._send(metric, value, 'g', tags)

    def _counter(self, metric: str, value: float = 1, tags: list[str] = []) -> None:
        self._send(metric, value, 'c', tags)

    def _estimate_cost_usd(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Estima el costo en USD basado en el modelo y tokens."""
        # Normalizar nombre del modelo
        costs = self.COST_PER_M_TOKENS.get(model, self.DEFAULT_COST)
        return (input_tokens * costs['input'] + output_tokens * costs['output']) / 1_000_000

    def export(self, metrics: list[TokenMetric]) -> ExportResult:
        """
        Exporta todas las métricas a Datadog.
        Agrega por task_type y modelo para reducir cardinalidad.
        """
        if not metrics:
            return ExportResult('datadog_statsd', True, 0)

        try:
            # Métricas por registro individual
            total_input = 0
            total_output = 0
            total_cost = 0.0

            for m in metrics:
                tags = [f"model:{m.model}", f"task:{m.task_type}"]

                self._gauge('tokens.input',  m.input_tokens,  tags)
                self._gauge('tokens.output', m.output_tokens, tags)
                self._gauge('tokens.total',  m.total_tokens,  tags)
                self._counter('api.requests', tags=tags)

                cost = self._estimate_cost_usd(m.model, m.input_tokens, m.output_tokens)
                self._gauge('cost.estimated_usd', cost, tags)

                total_input  += m.input_tokens
                total_output += m.output_tokens
                total_cost   += cost

            # Métricas de sesión acumuladas
            session_total = total_input + total_output
            self._gauge('session.tokens.total',       session_total)
            self._gauge('session.tokens.input',       total_input)
            self._gauge('session.tokens.output',      total_output)
            self._gauge('session.cost.estimated_usd', total_cost)
            self._gauge('session.requests',           len(metrics))
            self._gauge(
                'kpi.exceeded',
                1 if session_total > self.KPI_LIMIT else 0,
                [f"limit:{self.KPI_LIMIT}"]
            )

            logger.info(
                f"[Datadog] {len(metrics)} métricas enviadas a "
                f"{self.host}:{self.port} | "
                f"total_tokens:{session_total} | "
                f"costo_est:${total_cost:.4f}"
            )
            return ExportResult('datadog_statsd', True, len(metrics))

        except Exception as e:
            logger.error(f"[Datadog] Error exportando: {e}")
            return ExportResult('datadog_statsd', False, 0, str(e))

    def close(self) -> None:
        self._sock.close()


# =============================================================================
# GRAFANA (via Prometheus Pushgateway HTTP)
# =============================================================================

class GrafanaExporter:
    """
    Envía métricas a Grafana via Prometheus Pushgateway.
    No requiere instalar prometheus_client — construye el formato texto manualmente.

    Configura en Grafana:
      1. Instala Prometheus Pushgateway: docker run -d -p 9091:9091 prom/pushgateway
      2. Agrega data source Prometheus apuntando al pushgateway
      3. Crea dashboards usando las métricas claude_*

    Métricas expuestas en Prometheus format:
      claude_tokens_input_total
      claude_tokens_output_total
      claude_tokens_total
      claude_api_requests_total
      claude_cost_usd_total
      claude_kpi_exceeded (0 o 1)
    """

    KPI_LIMIT = 1500

    def __init__(
        self,
        pushgateway_url: str = 'http://localhost:9091',
        job: str = 'claude_copilot',
        instance: str = 'orchestrator',
    ):
        self.pushgateway_url = pushgateway_url.rstrip('/')
        self.job = job
        self.instance = instance

    def _build_prometheus_payload(self, metrics: list[TokenMetric]) -> str:
        """
        Construye el payload en formato Prometheus text exposition.
        https://prometheus.io/docs/instrumenting/exposition_formats/
        """
        lines = []
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        # Acumuladores por modelo+task
        by_label: dict[str, dict] = {}

        for m in metrics:
            key = f"{m.model}|{m.task_type}"
            if key not in by_label:
                by_label[key] = {
                    'model': m.model,
                    'task': m.task_type,
                    'input': 0,
                    'output': 0,
                    'requests': 0,
                }
            by_label[key]['input']    += m.input_tokens
            by_label[key]['output']   += m.output_tokens
            by_label[key]['requests'] += 1

        # claude_tokens_input_total
        lines.append('# HELP claude_tokens_input_total Total input tokens consumidos')
        lines.append('# TYPE claude_tokens_input_total counter')
        for data in by_label.values():
            label = f'model="{data["model"]}",task="{data["task"]}"'
            lines.append(f'claude_tokens_input_total{{{label}}} {data["input"]} {now_ms}')

        # claude_tokens_output_total
        lines.append('# HELP claude_tokens_output_total Total output tokens generados')
        lines.append('# TYPE claude_tokens_output_total counter')
        for data in by_label.values():
            label = f'model="{data["model"]}",task="{data["task"]}"'
            lines.append(f'claude_tokens_output_total{{{label}}} {data["output"]} {now_ms}')

        # claude_tokens_total
        lines.append('# HELP claude_tokens_total Total tokens (input + output)')
        lines.append('# TYPE claude_tokens_total counter')
        for data in by_label.values():
            label = f'model="{data["model"]}",task="{data["task"]}"'
            total = data['input'] + data['output']
            lines.append(f'claude_tokens_total{{{label}}} {total} {now_ms}')

        # claude_api_requests_total
        lines.append('# HELP claude_api_requests_total Número de llamadas a la API')
        lines.append('# TYPE claude_api_requests_total counter')
        for data in by_label.values():
            label = f'model="{data["model"]}",task="{data["task"]}"'
            lines.append(f'claude_api_requests_total{{{label}}} {data["requests"]} {now_ms}')

        # claude_kpi_exceeded (gauge global de sesión)
        session_total = sum(m.total_tokens for m in metrics)
        kpi_exceeded = 1 if session_total > self.KPI_LIMIT else 0
        lines.append('# HELP claude_kpi_exceeded 1 si se excedió el límite de tokens por sesión')
        lines.append('# TYPE claude_kpi_exceeded gauge')
        lines.append(f'claude_kpi_exceeded{{limit="{self.KPI_LIMIT}"}} {kpi_exceeded} {now_ms}')

        # claude_session_tokens_total (gauge de sesión)
        lines.append('# HELP claude_session_tokens_total Tokens totales en esta sesión')
        lines.append('# TYPE claude_session_tokens_total gauge')
        lines.append(f'claude_session_tokens_total {session_total} {now_ms}')

        return '\n'.join(lines) + '\n'

    def export(self, metrics: list[TokenMetric]) -> ExportResult:
        """
        Hace PUT al Pushgateway con las métricas en formato Prometheus.
        URL: {pushgateway}/metrics/job/{job}/instance/{instance}
        """
        if not metrics:
            return ExportResult('grafana_pushgateway', True, 0)

        url = f"{self.pushgateway_url}/metrics/job/{self.job}/instance/{self.instance}"
        payload = self._build_prometheus_payload(metrics)

        try:
            data = payload.encode('utf-8')
            req = urllib.request.Request(
                url,
                data=data,
                method='PUT',
                headers={'Content-Type': 'text/plain; version=0.0.4'},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status

            if status in (200, 202):
                session_total = sum(m.total_tokens for m in metrics)
                logger.info(
                    f"[Grafana] {len(metrics)} métricas enviadas a {url} "
                    f"(status:{status}) | session_tokens:{session_total}"
                )
                return ExportResult('grafana_pushgateway', True, len(metrics))
            else:
                err = f"HTTP {status}"
                logger.warning(f"[Grafana] Respuesta inesperada: {err}")
                return ExportResult('grafana_pushgateway', False, 0, err)

        except urllib.error.URLError as e:
            logger.warning(
                f"[Grafana] Pushgateway no disponible en {url}: {e.reason}. "
                "Las métricas se guardan en /tmp/token_metrics.json de todas formas."
            )
            return ExportResult('grafana_pushgateway', False, 0, str(e))
        except Exception as e:
            logger.error(f"[Grafana] Error inesperado: {e}")
            return ExportResult('grafana_pushgateway', False, 0, str(e))


# =============================================================================
# EXPORTER PRINCIPAL (orquesta Datadog + Grafana + JSON local)
# =============================================================================

class MetricsExporter:
    """
    Fachada que exporta a todos los destinos configurados en paralelo.
    Siempre guarda JSON local como fallback.

    Configuración via env vars:
      METRICS_DATADOG_HOST      (default: localhost)
      METRICS_DATADOG_PORT      (default: 8125)
      METRICS_GRAFANA_URL       (default: http://localhost:9091)
      METRICS_OUTPUT_PATH       (default: /tmp/token_metrics.json)
      METRICS_ENABLED           (default: true)
    """

    def __init__(self):
        self.enabled = os.environ.get('METRICS_ENABLED', 'true').lower() == 'true'
        self.output_path = Path(
            os.environ.get('METRICS_OUTPUT_PATH', '/tmp/token_metrics.json')
        )

        # Datadog
        dd_host = os.environ.get('METRICS_DATADOG_HOST', 'localhost')
        dd_port = int(os.environ.get('METRICS_DATADOG_PORT', '8125'))
        env_tag = os.environ.get('METRICS_ENV_TAG', 'dev')
        self._datadog = DatadogExporter(
            host=dd_host,
            port=dd_port,
            tags=[f"env:{env_tag}"],
        )

        # Grafana
        grafana_url = os.environ.get('METRICS_GRAFANA_URL', 'http://localhost:9091')
        self._grafana = GrafanaExporter(pushgateway_url=grafana_url)

    def export_all(self, raw_metrics: list[dict]) -> list[ExportResult]:
        """
        Exporta métricas a todos los destinos.
        raw_metrics: lista de dicts del formato export_for_grafana() del TokenTracker.

        Returns: lista de ExportResult por destino.
        """
        if not self.enabled:
            logger.info("[Metrics] Exportación deshabilitada (METRICS_ENABLED=false)")
            return []

        if not raw_metrics:
            logger.info("[Metrics] Sin métricas para exportar")
            return []

        metrics = [TokenMetric.from_dict(d) for d in raw_metrics]
        results: list[ExportResult] = []

        # 1. Siempre guardar JSON local (fallback y auditoría)
        json_result = self._save_json(raw_metrics)
        results.append(json_result)

        # 2. Datadog StatsD (no bloquea si no está disponible)
        dd_result = self._datadog.export(metrics)
        results.append(dd_result)

        # 3. Grafana Pushgateway (no bloquea si no está disponible)
        grafana_result = self._grafana.export(metrics)
        results.append(grafana_result)

        # Resumen
        ok_count = sum(1 for r in results if r.success)
        logger.info(
            f"[Metrics] Exportación completada: "
            f"{ok_count}/{len(results)} destinos OK"
        )
        for r in results:
            status = "✅" if r.success else "⚠️ "
            logger.info(f"  {status} {r.destination}: {r.records_sent} registros")

        return results

    def _save_json(self, raw_metrics: list[dict]) -> ExportResult:
        """Guarda las métricas en JSON local para auditoría y fallback."""
        try:
            # Append a histórico si ya existe
            existing: list[dict] = []
            if self.output_path.exists():
                try:
                    existing = json.loads(self.output_path.read_text())
                except json.JSONDecodeError:
                    existing = []

            all_metrics = existing + raw_metrics
            # Mantener últimas 10,000 entradas
            if len(all_metrics) > 10_000:
                all_metrics = all_metrics[-10_000:]

            self.output_path.write_text(
                json.dumps(all_metrics, indent=2, ensure_ascii=False),
                encoding='utf-8',
            )
            logger.info(f"[Metrics] JSON guardado: {self.output_path} ({len(all_metrics)} registros totales)")
            return ExportResult('json_local', True, len(raw_metrics))
        except Exception as e:
            logger.error(f"[Metrics] Error guardando JSON: {e}")
            return ExportResult('json_local', False, 0, str(e))


# =============================================================================
# CLI STANDALONE
# Uso: python metrics_exporter.py --source /tmp/token_metrics.json
#      python metrics_exporter.py --source /tmp/token_metrics.json --datadog-host dd-agent
#      python metrics_exporter.py --source /tmp/token_metrics.json --grafana-url http://pushgateway:9091
# =============================================================================

def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Exporta métricas de tokens Claude a Datadog/Grafana',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  # Exportar desde archivo JSON generado por orchestrator.py
  python metrics_exporter.py --source /tmp/token_metrics.json

  # Enviar a Datadog Agent en host remoto
  python metrics_exporter.py --source /tmp/token_metrics.json \\
    --datadog-host dd-agent.internal --datadog-port 8125

  # Enviar a Grafana Pushgateway en Docker
  python metrics_exporter.py --source /tmp/token_metrics.json \\
    --grafana-url http://localhost:9091

  # Ambos destinos + tag de entorno
  python metrics_exporter.py --source /tmp/token_metrics.json \\
    --env production --datadog-host dd-agent --grafana-url http://pushgateway:9091

Setup rápido de Grafana Pushgateway:
  docker run -d -p 9091:9091 prom/pushgateway
  # Luego agregar en Grafana: Data Source > Prometheus > http://localhost:9091

Setup rápido de Datadog Agent:
  docker run -d --name dd-agent \\
    -e DD_API_KEY=<tu_key> \\
    -e DD_SITE=datadoghq.com \\
    -p 8125:8125/udp \\
    gcr.io/datadoghq/agent:latest
        """
    )
    parser.add_argument(
        '--source', required=True,
        help='Archivo JSON con métricas (generado con --export-metrics en orchestrator.py)',
    )
    parser.add_argument(
        '--datadog-host', default=os.environ.get('METRICS_DATADOG_HOST', 'localhost'),
        help='Host del Datadog Agent StatsD (default: localhost)',
    )
    parser.add_argument(
        '--datadog-port', type=int,
        default=int(os.environ.get('METRICS_DATADOG_PORT', '8125')),
        help='Puerto StatsD del Datadog Agent (default: 8125)',
    )
    parser.add_argument(
        '--grafana-url',
        default=os.environ.get('METRICS_GRAFANA_URL', 'http://localhost:9091'),
        help='URL del Prometheus Pushgateway para Grafana (default: http://localhost:9091)',
    )
    parser.add_argument(
        '--env', default='dev',
        help='Tag de entorno para las métricas: dev | staging | production (default: dev)',
    )
    parser.add_argument(
        '--only-json', action='store_true',
        help='Solo guardar JSON local, no enviar a Datadog ni Grafana',
    )
    parser.add_argument(
        '--summary', action='store_true',
        help='Mostrar resumen de las métricas del archivo fuente',
    )
    return parser


def print_summary(metrics: list[TokenMetric]) -> None:
    """Imprime un resumen legible de las métricas."""
    if not metrics:
        print("Sin métricas para mostrar.")
        return

    total_input  = sum(m.input_tokens  for m in metrics)
    total_output = sum(m.output_tokens for m in metrics)
    total        = total_input + total_output

    print(f"\n{'─' * 50}")
    print(f"  📊 RESUMEN DE MÉTRICAS DE TOKENS")
    print(f"{'─' * 50}")
    print(f"  Registros:       {len(metrics)}")
    print(f"  Tokens input:    {total_input:,}")
    print(f"  Tokens output:   {total_output:,}")
    print(f"  Tokens total:    {total:,}")
    print(f"  KPI (1500):      {'✅ OK' if total <= 1500 else '❌ EXCEDIDO'}")

    # Agrupar por modelo
    by_model: dict[str, int] = {}
    by_task:  dict[str, int] = {}
    for m in metrics:
        by_model[m.model] = by_model.get(m.model, 0) + m.total_tokens
        by_task[m.task_type] = by_task.get(m.task_type, 0) + m.total_tokens

    print(f"\n  Por modelo:")
    for model, tokens in sorted(by_model.items(), key=lambda x: -x[1]):
        print(f"    {model}: {tokens:,} tokens")

    print(f"\n  Por tarea:")
    for task, tokens in sorted(by_task.items(), key=lambda x: -x[1]):
        print(f"    {task}: {tokens:,} tokens")

    # Rango de tiempo
    timestamps = sorted(m.timestamp_iso for m in metrics)
    if len(timestamps) > 1:
        print(f"\n  Período: {timestamps[0][:19]} → {timestamps[-1][:19]}")
    print(f"{'─' * 50}\n")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%SZ',
        stream=sys.stderr,
    )

    parser = build_cli()
    args = parser.parse_args()

    # Leer archivo fuente
    source = Path(args.source)
    if not source.exists():
        print(f"❌ Archivo no encontrado: {args.source}", file=sys.stderr)
        sys.exit(1)

    try:
        raw_metrics = json.loads(source.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        print(f"❌ JSON inválido en {args.source}: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(raw_metrics, list):
        print(f"❌ Se esperaba una lista JSON, no {type(raw_metrics).__name__}", file=sys.stderr)
        sys.exit(1)

    metrics = [TokenMetric.from_dict(d) for d in raw_metrics]

    if args.summary:
        print_summary(metrics)
        return

    if args.only_json:
        exporter = MetricsExporter()
        exporter._save_json(raw_metrics)
        print(f"✅ JSON guardado en {exporter.output_path}")
        return

    # Configurar via args (override env vars)
    os.environ['METRICS_DATADOG_HOST'] = args.datadog_host
    os.environ['METRICS_DATADOG_PORT'] = str(args.datadog_port)
    os.environ['METRICS_GRAFANA_URL']  = args.grafana_url
    os.environ['METRICS_ENV_TAG']      = args.env

    exporter = MetricsExporter()
    results = exporter.export_all(raw_metrics)

    # Exit code: falla si TODOS los destinos fallaron
    if results and all(not r.success for r in results):
        sys.exit(1)


if __name__ == '__main__':
    main()
