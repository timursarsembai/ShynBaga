# ShynBaga

Независимый бесплатный монитор истории цен на маркетплейсах. Название значит «честная цена». Проект не связан с Kaspi.kz и другими площадками.

Сейчас это сборщик данных: он хранит историю цен по каждому продавцу. Сайт появится, когда накопятся данные.

## Запуск

```bash
cp .env.example .env        # задать пароль и SHYNBAGA_PROXY_URL
docker compose up -d --build   # база и сборщик: обход сразу, затем каждые 6 часов от начала прошлого
docker compose logs -f collector
```

Проверить доступ двумя запросами, не трогая базу:

```bash
docker run --rm --network host --env-file .env shynbaga check
```

Тесты (нужна пустая база `shynbaga_test`, они её стирают):

```bash
docker build -t shynbaga . && docker run --rm --network host --env-file .env --entrypoint python shynbaga -m pytest -q
```

История товара по ссылке с маркетплейса. Если товара ещё нет в базе, он добавится и останется на отслеживании:

```bash
docker run --rm --network host --env-file .env shynbaga lookup 'https://kaspi.kz/shop/p/...'
```

## Устройство

- `marketplaces/` — по одному адаптеру на площадку за протоколом `Marketplace`. Новая площадка не требует менять схему.
- `price_intervals` — одна строка на отрезок неизменной цены продавца в регионе. Перерыв в наблюдениях больше 48 часов закрывает отрезок: мы не знаем, что было в это время.
- `crawl_runs` — журнал запусков, включая объём полученных данных для оценки расходов на прокси.

## Правила сбора

- Один запрос раз в 10 секунд, остановка после трёх отказов подряд.
- Только публичные данные. Без аккаунтов и обхода капчи.

## Уведомления

О блокировке, падении или неполном обходе сборщик пишет в Telegram (`SHYNBAGA_TELEGRAM_BOT_TOKEN`, `SHYNBAGA_TELEGRAM_CHAT_ID`). Проверка: `docker run --rm --network host --env-file .env shynbaga test-alert`.

Описание API Каспи взято из [market-sources](https://github.com/octrow/market-sources) (MIT).
