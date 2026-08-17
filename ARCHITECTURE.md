# Jony — architecture (шаг 1 флоу, до кода)

Paper-бот мульти-активной VRP-корзины: **ETH (Put+Call) + BTC (Call-only)**,
один счёт. Стратегия зафиксирована бэктестом 2026-07-02
(`opt` repo: `backend/services/basket_premium_backtest.py`, memory:
`finding_basket_eth_btc_call_mo4`): full +126.3% / maxDD 20.0% / holdout +3.7%
/ все 4 walk-forward квартала в плюсе / ~1.05 сделки в день. Решения ниже
зафиксированы — не пересматривать без новых данных.

## Стратегия (ровно как в бэктесте)

Per-coin генератор = живой Sniper1 V2-hybrid + V3 (без изменений параметров):

- `ret_7d > +1.0%` → только Put; `< -1.0%` → только Call; иначе обе стороны,
  выбирает MTF. (0.5→1.0 2026-08-17, config C; порог валидирован для P,
  для C — нет: см. комментарий у RET_7D_THRESHOLD.)
- **PUT-гейты per-coin (config C, 2026-08-17, RESEARCH_LOG Phase 7)**:
  ETH Put vol_pctile ≥ 0.60, regime ∈ {range}; BTC Put vol_pctile ≥ 0.40,
  regime ∈ {range, transition}; оба MTF 2/3 consensus = up, cooldown 6 баров.
- **ETH Call / BTC Call**: vol_pctile ≥ 0.45, regime ∈ {range, transition,
  trend}, MTF 1h-anchor = down, bull-фильтр EMA50/200 ≤ 1.05 — «config E»,
  сейчас ВЫКЛЮЧЕНЫ (env JONY_DISABLED_KEYS=ETH:C,BTC:C, аудит 2026-08-14).
- **BTC Put — ВКЛЮЧЁН 2026-08-14** (честный v2 отменил вердикт кривого
  харнеса; единственный ключ с плюсом train+holdout на обеих сигмах).
- Вход: 5-минутное окно, 5 поминутных проверок, tol1-дебаунс (допустим 1 сбой
  из 5), файр в конце окна. Кулдаун ts-based, независимый по (coin, side).
- Выходы (бэктестовый сет, БЕЗ доллар-SL Sniper1 — он в корзине не тестирован,
  кандидат на отдельный A/B позже):
  - Put: TP2 = +70% премии, SL = −200% премии, time-stop 96ч.
  - Call: TP2 = +80% премии, SL = −75% премии, time-stop 24ч.
- Экспирация: ближайшая ≥ 168ч (недельная), страйк ATM
  (шаг ETH 25 / BTC 500 — сверить с живым чейном при деплое).

## Счёт (paper, $800)

- MARGIN_PCT_PER_TRADE 0.15, MAX_OPEN 4 (всего), **cap 3 позиции на монету**,
  портфельный лимит маржи 0.80 × equity, компаундинг.
- dyn-size ×0.5, если WR последних 10 закрытий < 40%.
- Circuit breaker: 1 убыток → пауза 8ч (глобальная, живой ретюн Sniper1).
- Маржа позиции = (0.10 × strike + premium) × qty; лот: ETH 0.1, BTC 0.01.
- Cluster-stop в v1 НЕ включаем (в бэктесте не моделировался; концентрацию
  кроют per-coin cap + CB 1/8ч). Добавить, если живой кластер покажет дыру.

## Исполнение / данные

- Bybit public v5 REST, ключи для paper НЕ нужны (клайны + опционные тикеры).
- Клайны ETHUSDT/BTCUSDT 5m/15m/1h — прямой fetch раз в минуту (без отдельного
  poller'а и Postgres: 2 монеты × 3 ТФ = 6 запросов/мин).
- Paper-филл на входе: реальный bid опциона (fallback: mark × 0.99) — как
  `_paper_fill` Тягача. Марк раз в минуту для TP/SL/time-stop.
- TP/SL по премии от фактического entry credit.

## Хранилище / сервисы (шаблон Тягача)

- SQLite WAL в docker-volume (`data/jony.db`), single-writer (loop),
  API читает своими коннектами.
- Таблицы: `positions`, `equity_snapshots`, `bot_state`, `signal_audit`
  (та же дисциплина аудита, что спасла нас в Sniper1), `bot_control` (paused).
- Контейнеры: `jony_loop` (цикл раз в минуту) + `jony_api` (FastAPI :8200 —
  /health, /state, /positions, /equity; для интеграции в opt-app рейл позже).
- Telegram-уведомления с тегом **[Jony]** (открытие/закрытие/CB/ошибки),
  токен и chat_id в `.env` (gitignored).

## Repo / deploy

- Repo `git@github.com:bandurkas/Jony.git`, локально `~/Desktop/Jony`,
  VPS3 `/root/Jony` (место освобождено от Grogu).
- `TRADING_MODE=paper` в `.env`; live-флип — только после paper-гейта
  (20–30 циклов, LIVE KIT roadmap) и отдельного ключа/аккаунта
  (помним coin-keyed мину reconcile.py).

## Флоу

архитектура (этот файл) → код → code review → тесты (unit + smoke) →
review → deploy paper на VPS3 → paper-гейт.
