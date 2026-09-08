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

Workflow: [../.github/workflows/ci-deploy.yml](../.github/workflows/ci-deploy.yml).

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

## Данные не обновляются (`data_status: stale`)

**Симптомы.** Тулы отвечают `data_status: "stale"` с предупреждением «брокер недоступен
(…)», `investor_get_sync_status` показывает `stale_sections`, последняя сделка «застыла»
на старой дате, хотя вы торговали позже. Значит, каждый поход в Tinkoff Invest API падает
и сервер отдаёт последний удачный кэш (дата — в `as_of` и в `sections.*.fetched_at`).

**1. Причина** — в тексте предупреждения (`warnings`) и в логах сервера:

```bash
journalctl -u investor-mcp -n 300 --no-pager | grep -iE "broker|unavailable|unauthenticated|deadline|ssl|handshake"
```

**2. Проверка API прямо с VPS** (тем же токеном и SDK, что использует сервер):

```bash
cd /opt/investor-mcp && set -a && . ./.env && set +a && .venv/bin/python - <<'EOF'
import os
from tinkoff.invest import Client
with Client(os.environ["TINKOFF_INVEST_TOKEN"]) as client:
    print([account.name for account in client.users.get_accounts().accounts])
EOF
```

**3. Типичные ошибки и что делать.**

| В ошибке | Причина | Что делать |
| --- | --- | --- |
| `Ssl handshake failed … CERTIFICATE_VERIFY_FAILED` | С 3 августа 2026 сертификаты API T-Bank (`invest-public-api.tinkoff.ru` / `.tbank.ru`) выпущены под корнем **Минцифры** (пост T_InvestAPI «Отзыв старого сертификата и переход домена tinkoff.ru на сертификат Минцифры»). Его нет в стандартных доверенных корнях, gRPC рвёт TLS. | Установить корневой и промежуточный сертификаты Минцифры (см. ниже) **или** указать `TINKOFF_INVEST_CA_BUNDLE`. |
| `UNAUTHENTICATED` / `40003` | Токен отозван или истёк. | Выпустить новый токен, обновить `TINKOFF_INVEST_TOKEN` в `.env`, `systemctl restart investor-mcp`. |
| `DEADLINE_EXCEEDED` / `failed to connect to all addresses` без слов про SSL | Сеть: с VPS нет доступа к API (файрвол, блокировка зарубежных IP). | `curl -v https://invest-public-api.tbank.ru:443/` с VPS; при необходимости хостинг с российским IP. |
| `RESOURCE_EXHAUSTED` / `30042` | Лимит запросов в минуту. | Подождать; повторный `investor_sync_data`. |

**Сертификаты Минцифры (Ubuntu/Debian).** Официальная страница — <https://www.gosuslugi.ru/crt>
(сверьте отпечатки с опубликованными там):

```bash
curl -fsSL https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt \
  -o /usr/local/share/ca-certificates/russian_trusted_root_ca.crt
curl -fsSL https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt \
  -o /usr/local/share/ca-certificates/russian_trusted_sub_ca.crt
update-ca-certificates
systemctl restart investor-mcp
```

gRPC на Linux читает системный набор `/etc/ssl/certs/ca-certificates.crt`, поэтому после
`update-ca-certificates` ничего настраивать не нужно. Альтернатива без правки системного
хранилища: положить PEM-бандл (оба сертификата подряд) в файл и указать его в
`TINKOFF_INVEST_CA_BUNDLE=/opt/investor-mcp/russian_trusted_ca.pem`.

**4. Проверка.** `investor_sync_data` → `status: "success"`, `stale_sections: []`;
`investor_get_operations` за последние дни показывает свежие сделки.
