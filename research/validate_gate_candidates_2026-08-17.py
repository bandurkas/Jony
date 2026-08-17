"""Combined-portfolio validation of entry-gate candidates (2026-08-17).

Кандидаты из per-coin sweep (sweep_entry_gates_percoin.py):
  BTC:P  ret7d=1.0 vol=0.40 RT   (лучший баланс частота/качество, q3/4 обе сигмы)
  ETH:P  freq-pick  ret7d=2.0 vol=0.40 R
  ETH:P  safe-pick  ret7d=1.0 vol=0.60 R

Сравниваем 4 книги на полном портфельном replay_v2 (pkc=1, обе сигмы,
кварталы): live baseline / BTC-new+ETH-freq / BTC-new+ETH-safe / BTC-new only.
Замечание: ret7d разный per-coin -> в live это правка RET_7D_THRESHOLD на
per-coin словарь (сейчас один глобальный const) — небольшой код-чейндж.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
from replay_account_v2 import replay_v2

CALIB = {"b0": 0.3487, "b1": 0.2646, "floor": 0.25, "ceiling": 1.05}
MO, CAP, PKC = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP, 1

jc.COIN_SIDES = {"ETH": ("P", "C"), "BTC": ("C", "P")}

LIVE_PUT = dict(jc.PUT_GEN)  # vol 0.50, RT

def gen(vol, regimes):
    g = dict(jc.PUT_GEN)
    g["vol_threshold"] = vol
    g["regime_filter"] = regimes
    return g

BTC_NEW = {"ret7d": 1.0, "gen": gen(0.40, ("range", "transition"))}
ETH_FREQ = {"ret7d": 2.0, "gen": gen(0.40, ("range",))}
ETH_SAFE = {"ret7d": 1.0, "gen": gen(0.60, ("range",))}
LIVE = {"ret7d": 0.5, "gen": LIVE_PUT}

BOOKS = {
    "A live-baseline (ETH+BTC live-гейты)": {"ETH": LIVE, "BTC": LIVE},
    "B BTC-new + ETH-freq": {"ETH": ETH_FREQ, "BTC": BTC_NEW},
    "C BTC-new + ETH-safe": {"ETH": ETH_SAFE, "BTC": BTC_NEW},
    "D BTC-new only": {"BTC": BTC_NEW},
}


def coin_put_trades(coin: str, spec: dict, calib) -> list[dict]:
    jc.RET_7D_THRESHOLD = spec["ret7d"]
    je._BASE_CACHE.pop(coin, None)
    trades = je.coin_trades(coin, sides_enabled=("P",), put_gen=spec["gen"],
                            sigma_calib=calib)
    je._BASE_CACHE.pop(coin, None)
    return trades


def fixed_lot_pnl(t: dict) -> float:
    qty = t["lot"]
    notional = t["strike"] * qty
    premium_total = t["entry_credit"] * qty
    fee_open = jc.fee_usd(notional, premium_total)
    exit_credit = t["entry_credit"] * (1 - t["pnl_pct"])
    fee_close = jc.fee_usd(notional, exit_credit * qty)
    return (t["entry_credit"] - exit_credit) * qty - fee_open - fee_close


def main() -> None:
    for book_label, spec_by_coin in BOOKS.items():
        print(f"\n{'='*74}\n### {book_label}\n{'='*74}")
        for sig_label, calib in (("CLAMP", None), ("CALIB", CALIB)):
            trades = []
            for coin, spec in spec_by_coin.items():
                trades += coin_put_trades(coin, spec, calib)
            trades.sort(key=lambda t: t["entry_ts"])
            tr, ho = je.split(trades, 0.70)
            days = (trades[-1]["entry_ts"] - trades[0]["entry_ts"]) / 86_400_000
            rep_tr = replay_v2(tr, MO, CAP, per_key_cap=PKC)
            rep_ho = replay_v2(ho, MO, CAP, per_key_cap=PKC)
            rep_all = replay_v2(trades, MO, CAP, per_key_cap=PKC)
            qs = je.quarters(trades)
            qsums = [round(sum(fixed_lot_pnl(t) for t in q)) for q in qs if q]
            taken_day = rep_all["n_taken"] / days
            print(f"{sig_label}: signals={len(trades)} taken={rep_all['n_taken']} "
                  f"({taken_day:.2f} сделок/день после caps)")
            print(f"   replay train {rep_tr['return_pct']:+7.1f}% dd {rep_tr['max_dd']:.1f}% | "
                  f"holdout {rep_ho['return_pct']:+7.1f}% dd {rep_ho['max_dd']:.1f}%")
            print(f"   кварталы fixed-lot: {qsums} -> {sum(1 for s in qsums if s>0)}/4 плюс")


if __name__ == "__main__":
    main()
