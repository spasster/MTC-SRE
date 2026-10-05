import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup(service: str) -> None:
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service,
                "service.namespace": os.getenv("OTEL_SERVICE_NAMESPACE", "demo"),
            }
        )
    )
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip().rstrip("/")
    if endpoint:
        if not endpoint.endswith("/v1/traces"):
            endpoint = endpoint + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)


def current_trace_id() -> str:
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return ""
    return format(span_context.trace_id, "032x")
