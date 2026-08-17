"""Entry-gate sweep for the puts-only config (2026-08-17).

Контекст: после аудита 2026-08-14 бот торгует только P (ETH:P probation,
BTC:P star). Трендовый селектор сторон блокирует P при ret_7d < -0.5% —
с 2026-08-15 у бота 0 допустимых сторон (рынок в лёгком даунтренде).
Задача: найти макс. частоту входов без деградации робастности.

Оси (только PUT-сторона; mtf инертен — не трогаем; коллы убиты — не трогаем):
  ret7d_thr      in {0.5 (live), 1.0, 1.5, 2.0, 3.0, 999=off}
  vol_threshold  in {0.30, 0.40, 0.50 (live), 0.60}
  regime_filter  in {R, RT (live), RTT}

Планка отбора (зафиксирована ДО прогона):
  n_trades > baseline И portfolio replay_v2(pkc=1) train>0 И holdout>0
  на ОБЕИХ сигмах, кварталы (fixed-lot) не хуже baseline.

Готча движка: sides_code печётся в build_coin_base по jc.RET_7D_THRESHOLD
(memoized) — evict _BASE_CACHE до И после патча константы.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
from replay_account_v2 import replay_v2


def fixed_lot_pnl(t: dict) -> float:
    # копия perkey_decomp_2026-08-14.py::fixed_lot_pnl (файл неимпортируем: дефис в имени)
    qty = t["lot"]
    notional = t["strike"] * qty
    premium_total = t["entry_credit"] * qty
    fee_open = jc.fee_usd(notional, premium_total)
    exit_credit = t["entry_credit"] * (1 - t["pnl_pct"])
    fee_close = jc.fee_usd(notional, exit_credit * qty)
    return (t["entry_credit"] - exit_credit) * qty - fee_open - fee_close

CALIB = {"b0": 0.3487, "b1": 0.2646, "floor": 0.25, "ceiling": 1.05}
SIGMAS = [("CLAMP", None), ("CALIB", CALIB)]
RET7D_GRID = [0.5, 1.0, 1.5, 2.0, 3.0, 999.0]
VOL_GRID = [0.30, 0.40, 0.50, 0.60]
REGIME_GRID = {
    "R": ("range",),
    "RT": ("range", "transition"),
    "RTT": ("range", "transition", "trend"),
}
MO, CAP, PKC = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP, 1


def put_gen_for(vol_thr: float, regime_key: str) -> dict:
    g = dict(jc.PUT_GEN)
    g["vol_threshold"] = vol_thr
    g["regime_filter"] = REGIME_GRID[regime_key]
    return g


def evict_base_cache() -> None:
    je._BASE_CACHE.pop("ETH", None)
    je._BASE_CACHE.pop("BTC", None)


def q_stats(trades: list[dict]) -> tuple[int, int, list[float]]:
    qs = je.quarters(trades)
    sums = [sum(fixed_lot_pnl(t) for t in q) for q in qs if q]
    return sum(1 for s in sums if s > 0), len(sums), [round(s) for s in sums]


def run_variant(ret7d: float, vol_thr: float, regime_key: str) -> dict:
    row: dict = {"ret7d": ret7d, "vol": vol_thr, "regime": regime_key}
    for sig_label, calib in SIGMAS:
        trades = (je.coin_trades("ETH", sides_enabled=("P",),
                                 put_gen=put_gen_for(vol_thr, regime_key),
                                 sigma_calib=calib)
                  + je.coin_trades("BTC", sides_enabled=("P",),
                                   put_gen=put_gen_for(vol_thr, regime_key),
                                   sigma_calib=calib))
        trades.sort(key=lambda t: t["entry_ts"])
        tr, ho = je.split(trades, 0.70)
        days = ((trades[-1]["entry_ts"] - trades[0]["entry_ts"]) / 86_400_000
                if len(trades) > 1 else 0)
        rep_tr = replay_v2(tr, MO, CAP, per_key_cap=PKC)
        rep_ho = replay_v2(ho, MO, CAP, per_key_cap=PKC)
        qpos, qn, qsums = q_stats(trades)
        row[sig_label] = {
            "n": len(trades), "n_tr": len(tr), "n_ho": len(ho),
            "per_day": round(len(trades) / days, 3) if days else 0.0,
            "fx_tr": round(sum(fixed_lot_pnl(t) for t in tr), 1),
            "fx_ho": round(sum(fixed_lot_pnl(t) for t in ho), 1),
            "rep_tr_ret": round(rep_tr["return_pct"], 1), "rep_tr_dd": round(rep_tr["max_dd"] * 100, 1),
            "rep_ho_ret": round(rep_ho["return_pct"], 1), "rep_ho_dd": round(rep_ho["max_dd"] * 100, 1),
            "q_pos": qpos, "q_n": qn, "q_sums": qsums,
        }
    return row


def run_ret7d_group(ret7d: float) -> list[dict]:
    # один воркер = одна группа ret7d: build_coin_base кэшируется внутри
    # процесса, 12 вариантов группы переиспользуют его
    jc.RET_7D_THRESHOLD = ret7d
    evict_base_cache()
    rows = []
    for vol_thr in VOL_GRID:
        for regime_key in REGIME_GRID:
            row = run_variant(ret7d, vol_thr, regime_key)
            rows.append(row)
            c = row["CALIB"]
            print(f"ret7d={ret7d:>5} vol={vol_thr} reg={regime_key:3s} | "
                  f"CALIB n={c['n']:4d} ({c['per_day']:.2f}/d) "
                  f"fx tr/ho {c['fx_tr']:+9.0f}/{c['fx_ho']:+8.0f} "
                  f"rep tr/ho {c['rep_tr_ret']:+6.1f}%/{c['rep_ho_ret']:+6.1f}% "
                  f"q {c['q_pos']}/{c['q_n']}", flush=True)
    return rows


def main() -> None:
    import multiprocessing as mp
    out_path = Path(__file__).parent / "results" / "entry_gate_sweep_2026-08-17.json"
    out_path.parent.mkdir(exist_ok=True)
    with mp.get_context("spawn").Pool(len(RET7D_GRID)) as pool:
        groups = pool.map(run_ret7d_group, RET7D_GRID)
    rows = [r for g in groups for r in g]
    out_path.write_text(json.dumps(rows, indent=1))
    print(f"\nsaved {len(rows)} rows -> {out_path}")


if __name__ == "__main__":
    main()
