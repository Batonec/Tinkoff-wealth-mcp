# Техническая документация

Личный **MCP-сервер** (Python / FastMCP) над **Tinkoff Invest API**. Работает в режиме
чтения, подключается к Claude/ChatGPT как remote-коннектор. Деплой и CI — в
[DEPLOYMENT.md](DEPLOYMENT.md). Требования и контракт тулов —
[business_requirements.md](business_requirements.md),
[technical_requirements.md](technical_requirements.md),
[mcp_tool_schemas.md](mcp_tool_schemas.md).

## Топология

```
┌──────────────┐   remote MCP    ┌───────────────────┐  localhost   ┌────────────────────────┐
│  Claude /    │ ──── HTTPS ───▶ │ Cloudflare Tunnel │ ──────────▶  │   MCP-сервер :8000     │
│  ChatGPT     │                 └───────────────────┘              │   FastMCP, read-only   │
└──────────────┘                                                    └───────────┬────────────┘
                                                                                │
                              ┌─────────────────────────────────────────────────┼──────────────────┐
                              ▼                                                  ▼                  ▼
                     Tinkoff Invest API                                    SQLite-кэш          профиль / цели
                     счета · портфель · операции                          снимки · кэш         FIRE-цели,
                     купоны · дивиденды                                   состава/операций     лимиты, аллокация
```

## Слои (`src/investor_mcp/`)

```
server.py        тулы / ресурсы / промпты · транспорт (stdio | streamable-http) · авторизация
   │
service/         transport-agnostic «голова» — InvestorService собирается из миксинов:
   ├── core.py        dataclass + профиль/синк/счета/отчёты + сборка миксинов
   ├── cache.py       кэш позиций/счетов/операций (TTL, fresh/cached/stale)
   ├── portfolio.py   портфель, анализ, инструмент, симуляция, аллокация
   ├── risk.py        риск-сигналы (позиция/эмитент/сектор)
   ├── bonds.py       бонд-календарь (купоны/погашения/лестница)
   ├── research.py    новостной бриф + контекст-линзы
   ├── goals.py       прогресс к целям (капитал/доход/проекция)
   └── recommend.py   рекомендации по пополнению
   │
   ├── adapters/   брокер (только чтение): mock.py (Mock) | tinkoff.py (Tinkoff) + маппинг
   ├── storage.py  SQLite (профиль, счета, снимки, рекомендации, отчёты, кэш)
   ├── responses.py конверт ответа (ok_response/error_response) + ярлыки
   └── models.py   доменные модели (Money, Account, Instrument, Position, Operation, Profile)
```

Контракт MCP: **19 тулов, 11 ресурсов, 5 промптов**. Каждый тул возвращает конверт
`ok / data_status / as_of / summary / data / warnings / …` + машинный `error_code`.

## Поток запроса и кэш

```
Запрос тула про состав портфеля / операции
        │
        ▼
   кэш свежий (< TTL, по умолч. 24ч)? ──да──▶ отдать из памяти/SQLite     [data_status: cached]
        │
        нет
        ▼
   сходить в Tinkoff ──успех──▶ обновить кэш и отдать                     [data_status: fresh]
        │
        ошибка/недоступен
        ▼
   отдать последний кэш (даже просроченный)                              [data_status: stale]
```

- Брокер опрашивается **раз в сутки**, а не на каждый запрос (десятки→сотни вызовов экономятся).
- Принудительно: `investor_sync_data` или `investor_get_portfolio(refresh=true)`.
- `sync_data` тянет из API **счета + портфель + операции** и пишет снимок портфеля.
- `stale` — всегда **явный** сигнал, а не тихий откат: в `warnings` пишется ошибка брокера,
  дата кэша и подсказка, что проверить; `as_of` = время выгрузки данных (а не «сейчас»).
  Разделы (`accounts` / `positions` / `operations`) отслеживаются независимо.
- `investor_sync_data` при недоступном брокере честно возвращает `status: partial` +
  `stale_sections` (без кэша — ошибку `BROKER_*`), а `investor_get_sync_status` считает
  свежесть по возрасту кэша и последней ошибке брокера, а не по отчёту синка.
- После ошибки брокер не опрашивается 60 с (`broker_retry_cooldown_seconds`), чтобы цепочка
  тулов во время сбоя не стоила по таймауту каждая; принудительные обновления — исключение.
- На каждый gRPC-вызов ставится дедлайн (`TINKOFF_INVEST_TIMEOUT_SECONDS`, 20 с): зависший
  API не блокирует тул дольше бюджета MCP-клиента. Ошибки брокера пишутся в лог
  (`journalctl -u investor-mcp`). Диагностика — в [DEPLOYMENT.md](DEPLOYMENT.md).

## Оценка стоимости (сходится с брокером до копейки)

- Метаданные инструмента — типизированными запросами `share_by`/`bond_by`/`etf_by`
  (сектор, у облигаций — `risk_level`). Эмитент — по **бренду** (через Asset API), поэтому
  все выпуски одного эмитента схлопываются в одного.
- Все суммы приводятся к базовой валюте (**RUB**): валютные котировки конвертируются по
  курсу (`currencies()` + `get_last_prices`), по облигациям к цене добавляется **НКД**
  (`current_nkd`). Исходная валюта инструмента сохраняется для анализа валютной экспозиции.
- Доход: купоны — из `get_bond_coupons`, дивиденды — из `get_dividends` (за 12 мес).

## Безопасность

- **Только чтение** — торговые методы брокера не вызываются никогда.
- Опциональный **bearer-токен** (`INVESTOR_MCP_AUTH_TOKEN`) на HTTP-транспорте.
- **Секретный путь** (`INVESTOR_MCP_PATH`) для no-auth за туннелем.
- Проверка `Host` (anti-DNS-rebinding) выключена за доверенным прокси; строгий режим —
  `INVESTOR_MCP_ALLOWED_HOSTS`.
- Секреты только в `.env` (в git не попадает), не светятся в логах и ответах тулов.

## Переменные окружения

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `TINKOFF_INVEST_TOKEN` | _(пусто → mock)_ | Токен Tinkoff Invest API. Пусто = read-only mock-данные. |
| `TINKOFF_INVEST_SANDBOX` | `false` | Sandbox вместо боевого API. |
| `TINKOFF_INVEST_TARGET` | _(пусто → константа SDK)_ | gRPC-эндпоинт `host[:port]`, напр. `invest-public-api.tbank.ru:443`. |
| `TINKOFF_INVEST_CA_BUNDLE` | _(пусто → корни gRPC по умолчанию)_ | PEM-файл корневых сертификатов (сертификат Минцифры). |
| `TINKOFF_INVEST_TIMEOUT_SECONDS` | `20` | Дедлайн на один gRPC-вызов; `0` — без дедлайна. |
| `INVESTOR_MCP_STORAGE_PATH` | `./data/investor_mcp.db` | Путь к SQLite. |
| `INVESTOR_MCP_CACHE_TTL_SECONDS` | `86400` | TTL кэша состава (1 сутки). |
| `INVESTOR_MCP_AUTH_TOKEN` | _(пусто → без auth)_ | Если задан — требуется `Authorization: Bearer …`. |
| `INVESTOR_MCP_PATH` | `/mcp` | Путь MCP-эндпоинта (для no-auth — секретный). |
| `INVESTOR_MCP_ALLOWED_HOSTS` | _(пусто → проверка off)_ | Разрешённые `Host` (строгий режим). |
| `INVESTOR_MCP_HOST` / `INVESTOR_MCP_PORT` | `127.0.0.1` / `8000` | Адрес для `streamable-http`. |

## Запуск локально

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e .

investor-mcp                                                   # stdio (локальные клиенты)
investor-mcp --transport streamable-http --host 127.0.0.1 --port 8000   # remote HTTP
```

Без `TINKOFF_INVEST_TOKEN` сервер работает на read-only mock-данных.

### Боевой Tinkoff SDK

SDK (`tinkoff-investments`, импорт `tinkoff.invest`) снят с PyPI и тянет неопубликованную
зависимость `tinkoff` — поэтому рантайм-зависимости ставятся через extra, а сам SDK — с
`--no-deps`:

```bash
python -m pip install -e ".[tinkoff]"
python -m pip install --no-deps "git+https://github.com/RussianInvestments/invest-python.git"
```

Проверено на Python 3.10 (SDK 0.2.0b117). **protobuf обязательно `<5`.**

## Тесты

```bash
python -m unittest discover -s tests
```

Тесты используют mock-брокер и фейковые SDK-объекты — Tinkoff SDK для прогона **не нужен**
(CI ставит без него; адаптер импортирует SDK лениво).
