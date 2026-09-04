## Причина

Closes #

## Наблюдаемый контракт

- До изменения:
- После изменения:

## Проверки

- [ ] Регрессионная проверка падала до патча и проходит после
- [ ] `python -m pytest --cov=cryptomathxbot --cov-report=term-missing`
- [ ] `python -m ruff check .`
- [ ] `python -m mypy`
- [ ] `python -m pip_audit .`, `python -m pip_audit -r requirements-bootstrap.txt` и `python -m pip_audit -r requirements-windows.txt`
- [ ] `requirements-windows.txt` и `requirements-bootstrap.txt` актуальны, если менялись зависимости
- [ ] Проверен затронутый Telegram-сценарий (если применимо)

## Независимый review

- Роль/модель:
- Находки и решения:

## Приватность

- [ ] Нет токенов, SQLite, логов, user/chat ID и приватных сообщений
