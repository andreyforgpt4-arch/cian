## Что это

Python + Playwright бот для Циан:

- открывает объявление (в **авторизованной** сессии),
- отправляет сообщение продавцу,
- переходит на `https://novosibirsk.cian.ru/dialogs/`,
- находит только что отправленный диалог и прикрепляет картинку, скачанную с Google Drive по ID.

## Быстрый старт (Windows / PowerShell)

Создайте виртуальное окружение и установите зависимости:

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install
```

Создайте файл `.env` (скопируйте из `env.example` и отредактируйте при необходимости).

## 1) Одноразовая авторизация (сохранение сессии)

Запустите браузер, войдите в Циан вручную, затем закройте страницу — скрипт сохранит cookies/localStorage в `auth_state.json`:

```bash
python -m cian_bot.login
```

## 2) Отправка сообщения и прикрепление картинки

```bash
python -m cian_bot.run
```

## 3) Шаг 1: парсинг выдачи через Cian API (search-offers)

Подготовьте payload JSON (фильтры) и выполните:

```bash
python -m cian_bot.search --payload payload.json --limit 50
```

Результаты:
- `search_result.json` — сырой ответ API
- `offers_extracted.json` — упрощённый список `{offer_id, cian_url, photos[]}`

Состояние (чтобы не дублировать обработку между запусками) сохраняется в SQLite файле `state.db` (путь задаётся через `STATE_DB_PATH`).

## 4) Шаг 2: анализ фото через OpenAI Vision (оценка + поиск “плохого интерьера”)

Скрипт проходит фото **по порядку**, сохраняет результаты в БД и останавливается на первом фото, где:
- `category == interior`
- `score <= 6` (порог настраивается)

Запуск:

```bash
python -m cian_bot.score_photos --limit 50 --threshold 6 --status discovered
```

Если OpenAI недоступен из вашего региона, можно использовать n8n webhook:
- выставьте `PHOTO_SCORING_WEBHOOK_URL`
- `score_photos` будет отправлять туда `{offer_id, photo_index, photo_url}` и ожидать JSON по нашей схеме (см. `cian_bot/openai_vision.py` → `PHOTO_SCORE_JSON_SCHEMA`).

## Примечания

- Если Циан попросит капчу/подтверждение — пройдите это в режиме `login`.
- Если во время `run` всплывает **reCAPTCHA**, скрипт в headful режиме **подождёт**, пока вы решите её в открытом браузере, и затем продолжит.
- Если селекторы на сайте изменятся, возможно потребуется подстроить логику поиска кнопки “Написать сообщение” или загрузки вложения.

