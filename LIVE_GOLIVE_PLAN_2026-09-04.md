# Jony — план выхода в LIVE (Bybit), окно 3–5 сентября 2026

Статус на 2026-08-26. Источник правды по решениям — этот файл + RESEARCH_LOG_2026-08-14.md (Phase 10–10c).

## 0. Решения пользователя (зафиксировано)
- Дата: **go-live 4–5 сентября 2026**, до этого paper продолжается.
- Капитал: **$1500 USDT** (UTA; все опционы Bybit USDT-settled — проверено 26.08, USDC-контрактов 0). Причина: на $800 лотность BTC (~$110/лот при бюджете 15% = $120) режет 238 сигналов из ~4500 в бэктесте; на $1500 skip≈0. Не запускать на меньшем.
- Исполнение от mid (пассивные лимитки с ретритом к bid/ask) — часть execution-модуля.
- Корректировка советника по VRP-сигналу (IV−RV) — **позже**, после накопления IV-истории; в Track B только логгер.
- Любой деплой — строго по флоу: код → тесты → 2-раундовое ревью → деплой → live-check.

## 1. Аудит 25.08 — блокеры (не сняты)
- [x] **Bybit API-ключ** — новый установлен 26.08 (RbUF…): readOnly=0 (write-тест cancel-all ok), IP-whitelist 187.127.114.34, UTA 2.0 Pro (unifiedMarginStatus 5, REGULAR_MARGIN), бессрочный, без Withdraw; лишние Wallet: AccountTransfer/SubMemberTransfer — снять по желанию. Прописан в `/root/Jony/.env` и `/root/opt-app/.env` (бэкапы `.env.bak_2026-08-26`), контейнеры Jony пересозданы. opt-app-контейнеры не пересоздавались (архивный Boba1) — держат старый ключ в памяти до рестарта.
- [ ] **Execution-модуля нет** (bybit_client.py: «v1 has NO order-placement path»). `JONY_TRADING_MODE=live` сегодня меняет только строку в /health.
- [ ] Модель маржи `IM_RATE=0.10` занижает реальную маржу Bybit для шорт-опционов (~15% notional + premium) в ~1.5×.

## 1b. Сверка с биржей по новому ключу (26.08) — закрыто
- [x] Инструменты: BTC minQty 0.01/step 0.01/tick 5; ETH 0.1/0.1/0.1 — совпадает с `COIN_SPEC`. Все контракты USDT-settled (quote/settle USDT).
- [x] Комиссии: taker 0.0003 / maker 0.0002 — `FEE_RATE=0.0003` консервативен; лимитки от mid будут maker (0.02%).
- [x] Коллатерал UTA: USDT/USDC включены (ratio 1.0), BTC/ETH выключены → пополнение USDT работает как маржа напрямую.
- [x] Открытых ордеров/позиций по опционам на аккаунте нет; баланс $0.98 → **пополнить $1500 USDT**.
- [ ] Реальная IM для шорт-опциона — измерить на первом shadow-ордере (API формулу не отдаёт), после чего зафиксировать `IM_RATE`.

## 2. Track B — объём работ (до 31.08, ревью 2 раунда)
1. **Execution-модуль** (`services/execution.py` + ветка live в `try_fire`/`_close`/`close_all_now`/settlement):
   - sell-to-open лимиткой от **mid**, таймаут N мин → ретрит к bid; buy-to-close симметрично (mid → ask); тот же spread-guard, что в paper.
   - `cancel_order`, обработка partial/reject, идемпотентность по clientOrderId.
   - reconcile каждый тик: `position/list` ↔ таблица `positions`; расхождение → пауза + TG.
   - реальные fill/fee в БД (`entry_source=mid|bid|ask`), чтобы A/B исполнения был измерим.
   - маржа: `IM_RATE` → 0.15 или чтение реального IM из `position/list`.
   - экспирация/settlement по факту биржи.
2. **Согласовать SL и account-breaker**: SL −175% ($47 на $911) > daily breaker 2.5% ($22.8); breaker только блокирует входы. Правило: SL_usd = min(−175% кредита, −2.5% equity) или breaker force-close — решить на ревью.
3. **Advisor self-lock fix**: `decide_entry` требует `posture=='normal'`; tight из-за ITM-позиции блокирует входы по другой монете. Разрешать вход при позиционной причине tight и `market_risk != high`, ключ другой монеты.
4. **IV-логгер**: писать ATM markIv ETH/BTC (тикеры уже тянутся ежеминутно) в таблицу `iv_log` — для будущего IV−RV гейта (VPS2 хранит только ETH 18.06–09.07, коллектор мёртв).
5. **`near d=1.5 ×0.5`** как env (`JONY_NEAR_HIGH_PCT=1.5`, `JONY_NEAR_HIGH_MULT=0.5`, default off): size×0.5 для путов, открытых ≤1.5% от 7д-хая. Бэктест $1500: DD −15%, доходность ±5%; live-replay −$5.85. Включать по решению пользователя.
6. Бэклог: usage/cost советника в advice.jsonl.

## 3. Paper-shadow 31.08–03.09
- Live-контейнер с `qty = min lot` или dry-run флагом (ордера ставятся и отменяются, без филла) — проверить путь place/cancel/reconcile на реальном стакане.
- Сравнить фактические fill-цены с paper-филлами тех же сигналов (mid vs bid).
- 0 расхождений reconcile за 72ч; TG-алерты приходят.

## 4. Чек-лист перепроверки ПЕРЕД кнопкой (3–4 сентября)
Инфра
- [ ] VPS3: `git status` чистый, HEAD == origin/main, образы пересобраны, `docker compose ps` — loop/api/advisor Up.
- [ ] `.env`: `JONY_TRADING_MODE=live`, новый ключ, `JONY_START_EQUITY_USD=1500`, `JONY_ADVISOR_ONLY_KEYS=ETH:C,BTC:C`, бэкап `.env.bak_<date>`.
- [ ] Ключ: `query-api` ok (проверено 26.08), `wallet-balance` ≥ $1500 USDT.
- [ ] NTP синхронизирован; pybit 5.7.0; recv_window.
Стратегия/риск
- [ ] Коллы механически выключены (advisor-only), CALL-guard'ы включены, kill-switch коллов активен.
- [ ] per_key_cap=1, MAX_OPEN 10 / PER_COIN_CAP 6, PORT_MARGIN_CAP 0.80 — сверить с реальной маржой на $1500 (≈ 2 BTC-лота + ETH).
- [ ] SL/breaker согласованы (п.2.2), account-breaker daily 2.5% / weekly 5% / streak 3.
- [ ] Trail в tight (20%/10пп), spread-guard выхода (10%/5%/10 мин), wake-cooldown 30 мин — на месте.
- [ ] `near d=1.5×0.5` — включён/выключен по решению; проверить env читается.
- [ ] Advisor: backend cli, OAuth-токен валиден, `ADVISOR_EXECUTE=profit_only`, self-lock fix задеплоен, тик проходит, TG приходит.
- [ ] **Решение (02.09, открыто): `ADVISOR_ENTRIES` на live.** Рекомендация — `off` (override-путы REJECTED Phase 14, коллы n=3, A/B продолжает paper); если `on` — только с Track B 02.09 (override запрещён кодом) и `JONY_ADVISOR_STALE_H=3`.
Paper-выход
- [ ] Открытые paper-позиции закрыты или осознанно перенесены (в live их нет — стартуем с пустой книгой).
- [ ] Paper-итог зафиксирован (на 26.08: $902–911, 72 закрытых, WR 65%, maxDD 4.04%).
Первые сутки live
- [ ] Первая сделка — руками проверить fill/fee/маржу против Bybit UI; reconcile 0 расхождений.
- [ ] Через 24ч: сверка equity Jony ↔ wallet-balance; отложенных выходов 0; TG-лог полный.

## 5. Что НЕ трогаем (исследовано и отвергнуто, см. RESEARCH_LOG Phase 10–10c)
Хеджи (крыло/дельта/уровневый), структурный стоп, roll-down, DD-scale, фильтры regime/vol, кросс-монетный кап, hard-filter у хая, OTM-страйк, тенор, микро-TP, вторая нога. Стратегия в своём классе параметров выработана; резерв — исполнение, капитал, советник, IV-гейт (после данных).

## 6. Открытые вопросы к пользователю
- Включать ли `near d=1.5×0.5` на старте (страховка ±0) или держать выключенным, пока рынок бычий.
- Правило согласования SL/breaker (п.2.2): резать SL до −2.5% equity или force-close по breaker.
- ~~API-ключ~~ — сделано 26.08.

## 9. Runbook: shadow-прогон на mainnet (после ревью Track B, $150 USDT на UTA)
Цель: проверить place/amend/cancel/fill/reconcile/settlement на реальном стакане минимальным лотом.
1. Деплой кода Track B на VPS3 (после 2 раундов ревью и «ок» пользователя): `git pull && docker compose build && up -d --force-recreate` — **в paper-режиме**; убедиться, что paper-книга не изменилась (равенство equity/позиций до и после).
2. Отдельный compose-проект `jony-shadow` (свой volume, порт 8201) с `.env`: `JONY_TRADING_MODE=live`, `JONY_START_EQUITY_USD=150`, `JONY_MAX_OPEN=1`, `JONY_PER_COIN_CAP=1`, `JONY_MARGIN_PCT=0.5` (чтобы 1 лот ETH влезал), коллы advisor-only, `ADVISOR_ENTRIES=off` (shadow — только механика). Сеть: тот же ключ RbUF (IP-whitelist VPS3).
3. Первый ордер — вручную через API `POST /entry_test` (или дождаться сигнала ETH:P): проверить в Bybit UI: лимитка на mid, ретрит через 90 с, филл, `positions.exchange_im_usd` ≠ NULL → записать реальную IM → `JONY_IM_RATE`.
4. Закрытие: дождаться механики или `POST /close_position/{id}` → проверить urgent-путь (ask, chase +2%), реальную комиссию (`fee_usd` в orders), pnl в `positions`.
5. Reconcile: 24 ч без `EXEC HALT` в TG; искусственная проверка — руками открыть 0.1 ETH опцион в UI → бот должен встать в halt в течение минуты, закрыть руками → halt снят.
6. Экспирация: одна позиция дожить до пятницы 08:00 UTC → `expiry_settle` по delivery record.
7. Критерии перехода на $1500: 0 расхождений reconcile, все ордера в `orders` со статусами filled/no_fill (нет error/active-зависших), логи без traceback, TG-поток полный.
