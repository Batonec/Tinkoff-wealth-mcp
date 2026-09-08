"""Broker-error helpers: short descriptions, MCP error codes, and actionable hints.

Shared by the service (cache fallback, sync status) and the adapters (logging).
Nothing here imports gRPC or the Tinkoff SDK: errors are inspected duck-typed via the
``code`` / ``details`` attributes that both ``tinkoff.invest.RequestError`` and a raw
``grpc.RpcError`` expose, so the helpers work without the SDK installed (unit tests, CI).
"""

from __future__ import annotations

from typing import Any

_AUTH_CODES = {"UNAUTHENTICATED", "PERMISSION_DENIED"}


class BrokerUnavailableError(RuntimeError):
    """Raised instead of calling the broker while a recent failure is cooling down."""


def _call_if_callable(value: Any) -> Any:
    if callable(value):
        try:
            return value()
        except Exception:  # noqa: BLE001 — best effort only
            return None
    return value


def status_name(exc: BaseException) -> str | None:
    """gRPC status name (``UNAVAILABLE``, ``UNAUTHENTICATED``, ...) if the error has one."""
    code = _call_if_callable(getattr(exc, "code", None))
    if code is None:
        return None
    return getattr(code, "name", None) or str(code)


def describe_error(exc: BaseException) -> str:
    """One-line, token-free description of a broker failure for logs and warnings.

    ``tinkoff.invest.RequestError`` does not fill ``Exception.args`` (``str(exc)`` is
    empty), so the gRPC status and details are read explicitly.
    """
    name = status_name(exc)
    details = _call_if_callable(getattr(exc, "details", None))
    text = str(details or "").strip() or str(exc).strip() or type(exc).__name__
    text = " ".join(text.split())
    if name and not text.startswith(name):
        text = f"{name}: {text}"
    return text[:400]


def classify_broker_error(exc: BaseException) -> str:
    """Map a broker failure to the tool contract's ``BROKER_*`` error codes."""
    name = status_name(exc) or ""
    if name in _AUTH_CODES:
        return "BROKER_AUTH_FAILED"
    if name == "RESOURCE_EXHAUSTED":
        return "BROKER_RATE_LIMIT"
    return "BROKER_API_UNAVAILABLE"


def error_hint(error: str) -> str | None:
    """What to check for a given broker error text (Russian, aimed at the operator)."""
    low = error.lower()
    if any(marker in low for marker in ("certificate", "handshake", "ssl", "tls")):
        return (
            "Похоже на недоверенный TLS-сертификат API: с августа 2026 T-Bank использует "
            "сертификат Минцифры. Установите корневой сертификат Минцифры на сервере или "
            "укажите TINKOFF_INVEST_CA_BUNDLE (см. docs/DEPLOYMENT.md)."
        )
    if any(marker in low for marker in ("unauthenticated", "permission_denied", "40003")):
        return (
            "Токен Tinkoff Invest недействителен или отозван: выпустите новый и обновите "
            "TINKOFF_INVEST_TOKEN."
        )
    if any(marker in low for marker in ("resource_exhausted", "ratelimit", "rate limit", "30042")):
        return "Превышен лимит запросов к API брокера: повторите чуть позже."
    if any(marker in low for marker in ("deadline", "unavailable", "connect", "dns", "timeout", "timed out")):
        return (
            "Сервер не дождался ответа API брокера (сеть/таймаут): проверьте исходящий доступ с VPS "
            "к invest-public-api на порт 443 и логи `journalctl -u investor-mcp`."
        )
    return None
