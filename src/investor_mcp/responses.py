"""Response-envelope helpers (ok/error) and shared label maps for tool outputs."""

from __future__ import annotations

from typing import Any

from .models import utc_now_iso


def ok_response(
    summary: str,
    data: dict[str, Any],
    data_status: str = "fresh",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "data_status": data_status,
        "as_of": utc_now_iso(),
        "summary": summary,
        "data": data,
        "warnings": warnings or [],
        "sources": [],
        "resource_links": [],
    }


def error_response(
    error_code: str,
    summary: str,
    data: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Machine-readable tool error. Mapped to isError=true at the MCP layer."""
    return {
        "ok": False,
        "error_code": error_code,
        "data_status": "unavailable",
        "as_of": utc_now_iso(),
        "summary": summary,
        "data": data or {},
        "warnings": warnings or [],
        "sources": [],
        "resource_links": [],
    }


def _asset_class_label(asset_class: str) -> str:
    return {
        "stock": "Акции",
        "bond": "Облигации",
        "fund": "Фонды",
        "currency": "Валюта",
        "cash": "Кэш",
    }.get(asset_class, asset_class)


def _sector_label(sector: str) -> str:
    return {
        "financial": "Финансы",
        "energy": "Нефтегаз/энергетика",
        "materials": "Металлы и материалы",
        "it": "IT",
        "consumer": "Потребительский сектор",
        "telecom": "Телеком",
        "utilities": "Электроэнергетика/ЖКХ",
        "real_estate": "Недвижимость",
        "government": "Госбумаги",
        "health_care": "Здравоохранение",
    }.get(sector, sector)


# Issuer names that are cash/currency placeholders, not real news subjects.
_NON_ISSUER = {"", "Cash", "Денежный рынок", "Российский рубль", "Доллар США", "Евро", "Dollar", "Euro"}
