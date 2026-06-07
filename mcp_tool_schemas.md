# MCP tool schemas: инвестиционный помощник

Связанные документы:

- [business_requirements.md](business_requirements.md)
- [technical_requirements.md](technical_requirements.md)

Этот документ содержит подробные JSON-схемы tools и расширенные детали MCP-контракта.

Короткая архитектурная версия: [mcp_server_api_design.md](mcp_server_api_design.md).

## 1. Задача MCP-сервера

MCP-сервер должен дать AI-ассистенту безопасный и структурированный доступ к инвестиционному контексту пользователя:

- состав портфеля;
- цели и риск-профиль;
- счета и позиции;
- история операций;
- рыночные данные;
- новости и события;
- риск-сигналы;
- рекомендации;
- отчеты и исследования.

Фронт на этом этапе не проектируется. MCP-сервер должен быть полезен сам по себе: к нему подключается AI-клиент, получает контекст, вызывает tools и собирает ответы для пользователя.

## 2. MCP-принципы

MCP-сервер должен использовать три базовые сущности:

- `tools` - исполняемые операции: синхронизировать данные, построить отчет, посчитать риск, сгенерировать рекомендацию;
- `resources` - читаемый контекст: текущий портфель, цели, снимки, отчеты, риск-сигналы;
- `prompts` - готовые сценарии общения: недельный обзор, разбор падения портфеля, идея для следующего пополнения.

Tools должны возвращать структурированный результат через `structuredContent` и короткое текстовое резюме через `content`. Это позволит и человеку читать ответ, и агенту надежно использовать результат дальше.

MCP-сервер в MVP работает в режиме чтения и аналитики. Он не выставляет заявки и не совершает сделки.

## 3. Границы ответственности

MCP-сервер отвечает за:

- получение и кэширование брокерских данных;
- нормализацию данных;
- расчет структуры портфеля;
- расчет отклонений от целей;
- поиск риск-сигналов;
- подготовку отчетов;
- подготовку исследований;
- хранение истории рекомендаций;
- выдачу данных и аналитики AI-клиенту.

MCP-сервер не отвечает за:

- UI;
- автоматическое совершение сделок;
- юридически значимую индивидуальную инвестиционную рекомендацию;
- гарантии доходности;
- полную налоговую аналитику в MVP;
- поддержку международных рынков в MVP.

## 4. Capabilities

MCP-сервер должен объявлять:

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

На старте список tools и prompts статичный. Список resources может меняться после синхронизации данных: появляются новые снимки портфеля, отчеты, исследования и риск-сигналы.

## 5. Общие соглашения API

### 5.1. Имена tools

Имена tools должны быть стабильными, в `snake_case`, с префиксом `investor_`.

Примеры:

- `investor_get_portfolio`;
- `investor_scan_risks`;
- `investor_generate_report`.

### 5.2. Даты и время

- Все timestamp в API возвращаются в ISO 8601.
- Внутреннее хранение - UTC.
- Пользовательская зона по умолчанию - `Europe/Moscow`.
- Периоды в пользовательских запросах можно передавать как `day`, `week`, `month`, `custom`.

### 5.3. Деньги

Денежные значения возвращаются объектом:

```json
{
  "amount": 12345.67,
  "currency": "RUB"
}
```

### 5.4. Идентификатор инструмента

Инструмент можно передавать одним объектом:

```json
{
  "id_type": "ticker",
  "id": "SBER"
}
```

Допустимые `id_type`:

- `ticker`;
- `figi`;
- `uid`;
- `isin`;
- `internal_id`.

### 5.5. Статус данных

Каждый аналитический результат должен показывать статус данных:

```json
{
  "data_status": "fresh",
  "as_of": "2026-06-07T09:00:00Z",
  "warnings": []
}
```

Допустимые `data_status`:

- `fresh` - данные свежие;
- `cached` - данные из кэша, но еще приемлемые;
- `stale` - данные устарели;
- `partial` - часть данных недоступна;
- `unavailable` - данных недостаточно для ответа.

### 5.6. Общая форма ответа tool

Любой tool должен возвращать:

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

`structuredContent` обязателен для всех tools, кроме критических ошибок уровня протокола.

## 6. Resources

Resources - это читаемые сущности, которые AI-клиент может подмешивать в контекст.

### 6.1. Список resources

| URI | MIME type | Назначение |
| --- | --- | --- |
| `investor://profile/current` | `application/json` | Текущие цели, риск-профиль и ограничения пользователя |
| `investor://accounts` | `application/json` | Список брокерских счетов и их статус |
| `investor://portfolio/current` | `application/json` | Актуальный агрегированный портфель |
| `investor://portfolio/snapshots/{snapshot_id}` | `application/json` | Исторический снимок портфеля |
| `investor://positions/{instrument_id}` | `application/json` | Детали позиции по инструменту |
| `investor://operations/{account_id}/{from_date}/{to_date}` | `application/json` | Операции по счету за период |
| `investor://risks/current` | `application/json` | Последний набор риск-сигналов |
| `investor://recommendations/{recommendation_id}` | `application/json` | Сохраненная рекомендация |
| `investor://reports/{report_type}/{date}` | `text/markdown` | Сформированный отчет |
| `investor://research/{instrument_id}/{date}` | `text/markdown` | Исследование по инструменту |
| `investor://schema/domain` | `application/json` | Описание доменной модели |

### 6.2. Resource: `investor://portfolio/current`

Пример структуры:

```json
{
  "as_of": "2026-06-07T09:00:00Z",
  "data_status": "fresh",
  "base_currency": "RUB",
  "total_value": {
    "amount": 1250000.0,
    "currency": "RUB"
  },
  "accounts": [],
  "positions": [],
  "allocation": {
    "by_asset_class": [],
    "by_currency": [],
    "by_sector": [],
    "by_issuer": []
  }
}
```

### 6.3. Resource: `investor://profile/current`

Пример структуры:

```json
{
  "base_currency": "RUB",
  "risk_profile": "balanced",
  "horizon": "long_term",
  "monthly_contribution": {
    "amount": 50000,
    "currency": "RUB"
  },
  "target_allocation": [
    {
      "asset_class": "bond",
      "target_percent": 50
    },
    {
      "asset_class": "stock",
      "target_percent": 35
    }
  ],
  "limits": {
    "max_single_issuer_percent": 15,
    "max_single_position_percent": 10,
    "max_high_risk_percent": 20
  }
}
```

## 7. Tools MVP

### 7.1. `investor_sync_data`

Синхронизирует брокерские и рыночные данные.

Use cases:

- первое подключение;
- ручное обновление перед отчетом;
- обновление цен;
- дозагрузка операций.

Input:

```json
{
  "type": "object",
  "properties": {
    "mode": {
      "type": "string",
      "enum": ["full", "incremental", "prices", "operations", "instruments"]
    },
    "account_ids": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "from_date": {
      "type": "string",
      "format": "date"
    },
    "to_date": {
      "type": "string",
      "format": "date"
    },
    "force": {
      "type": "boolean",
      "default": false
    }
  },
  "required": ["mode"]
}
```

Output data:

```json
{
  "sync_id": "sync_20260607_090000",
  "mode": "incremental",
  "started_at": "2026-06-07T09:00:00Z",
  "finished_at": "2026-06-07T09:00:12Z",
  "accounts_synced": 2,
  "positions_synced": 18,
  "operations_synced": 42,
  "prices_synced": 18,
  "status": "success"
}
```

### 7.2. `investor_get_sync_status`

Возвращает состояние последней синхронизации.

Input:

```json
{
  "type": "object",
  "properties": {}
}
```

Output data:

```json
{
  "last_success_at": "2026-06-07T09:00:12Z",
  "last_attempt_at": "2026-06-07T09:00:12Z",
  "status": "success",
  "data_status": "fresh",
  "stale_sections": []
}
```

### 7.3. `investor_get_profile`

Возвращает инвестиционный профиль пользователя.

Input:

```json
{
  "type": "object",
  "properties": {}
}
```

Output data: содержимое `investor://profile/current`.

### 7.4. `investor_save_profile`

Сохраняет цели, риск-профиль и ограничения пользователя.

Это запись в локальные настройки, не брокерская операция.

Input:

```json
{
  "type": "object",
  "properties": {
    "base_currency": {
      "type": "string",
      "default": "RUB"
    },
    "risk_profile": {
      "type": "string",
      "enum": ["conservative", "balanced", "aggressive", "custom"]
    },
    "horizon": {
      "type": "string",
      "enum": ["short_term", "medium_term", "long_term"]
    },
    "monthly_contribution": {
      "type": "object"
    },
    "target_allocation": {
      "type": "array"
    },
    "limits": {
      "type": "object"
    },
    "notes": {
      "type": "string"
    }
  },
  "required": ["risk_profile", "horizon"]
}
```

Output data:

```json
{
  "profile_saved": true,
  "profile_resource": "investor://profile/current"
}
```

### 7.5. `investor_list_accounts`

Возвращает счета, доступные для анализа.

Input:

```json
{
  "type": "object",
  "properties": {
    "include_inactive": {
      "type": "boolean",
      "default": false
    }
  }
}
```

Output data:

```json
{
  "accounts": [
    {
      "account_id": "string",
      "name": "string",
      "type": "brokerage",
      "status": "open",
      "included_in_analysis": true
    }
  ]
}
```

### 7.6. `investor_select_accounts`

Выбирает счета, которые входят в агрегированный портфель.

Это локальная настройка, не брокерская операция.

Input:

```json
{
  "type": "object",
  "properties": {
    "account_ids": {
      "type": "array",
      "items": {
        "type": "string"
      }
    }
  },
  "required": ["account_ids"]
}
```

Output data:

```json
{
  "selected_account_ids": ["string"],
  "accounts_resource": "investor://accounts"
}
```

### 7.7. `investor_get_portfolio`

Возвращает текущий портфель.

Input:

```json
{
  "type": "object",
  "properties": {
    "account_ids": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "refresh": {
      "type": "boolean",
      "default": false
    },
    "include_positions": {
      "type": "boolean",
      "default": true
    },
    "include_allocation": {
      "type": "boolean",
      "default": true
    }
  }
}
```

Output data: содержимое `investor://portfolio/current`.

### 7.8. `investor_analyze_portfolio`

Считает структуру, концентрации и отклонения портфеля.

Input:

```json
{
  "type": "object",
  "properties": {
    "account_ids": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "as_of": {
      "type": "string",
      "format": "date-time"
    },
    "include_goal_comparison": {
      "type": "boolean",
      "default": true
    }
  }
}
```

Output data:

```json
{
  "portfolio_value": {
    "amount": 1250000,
    "currency": "RUB"
  },
  "allocation": {
    "by_asset_class": [],
    "by_currency": [],
    "by_sector": [],
    "by_issuer": []
  },
  "concentration": {
    "top_positions": [],
    "top_issuers": [],
    "top_sectors": []
  },
  "goal_deviation": [],
  "key_findings": []
}
```

### 7.9. `investor_explain_portfolio_change`

Объясняет изменение портфеля за период.

Input:

```json
{
  "type": "object",
  "properties": {
    "period": {
      "type": "string",
      "enum": ["day", "week", "month", "custom"]
    },
    "from_date": {
      "type": "string",
      "format": "date"
    },
    "to_date": {
      "type": "string",
      "format": "date"
    },
    "account_ids": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "include_news": {
      "type": "boolean",
      "default": true
    }
  },
  "required": ["period"]
}
```

Output data:

```json
{
  "period": {
    "from": "2026-06-01",
    "to": "2026-06-07"
  },
  "total_change": {
    "amount": -12500,
    "currency": "RUB"
  },
  "total_change_percent": -1.0,
  "contributors": {
    "positive": [],
    "negative": []
  },
  "currency_effect": [],
  "related_events": [],
  "interpretation": "string"
}
```

### 7.10. `investor_get_operations`

Возвращает операции по счетам за период.

Input:

```json
{
  "type": "object",
  "properties": {
    "account_ids": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "from_date": {
      "type": "string",
      "format": "date"
    },
    "to_date": {
      "type": "string",
      "format": "date"
    },
    "instrument": {
      "type": "object"
    },
    "operation_types": {
      "type": "array",
      "items": {
        "type": "string"
      }
    }
  },
  "required": ["from_date", "to_date"]
}
```

Output data:

```json
{
  "operations": [],
  "total_count": 0,
  "resource": "investor://operations/account_id/2026-06-01/2026-06-07"
}
```

### 7.11. `investor_get_instrument`

Возвращает карточку инструмента.

Input:

```json
{
  "type": "object",
  "properties": {
    "instrument": {
      "type": "object",
      "properties": {
        "id_type": {
          "type": "string"
        },
        "id": {
          "type": "string"
        }
      },
      "required": ["id_type", "id"]
    },
    "include_position": {
      "type": "boolean",
      "default": true
    },
    "include_events": {
      "type": "boolean",
      "default": true
    }
  },
  "required": ["instrument"]
}
```

Output data:

```json
{
  "instrument": {},
  "position": {},
  "events": [],
  "resource": "investor://positions/instrument_id"
}
```

### 7.12. `investor_scan_risks`

Ищет риск-сигналы по портфелю.

Input:

```json
{
  "type": "object",
  "properties": {
    "account_ids": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "risk_types": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": [
          "concentration",
          "currency",
          "sector",
          "issuer",
          "credit",
          "liquidity",
          "rate",
          "corporate_event",
          "goal_mismatch"
        ]
      }
    },
    "severity_min": {
      "type": "string",
      "enum": ["low", "medium", "high", "critical"],
      "default": "low"
    }
  }
}
```

Output data:

```json
{
  "risk_signals": [
    {
      "id": "risk_123",
      "severity": "high",
      "type": "concentration",
      "title": "Высокая доля одного эмитента",
      "affected_positions": [],
      "portfolio_share_percent": 18.2,
      "why_it_matters": "string",
      "suggested_actions": []
    }
  ],
  "resource": "investor://risks/current"
}
```

### 7.13. `investor_get_news_digest`

Возвращает выжимку новостей и событий по портфелю.

Input:

```json
{
  "type": "object",
  "properties": {
    "period": {
      "type": "string",
      "enum": ["day", "week", "month", "custom"]
    },
    "from_date": {
      "type": "string",
      "format": "date"
    },
    "to_date": {
      "type": "string",
      "format": "date"
    },
    "account_ids": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "importance_min": {
      "type": "string",
      "enum": ["low", "medium", "high"],
      "default": "medium"
    }
  },
  "required": ["period"]
}
```

Output data:

```json
{
  "events": [
    {
      "id": "event_123",
      "date": "2026-06-07",
      "title": "string",
      "type": "rating_change",
      "importance": "high",
      "affected_instruments": [],
      "portfolio_impact": "string",
      "suggested_attention": "watch"
    }
  ],
  "summary": "string"
}
```

### 7.14. `investor_recommend_next_action`

Дает рекомендации по следующему действию с портфелем.

Основной MVP-сценарий - что купить при следующем пополнении.

Input:

```json
{
  "type": "object",
  "properties": {
    "available_cash": {
      "type": "object",
      "properties": {
        "amount": {
          "type": "number"
        },
        "currency": {
          "type": "string",
          "default": "RUB"
        }
      }
    },
    "goal": {
      "type": "string",
      "enum": ["next_purchase", "rebalance", "risk_reduce", "income", "growth"],
      "default": "next_purchase"
    },
    "max_options": {
      "type": "integer",
      "default": 3
    },
    "account_ids": {
      "type": "array",
      "items": {
        "type": "string"
      }
    }
  },
  "required": ["available_cash"]
}
```

Output data:

```json
{
  "recommendations": [
    {
      "id": "rec_123",
      "action": "buy",
      "instrument": {},
      "amount": {
        "amount": 30000,
        "currency": "RUB"
      },
      "rationale": "string",
      "goal_alignment": "string",
      "portfolio_effect": {},
      "risks": [],
      "alternatives": [],
      "confidence": "medium"
    }
  ],
  "disclaimer": "Аналитический сценарий, не гарантия результата."
}
```

### 7.15. `investor_simulate_action`

Симулирует покупку, продажу или перераспределение без реальной сделки.

Input:

```json
{
  "type": "object",
  "properties": {
    "actions": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "action": {
            "type": "string",
            "enum": ["buy", "sell", "reduce", "increase"]
          },
          "instrument": {
            "type": "object"
          },
          "amount": {
            "type": "object"
          }
        },
        "required": ["action", "instrument", "amount"]
      }
    },
    "account_ids": {
      "type": "array",
      "items": {
        "type": "string"
      }
    }
  },
  "required": ["actions"]
}
```

Output data:

```json
{
  "before": {},
  "after": {},
  "changed_metrics": [],
  "new_risks": [],
  "reduced_risks": [],
  "goal_impact": []
}
```

### 7.16. `investor_generate_report`

Формирует отчет.

Input:

```json
{
  "type": "object",
  "properties": {
    "report_type": {
      "type": "string",
      "enum": ["daily", "weekly", "monthly", "custom"]
    },
    "from_date": {
      "type": "string",
      "format": "date"
    },
    "to_date": {
      "type": "string",
      "format": "date"
    },
    "account_ids": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "format": {
      "type": "string",
      "enum": ["markdown", "json"],
      "default": "markdown"
    }
  },
  "required": ["report_type"]
}
```

Output data:

```json
{
  "report_id": "weekly_2026-06-07",
  "report_type": "weekly",
  "period": {
    "from": "2026-06-01",
    "to": "2026-06-07"
  },
  "markdown": "# Недельный отчет...",
  "resource": "investor://reports/weekly/2026-06-07"
}
```

### 7.17. `investor_research_instrument`

Готовит исследование по инструменту или эмитенту.

Input:

```json
{
  "type": "object",
  "properties": {
    "instrument": {
      "type": "object"
    },
    "depth": {
      "type": "string",
      "enum": ["brief", "standard", "deep"],
      "default": "standard"
    },
    "focus": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": ["fundamentals", "news", "risks", "portfolio_fit", "bonds", "dividends"]
      }
    }
  },
  "required": ["instrument"]
}
```

Output data:

```json
{
  "research_id": "research_SBER_2026-06-07",
  "instrument": {},
  "markdown": "# Исследование...",
  "key_points": [],
  "risks": [],
  "portfolio_fit": "string",
  "resource": "investor://research/instrument_id/2026-06-07"
}
```

## 8. Tools после MVP

Эти tools не нужны на первом шаге, но их полезно зарезервировать:

- `investor_compare_benchmark` - сравнить портфель с индексом или модельным портфелем;
- `investor_generate_monthly_strategy` - месячный стратегический обзор;
- `investor_get_bond_ladder` - анализ лестницы погашений облигаций;
- `investor_check_credit_quality` - отдельная проверка кредитного качества;
- `investor_evaluate_past_recommendations` - оценка качества прошлых рекомендаций;
- `investor_export_report` - экспорт отчета в файл;
- `investor_manage_watchlist` - управление watchlist;
- `investor_acknowledge_risk` - отметить риск как просмотренный.

## 9. Prompts

Prompts нужны, чтобы AI-клиент запускал типовые сценарии одинаково.

### 9.1. `portfolio_weekly_review`

Назначение: подготовить недельный отчет по портфелю.

Arguments:

```json
[
  {
    "name": "week_ending",
    "type": "string",
    "required": false
  }
]
```

Ожидаемый workflow:

1. `investor_get_sync_status`
2. `investor_sync_data`, если данные устарели
3. `investor_get_portfolio`
4. `investor_analyze_portfolio`
5. `investor_explain_portfolio_change`
6. `investor_get_news_digest`
7. `investor_scan_risks`
8. `investor_generate_report`

### 9.2. `portfolio_drop_explainer`

Назначение: ответить на вопрос "почему портфель упал?"

Arguments:

```json
[
  {
    "name": "period",
    "type": "string",
    "required": false
  }
]
```

Workflow:

1. Проверить свежесть данных.
2. Получить портфель.
3. Объяснить изменение за период.
4. Подмешать новости и события.
5. Отделить рыночный шум от потенциально фундаментальных причин.

### 9.3. `next_purchase_advice`

Назначение: подобрать варианты для следующего пополнения.

Arguments:

```json
[
  {
    "name": "amount",
    "type": "number",
    "required": true
  },
  {
    "name": "currency",
    "type": "string",
    "required": false
  }
]
```

Workflow:

1. Получить профиль.
2. Получить портфель.
3. Посчитать отклонение от целей.
4. Просканировать риски.
5. Сформировать 1-3 варианта действий.
6. Объяснить риски и альтернативы.

### 9.4. `instrument_deep_dive`

Назначение: развернуто исследовать актив.

Arguments:

```json
[
  {
    "name": "instrument",
    "type": "string",
    "required": true
  },
  {
    "name": "depth",
    "type": "string",
    "required": false
  }
]
```

Workflow:

1. Получить карточку инструмента.
2. Проверить наличие позиции в портфеле.
3. Получить новости и события.
4. Сформировать исследование.
5. Оценить соответствие портфелю пользователя.

### 9.5. `risk_review`

Назначение: показать, что сейчас требует внимания.

Workflow:

1. Получить портфель.
2. Просканировать риски.
3. Сгруппировать риски по серьезности.
4. Предложить действия: наблюдать, изучить, сократить, перераспределить.

## 10. Ошибки

Tool execution errors должны возвращаться через `isError: true`, если ошибка относится к исполнению tool.

Пример:

```json
{
  "content": [
    {
      "type": "text",
      "text": "Не удалось обновить портфель: брокерский API временно недоступен. Использую последний кэш от 2026-06-07 09:00."
    }
  ],
  "structuredContent": {
    "ok": false,
    "error_code": "BROKER_API_UNAVAILABLE",
    "fallback_used": "cache",
    "data_status": "cached",
    "as_of": "2026-06-07T09:00:00Z"
  },
  "isError": true
}
```

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

## 11. Безопасность

MVP должен быть read-only относительно брокера.

Требования:

- не добавлять tools для выставления заявок;
- не использовать торговые методы брокерского API;
- не возвращать токены и секреты в tool results;
- не логировать токены и секреты;
- не отдавать сырые брокерские ответы без санитизации;
- явно маркировать рекомендации как аналитические сценарии;
- хранить audit log вызовов tools;
- для write-tools локальных настроек возвращать понятное описание, что именно изменилось.

Локальные write-tools в MVP допустимы:

- `investor_save_profile`;
- `investor_select_accounts`.

Они не должны менять брокерский счет и не должны совершать торговые действия.

## 12. Минимальный MVP MCP-сервера

Чтобы сервер уже был полезен без фронта, минимальный набор:

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
- `investor_generate_report`;
- `portfolio_weekly_review`;
- `portfolio_drop_explainer`;
- `next_purchase_advice`;
- resources для профиля, счетов, текущего портфеля, рисков и отчетов.

## 13. Открытые вопросы

- Где физически хранить данные MCP-сервера: SQLite, Postgres или файловый кэш?
- Должен ли MCP-сервер сам ходить во внешние новостные источники или это будет отдельный MCP-сервер?
- Нужно ли в MVP делать `investor_research_instrument`, или достаточно отчетов, рисков и рекомендаций?
- Нужны ли resource subscriptions после синхронизации или достаточно ручного перечитывания resources?
- Должен ли `investor_sync_data` быть быстрым синхронным tool или долгой задачей с прогрессом?
- Нужно ли сразу разделять "аналитические рекомендации" и "технические сценарии ребалансировки" разными tools?
