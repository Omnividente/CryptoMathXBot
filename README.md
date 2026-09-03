# CryptoMathXBot

Современный Telegram-бот для расчёта стоимости криптовалютных выражений в USD и RUB.

## Возможности

- цены и изменение за 24 часа из Binance и KuCoin;
- fallback через CoinGecko и CoinPaprika;
- курс USD/RUB от ЦБ РФ с краткосрочным stale-кэшем;
- выражения с числами, тикерами, скобками, `+`, `-`, `*`, `/` и степенями;
- русские математические формулировки и исправление русской раскладки;
- графики за 1 час, 24 часа и 7 дней;
- inline mode;
- персональные избранные монеты в SQLite;
- ограничение частоты запросов, ограничение параллелизма и защита от второго процесса;
- приватные ответы в группах, где это поддерживает Bot API, без публичной публикации при ошибке;

## Требования

- Python 3.10 для Windows launcher; Python 3.10+ при ручной установке;
- токен Telegram Bot API;
- сетевой доступ к Telegram и публичным API котировок.

## Запуск в Windows

1. Установите Python 3.10.
2. Создайте `BOT_TOKEN.txt` в корне проекта и поместите туда только токен бота. Файл уже исключён из Git.
3. Если нужен inline mode, в `@BotFather` выполните `/setinline`, выберите бота и задайте placeholder, например `BTC или выражение`. Без этого Telegram не отправляет боту `InlineQuery` updates.
4. Один раз создайте изолированное runtime-окружение:

```powershell
.\start.ps1 -Install
```

`-Install` пересоздаёт только `.runtime-venv`, hash-проверенно обновляет `pip` по `requirements-bootstrap.txt`, затем устанавливает binary wheels с официального PyPI строго по `requirements-windows.txt`. Рабочая `.venv` разработчика не затрагивается.

Последующие запуски не скачивают и не обновляют пакеты:

```powershell
.\start.ps1
```

При изменении любого lock-файла launcher остановится и потребует снова выполнить `-Install`. Для подробного уровня логирования:

```powershell
.\start.ps1 -ShowAppLogs
```

Токен можно передать без файла:

```powershell
$env:CRYPTOMATHX_BOT_TOKEN = "<token>"
.\start.ps1
```

## Ручная установка для разработки

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --editable ".[dev]"
.\.venv\Scripts\python.exe -m cryptomathxbot
```

## Команды

- `/start` — открыть калькулятор;
- `/price 0.5 BTC + 2 ETH` — рассчитать выражение;
- `/favorites BTC ETH XMR` — настроить быстрые кнопки;
- `/settings` — открыть настройки;
- `/help` — примеры и правила;
- `/ping` — проверить состояние процесса.

Обычный текст в группах бот обрабатывает только при упоминании бота или в ответе на его сообщение. Зарегистрированные команды `/price`, `/favorites`, `/settings`, `/help` и `/ping` работают без упоминания; личные команды помечены как приватные. Обычная переписка не отправляется рыночным провайдерам.

## Конфигурация

Все параметры необязательны, кроме токена:

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `CRYPTOMATHX_BOT_TOKEN` | — | токен бота; имеет приоритет над файлом |
| `CRYPTOMATHX_TOKEN_FILE` | `BOT_TOKEN.txt` | путь к файлу токена |
| `CRYPTOMATHX_DATA_DIR` | `data` | SQLite и lock-файл |
| `CRYPTOMATHX_LOG_DIR` | `logs` | журналы процесса |
| `CRYPTOMATHX_OWNER_CHAT_ID` | — | chat ID для уведомления о готовности процесса |
| `CRYPTOMATHX_DEFAULT_FAVORITES` | `BTC ETH XMR` | избранные по умолчанию |
| `CRYPTOMATHX_MAX_FAVORITES` | `8` | максимум быстрых кнопок |
| `CRYPTOMATHX_MAX_SYMBOLS` | `8` | максимум тикеров в выражении |
| `CRYPTOMATHX_CONCURRENT_UPDATES` | `8` | число одновременно обрабатываемых апдейтов |
| `CRYPTOMATHX_QUERY_CONCURRENCY` | `6` | число одновременных рыночных запросов |
| `CRYPTOMATHX_RATE_LIMIT_REQUESTS` | `8` | запросов пользователя за окно |
| `CRYPTOMATHX_RATE_LIMIT_WINDOW` | `30` | окно лимита в секундах |
| `CRYPTOMATHX_HTTP_TIMEOUT` | `10` | таймаут внешних API в секундах |
| `CRYPTOMATHX_HTTP_RETRIES` | `2` | повторы временных сбоев |
| `CRYPTOMATHX_CHART_DPI` | `140` | разрешение PNG-графика |

## Архитектура

Код разделён на небольшие слои в `src/cryptomathxbot`:

- `app.py` — lifecycle PTB, handlers и Telegram UX;
- `calculator.py` — безопасный AST-парсер и Decimal-вычисления;
- `market.py` — HTTP-клиент, провайдеры, fallback и кэши;
- `storage.py` — асинхронная обёртка над SQLite и миграция legacy `favorites.json`;
- `ui.py` — HTML-тексты и inline-клавиатуры;
- `charts.py` — изолированный rendering в worker thread;
- `session.py`, `cache.py`, `rate_limit.py` — короткоживущие состояния и ограничения.

## Проверки

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=cryptomathxbot --cov-report=term-missing
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pip_audit .
.\.venv\Scripts\python.exe -m pip_audit -r requirements-bootstrap.txt
.\.venv\Scripts\python.exe -m piptools compile --generate-hashes --reuse-hashes --strip-extras --no-header --resolver=backtracking --index-url=https://pypi.org/simple --output-file=requirements-windows.txt pyproject.toml
```

Тесты используют mock transport и не требуют токена или реального Telegram API.

## Безопасность и приватность

- Не публикуйте `BOT_TOKEN.txt`, переменные окружения, `data/` и `logs/`.
- Запросы пользователей не записываются в журналы.
- Выражения разбираются без `eval`/`exec`; разрешён ограниченный набор AST-узлов.
- Данные избранного хранятся локально и разделяются по пользователю/чату.
- Внешние цены являются справочной информацией; при недоступности провайдера бот явно помечает stale-цену.

Процесс работы с ошибками и независимым AI-аудитом описан в [CONTRIBUTING.md](CONTRIBUTING.md). Политика сообщения уязвимостей — в [SECURITY.md](SECURITY.md).

## Лицензия

MIT. См. [LICENSE](LICENSE).
