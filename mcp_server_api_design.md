# MCP API design: инвестиционный помощник

Связанные документы:

- [business_requirements.md](business_requirements.md)
- [technical_requirements.md](technical_requirements.md)
- [mcp_tool_schemas.md](mcp_tool_schemas.md)

## 1. Что проектируем

Проектируем MCP-сервер, через который AI-ассистент сможет работать с инвестиционным портфелем пользователя без фронта.

MCP-сервер должен давать ассистенту:

- контекст портфеля;
- цели и риск-профиль;
- брокерские счета;
- позиции и операции;
- рыночные данные;
- новости и события;
- риск-сигналы;
- рекомендации;
- отчеты;
- исследования.

MVP работает в режиме чтения и аналитики. Сервер не выставляет заявки и не совершает сделки.

## 2. Базовая форма MCP

Сервер должен использовать три MCP-сущности:

- `tools` - действия и расчеты, которые вызывает модель;
- `resources` - читаемый контекст, который можно подмешивать в диалог;
- `prompts` - готовые сценарии работы помощника.

Подробные JSON-схемы tools вынесены в [mcp_tool_schemas.md](mcp_tool_schemas.md).

## 3. Capabilities

Сервер объявляет:

```json
{
  "capabilities": {
    "tools": {
      "listChanged": false
    },
    "resources": {
      "subscribe": false,
      "listChanged": true
    },
    "prompts": {
      "listChanged": false
    }
  }
}
```

На старте список tools и prompts статичный. Список resources может меняться после синхронизации данных: появляются снимки портфеля, отчеты, исследования и риск-сигналы.

## 4. Общие соглашения

Имена tools:

- стабильные;
- в `snake_case`;
- с префиксом `investor_`.

Примеры:

- `investor_get_portfolio`;
- `investor_scan_risks`;
- `investor_generate_report`.

Даты и время:

- timestamps в ISO 8601;
- внутреннее хранение в UTC;
- пользовательская зона по умолчанию `Europe/Moscow`.

Денежные значения:

```json
{
  "amount": 12345.67,
  "currency": "RUB"
}
```

Статус данных:

- `fresh` - данные свежие;
- `cached` - данные из кэша, но еще приемлемые;
- `stale` - данные устарели;
- `partial` - часть данных недоступна;
- `unavailable` - данных недостаточно для ответа.

Каждый tool возвращает короткий текст для человека и `structuredContent` для агента.

## 5. Resources

Resources - это читаемые данные, которые AI-клиент может использовать как контекст.

| URI | MIME type | Назначение |
| --- | --- | --- |
| `investor://profile/current` | `application/json` | Цели, риск-профиль и ограничения пользователя |
| `investor://accounts` | `application/json` | Брокерские счета и их статус |
| `investor://portfolio/current` | `application/json` | Актуальный агрегированный портфель |
| `investor://portfolio/snapshots/{snapshot_id}` | `application/json` | Исторический снимок портфеля |
| `investor://positions/{instrument_id}` | `application/json` | Детали позиции по инструменту |
| `investor://operations/{account_id}/{from}/{to}` | `application/json` | Операции по счету за период |
| `investor://risks/current` | `application/json` | Последний набор риск-сигналов |
| `investor://recommendations/{recommendation_id}` | `application/json` | Сохраненная рекомендация |
| `investor://reports/{report_type}/{date}` | `text/markdown` | Сформированный отчет |
| `investor://research/{instrument_id}/{date}` | `text/markdown` | Исследование по инструменту |
| `investor://schema/domain` | `application/json` | Описание доменной модели |

## 6. Tools MVP

### 6.1. Синхронизация и состояние данных

| Tool | Назначение |
| --- | --- |
| `investor_sync_data` | Синхронизировать брокерские и рыночные данные |
| `investor_get_sync_status` | Проверить свежесть данных и статус последней синхронизации |

`investor_sync_data` поддерживает режимы:

- `full`;
- `incremental`;
- `prices`;
- `operations`;
- `instruments`.

### 6.2. Профиль и счета

| Tool | Назначение |
| --- | --- |
| `investor_get_profile` | Получить цели, риск-профиль и ограничения |
| `investor_save_profile` | Сохранить локальный инвестиционный профиль |
| `investor_list_accounts` | Получить брокерские счета |
| `investor_select_accounts` | Выбрать счета, входящие в анализ |

`investor_save_profile` и `investor_select_accounts` меняют только локальные настройки. Они не меняют брокерский счет и не совершают сделки.

### 6.3. Портфель и операции

| Tool | Назначение |
| --- | --- |
| `investor_get_portfolio` | Получить текущий портфель |
| `investor_analyze_portfolio` | Посчитать структуру, концентрации и отклонения от целей |
| `investor_explain_portfolio_change` | Объяснить изменение портфеля за период |
| `investor_get_operations` | Получить операции за период |
| `investor_get_instrument` | Получить карточку инструмента и связанную позицию |

### 6.4. Риски, новости и рекомендации

| Tool | Назначение |
| --- | --- |
| `investor_scan_risks` | Найти риск-сигналы по портфелю |
| `investor_get_news_digest` | Собрать выжимку новостей и событий |
| `investor_recommend_next_action` | Предложить следующее действие, чаще всего покупку на пополнение |
| `investor_simulate_action` | Смоделировать покупку/продажу/перераспределение без реальной сделки |

`investor_simulate_action` не должен выставлять заявку. Это только расчет "что будет, если".

### 6.5. Отчеты и исследования

| Tool | Назначение |
| --- | --- |
| `investor_generate_report` | Сформировать ежедневный, недельный или месячный отчет |
| `investor_research_instrument` | Подготовить исследование по активу или эмитенту |

Для MVP обязательны недельный отчет, риск-обзор и рекомендация по следующему пополнению. Глубокое исследование инструмента можно оставить как расширение MVP, если нужно быстрее дойти до рабочего сервера.

## 7. Минимальный набор tools для первой рабочей версии

Минимально полезный MCP-сервер:

- `investor_sync_data`;
- `investor_get_sync_status`;
- `investor_get_profile`;
- `investor_save_profile`;
- `investor_list_accounts`;
- `investor_select_accounts`;
- `investor_get_portfolio`;
- `investor_analyze_portfolio`;
- `investor_explain_portfolio_change`;
- `investor_scan_risks`;
- `investor_get_news_digest`;
- `investor_recommend_next_action`;
- `investor_generate_report`.

Остальное можно добавить после первого прохода.

## 8. Prompts

Prompts нужны, чтобы AI-клиент запускал типовые сценарии одинаково.

| Prompt | Назначение |
| --- | --- |
| `portfolio_weekly_review` | Подготовить недельный отчет |
| `portfolio_drop_explainer` | Ответить "почему портфель упал?" |
| `next_purchase_advice` | Подобрать варианты для пополнения |
| `instrument_deep_dive` | Исследовать актив или эмитента |
| `risk_review` | Показать, что требует внимания |

### 8.1. `portfolio_weekly_review`

Workflow:

1. Проверить свежесть данных.
2. Синхронизировать данные, если они устарели.
3. Получить портфель.
4. Проанализировать структуру.
5. Объяснить изменение за неделю.
6. Получить новости.
7. Просканировать риски.
8. Сформировать отчет.

### 8.2. `next_purchase_advice`

Workflow:

1. Получить профиль.
2. Получить портфель.
3. Посчитать отклонение от целей.
4. Просканировать риски.
5. Сформировать 1-3 варианта действий.
6. Объяснить риски и альтернативы.

### 8.3. `portfolio_drop_explainer`

Workflow:

1. Проверить свежесть данных.
2. Получить портфель.
3. Объяснить изменение за период.
4. Подмешать новости и события.
5. Отделить рыночный шум от фундаментальных причин.

## 9. Общая форма ответа tool

Все tools должны возвращать:

```json
{
  "content": [
    {
      "type": "text",
      "text": "Короткое человекочитаемое резюме результата."
    }
  ],
  "structuredContent": {
    "ok": true,
    "data_status": "fresh",
    "as_of": "2026-06-07T09:00:00Z",
    "summary": "Короткий вывод.",
    "data": {},
    "warnings": [],
    "sources": [],
    "resource_links": []
  },
  "isError": false
}
```

Ошибки исполнения tool возвращаются через `isError: true` и машинно-читаемый `error_code`.

Коды ошибок:

- `BROKER_API_UNAVAILABLE`;
- `BROKER_AUTH_FAILED`;
- `BROKER_RATE_LIMIT`;
- `DATA_NOT_SYNCED`;
- `DATA_STALE`;
- `INSTRUMENT_NOT_FOUND`;
- `ACCOUNT_NOT_FOUND`;
- `PROFILE_NOT_CONFIGURED`;
- `INSUFFICIENT_DATA`;
- `VALIDATION_ERROR`;
- `INTERNAL_ERROR`.

## 10. Безопасность

MVP должен быть read-only относительно брокера.

Требования:

- не добавлять tools для выставления заявок;
- не использовать торговые методы брокерского API;
- не возвращать токены и секреты в tool results;
- не логировать токены и секреты;
- не отдавать сырые брокерские ответы без санитизации;
- явно маркировать рекомендации как аналитические сценарии;
- хранить audit log вызовов tools;
- для локальных write-tools возвращать понятное описание, что именно изменилось.

## 11. Что проектировать дальше

Следующий шаг после согласования этого API:

1. Утвердить минимальный набор tools.
2. Выбрать хранилище: SQLite или Postgres.
3. Описать доменные модели.
4. Сделать skeleton MCP-сервера.
5. Подключить Tinkoff Invest API.
6. Реализовать sync + portfolio + analyze.
7. Добавить reports + risks + next action.

