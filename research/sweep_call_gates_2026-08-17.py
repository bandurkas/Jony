"""CALL-side gate sweep (2026-08-17) — запрос пользователя «вернуться в рынок».

Контекст: config C (2026-08-17) деплоен puts-only; RET_7D_THRESHOLD=1.0
валидирован ТОЛЬКО для путов, CALL-зона расширена без бэктеста (warning в
core/strategy.py). Live-история коллов: 2/3 потока сделок, −$36 суммарно —
под СТАРЫМИ гейтами. Вопрос: существует ли CALL-конфиг, проходящий тот же
пре-регистрированный бар, что и Phase 7 для путов (fixed-lot train>0 И
holdout>0 на ОБЕИХ сигмах; replay pkc=1 в плюс; кварталы преимущественно
положительные)?

Семантика V2-селектора: C разрешён при ret_7d < +T (band + даунтренд-зона);
в текущем рынке (ret_7d ≈ −2.5%) именно коллы — активная сторона.
Грид: ret7d × vol_threshold × regime × bull_max, MTF коллов зашит в engine
как dir1h=="down" (live-паритет). Exit-параметры live (tp2 .70/sl .75/24h).
Запуск: SWEEP_COIN=ETH|BTC python3 sweep_call_gates_2026-08-17.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je
from replay_account_v2 import replay_v2

COIN = os.environ.get("SWEEP_COIN", "ETH")
SIGMAS = [("CLAMP", None), ("CALIB", jc.CALIB)]
RET7D_GRID = [0.5, 1.0, 1.5, 2.0, 999.0]
VOL_GRID = [0.30, 0.45, 0.60]
REGIME_GRID = {
    "R": ("range",),
    "RT": ("range", "transition"),
    "RTT": ("range", "transition", "trend"),
}
BULL_GRID = {"b105": 1.05, "boff": None}
MO, CAP, PKC = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP, 1


def call_gen_for(vol_thr: float, regime_key: str, bull_key: str) -> dict:
    g = dict(jc.CALL_GEN)
    g["vol_threshold"] = vol_thr
    g["regime_filter"] = REGIME_GRID[regime_key]
    g["bull_market_ratio_max"] = BULL_GRID[bull_key]
    return g


def q_stats(trades: list[dict]) -> tuple[int, int, list[float]]:
    qs = je.quarters(trades)
    sums = [sum(jc.fixed_lot_pnl(t) for t in q) for q in qs if q]
    return sum(1 for s in sums if s > 0), len(sums), [round(s) for s in sums]


def run_variant(ret7d: float, vol_thr: float, regime_key: str,
                bull_key: str) -> dict:
    row: dict = {"coin": COIN, "ret7d": ret7d, "vol": vol_thr,
                 "regime": regime_key, "bull": bull_key}
    for sig_label, calib in SIGMAS:
        trades = je.coin_trades(COIN, sides_enabled=("C",),
                                call_gen=call_gen_for(vol_thr, regime_key, bull_key),
                                sigma_calib=calib)
        trades.sort(key=lambda t: t["entry_ts"])
        if not trades:
            row[sig_label] = {"n": 0}
            continue
        tr, ho = je.split(trades, 0.70)
        days = ((trades[-1]["entry_ts"] - trades[0]["entry_ts"]) / 86_400_000
                if len(trades) > 1 else 0)
        rep_tr = replay_v2(tr, MO, CAP, per_key_cap=PKC)
        rep_ho = replay_v2(ho, MO, CAP, per_key_cap=PKC)
        qpos, qn, qsums = q_stats(trades)
        row[sig_label] = {
            "n": len(trades), "n_tr": len(tr), "n_ho": len(ho),
            "per_day": round(len(trades) / days, 3) if days else 0.0,
            "fx_tr": round(sum(jc.fixed_lot_pnl(t) for t in tr), 1),
            "fx_ho": round(sum(jc.fixed_lot_pnl(t) for t in ho), 1),
            "rep_tr_ret": round(rep_tr["return_pct"], 1), "rep_tr_dd": round(rep_tr["max_dd"], 1),
            "rep_ho_ret": round(rep_ho["return_pct"], 1), "rep_ho_dd": round(rep_ho["max_dd"], 1),
            "q_pos": qpos, "q_n": qn, "q_sums": qsums,
        }
    return row


def run_ret7d_group(ret7d: float) -> list[dict]:
    # live-паритет COIN_SIDES + патч порога; _BASE_CACHE обязан переехать
    # (allowed_C печётся в build_coin_base от jc.RET_7D_THRESHOLD)
    jc.COIN_SIDES = {"ETH": ("P", "C"), "BTC": ("C", "P")}
    jc.RET_7D_THRESHOLD = ret7d
    je._BASE_CACHE.pop("ETH", None)
    je._BASE_CACHE.pop("BTC", None)
    rows = []
    for vol_thr in VOL_GRID:
        for regime_key in REGIME_GRID:
            for bull_key in BULL_GRID:
                row = run_variant(ret7d, vol_thr, regime_key, bull_key)
                rows.append(row)
                c = row["CALIB"]
                if c.get("n"):
                    print(f"{COIN}:C ret7d={ret7d:>5} vol={vol_thr} reg={regime_key:3s} "
                          f"{bull_key} | CALIB n={c['n']:4d} ({c['per_day']:.2f}/d) "
                          f"fx tr/ho {c['fx_tr']:+8.0f}/{c['fx_ho']:+7.0f} "
                          f"rep tr/ho {c['rep_tr_ret']:+6.1f}%/{c['rep_ho_ret']:+6.1f}% "
                          f"q {c['q_pos']}/{c['q_n']}", flush=True)
    return rows


def main() -> None:
    import multiprocessing as mp
    out_path = Path(__file__).parent / "results" / f"call_gate_sweep_{COIN}_2026-08-17.json"
    out_path.parent.mkdir(exist_ok=True)
    with mp.get_context("spawn").Pool(len(RET7D_GRID)) as pool:
        groups = pool.map(run_ret7d_group, RET7D_GRID)
    rows = [r for g in groups for r in g]
    out_path.write_text(json.dumps(rows, indent=1))
    print(f"\nsaved {len(rows)} rows -> {out_path}")


if __name__ == "__main__":
    main()
