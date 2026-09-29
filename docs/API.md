# Контракт локального HTTP API

База: `http://127.0.0.1:8080`. Формат JSON в UTF-8, кроме PDF/XLSX и статических файлов. Описание соответствует `collector_risk/server.py` после исправления сигнатуры проверки прав. OpenAPI 3.1: [openapi.yaml](openapi.yaml); его можно открыть в Swagger Editor/UI. Сервер **не** публикует встроенную страницу Swagger.

## Авторизация и ошибки

`POST /api/login` принимает `{"user":"dispatcher","password":"demo"}` и при успехе возвращает `user_id`, `role`, `csrf` и cookie `demo_session` (`HttpOnly`, `SameSite=Strict`). Передавайте cookie для всех рабочих маршрутов. Для POST, кроме `login` и `logout`, добавьте `X-CSRF-Token` из ответа входа. `GET /api/me` показывает сессию и список прав; `POST /api/logout` удаляет сессию. Пароль `demo` пригоден только для локального демо.

Общий формат ошибки: `{"error":"текст"}`. Типовые коды: **200** успешно, **201** новая локальная запись, **400** неверные поля/даты/фильтры, **401** нет сессии или неверный логин, **403** нет права или CSRF, **404** неизвестный путь либо ID уведомления/черновика. Неизвестный ID в `GET /api/tickets/{id}` в текущей версии дает **200** с `local_draft:null` и статусом внешней заглушки. Неверный JSON/отсутствующие поля могут дать 400. Необработанные ошибки сервера не имеют гарантированного стабильного JSON-контракта; промышленная обработка 500 еще не выполнена.

Сервер проверяет **право роли**, но пока не ограничивает права списком конкретных `object_id`. Записи чтения и отказов аудитируются на защищенных API; `/health`, `/api/me` и выдача статических файлов не проходят тот же контроль аудита.

## Маршруты

| Метод и путь | Право | Данные запроса / ответ | Коды |
| --- | --- | --- | --- |
| `GET /health` | открытый | `ok`, `forecast_date`, `mode=historical demo` | 200 |
| `GET /api/me` | открытый | текущий пользователь с `permissions` либо `authenticated:false` | 200 |
| `POST /api/login` | открытый | `user`, `password`; cookie и CSRF | 200, 400, 401 |
| `POST /api/logout` | открытый | JSON объект `{}`; снимает cookie | 200, 400 |
| `GET /api/forecasts` | `forecasts:read` | массив исторических записей, включая `valid_from`, `valid_to`, `status`, `score` | 200, 401, 403 |
| `GET /api/objects` | `objects:read` | активные узлы реестра с категориями | 200, 401, 403 |
| `GET /api/equipment?object_id=...&system=...&q=...` | `objects:read` | до 200 совпадений из исходного CSV плюс локальные; счетчики | 200, 400, 401, 403 |
| `POST /api/equipment` | `equipment:write` | `object_id`, `system_name`, `sensor_type`, `name`; локальный `id` | 201, 400, 401, 403 |
| `POST /api/objects` | `objects:write` | `parent_id`, `name`; отрицательный ID локального объекта | 201, 400, 401, 403 |
| `GET /api/metrics` | `reports:read` | JSON из `metrics.json` | 200, 401, 403 |
| `GET /api/decisions` | `decisions:write` | последние 200 записей | 200, 401, 403 |
| `POST /api/decisions` | `decisions:write` | `object_id`, `category`, `decision`, `reason`; оператор берется из сессии | 201, 400, 401, 403 |
| `GET /api/notifications` | `notifications:read` | последние 200 оповещений | 200, 401, 403 |
| `POST /api/notifications/{id}/ack` | `notifications:ack` | `{}`; отметка просмотра | 200, 400, 401, 403, 404 |
| `GET /api/tickets/{id}` | `tickets:read` | локальная заявка или `null`, внешний статус всегда помечен заглушкой | 200, 400, 401, 403 |
| `POST /api/tickets/drafts` | `tickets:write` | `object_id`, `equipment`, `reason`, `priority`, `due_at` | 201, 400, 401, 403 |
| `POST /api/tickets/{id}/confirm` | `tickets:write` | `{"confirm":true}`; `external_sent:false` | 200, 400, 401, 403, 404 |
| `GET /api/stream` | `stream:write` | курсор, всего записей, watermark, время обработки | 200, 401, 403 |
| `GET /api/stream/events` | `stream:write` | последние 200 исторических событий | 200, 401, 403 |
| `POST /api/stream/control` | `stream:write` | `command=start|pause|step`, `speed` или `count` | 200, 400, 401, 403 |
| `GET /api/registry/changes` | `registry:sync` | последние 200 изменений | 200, 401, 403 |
| `POST /api/registry/sync` | `registry:sync` | `{}`; сверка локального CSV, `mode=stub` | 200, 400, 401, 403 |
| `GET /api/audit` | `audit:read` | `chain_ok`, последние 200 строк | 200, 401, 403 |
| `GET /api/roles` | `roles:manage` | каталог разрешений, роли и пользователи | 200, 401, 403 |
| `POST /api/roles` | `roles:manage` | `role_id`, `title`, `permissions` | 200, 400, 401, 403 |
| `POST /api/users` | `roles:manage` | `user_id`, `role_id`, `password` при создании | 200, 400, 401, 403 |
| `GET /api/report/options` | `reports:read` | список систем для фильтра | 200, 401, 403 |
| `GET /api/reports` | `reports:read` | отчет JSON | 200, 400, 401, 403 |
| `GET /api/reports.xlsx`, `/api/reports.pdf` | `reports:read` | бинарный экспорт одного и того же набора данных | 200, 400, 401, 403 |

Для отчетов: `kind=management|forecast|activity|quality|audit`, `from`, `to` — ISO дата, интервал до 367 календарных дней; дополнительно `object_id`, `system`, `status`, `min_score` (0–100, где применимо). Отчет по локальным действиям использует время создания записей; отчет прогнозов показывает исторический снимок независимо от указанного периода.

Решения: `decision=проверка|выезд|ложное срабатывание|наблюдение`; пара объект/система должна быть в прогнозе, `reason` обязателен. Заявка: `priority=обычный|высокий|критический`, `due_at=YYYY-MM-DD`. Подтверждение не отправляет ее наружу. Оповещения и поток получены из исторической эмуляции, а не от действующей учетной системы.
