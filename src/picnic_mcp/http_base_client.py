"""HttpBaseClient — a self-contained, framework-agnostic abstract HTTP client.

Python port of https://gist.github.com/alvinsng/fb7474edafef5e3f4a0e63e3c4888946

Design goals:
  - Every outbound HTTP call goes through one pipeline so you get
    consistent logging, error mapping, metrics, and query/body
    serialization for free.
  - Subclasses focus on vendor specifics: base URL, auth headers,
    and optional error-body translation.

Usage:
  1. Subclass `HttpBaseClient`, set `base_url`, implement
     `build_auth_headers()`.
  2. Optionally override `map_http_error()` to parse vendor error bodies.
  3. Call `self.get/post/put/patch/delete()` from public methods.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────── Enums ────────────────────────────────


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


# ─────────────────────────────── Types ────────────────────────────────

# Opaque string identifying an external service (e.g. "picnic").
type ExternalDependency = str

# Acceptable query-parameter value types.
type Primitive = str | int | float | bool | None
type QueryParams = Mapping[str, Primitive | Sequence[Primitive]]

# Structured metadata attached to errors and log calls.
type LogMetadata = dict[str, Any]

# Labels pushed to the metrics layer for each request.
type MetricLabels = dict[str, str]


@dataclass(frozen=True, slots=True)
class RequestConfig:
    """Full per-request configuration consumed by `HttpBaseClient.request()`."""

    method: HttpMethod
    path: str
    endpoint: str
    query: QueryParams | None = None
    body: Any = None
    headers: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ClientMetricOptions:
    """Options forwarded to the metrics layer."""

    labels: MetricLabels = field(default_factory=dict)
    is_user_error: Callable[[BaseException], bool] | None = None


# ─────────────────────────── Error classes ────────────────────────────


class ResponseError(Exception):
    """HTTP error with a numeric `status_code`.

    Callers branch on `err.status_code` (e.g. `if err.status_code == 404`)
    instead of catching per-status subclasses.
    """

    def __init__(self, message: str, status_code: int, metadata: LogMetadata | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.metadata = metadata or {}


# ───────────────────────── Utility functions ──────────────────────────


def map_status_to_response_error(
    status: int, message: str, metadata: LogMetadata | None = None
) -> ResponseError:
    """Map an HTTP status code to a `ResponseError`."""
    return ResponseError(message, status, {"status_code": status, **(metadata or {})})


def is_transient_fetch_error(error: BaseException) -> bool:
    """True when the request failed at the transport layer (DNS, TCP reset,
    TLS, timeout) rather than returning an HTTP response."""
    return isinstance(error, httpx.TransportError)


# Swap these for structlog, Sentry, etc. Signatures stay `(message, metadata)`.
def log_info(message: str, metadata: LogMetadata | None = None) -> None:
    logger.info("%s %s", message, metadata or "")


def log_warn(message: str, metadata: LogMetadata | None = None) -> None:
    logger.warning("%s %s", message, metadata or "")


# ─────────────────────────── Metrics layer ────────────────────────────


async def call_with_metrics[T](
    fn: Callable[[], Awaitable[T]],
    dependency: ExternalDependency,
    options: ClientMetricOptions | None = None,
) -> T:
    """Placeholder metrics wrapper — does nothing except await `fn`.

    Replace the body with Prometheus / StatsD / OpenTelemetry, using
    `dependency` and `options.labels` to tag counters and histograms.
    """
    return await fn()


# ──────────────────────────── BaseClient ──────────────────────────────


class BaseClient(ABC):
    """Wraps an outbound call with metric recording and validates that the
    `endpoint` label belongs to the dependency the client was constructed
    with (e.g. "picnic/getViewer" must start with "picnic/")."""

    def __init__(self, dependency: ExternalDependency):
        self.dependency = dependency

    async def perform_request[T](
        self,
        endpoint: str,
        fn: Callable[[], Awaitable[T]],
        options: ClientMetricOptions | None = None,
    ) -> T:
        if not endpoint.startswith(f"{self.dependency}/"):
            raise ValueError(
                f'BaseClient: endpoint "{endpoint}" does not belong to '
                f'dependency "{self.dependency}"'
            )
        opts = options or ClientMetricOptions()
        labels = {**opts.labels, "endpoint": endpoint}
        return await call_with_metrics(
            fn, self.dependency, ClientMetricOptions(labels, opts.is_user_error)
        )


# ────────────────────────── HttpBaseClient ────────────────────────────


class HttpBaseClient(BaseClient):
    """Abstract HTTP client. The only place in the codebase that should call
    httpx directly — every other outbound HTTP call goes through a subclass.

    Subclasses:
      - set `base_url` and (optionally) override `default_headers`
      - implement `build_auth_headers` to attach vendor credentials
      - may override `map_http_error` to translate vendor error envelopes
        into `ResponseError`
    """

    base_url: str

    def __init__(
        self,
        dependency: ExternalDependency,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ):
        super().__init__(dependency)
        self._http = http or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    def default_headers(self) -> dict[str, str]:
        """Base headers applied to every request before auth + per-call headers."""
        return {}

    @abstractmethod
    async def build_auth_headers(self) -> dict[str, str]:
        """Vendor-specific authentication headers. Called on every request.
        Implementations are responsible for their own memoization."""

    def map_http_error(self, response: httpx.Response, body: Any) -> Exception:
        """Translate a non-2xx response into an exception. Subclasses typically
        override to extract vendor error details before falling back here."""
        status_text = response.reason_phrase or "HTTP error"
        message = f"Upstream {self.dependency} responded {response.status_code} {status_text}"
        return map_status_to_response_error(response.status_code, message)

    def on_response(self, response: httpx.Response) -> None:
        """Hook for every received response (e.g. to capture rotated tokens)."""

    async def request(self, cfg: RequestConfig) -> Any:
        """Core request pipeline. Subclasses call this (or the verb helpers)."""
        url = self._build_url(cfg.path, cfg.query)
        method = cfg.method

        headers = {
            **self.default_headers(),
            **(await self.build_auth_headers()),
            **(cfg.headers or {}),
        }
        content: bytes | None = None
        if cfg.body is not None and method is not HttpMethod.GET:
            content = json.dumps(cfg.body).encode()
            if not any(k.lower() == "content-type" for k in headers):
                headers["Content-Type"] = "application/json"

        base_log_metadata: LogMetadata = {
            "service_name": self.dependency,
            "method": method.value,
            "url": url,
            "endpoint": cfg.endpoint,
        }
        log_info("HttpBaseClient - Upstream request starting", base_log_metadata)
        start = time.perf_counter()

        # Captured in the outer scope so the single `except` can enrich its
        # log with whatever wire-level state we observed before the failure.
        response: httpx.Response | None = None
        raw_text: str | None = None
        parsed: Any = None

        try:
            response = await self.perform_request(
                cfg.endpoint,
                lambda: self._http.request(method.value, url, headers=headers, content=content),
                ClientMetricOptions(labels={"method": method.value}),
            )
            self.on_response(response)
            raw_text = self._read_response_body(response)
            parsed = self._parse_response_body(response, raw_text)

            if not response.is_success:
                raise self.map_http_error(response, parsed)

            log_info(
                "HttpBaseClient - Upstream request succeeded",
                {
                    **base_log_metadata,
                    "status_code": response.status_code,
                    "duration_ms": (time.perf_counter() - start) * 1000,
                },
            )
            return parsed
        except Exception as raw_cause:
            error = self._translate_transport_error(raw_cause, method, url)
            log_warn(
                "HttpBaseClient - Upstream request failed",
                {
                    **base_log_metadata,
                    "duration_ms": (time.perf_counter() - start) * 1000,
                    **({"status_code": response.status_code} if response is not None else {}),
                    **({"body": parsed} if parsed is not None else {}),
                    **({"body_preview": raw_text[:1000]} if raw_text is not None else {}),
                    "cause": repr(error),
                },
            )
            if error is raw_cause:
                raise
            raise error from raw_cause

    def _translate_transport_error(
        self, cause: Exception, method: HttpMethod, url: str
    ) -> Exception:
        """Surface transport failures as 503 so callers treat the dependency
        as unavailable. All other errors pass through unchanged."""
        if is_transient_fetch_error(cause):
            return ResponseError(
                f"Upstream {self.dependency} is unavailable. Please try again later.",
                503,
                {"cause": repr(cause), "method": method.value, "url": url,
                 "service_name": self.dependency},
            )
        return cause

    # ── Verb helpers ─────────────────────────────────────────────────────

    async def get(self, path: str, query: QueryParams | None = None, *, endpoint: str,
                  headers: Mapping[str, str] | None = None) -> Any:
        return await self.request(RequestConfig(HttpMethod.GET, path, endpoint, query=query,
                                                headers=headers))

    async def post(self, path: str, body: Any = None, *, endpoint: str,
                   query: QueryParams | None = None,
                   headers: Mapping[str, str] | None = None) -> Any:
        return await self.request(RequestConfig(HttpMethod.POST, path, endpoint, query=query,
                                                body=body, headers=headers))

    async def put(self, path: str, body: Any = None, *, endpoint: str,
                  query: QueryParams | None = None,
                  headers: Mapping[str, str] | None = None) -> Any:
        return await self.request(RequestConfig(HttpMethod.PUT, path, endpoint, query=query,
                                                body=body, headers=headers))

    async def patch(self, path: str, body: Any = None, *, endpoint: str,
                    query: QueryParams | None = None,
                    headers: Mapping[str, str] | None = None) -> Any:
        return await self.request(RequestConfig(HttpMethod.PATCH, path, endpoint, query=query,
                                                body=body, headers=headers))

    async def delete(self, path: str, query: QueryParams | None = None, *, endpoint: str,
                     headers: Mapping[str, str] | None = None) -> Any:
        return await self.request(RequestConfig(HttpMethod.DELETE, path, endpoint, query=query,
                                                headers=headers))

    # ── Internals ────────────────────────────────────────────────────────

    def _build_url(self, path: str, query: QueryParams | None = None) -> str:
        base = self.base_url.rstrip("/") + (path if path.startswith("/") else f"/{path}")
        if not query:
            return base
        params: list[tuple[str, str]] = []
        for key, value in query.items():
            values = value if isinstance(value, (list, tuple)) else [value]
            params += [(key, _stringify(v)) for v in values if v is not None]
        if not params:
            return base
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}{httpx.QueryParams(params)}"

    @staticmethod
    def _read_response_body(response: httpx.Response) -> str | None:
        """Raw text, or None for 204 / empty bodies."""
        if response.status_code == 204:
            return None
        return response.text or None

    def _parse_response_body(self, response: httpx.Response, raw_text: str | None) -> Any:
        """Parse by content-type. JSON that fails to decode surfaces as a 503."""
        if raw_text is None:
            return None
        if "application/json" in response.headers.get("content-type", ""):
            try:
                return json.loads(raw_text)
            except json.JSONDecodeError as cause:
                raise ResponseError(
                    f"Upstream {self.dependency} returned a non-JSON response body",
                    503,
                    {"cause": repr(cause), "status_code": response.status_code,
                     "service_name": self.dependency, "body_preview": raw_text[:500]},
                ) from cause
        return raw_text


def _stringify(value: Primitive) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
