# InvestAnalytic — Investor MCP server

Personal investment-assistant **MCP server** (Python / [FastMCP](https://github.com/modelcontextprotocol/python-sdk)).
Read-only, focused on the Russian market. It exposes a portfolio analytics layer
over the **Tinkoff Invest API** and connects to **ChatGPT / Claude** as a remote
MCP connector, so you can ask about your portfolio in natural language.

Requirements & contract docs:
[business_requirements.md](business_requirements.md) ·
[technical_requirements.md](technical_requirements.md) ·
[mcp_server_api_design.md](mcp_server_api_design.md) ·
[mcp_tool_schemas.md](mcp_tool_schemas.md).
Deployment & CI: [DEPLOYMENT.md](DEPLOYMENT.md).

## How it works

```
ChatGPT / Claude  ──(remote MCP, HTTPS)──>  Cloudflare Tunnel  ──>  MCP server (localhost)
                                                                      │
                                  server.py (FastMCP: tools/resources/prompts, auth)
                                  service.py (analytics + cache + persistence)
                                  ├── adapters.py  → Tinkoff Invest API (read-only) / Mock
                                  └── storage.py   → SQLite (profile, snapshots, cache, …)
```

- **`server.py`** — FastMCP surface: **17 tools, 11 resources, 5 prompts**. Transports:
  `stdio` (local clients) and `streamable-http` (remote). Optional bearer auth and
  reverse-proxy host validation.
- **`service.py`** — transport-agnostic analytics. Every tool returns the contract
  envelope (`ok / data_status / as_of / summary / data / warnings / sources / resource_links`)
  and machine-readable `error_code`s. Also owns caching and persistence orchestration.
- **`adapters.py`** — `BrokerAdapter` protocol with `MockBrokerAdapter` (offline dev) and
  the real read-only `TinkoffInvestAdapter`. `build_broker_adapter()` picks one from env.
- **`storage.py`** — SQLite persistence.
- **`models.py`** — domain models (Money, Account, Instrument, Position, Operation, InvestorProfile).

### Tinkoff adapter (valuation)

- Accounts / portfolio / operations via `invest-python`; instrument metadata via the
  **typed** lookups `share_by` / `bond_by` / `etf_by` (they carry `sector`, and bonds
  carry `risk_level`).
- **Valuation is converted to the base currency (RUB):** foreign quotes (USD/HKD/…) are
  converted with FX rates (`currencies()` + `get_last_prices`), and bond **accrued coupon
  (НКД, `current_nkd`)** is added to the price. The instrument keeps its **original**
  currency so currency-exposure analysis still works. This matches the broker's reported
  total to the kopeck.
- **Read-only:** no order-placing methods are ever called.

### Caching of portfolio-composition data

Positions and accounts are cached for `cache_ttl_seconds` (**default 1 day**), in memory
**and** persisted to SQLite. All composition tools (`investor_get_portfolio`,
`investor_analyze_portfolio`, `investor_scan_risks`, `investor_get_instrument`, …) read
from the cache, so the broker is hit **once per day** instead of ~100+ Tinkoff calls per
request.

- `data_status` reflects the source: `fresh` (just fetched), `cached` (within TTL),
  `stale` (broker unavailable → last cache served for offline resilience).
- **Force refresh:** `investor_sync_data` or `investor_get_portfolio(refresh=true)`.
- A sync also writes a portfolio **snapshot** (`investor://portfolio/snapshots/{id}`).

### Risk analysis

`investor_scan_risks` flags single-**position**, single-**issuer** and single-**sector**
concentration against the profile limits (`max_single_position_percent`,
`max_single_issuer_percent`, `max_single_sector_percent`, `max_high_risk_percent`).
> Known limitation: issuer concentration groups by instrument name, so multiple bond
> issues of one issuer (e.g. several ГК Самолёт series) are not yet merged.

### Security

- Read-only relative to the broker (no trading tools).
- Optional **bearer auth** (`INVESTOR_MCP_AUTH_TOKEN`) on the HTTP transport.
- A **secret URL path** (`INVESTOR_MCP_PATH`) for no-auth deployments behind a tunnel.
- DNS-rebinding `Host` check is disabled by default behind a trusted proxy; set
  `INVESTOR_MCP_ALLOWED_HOSTS` for strict mode.
- Tokens/secrets live only in `.env` (git-ignored), never in logs or tool results.

## Configuration (environment)

See [.env.example](.env.example). All optional except the Tinkoff token for real data.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TINKOFF_INVEST_TOKEN` | _(empty → mock)_ | Tinkoff Invest API token. Empty = read-only mock data. |
| `TINKOFF_INVEST_SANDBOX` | `false` | Use the Tinkoff sandbox instead of production. |
| `INVESTOR_MCP_STORAGE_PATH` | `./data/investor_mcp.db` | SQLite database path. |
| `INVESTOR_MCP_CACHE_TTL_SECONDS` | `86400` | Composition cache TTL (1 day). |
| `INVESTOR_MCP_AUTH_TOKEN` | _(empty → no auth)_ | If set, require `Authorization: Bearer <token>`. |
| `INVESTOR_MCP_PATH` | `/mcp` | HTTP path of the MCP endpoint (use a secret path for no-auth). |
| `INVESTOR_MCP_ALLOWED_HOSTS` | _(empty → host check off)_ | Comma-separated allowed Host values (strict mode). |
| `INVESTOR_MCP_HOST` / `INVESTOR_MCP_PORT` | `127.0.0.1` / `8000` | Bind address for `streamable-http`. |

## Run locally

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e .

# stdio (local MCP clients)
investor-mcp

# remote HTTP transport
investor-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

With no `TINKOFF_INVEST_TOKEN` the server runs on read-only mock data.

### Real Tinkoff data

The SDK (`tinkoff-investments`, import `tinkoff.invest`) is no longer on PyPI and its
metadata pins an unpublished `tinkoff` package, so install the runtime deps via the
`tinkoff` extra and the SDK itself with `--no-deps`:

```bash
python -m pip install -e ".[tinkoff]"
python -m pip install --no-deps "git+https://github.com/RussianInvestments/invest-python.git"
```

Then set `TINKOFF_INVEST_TOKEN` in `.env` and restart. Verified on Python 3.10 (SDK
0.2.0b117, protobuf 4.25.x). protobuf **must** be `<5`.

## Tests

```bash
python -m unittest discover -s tests
```

Tests use the mock broker and faked SDK objects, so the Tinkoff SDK is **not** required
to run them.

## Deployment

The server runs on a VPS behind a Cloudflare Tunnel, managed by systemd, with code
deployed automatically by GitHub Actions on push to `main`. See [DEPLOYMENT.md](DEPLOYMENT.md).
