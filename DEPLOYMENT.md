# Deployment

The Investor MCP server runs on a VPS, listens only on `127.0.0.1`, and is published
to the internet over HTTPS by a **Cloudflare Tunnel**. ChatGPT/Claude connect to it as a
remote MCP connector. Code is shipped by **GitHub Actions** on push to `main`.

## Server layout (VPS)

```
/opt/investor-mcp/
├── src/ pyproject.toml README.md   # app code (rsynced by CI)
├── .venv/                          # Python 3.10 virtualenv
├── .env                            # secrets & config (NOT in git, chmod 600)
└── data/investor_mcp.db            # SQLite (profile, snapshots, cache, …)
```

Two systemd services:

- **`investor-mcp.service`** — runs `investor-mcp --transport streamable-http --host 127.0.0.1 --port 8000`, `EnvironmentFile=/opt/investor-mcp/.env`, `Restart=always`.
- **`cloudflared`** (token-managed named tunnel) — publishes `https://mcp.<domain>` → `http://localhost:8000`. A `cloudflared-quick` unit (ephemeral `trycloudflare.com` URL) can be used before the domain is live.

The public connector URL is `https://<host>/<INVESTOR_MCP_PATH>` — the secret path is the
access control for the no-auth setup, so treat the full URL as a secret.

## First-time provisioning

```bash
# on the VPS
apt-get install -y python3.10-venv
mkdir -p /opt/investor-mcp/data && cd /opt/investor-mcp
python3 -m venv .venv && .venv/bin/pip install -U pip
# (code is delivered by CI or initial rsync)
.venv/bin/pip install -e .
.venv/bin/pip install -e ".[tinkoff]"
.venv/bin/pip install --no-deps "git+https://github.com/RussianInvestments/invest-python.git"
```

`.env` (chmod 600):

```ini
INVESTOR_MCP_STORAGE_PATH=/opt/investor-mcp/data/investor_mcp.db
INVESTOR_MCP_PATH=/<secret>/mcp
INVESTOR_MCP_CACHE_TTL_SECONDS=86400
TINKOFF_INVEST_TOKEN=<your fresh token>
# INVESTOR_MCP_AUTH_TOKEN=<token>   # optional bearer auth
```

Cloudflare Tunnel (named): create a tunnel in the Cloudflare Zero Trust dashboard, run
`cloudflared service install <TOKEN>`, then add a Public Hostname `mcp.<domain>` →
`http://localhost:8000`.

## CI/CD (GitHub Actions)

Workflow: [.github/workflows/ci-deploy.yml](.github/workflows/ci-deploy.yml).

- **On every push & PR:** run the test suite (`python -m unittest`).
- **On push to `main` (after tests pass):** rsync the code to the VPS, `pip install -e .`,
  and `systemctl restart investor-mcp`.

### Required repository secrets

`Settings → Secrets and variables → Actions → New repository secret`:

| Secret | Value |
| --- | --- |
| `VPS_HOST` | VPS IP / host (e.g. `89.124.83.32`) |
| `VPS_USER` | SSH user (e.g. `root`) |
| `VPS_SSH_KEY` | **Private** key of a dedicated deploy keypair (full PEM, incl. BEGIN/END lines) |

The matching **public** key must be in the VPS user's `~/.ssh/authorized_keys`. The deploy
key is dedicated and can be rotated independently of your personal key.

> The deploy step only touches `/opt/investor-mcp` and restarts `investor-mcp.service`;
> it does not modify the Cloudflare tunnel, firewall, or other services.

## Operations

```bash
systemctl status investor-mcp cloudflared      # health
journalctl -u investor-mcp -n 100 --no-pager   # logs (no secrets logged)
# rotate the Tinkoff token: edit /opt/investor-mcp/.env then:
systemctl restart investor-mcp
```
