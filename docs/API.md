# API Part 3

База: `/api/v1/`. JSON, snake_case, завершающий `/`. Полная схема — `openapi.yaml`, Swagger — `/api/docs/`. Время ISO 8601 с timezone, идентификаторы UUID.

| Метод и путь | Доступ | Назначение |
|---|---|---|
| GET / | Public | Название/версия API |
| GET /health/live/ | Public | Процесс |
| GET /health/ready/ | Public | Соединение с БД |
| GET /config/ | Public | Контакты, языки, возможности |
| POST /auth/request-code/ | Public | Запрос кода, 202 |
| POST /auth/verify-code/ | Public | Проверка, вход/регистрация, 200 |
| POST /auth/refresh/ | Refresh в JSON | Новая пара токенов |
| POST /auth/logout/ | Bearer | Завершить текущую сессию, 204 |
| POST /auth/logout-all/ | Bearer | Отозвать все JWT-сессии пользователя, 204 |
| GET /auth/sessions/ | Bearer | Массив собственных активных сессий |
| DELETE /auth/sessions/{id}/ | Bearer | Отозвать свою сессию; чужая 404 |
| GET, PATCH /accounts/me/ | Bearer или Django session | Собственный профиль |
| GET /legal/documents/?language=ru | Public | Три действующих документа выбранного языка |
| GET /legal/consents/ | Auth | Своя история принятия |
| POST /legal/consents/ | Auth | Принять конкретные версии + подтвердить 18+ |

## Пример последовательности

```json
{"email":"client@example.com","language":"ru"}
```

Отправить в `request-code`; сохранить `challenge_id` из ответа. Ответ также содержит `expires_in: 600`, `resend_after: 60`, нейтральное сообщение. Код и существование аккаунта не возвращаются.

```json
{"challenge_id":"UUID_ИЗ_ОТВЕТА","code":"КОД_ИЗ_ПИСЬМА","device_name":"My phone"}
```

Отправить в `verify-code`. Ответ: `access`, `refresh`, `token_type`, `access_expires_in`, `session_expires_at`, `session_id`, `user`, `is_new_user`. При первом входе `user.requires_consent=true`.

Для защищённых запросов: `Authorization: Bearer ACCESS_TOKEN`. Получить документы на `user.preferred_language`, показать полные тексты, затем после явного согласия отправить в `/legal/consents/`:

```json
{"documents":["UUID_TERMS","UUID_PRIVACY","UUID_PERSONAL_DATA"],"adult_confirmed":true}
```

Ответ — профиль с `requires_consent=false`. Повторное принятие тех же версий не создаёт дубликатов. Новая активная версия или смена языка может снова потребовать согласие. История старых версий остаётся.

PATCH `/accounts/me/` принимает только `first_name`, `last_name`, `phone_number` (E.164, например +77017700026), `preferred_language` (ru/kk/en). Email, role, staff и неизвестные поля отклоняются с 400. Телефон — контакт, `phone_verified=false`; SMS не отправляются.

POST refresh: `{"refresh":"CURRENT_REFRESH_TOKEN"}` без Authorization. **Заменить оба токена** полученной парой. Старая refresh больше не действует. Последовательность обновления и обработки сетевых сбоев описана в AUTH.md.

## Ошибки

```json
{"error":{"code":"invalid_code","message":"Code is invalid, expired or already used. Request a new code if needed.","details":{}}}
```

Основные статусы: 400 поля/код/устаревшие документы; 401 отсутствует или недействителен Bearer; 403 запрет доступа; 404 чужая/несуществующая сессия; 429 лимит (заголовок `Retry-After`); 503 SMTP либо документы не готовы. `legal_not_ready` требует подготовки документов; повторный запрос кода проблему не исправит. Сообщения ошибок технические английские; UI переводит `error.code` через i18n, не сравнивает полный текст.

API помечен `Cache-Control: no-store`; запрос получает серверный `X-Request-ID`. CSRF сохраняется для Django-session запросов; Bearer не зависит от cookies. Anonymous `/accounts/me/` теперь 401 (в Part 1 был 403). Django session допускается для профиля/согласий; управление устройствами требует JWT.


## Каталог (добавлено в Part 3)

Префикс `/api/v1/catalog/`: GET categories/, GET services/, GET services/{slug}/, GET services/{slug}/form/, POST services/{slug}/validate/. Полный контракт, примеры и условия — CATALOG.md. GET публичные, validate требует вход, подтверждённый email и актуальные согласия. Публичный config теперь содержит features.catalog=true; orders/payments/ai всё ещё false. Валидация анкеты не создаёт заявку, не сохраняет ответы и не загружает документы.
