# Разработка и окружения

## Установка зависимостей

`requirements/base.txt` содержит точные runtime-версии, `requirements/dev.txt` добавляет Ruff. `requirements.txt` ссылается на base. Проверенная платформа: Python 3.12. При обновлениях зависимостей нужно повторить тесты, проверить новые миграции и контракт OpenAPI.

Для запуска достаточно SQLite. Для реального размещения предусмотрен PostgreSQL, файлы и БД должны размещаться с учётом требований к данным в Казахстане. Публичного сервера этот архив не создаёт.

## Окружения

| Окружение | Выбор | Особенности |
|---|---|---|
| local | По умолчанию у manage.py | DEBUG=True, SQLite, публичные docs, console email |
| test | `--settings=config.settings.test` | Отдельная БД, быстрый тестовый хешер, locmem email |
| production | `--settings=config.settings.production` | DEBUG=False, PostgreSQL, HTTPS, secure cookies |

Процессные переменные имеют приоритет над `.env`. `manage.py` по умолчанию выбирает local до чтения `.env`: для production всегда передавать явный `--settings` либо экспортировать DJANGO_SETTINGS_MODULE в окружение процесса. WSGI/ASGI по умолчанию выбирают production.

Нельзя запускать production с test settings: там намеренно слабый быстрый хешер для автотестов. Секреты для local, staging и production должны отличаться.

## PostgreSQL локально (необязательно)

Нужен Docker Desktop. В `.env` добавь две строки, заменив YOUR_PASSWORD на собственный пароль. Для простого копирования URL используй случайный пароль из букв/цифр; спецсимволы в URL нужно percent-encode.

```dotenv
POSTGRES_PASSWORD=YOUR_PASSWORD
DATABASE_URL=postgresql://ocenka360:YOUR_PASSWORD@127.0.0.1:5432/ocenka360
```

```bash
docker compose up -d db
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Это новая БД: записи из SQLite автоматически не переносятся. Не удаляй исходную базу, если в ней уже есть нужные данные. Контейнерный порт доступен только через 127.0.0.1.

Тесты на этой локальной PostgreSQL:

```bash
TEST_DATABASE_URL='postgresql://ocenka360:YOUR_PASSWORD@127.0.0.1:5432/ocenka360' python manage.py test --settings=config.settings.test
```

Django создаёт отдельную БД test_ocenka360; пользователь должен иметь право создания тестовой БД. Не использовать рабочие production credentials для тестов.

`docker compose stop db` останавливает базу с сохранением данных. Не выполнять `docker compose down -v`, если данные нужны: `-v` удаляет volume.

## Подготовка production-конфигурации

`.env.production.example` — шаблон, не готовое окружение. Потребуются реальный домен, PostgreSQL, секрет, TLS и настроенный reverse proxy. SMTP в Part 1 не отправляет клиентские коды, даже если параметры заполнены.

После настройки:

```bash
python manage.py check --deploy --settings=config.settings.production
python manage.py migrate --settings=config.settings.production
python manage.py collectstatic --noinput --settings=config.settings.production
gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 2 --access-logfile -
```

Не выполнять эти команды с шаблонными значениями. Это подготовленный профиль настроек, а не обещание готовности Part 1 к публичной эксплуатации. Защита admin-входа, общий cache/rate limiter, HTTPS reverse proxy, резервные копии, мониторинг и домен входят в дальнейшую эксплуатационную подготовку.

`TRUST_PROXY_HTTPS=True` разрешён только когда proxy заменяет входящий X-Forwarded-Proto, а прямой доступ к приложению закрыт. Иначе клиент может подделать HTTPS-признак. CORS и CSRF не заменяют авторизацию.

HSTS ограничен одним часом для текущего хоста. `check --deploy` может сообщить W005/W021: includeSubDomains и preload сознательно не включены до проверки всех поддоменов. Не подавлять предупреждения и не включать preload автоматически.

## Типичные проблемы

- **No module named django**: активировать `.venv`, установить requirements.
- **DJANGO_SECRET_KEY missing**: выполнить `python scripts/init_env.py`.
- **DisallowedHost**: добавить точный hostname/IP в DJANGO_ALLOWED_HOSTS; не ставить `*`.
- **403 на /accounts/me/**: войти в admin в том же браузере и на том же hostname; клиентский OTP ещё не реализован.
- **Оценщик не входит в admin**: это ожидаемо. Не давать ему superuser ради входа; его кабинет будет отдельным.
- **Email не приходит**: Part 1 не содержит отправки кодов. Console backend — только подготовка для следующего Part.
- **Занят порт 8000**: `python manage.py runserver 127.0.0.1:8001`.
- **Изменения модели**: создавать новую миграцию; не удалять 0001 после применения.
- **Потерян пароль**: `python manage.py changepassword your-email@example.com`.

## Проверка CI

`.github/workflows/ci.yml` запускается после загрузки проекта в GitHub. Есть две ветки проверки: SQLite и PostgreSQL 17. В среде сборки ZIP workflow не запускался. Сам YAML не является доказательством пройденного PostgreSQL-теста; фактические локальные проверки перечислены в VALIDATION.md.
