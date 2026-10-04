# Telegram Research API

Это закрытый исследовательский стенд для тестирования Telegram-сессий на
центральном аккаунте владельца. API проверяет токен и единственный allowlisted
recipient, после чего отправляет через выбранную Telethon-сессию. Автоответы
выключены по умолчанию и слушают только личные сообщения от центрального аккаунта.

## Как устроено

- `api/` — HTTP-маршруты и форматы запросов/ответов;
- `services/` — проверки получателей, лимитов и расписания;
- `integrations/telegram.py` — единственное место, которое вызывает Telethon;
- `database/connection.py` — SQLite-соединения и схема;
- `database/models.py` — простые объекты данных;
- `database/requests.py` — все SQL-запросы к базе;
- `settings.py` — настройки из переменных окружения.

Если совсем просто: API получает команду отправить текст, проверяет адресата,
аккаунт и общий интервал, затем передаёт сообщение Telethon. Планировщик выбирает
случайную заготовку и отправителя по кругу. При включённом автоответе Telethon
слушает ответы только центрального тестового аккаунта, берёт последние реплики
из SQLite и передаёт их вместе с prompt в OpenAI-compatible LLM endpoint.
Полученные updates дедуплицируются по Telegram message ID.


## Запуск

Python 3.9 или новее:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp -n .env.example .env
```

Файл `.env` уже создан в этой рабочей копии; если его нет в новой копии,
`cp -n` создаст его из примера и не перезапишет существующий. Файл исключён из
Git: внесите секреты только туда, не в `.env.example` и не в исходники.

Где взять значения:

- `CONTROL_BOT_TOKEN` — создать бота через `@BotFather` и скопировать token;
- `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` — создать приложение на `my.telegram.org`;
- `CONTROL_ADMIN_IDS` — numeric user ID, например узнать через `@userinfobot`;
- `API_TOKEN` — общий секрет API и админ-бота; сгенерировать можно `openssl rand -hex 32`;
- `TELEGRAM_ALLOWED_RECIPIENTS` — username центрального тестового аккаунта без `@`;
- `LLM_API_KEY` — необязателен, нужен только для автоответов по prompt.

Запустите API в первом терминале из корня проекта:

```bash
source .venv/bin/activate
uvicorn research_sim.api.app:app --host 127.0.0.1 --port 8000
```

Во втором терминале, также из корня проекта, запустите админ-бота:

```bash
source .venv/bin/activate
python -m research_sim.bot.app
```

Откройте бота в Telegram и отправьте `/start`. API проверяется по
`http://127.0.0.1:8000/health`; интерактивная документация доступна на
`http://127.0.0.1:8000/docs`.

## Развёртывание из GitHub через PM2

На Ubuntu/Debian-сервере один раз установите системные инструменты и клонируйте
репозиторий по SSH (подставьте его URL):

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip nodejs npm
git clone git@github.com:OWNER/REPOSITORY.git
cd REPOSITORY
./deploy.sh
```

При первом запуске скрипт создаст venv, установит Python- и PM2-зависимости,
спросит недостающие настройки и сохранит их в закрытый `.env`. Токены вводятся
без отображения в терминале, `API_TOKEN` скрипт генерирует сам. Перед запуском
понадобятся `CONTROL_BOT_TOKEN` из `@BotFather`, ваш numeric admin ID,
`TELEGRAM_API_ID`/`TELEGRAM_API_HASH` с `my.telegram.org` и username центрального
тестового аккаунта. LLM API key можно пропустить. Запускайте `./deploy.sh`
непосредственно в интерактивной SSH-сессии, не через `curl | bash` и не с
перенаправленным stdin. Затем скрипт запускает два
процесса из `ecosystem.config.cjs` под PM2. Для проверки:

```bash
pm2 status
pm2 logs telegram-research-api --lines 50
pm2 logs telegram-research-bot --lines 50
curl http://127.0.0.1:8000/health
```

Чтобы процессы восстановились после перезагрузки сервера, выполните `pm2
startup`, запустите команду, которую PM2 напечатает для вашей системы, затем:

```bash
pm2 save
```

При следующих обновлениях достаточно снова запустить тот же скрипт из каталога
репозитория: он выполнит fast-forward `git pull`, обновит зависимости и
перезапустит процессы:

```bash
./deploy.sh
```

API в конфигурации слушает только `127.0.0.1`; наружу его публиковать не нужно
для работы бота на том же сервере. Для быстрого теста откройте бота, отправьте
`/start`, затем `/whoami` и убедитесь, что ID есть в `CONTROL_ADMIN_IDS`.

Меню можно проверить до подготовки Telethon-сессий. Для отправки создайте
авторизованные файлы `<account_key>.session` в `TELEGRAM_SESSION_DIR`, затем
зарегистрируйте ключ через `/add_account`. Не кладите `.env` и `.session` в Git.

API слушает только локальный интерфейс по умолчанию. Для каждого аккаунта нужна
авторизованная Telethon-сессия. Сессии должны лежать вне репозитория в
`TELEGRAM_SESSION_DIR`; файлы и API-токен не публикуйте. Файлы `.session` от
Pyrogram несовместимы с Telethon: их нельзя просто переименовать, каждый аккаунт
нужно авторизовать и создать новую Telethon-сессию.

API и бот можно запустить и проверить меню без зарегистрированных сессий. Для
фактической отправки положите заранее авторизованные Telethon-файлы
`<account_key>.session` в `TELEGRAM_SESSION_DIR`, затем добавьте их через
`/add_account`. Пока автоответы выключены, `LLM_API_KEY` можно не заполнять.

## Аккаунты отправителя

Добавьте собственные аккаунты в API. `account_key` должен совпадать с именем
Telethon-сессии без суффикса `.session`: например, ключ `business` использует
файл `$TELEGRAM_SESSION_DIR/business.session`. В SQLite записываются ключ,
подпись и статистика использования, но не телефон, пароль, API hash или файл
сессии.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/accounts \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"account_key":"business","label":"Бизнес"}'

curl -X POST http://127.0.0.1:8000/api/v1/accounts \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"account_key":"personal","label":"Личный"}'

curl http://127.0.0.1:8000/api/v1/accounts \
  -H "Authorization: Bearer $API_TOKEN"
```

`POST /api/v1/accounts` регистрирует аккаунт, `GET /api/v1/accounts` показывает
реестр и число отправок, `PATCH /api/v1/accounts/{account_key}` с телом
`{"enabled":false}` временно исключает сессию из чередования. API использует
общие `TELEGRAM_API_ID` и `TELEGRAM_API_HASH`, а session-файлы должны быть уже
авторизованы отдельно для каждого вашего аккаунта.

## Отправить сообщение

Все endpoints, кроме `/health`, требуют `Authorization: Bearer <API_TOKEN>`.
Получатель обязан входить в `TELEGRAM_ALLOWED_RECIPIENTS`; для этой установки
укажите там только центральный тестовый аккаунт. Отправка ограничена одним
сообщением за минимальный интервал, по умолчанию 30 секунд. Укажите
`account_index` из `GET /api/v1/accounts`; индекс начинается с 1 и соответствует
позиции в текущем списке. Выбранный аккаунт должен быть включён.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/messages/send \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"recipient":"partner_username","text":"Согласованное тестовое сообщение","account_index":1}'
```

Для фотографии используйте `POST /api/v1/messages/send-with-photo` с
`multipart/form-data`: поля `recipient`, `text`, `account_index` и файл `photo`.
Поддерживается JPEG до 10 MB. Файл передаётся Telethon из памяти, не сохраняется
приложением; подпись ограничена 1024 символами.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/messages/send-with-photo \
  -H "Authorization: Bearer $API_TOKEN" \
  -F "recipient=partner_username" \
  -F "text=Согласованное сообщение с фото" \
  -F "account_index=1" \
  -F "photo=@./test.jpg;type=image/jpeg"
```

История для конкретного отправителя доступна по адресу
`GET /api/v1/accounts/{account_key}/history?limit=50`. Для следующей страницы
передайте `before_id` со значением `id` последней записи. История показывает
время, получателя, текст, статус, наличие фотографии и ошибку при неудаче; сами
фото не хранятся.

## Настроить кампанию

Настройка меняется через бота `/campaign_setup` или
`PUT /api/v1/settings/campaign` и хранится в SQLite. Пул содержит шаблоны,
из которых каждый слот выбирает случайный; поддерживаются `{date}`, `{day}` и
`{slot}`. Расписание 1/2/1 задаётся по дням в зоне `Europe/Moscow` (МСК), а
плановые отправки идут по кругу через активные аккаунты. Для каждого sender-
аккаунта можно задать собственное описание манеры, процент сохраняемых слов и
процент точной пунктуации. При плановой отправке текст форматируется профилем
выбранного аккаунта; ручная отправка не изменяется. Непустое описание личности
переписывает плановый шаблон через LLM, поэтому для такого профиля заданный
`LLM_API_KEY` обязателен. Проценты точности применяются после переписывания.

```bash
curl -X PUT http://127.0.0.1:8000/api/v1/settings/campaign \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "campaign_id":"partner-checkin",
    "enabled":false,
    "start_date":"2026-10-05",
    "recipient":"partner_username",
    "phrases":["Тестовый этап {day}, слот {slot}"],
    "day_slots":{"1":["10:00"],"2":["10:00","15:00"],"3":["10:00"]},
    "reply_prompt":"Ты отвечаешь центральному тестовому аккаунту кратко и по теме.",
    "auto_reply_enabled":false
  }'
```

После проверки конфигурации включите плановые отправки (`enabled=true`) и/или
автоответы (`auto_reply_enabled=true`) отдельно. Планировщик обрабатывает только
совпавший слот. Автоответы требуют `reply_prompt`, `LLM_API_KEY`, хотя бы одной
активной сессии и ровно одного центрального recipient в allowlist. Они не зависят
от включения планового расписания. При остановке API Telethon listeners
отключаются; после старта синхронизируются снова. Повторный слот и повторный
входящий Telegram update не отправляются дважды.

## Триггер по активациям пакетов

URL источника задаётся в `.env` через `PACK_ACTIVATION_ENDPOINT`; опциональный
Bearer token — через `PACK_ACTIVATION_API_TOKEN`. API опрашивает этот URL сразу
после старта, затем каждые 5 минут. Первая успешная выборка сохраняется как
baseline и ничего не отправляет. Далее используется `activations_since_previous_sync`;
снимки первой и последней выборки, остатки порогов и ожидающие отправки хранятся
в SQLite и переживают рестарт.

Контракт endpoint: `GET` с `window_hours=24` и, начиная со второго запроса,
`since=<ISO-8601 timestamp из as_of предыдущего ответа>`. Он должен вернуть все
шесть пакетов, даже если у некоторых нулевые значения:

```json
{
  "as_of": "2026-10-04T12:05:00Z",
  "window_hours": 24,
  "packs": [
    {"pack_size": 8100, "activations_24h": 120000, "activations_since_previous_sync": 0},
    {"pack_size": 3650, "activations_24h": 40000, "activations_since_previous_sync": 0},
    {"pack_size": 1800, "activations_24h": 20000, "activations_since_previous_sync": 0},
    {"pack_size": 660, "activations_24h": 5000, "activations_since_previous_sync": 0},
    {"pack_size": 325, "activations_24h": 2000, "activations_since_previous_sync": 0},
    {"pack_size": 60, "activations_24h": 300, "activations_since_previous_sync": 0}
  ]
}
```

В первом ответе `activations_since_previous_sync` можно передать нулём: он
сохраняется как стартовый baseline и не создаёт сообщений. В следующих ответах
поле должно содержать точный count событий после переданного `since` до `as_of`.

`activations_24h` предназначено для отображения/контроля окна, а дельта с прошлого
успешного sync нужна для точного накопления порога: rolling total за 24 часа может
уменьшаться, когда старые события выпадают из окна. Настройка в боте, например
`/activation_rules 8100x10,3650x4,1800x0,660x0,325x0,60x0`, означает одно сообщение
на каждые `8100 × 10 = 81000` новых активаций пакета 8100 и одно на каждые
`3650 × 4 = 14600` новых активаций пакета 3650. Множитель `0` отключает пакет.
Остаток неполного порога переносится на следующие sync. За один пятиминутный цикл
отправляется максимум одно сообщение; очередь и общий rate limit не теряют
остальные достигнутые пороги.

Сначала задайте общий пул сообщений через `/campaign_setup`, затем правила
`/activation_rules`, проверьте состояние `/activation_status` и включите
`/activation_enable`. Для шаблона сообщения доступны `{date}`, `{day}`, `{slot}`,
`{pack}` и `{activations}`. Выключить этот триггер можно `/activation_disable`;
обычное расписание и автоответы управляются отдельно. Для фактической отправки
нужны активная Telethon-сессия и центральный адресат из allowlist.

## Endpoints

- `GET /health` — проверка доступности;
- `GET /api/v1/accounts` — список зарегистрированных аккаунтов отправителя;
- `POST /api/v1/accounts` — добавить аккаунт в реестр;
- `PATCH /api/v1/accounts/{account_key}` — включить или отключить аккаунт;
- `GET /api/v1/accounts/{account_key}/history` — постраничная история аккаунта;
- `POST /api/v1/messages/send` — отправить одно allowlisted сообщение;
- `POST /api/v1/messages/send-with-photo` — отправить JPEG с подписью;
- `GET /api/v1/settings/campaign` — прочитать настройки;
- `PUT /api/v1/settings/campaign` — проверить и сохранить настройки;
- `POST /api/v1/campaign/tick` — обработать текущий слот расписания.
- `GET /api/v1/activations/status` — настройки, baseline/latest snapshot и очередь.

Документация схем API доступна в `/docs` после запуска сервера.

## Telegram-бот управления

Бот даёт администратору меню для списка аккаунтов, отправки текста или JPEG,
истории по выбранному аккаунту, шаблонов, промпта и расписания.
История и статусы показывают время по Москве (`Europe/Moscow`). Расписание
обрабатывается автоматически, пока запущен процесс бота.

Создайте отдельного Telegram-бота через `@BotFather` и заполните его токен,
`CONTROL_ADMIN_IDS` и остальные значения в корневом `.env`. API и бот читают
один и тот же файл, поэтому не задавайте для бота повторные `export`-значения:
они имеют приоритет над `.env` и могут подменить реальные параметры.

```bash
source .venv/bin/activate
python -m research_sim.bot.app
```

Узнать свой numeric ID можно командой `/whoami`; доступ к панели будет только
у ID из `CONTROL_ADMIN_IDS`. Несколько администраторов задаются через запятую.

Добавление аккаунта в боте регистрирует ключ и подпись в API, но не загружает
секретную сессию через Telegram. Сначала положите уже авторизованный файл,
например `business.session`, в каталог сессий на сервере API, затем выполните
`/add_account` и укажите ключ `business`. Для каждого аккаунта нужен отдельный
авторизованный `.session` файл. В меню `/send` выберите аккаунт, введите текст,
а затем отправьте JPEG или нажмите `/skip`. Бот направляет сообщения только
центральному получателю из `TELEGRAM_ALLOWED_RECIPIENTS`.

`/campaign_setup` просит дату, шаблоны по одному на строку, prompt для ответов и
слоты дней 1–3 в формате `10:00 | 10:00,15:00 | 10:00`. Доступны переменные
`{date}`, `{day}`, `{slot}`. Конфигурация сохраняется выключенной. Плановые
сообщения включаются `/campaign_enable`, автоответы отдельно включаются
`/auto_reply_enable`; остановить автоответы можно `/auto_reply_disable`.
Для генерации ответов API использует `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`.
LLM получает prompt и до 12 последних реплик именно этой sender-сессии. Новые
личные сообщения принимаются только от центрального allowlisted аккаунта.

Для настройки индивидуальной манеры выбранного sender-аккаунта используй
`/persona_setup`: введи описание, затем проценты точности слов и пунктуации.
Команда `/dialogue_preview` попросит выбрать профиль, задать исследовательский
контекст и ввести пять фиксированных ответов центрального тестового аккаунта.
AI предложит пять реплик sender-профиля, чередующихся с этими ответами.
Результат сохранится в БД как preview и будет показан в боте; preview сам по
себе ничего не отправляет. Позже сохранённые варианты можно открыть через
`/dialogues`. Настройки профилей доступны и через
`/api/v1/accounts/{account_key}/persona`, а preview создаётся через
`POST /api/v1/research/dialogues/propose`.

## Тесты

```bash
python -m unittest discover -s tests -v
```

Тесты используют подменённый Telethon-клиент и не обращаются к Telegram.
