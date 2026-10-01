# Scalp Screener

Binance Futures: тиковые события (крупные принты, всплески) и плотности стакана с ранжированием
по поведению цены (держалась / съедена / снята без касания = "всратая").

Дашборд: `/` (параметр `?token=` если задан `AUTH_TOKEN`). JSON: `/api/state`. WS: `/ws`.

## Переменные окружения (все необязательные)
`SYMBOLS_N`(20) `DEPTH_EVERY_S`(10) `DEPTH_LIMIT`(500) `DENSITY_MIN_USD`(300000) `DENSITY_MULT`(8)
`DENSITY_MAX_DIST_PCT`(3) `TICK_BIG_USD`(250000) `TICK_BURST_USD`(400000)
`BOT_TOKEN` + `ALERT_CHAT_ID` (Telegram-алёрты) `ALERT_MIN_SCORE`(3.0) `AUTH_TOKEN`
`BINANCE_REST` / `BINANCE_WS` — если регион Railway блокируется Binance, можно подменить хост.

Тесты: `python -m pytest tests`. Root directory сервиса на Railway: `screener`.
