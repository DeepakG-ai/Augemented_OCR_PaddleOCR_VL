import mlflow
import mlflow.tracing

with mlflow.start_span("root") as s:
    headers = mlflow.tracing.get_tracing_context_headers_for_http_request()

print("Headers:", headers)

# Simulate another process:
with mlflow.tracing.set_tracing_context_from_http_request_headers(headers):
    with mlflow.start_span("child_process_span") as s2:
        print("Child Span ID:", s2.span_id)
        print("Child Trace ID:", s2.trace_id)
