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

Plus two helper services for reaching the Tinkoff API from abroad — see
[Reaching the Tinkoff API via the RU relay](#reaching-the-tinkoff-api-via-the-ru-relay).

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

## Reaching the Tinkoff API via the RU relay

Since 2026-10-17 `invest-public-api.tinkoff.ru` (same IP as `…tbank.ru`) does not answer
TLS from foreign IPs: TCP connects, the ClientHello gets no reply. The VPS is in the
Netherlands, so every gRPC call failed with
`UNAVAILABLE … Handshake read failed (Endpoint closing)`. The server kept serving stale
cache, and clients showed "Handshake failed". From a Russian IP the API answers normally.

The fix routes **only the server's gRPC traffic** out through the Moscow VPN relay. The
relay's xray/3x-ui is not touched (production VPN, see the `Vpn-tools` repo):

```
investor-mcp ──grpc_proxy──▶ tinyproxy 127.0.0.1:8890 ──socks5──▶ ssh -D 127.0.0.1:1081
   (NL VPS)                  (CONNECT, domain filter)            tinkoff-tunnel.service
                                                                         │ SSH
                                                                         ▼
                                              RU relay, user tinkoff-tunnel ──▶ Tinkoff API :443
```

The Tinkoff certificate chains to the **Russian Trusted Root CA** (Ministry of Digital
Development). That root is not in gRPC's bundled roots, so gRPC also gets a custom roots
file. The system trust store is left unchanged.

### On the relay

A dedicated user without a shell that may only forward to the Tinkoff hosts:

```bash
useradd --system --create-home --shell /usr/sbin/nologin tinkoff-tunnel
install -d -m 700 -o tinkoff-tunnel -g tinkoff-tunnel /home/tinkoff-tunnel/.ssh
# /home/tinkoff-tunnel/.ssh/authorized_keys (chmod 600, owned by tinkoff-tunnel), one line:
restrict,port-forwarding,permitopen="invest-public-api.tinkoff.ru:443",permitopen="invest-public-api.tbank.ru:443",permitopen="sandbox-invest-public-api.tinkoff.ru:443" ssh-ed25519 AAAA… investor-mcp-tunnel
```

### On the VPS

```bash
ssh-keygen -t ed25519 -N "" -C investor-mcp-tunnel -f /root/.ssh/tinkoff_tunnel
ssh-keyscan -t ed25519 <relay> > /root/.ssh/relay_known_hosts   # compare the fingerprint with a known-good one!
apt-get install -y tinyproxy
```

`/etc/systemd/system/tinkoff-tunnel.service`:

```ini
[Unit]
Description=SSH SOCKS tunnel to RU relay for Tinkoff Invest API (investor-mcp)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ConnectTimeout=15 -o UserKnownHostsFile=/root/.ssh/relay_known_hosts -i /root/.ssh/tinkoff_tunnel -D 127.0.0.1:1081 tinkoff-tunnel@<relay>
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`/etc/tinyproxy/tinyproxy.conf` (the stock file is kept as `tinyproxy.conf.orig`):

```
User tinyproxy
Group tinyproxy
Port 8890
Listen 127.0.0.1
Timeout 600
Syslog On
LogLevel Notice
MaxClients 20
Allow 127.0.0.1
ConnectPort 443
DisableViaHeader Yes
upstream socks5 127.0.0.1:1081
Filter "/etc/tinyproxy/filter"
FilterURLs Off
FilterExtended On
FilterDefaultDeny Yes
```

`/etc/tinyproxy/filter`: allowed hosts, one regex per line:

```
^invest-public-api\.tinkoff\.ru$
^invest-public-api\.tbank\.ru$
^sandbox-invest-public-api\.tinkoff\.ru$
```

`/etc/systemd/system/tinyproxy.service.d/foreground.conf`. Without it the stock forking
unit times out on its PID file:

```ini
[Service]
Type=simple
PIDFile=
ExecStart=
ExecStart=/usr/bin/tinyproxy -d -c /etc/tinyproxy/tinyproxy.conf
```

Root CA for gRPC: download it from the official source, **check the SHA-256**, and append
it to the system bundle:

```bash
mkdir -p /opt/investor-mcp/certs
curl -o /opt/investor-mcp/certs/russian_trusted_root_ca.pem \
  https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt   # fetch from a RU IP
openssl x509 -in /opt/investor-mcp/certs/russian_trusted_root_ca.pem -noout -fingerprint -sha256
# expected: D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31
cat /etc/ssl/certs/ca-certificates.crt /opt/investor-mcp/certs/russian_trusted_root_ca.pem \
  > /opt/investor-mcp/certs/grpc-roots.pem
```

`/etc/systemd/system/investor-mcp.service.d/tinkoff-proxy.conf`:

```ini
[Unit]
Wants=tinyproxy.service tinkoff-tunnel.service
After=tinyproxy.service tinkoff-tunnel.service

[Service]
Environment=grpc_proxy=http://127.0.0.1:8890
Environment=GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/opt/investor-mcp/certs/grpc-roots.pem
```

```bash
systemctl daemon-reload
systemctl enable --now tinkoff-tunnel tinyproxy
systemctl restart investor-mcp
```

`grpc_proxy` is read only by gRPC; other HTTP clients in the process ignore it. CI deploys
do not touch any of these files, so the setup survives redeploys.

### Checks

```bash
systemctl is-active tinkoff-tunnel tinyproxy investor-mcp
curl -sk -o /dev/null -w "%{http_code} tls=%{time_appconnect}s\n" \
  --proxy http://127.0.0.1:8890 https://invest-public-api.tinkoff.ru/   # 404 + non-zero tls = OK
curl -s -o /dev/null -w "%{http_code}\n" --proxy http://127.0.0.1:8890 https://ya.ru/   # 000 = filter works
journalctl -u investor-mcp --since -1h | grep -E "UNAVAILABLE|VERIFY_FAILED"            # should be empty
```

Then call `investor_sync_data(force=true)` from the chat.

Symptoms and their meaning:

| Log line | Meaning |
| --- | --- |
| `Handshake read failed (Endpoint closing)` | traffic is not going through the relay: check the drop-in and the tunnel |
| `CERTIFICATE_VERIFY_FAILED: self signed certificate in certificate chain` | the tunnel works, but gRPC is not using `grpc-roots.pem` |
| `failed to connect to all addresses` + tunnel in `activating` | SSH to the relay does not come up: `journalctl -u tinkoff-tunnel` |

The relay is a single point of failure. If it is down, the server keeps serving the
last cache, marked `stale`.

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
systemctl status investor-mcp cloudflared tinkoff-tunnel tinyproxy   # health
journalctl -u investor-mcp -n 100 --no-pager   # logs (no secrets logged)
# rotate the Tinkoff token: edit /opt/investor-mcp/.env then:
systemctl restart investor-mcp
```
