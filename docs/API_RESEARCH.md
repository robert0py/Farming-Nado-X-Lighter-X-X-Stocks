# Проверка интеграций — 8 октября 2026

Этот документ сохраняет результаты исследования этапа чтения балансов. В версии 0.3.0 торговые адаптеры и отдельный worker реализованы; актуальные сведения об авторизации и исполнении находятся в [TRADING.md](TRADING.md). Таблица ниже — исторический снимок, а не конфигурация торгового движка.

Проверены документация обеих площадок, OpenAPI Lighter Robinhood и живые публичные endpoints. Это снимок доступности, а не постоянный список разрешённых торговых инструментов.

## Nado

Официальная [интеграция xStocks Spot Trading](https://docs.nado.xyz/developer-resources/api/xstocks-spot-trading) описывает wrapped spot xStocks. У wrapped-токена и базовой акции разная единица количества: эквивалент xStock = количество wrapped-токенов × exchange rate. Для будущего хеджа нужны также актуальные multiplier, корпоративные действия и правила единиц Lighter.

[V2 Symbols](https://docs.nado.xyz/developer-resources/api/v2/symbols) предоставляет `type`, `product_id`, `trading_status`, ограничения размера/цены, часы рынка и `exchange_rate_x18`. Наличие последнего подтверждает тип продукта xStocks spot, но само по себе не гарантирует начисление points конкретному аккаунту.

Новые интеграции используют [unified endpoints](https://docs.nado.xyz/developer-resources/api/endpoints): `https://api.prod.nado.xyz/gateway/v1` и `https://api.prod.nado.xyz/archive/v2`. [Subaccount Info](https://docs.nado.xyz/developer-resources/api/gateway/queries/subaccount-info) читается без подписи через GET с bytes32-субаккаунтом (20 байт адреса + 12 байт имени). `exists=false` означает отсутствие субаккаунта. Spot-суммы и health имеют масштаб 10¹⁸. USDT0 имеет product_id=0; archive symbols не включает этот quote-продукт.

Живой gateway требует `Accept-Encoding` с поддержкой сжатия; приложение отправляет gzip и распаковывает ответ. Без этого заголовка gateway вернул HTTP 403, даже когда archive отвечал успешно.

[Правила xPoints на Nado](https://docs.nado.xyz/incentives-and-rewards/xpoints-on-nado) описывают удержание и торговлю xStocks, с ежедневным расчётом и меняющимися quests. До торговли нужно повторно подтвердить действующую программу, регистрацию/eligibility аккаунта и соответствие конкретного токена. Perpetual Nado не является допустимой стороной будущей стратегии.

## Найденные соответствия рынков

Получены `GET /archive/v2/symbols?product_type=spot` у Nado и `GET /api/v1/orderBookDetails` у Lighter RH. Символы сопоставлены как кандидаты по базовой акции; экономическая эквивалентность ещё требует проверки multiplier/контракта.

| Базовый актив | Nado spot / id | Nado status | Lighter perp / id | Lighter status |
| --- | --- | --- | --- | --- |
| AAPL | wAAPLx / 143 | live | AAPL / 10 | active |
| GOOGL | wGOOGLx / 147 | live | GOOGL / 12 | active |
| META | wMETAx / 149 | live | META / 13 | active |
| QQQ | wQQQx / 115 | live | QQQ / 25 | active |
| TSLA | wTSLAx / 155 | live | TSLA / 16 | active |
| NVDA | wNVDAx / 153 | live | NVDA / 15 | active |
| SPY | wSPYx / 117 | live | SPY / 26 | active |
| AMZN | wAMZNx / 145 | **not_tradable** | AMZN / 11 | active |
| MSFT | wMSFTx / 151 | **not_tradable** | MSFT / 14 | active |

У найденных Nado xStocks текущий `min_size` — 100 USDT0, размер/цена ограничены индивидуальными increments. У перечисленных Lighter perps `min_quote_amount` — 10 USDG и есть собственные минимальные base amounts/precision. Эти значения надо читать заново перед каждой сделкой. Проверка статуса не подтверждает глубину стакана, достаточность маржи или возможность исполнения конкретного объёма.

## Lighter Robinhood Chain

[Get Started](https://apidocs.rh.lighter.xyz/docs/get-started) и [Robinhood Chain Lighter Domains](https://docs.robinhood.com/chain/lighter-domains/) подтверждают отдельную инстанцию с API `https://api.rh.lighter.xyz/`. Это не Lighter Core: другой набор аккаунтов, рынков и ликвидности. Signing chain ID — 466324, L1 Robinhood Chain ID — 4663; их нельзя подменять друг другом. Здесь signer не реализован.

[OpenAPI](https://apidocs.rh.lighter.xyz/openapi/api.json) описывает публичные GET `accountsByL1Address` и `account?by=index&value=...`. Вход/ключ не требуются для текущих полей баланса. Пагинация списка субаккаунтов учитывается; при нескольких аккаунтах выбор делается явно. API-суммы Lighter — десятичные строки без x18-масштабирования. Приложение отдельно отображает `balance`, `locked_balance` и `margin_balance`.

На живом публичном примере подтверждено: общий ответ имеет `code=200`, вложенный аккаунт `code=0`, хотя OpenAPI показывает пример 200 для обоих. Адаптер принимает только эти коды успеха и сверяет index/адрес. Проверено чтение баланса и поиск трёх субаккаунтов публичного примера. Балансы пользователя не проверялись: его адреса ещё не предоставлены. Для Nado проверены живые symbols и корректная обработка отсутствующего аккаунта; успешный ненулевой аккаунт проверяется синтетическими тестами.

Документация поддерживает ордер perpetual с `is_ask=True`, что позволяет открывать short при достаточной марже и разрешённом состоянии рынка. Продажа имеющегося spot-токена Lighter не заменяет short. Реальный short не выполнялся и разрешения/маржа конкретного аккаунта пока не проверены.

## Ограничения и будущая проверка исполнения

[Nado rate limits](https://docs.nado.xyz/developer-resources/api/rate-limits): IP query bucket 2400 weight/min; обычный subaccount_info имеет weight 2. [Lighter RH rate limits](https://apidocs.rh.lighter.xyz/docs/rate-limits): standard — 60 requests/min; при превышении возможны HTTP 429 и 405, firewall cooldown 60 секунд. Текущий viewer обновляется вручную; транспорт ограничивает повторы и соблюдает cooldown.

Lighter trading требует API signer, per-key nonce и параметров precision. Стандартный auth token ограничен 8 часами; он не подходит как бесконечная серверная сессия без обновления. Keys/fees/quota зависят от типа аккаунта и должны быть изучены заново на этапе торговли: [API Keys](https://apidocs.rh.lighter.xyz/docs/api-keys), [Account Types](https://apidocs.rh.lighter.xyz/docs/account-types), [Volume Quota](https://apidocs.rh.lighter.xyz/docs/volume-quota).

Общий план должен учитывать basis между venues, USDG/USDT0, funding, комиссии, wrapper multiplier, market hours и независимые ликвидации. API не предоставляет атомарную сделку между двумя площадками. Вне сессии Nado может запретить новое открытие; круглосуточный сервер должен ожидать доступного рынка, а не обещать круглосуточное исполнение.
