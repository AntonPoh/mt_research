# Scalp Screener

Binance Futures: тиковые события (крупные принты, всплески) и плотности стакана с ранжированием
по поведению цены (держалась / съедена / снята без касания = "всратая").

Модули: `density.py` (плотности и их качество), `ticks.py` (принты/всплески), `algos.py` (TWAP и периодические серии),
`hyperliquid.py` (поток сделок с адресами), `static/index.html` (дашборд + интеграция с MetaScalp).

Алго-сканер: на Hyperliquid серии группируются по кошельку (taker) и монете; интервал ~30с и равные слайсы = TWAP.
На Binance адресов нет, ловятся повторяющиеся принты одинакового размера. `impact_bps` — движение цены в сторону
алго с начала серии; строки без эффекта скрываются в UI.

MetaScalp: дашборд сам находит локальный API (127.0.0.1:17845–17855) и вызывает `POST /api/change-ticker`
(`TickerPattern` + `Binding`) и `POST /api/notifications`. Автооткрытие включается галочками в панели.

Дашборд: `/` (параметр `?token=` если задан `AUTH_TOKEN`). JSON: `/api/state`. WS: `/ws`.

## Переменные окружения (все необязательные)
`SYMBOLS_N`(20) `DEPTH_EVERY_S`(10) `DEPTH_LIMIT`(500) `DENSITY_MIN_USD`(300000) `DENSITY_MULT`(8)
`DENSITY_MAX_DIST_PCT`(2) `TICK_BIG_USD`(250000) `TICK_BURST_USD`(400000)
`DENSITY_VOL_FRAC`(0.001) `DENSITY_FLOOR_USD`(150000) `HL_COINS_N`(40) `HL_TWAP_ALERT_USD`(100000) `ALGO_MIN_PRINT_USD`(5000)
`BOT_TOKEN` + `ALERT_CHAT_ID` (Telegram-алёрты) `ALERT_MIN_SCORE`(3.0) `AUTH_TOKEN`
`BINANCE_REST` / `BINANCE_WS` — если регион Railway блокируется Binance, можно подменить хост.

Тесты: `python -m pytest tests`. Root directory сервиса на Railway: `screener`.
