# Контракт API — Part 1

Базовый локальный URL: `http://127.0.0.1:8000/api/v1/`.

| Метод | Путь от корня сервера | Доступ | Ответ |
|---|---|---|---|
| GET | `/api/v1/` | Публичный | service, version, api_version |
| GET | `/api/v1/health/live/` | Публичный | 200 status=ok; без обращения к БД |
| GET | `/api/v1/health/ready/` | Публичный | 200 status=ok либо 503 status=unavailable |
| GET | `/api/v1/config/` | Публичный | Компания, языки, валюта, timezone, features |
| GET | `/api/v1/accounts/me/` | Сессия | Только собственный профиль |
| GET | `/api/schema/` | Local публичный; production staff | OpenAPI |
| GET | `/api/docs/` | Local публичный; production staff | Swagger UI |

Readiness проверяет соединение с БД (`SELECT 1`), а не применение миграций. Применение миграций проверяют отдельным шагом до запуска сервера.

```bash
curl http://127.0.0.1:8000/api/v1/health/ready/
curl http://127.0.0.1:8000/api/v1/config/
```

Конфигурация:

```json
{
  "company": {
    "name": "Оценка 360",
    "legal_name": null,
    "phone": "+77017700026",
    "email": "sati-ocenka@mail.ru",
    "address": "Казахстан, Астана, проспект Кабанбай батыр, 46а"
  },
  "default_language": "ru",
  "supported_languages": ["ru", "kk", "en"],
  "currency": "KZT",
  "timezone": "Asia/Almaty",
  "features": {"email_otp": false, "orders": false, "payments": false, "ai": false}
}
```

Features описывают реализованные серверные функции, а не разрешения пользователя. Контроль доступа всегда выполняется сервером.

`/accounts/me/`: id, email, first_name, last_name, phone_number, preferred_language, role, email_verified_at, date_joined. Пароль, permissions, staff/superuser-флаги не выдаются. Профиль только для чтения. Нет получения чужого профиля по ID, списка всех пользователей или публичного назначения ролей.

Ошибки:

```json
{"error":{"code":"not_authenticated","message":"Authentication credentials were not provided.","details":{}}}
```

Клиенты должны ориентироваться на `error.code`, а тексты переводить у себя. Серверный message может меняться и локализоваться. Поля в details сохраняются при ошибках валидации. Стандартные коды: not_authenticated, permission_denied, validation_error, method_not_allowed, not_found, throttled, server_error. Health 503 специально использует status вместо общей оболочки ошибок.

В Part 1 анонимный /me возвращает 403, а не 401, по правилам DRF SessionAuthentication. PATCH /me аутентифицированному пользователю — 405. Неожиданные ошибки не раскрывают внутренние exception messages. Django 404/500 оформлены JSON для `/api/` при DEBUG=False; локальный DEBUG может показывать диагностические страницы.

Все ответы `/api/` имеют Cache-Control: no-store и X-Request-ID. Лимиты DRF по умолчанию: 60/min для анонимных, 120/min для аутентифицированных; 429 включает Retry-After. Health endpoints исключены из этих лимитов. Это локальные ограничения на процесс, не DDoS-защита и не финальная защита авторизации.

## Для Mobile Part 1

Физический телефон не видит Mac по 127.0.0.1. Когда понадобится тест с телефона в одной доверенной Wi-Fi сети, добавить LAN-IP Mac в DJANGO_ALLOWED_HOSTS и запустить `python manage.py runserver 0.0.0.0:8000`. Базой для телефона будет `http://LAN-IP:8000/api/v1/`. Настройки HTTP для development build согласуются в Mobile Part 1; production использует HTTPS. Не открывать dev-сервер в интернете.

## Для Frontend

CORS разрешает только заданные origin. CORS_ALLOW_CREDENTIALS=False намеренно: междоменные cookie-сессии для Next.js ещё не реализованы. Доступ к публичной конфигурации возможен, а окончательная клиентская авторизация и CSRF-схема будут согласованы в Part 2. Не включать credentials или wildcard «чтобы заработало».
