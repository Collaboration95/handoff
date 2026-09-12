from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values


_REDACTED = "[REDACTED]"
_SECRET_KEY_PARTS = ("api_key", "apikey", "authorization", "cookie", "password", "secret", "token")
_scope_depth: ContextVar[int] = ContextVar("finance_trace_scope_depth", default=0)


def _root_env() -> dict[str, str]:
    path = Path(__file__).resolve().parents[2] / ".env.local"
    if not path.exists():
        return {}
    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def sanitize_trace_data(value: Any) -> Any:
    """Return trace-safe data without secret-shaped values or signed URL queries."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            str(key): (
                _REDACTED
                if any(part in str(key).lower() for part in _SECRET_KEY_PARTS)
                else sanitize_trace_data(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_trace_data(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        parsed = urlsplit(value)
        if parsed.query:
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, _REDACTED, parsed.fragment))
    return value


@lru_cache(maxsize=1)
def _configured_client_cached() -> tuple[Any | None, str | None]:
    local = _root_env()
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY") or local.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY") or local.get("LANGFUSE_SECRET_KEY")
    base_url = (
        os.environ.get("LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_HOST")
        or local.get("LANGFUSE_BASE_URL")
        or local.get("LANGFUSE_HOST")
    )
    if not public_key or not secret_key:
        return None, None
    try:
        from langfuse import Langfuse

        return (
            Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                base_url=base_url,
                environment="development",
            ),
            None,
        )
    except Exception as exc:
        return None, f"telemetry initialization failed: {type(exc).__name__}"


def _configured_client() -> tuple[Any | None, str | None]:
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("LANGFUSE_TRACE_DURING_TESTS") != "1":
        return None, None
    return _configured_client_cached()


class TraceScope:
    def __init__(self, *, client: Any | None, span: Any | None, status: str, error: str | None):
        self._client = client
        self._span = span
        self.status = status
        self.error = error
        self.trace_id: str | None = None
        self.trace_url: str | None = None
        if client is not None and span is not None:
            try:
                self.trace_id = client.get_current_trace_id()
                self.trace_url = client.get_trace_url(trace_id=self.trace_id) if self.trace_id else None
            except Exception as exc:
                self.status = "error"
                self.error = f"telemetry context failed: {type(exc).__name__}"

    def set_output(self, output: Any) -> None:
        if self._span is None:
            return
        try:
            self._span.update(output=sanitize_trace_data(output))
        except Exception as exc:
            self.status = "error"
            self.error = f"telemetry update failed: {type(exc).__name__}"

    def set_usage(self, usage: dict[str, Any]) -> None:
        if self._span is None:
            return
        clean = {
            key: int(value)
            for key, value in usage.items()
            if key in {"input_tokens", "output_tokens", "total_tokens", "cached_tokens"}
            and isinstance(value, (int, float))
        }
        try:
            self._span.update(usage_details=clean)
        except Exception as exc:
            self.status = "error"
            self.error = f"telemetry usage failed: {type(exc).__name__}"

    def score(self, name: str, value: bool | float | int, *, comment: str | None = None) -> None:
        if self._span is None:
            return
        try:
            self._span.score_trace(
                name=name,
                value=float(value),
                data_type="BOOLEAN" if isinstance(value, bool) else "NUMERIC",
                comment=comment,
            )
        except Exception as exc:
            self.status = "error"
            self.error = f"telemetry score failed: {type(exc).__name__}"


@contextmanager
def trace_scope(
    name: str,
    input: Any,
    *,
    as_type: str = "span",
    model: str | None = None,
    metadata: dict[str, Any] | None = None,
    version: str | None = None,
    session_id: str | None = None,
) -> Iterator[TraceScope]:
    """Create a safe Langfuse observation, or an explicit local-only scope."""
    client, setup_error = _configured_client()
    if client is None:
        yield TraceScope(
            client=None,
            span=None,
            status="error" if setup_error else "local",
            error=setup_error,
        )
        return

    context = None
    attributes_context = None
    depth_token = None
    try:
        is_root = _scope_depth.get() == 0
        context = client.start_as_current_observation(
            name=name,
            as_type=as_type,
            input=sanitize_trace_data(input),
            metadata=sanitize_trace_data(metadata) if metadata else None,
            version=version,
            model=model,
        )
        span = context.__enter__()
        depth_token = _scope_depth.set(_scope_depth.get() + 1)
    except Exception as exc:
        yield TraceScope(
            client=None,
            span=None,
            status="error",
            error=f"telemetry start failed: {type(exc).__name__}",
        )
        return

    scope = TraceScope(client=client, span=span, status="hosted", error=None)
    try:
        if is_root and (metadata or session_id):
            from langfuse import propagate_attributes

            attributes_context = propagate_attributes(
                session_id=session_id,
                metadata=sanitize_trace_data(metadata) if metadata else None,
                trace_name=name,
            )
            attributes_context.__enter__()
    except Exception as exc:
        scope.status = "error"
        scope.error = f"telemetry attributes failed: {type(exc).__name__}"

    try:
        yield scope
    except BaseException as exc:
        try:
            span.update(level="ERROR", status_message=type(exc).__name__)
        except Exception:
            pass
        raise
    finally:
        if attributes_context is not None:
            try:
                attributes_context.__exit__(None, None, None)
            except Exception as exc:
                scope.status = "error"
                scope.error = f"telemetry attributes failed: {type(exc).__name__}"
        try:
            context.__exit__(None, None, None)
        except Exception as exc:
            scope.status = "error"
            scope.error = f"telemetry finish failed: {type(exc).__name__}"
        if depth_token is not None:
            _scope_depth.reset(depth_token)


def flush_traces() -> str | None:
    client, setup_error = _configured_client()
    if client is None:
        return setup_error
    try:
        client.flush()
    except Exception as exc:
        return f"telemetry flush failed: {type(exc).__name__}"
    return None
