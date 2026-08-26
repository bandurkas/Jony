# Jony — execution-модуль (Track B, шаг 1: архитектура)

Цель: `JONY_TRADING_MODE=live` торгует реальными ордерами на Bybit (UTA, USDT-опционы),
paper-путь остаётся байт-в-байт прежним. Решения пользователя 26.08: mid-лимитки с
ретритом (90 с), частичный филл = оставляем налитое, SL = min(−175% кредита, −2.5% equity),
капитал $1500 USDT, shadow на mainnet с $150 (ETH 0.1 лота).

## 1. Принципы
- **Единый writer** — только loop пишет в БД и шлёт ордера. API/advisor как раньше.
- **Асинхронная машина состояний**, не блокирующие ожидания: loop тикает раз в 5 с;
  ордер живёт в таблице `orders` и продвигается на каждом тике. Ожидание mid 90 с и
  ретрит не задерживают exits/гейты других монет.
- **Fail-closed**: live без ключа/при ошибке reconcile → `exec_halt` (новые входы
  блокируются, выходы работают, TG-алерт). Ничего не «додумываем» за биржу: позиция в БД
  появляется только по факту филла с реальной ценой/комиссией.
- **Один слой абстракции**: `services/execution.py` → `PaperExecutor` / `LiveExecutor`
  с одинаковым интерфейсом; loop не знает про pybit.

## 2. Интерфейс Executor
```
place_open(intent)  -> order_id          # sell-to-open limit @mid (tick-rounded)
place_close(intent) -> order_id          # buy-to-close limit @mid; urgent → @ask
amend_price(intent, price) -> bool
cancel(intent) -> bool
poll(intent) -> {status, filled_qty, avg_price, fee}   # order/realtime + execution/list
exchange_positions() -> {symbol: {size, avg_price, position_im}}   # reconcile
```
PaperExecutor: place_* «наливает» мгновенно по прежним правилам (bid / ask, mark-fallback,
spread-guard) — poll сразу Filled. Так paper проходит ту же машину состояний и тестируется
тем же кодом.

## 3. Машина состояний ордера (`orders`)
```
NEW(mid) --90с не налит--> RETREAT(bid|ask) --60с--> CANCEL → partial? finalize : no_fill
   |                              |
   +---- Filled/Partial+timeout --+--> FINALIZE
urgent close (sl/trail/lockdown/expiry): сразу @ask, 30с → cancel+replace @ask*1.02 IOC
```
- `orders`: id, kind(open|close), pos_id, coin, side, option_symbol, qty, price, stage
  (mid|retreat|urgent|done|cancelled), order_id, order_link_id, placed_at_ms,
  filled_qty, avg_price, fee_usd, status, reason, payload, created_at_ms, updated_at_ms.
- Идемпотентность: `orderLinkId = jony-{kind}-{intent_id}` — рестарт loop не задваивает.
- Finalize open: `insert_position` с `entry_credit=avg_price`, `qty=filled_qty`,
  `fee_open_usd=execFee`, `entry_source=mid|retreat`, `margin_usd` = positionIM с биржи
  (fallback формула). Finalize close: `_close(exit_debit=avg_price, fee=execFee)`.
- Частичный филл на таймауте: cancel остатка, finalize по налитому (лоты 0.01/0.1).
- Позиция с активным close-ордером помечается `closing` (колонка `closing_order_id`),
  manage_exits её пропускает до финализации.

## 4. Изменения loop.py
- `try_fire`: после sizing → `executor.submit_open(...)`. Paper: позиция появляется на
  том же тике (poll=Filled). Live: `orders` row + TG «ORDER open …».
- `manage_exits` / `close_all_now` / `close_position_now`: вместо `_close(...)` →
  `executor.submit_close(p, reason, status, urgent, cap_price)`; `_close` вызывается
  из finalize. Spread-guard/defer логика сохраняется как расчёт цены и urgency.
- `process_orders(conn, state, now)` — новый шаг в начале каждого тика (до exits).
- `reconcile(conn, now)` — раз в минуту в live: биржевые позиции vs открытые в БД по
  symbol/qty. Расхождение → `exec_halt=1` + TG; повторная сверка каждую минуту, снятие
  halt автоматически при совпадении. Биржевая позиция без записи в БД → halt (никогда не
  «усыновляем» молча).
- Экспирация в live: Bybit сам делает settlement; когда позиция исчезла с биржи после
  expiry → `_close` по `delivery-record` (exec price/fee) — fallback intrinsic по споту.
- `sl_pct` при открытии = `min(SL_PCT, SL_EQUITY_CAP_PCT×equity/(credit×qty))`
  (env `JONY_SL_EQUITY_CAP_PCT=0.025`; 0 = выкл). Применяется в обоих режимах.
- `near-high` size mult: env `JONY_NEAR_HIGH_PCT` (0 = выкл), `JONY_NEAR_HIGH_MULT=0.5`;
  `dist_7d_high_pct` считается в loop из k1h и кладётся в ev; `size_position(size_mult=)`.
- IV-логгер: раз в 10 мин `iv_log(ts_ms, coin, spot, iv_p, iv_c, sym_p, sym_c)` из
  ATM-недельных тикеров (уже тянутся).

## 5. Advisor self-lock
`decide_entry`: posture `normal` **или** `tight` при `market_risk != "high"`; lockdown —
запрет. `process_entry_requests` в loop: запрет только на `lockdown` (breaker/CB/капы
проверяет try_fire). Advisor-позиции и так под безусловным трейлом.

## 6. Конфиг (env)
```
JONY_TRADING_MODE=paper|live      BYBIT_TESTNET=0|1
JONY_EXEC_MID_WAIT_S=90           JONY_EXEC_RETREAT_WAIT_S=60   JONY_EXEC_URGENT_WAIT_S=30
JONY_SL_EQUITY_CAP_PCT=0.025      JONY_IM_RATE (default 0.10 = бэктест; после замера реальной IM на первом shadow-филле выставить ~0.15 в .env)
JONY_NEAR_HIGH_PCT=0              JONY_NEAR_HIGH_MULT=0.5
JONY_IV_LOG_EVERY_MIN=10
```
Live стартует только если ключ есть и `query-api` отвечает readOnly=0 — иначе loop
пишет ошибку и выходит (compose перезапустит; TG-алерт).

## 7. Тесты
- `tests/test_execution.py`: FakeBybit (in-memory ордера с управляемыми филлами) → машина
  состояний: mid→retreat→cancel, partial finalize, urgent close, идемпотентность по
  orderLinkId, reconcile mismatch → halt/unhalt, settlement по delivery-record/fallback.
- Существующие 147 тестов должны пройти без изменений paper-поведения (кроме sl-cap:
  отдельный тест).
- Ревью: 2 раунда (независимые агенты), затем testnet/mainnet-shadow.

## 8. Не в этом треке
Мониторинг маржи/ликвидации (UTA cross) — первый shadow-ордер измеряет реальную IM,
после чего фиксируем `JONY_IM_RATE`. Roll/хеджи — REJECTED (RESEARCH_LOG Phase 10).
