"""Event-ordered portfolio replay — prerequisite for ANY portfolio-level
protection mechanism (concentration cap, MTM equity guard).

WHY THIS EXISTS
---------------
je.replay_account() iterates trades in entry_ts order and applies the trade's
FULL realized pnl to `equity` and `recent_pnls` at the moment the trade OPENS
(jony_engine.py:578-581). With ~2.18 trades/day and a 120h PUT hold, ~10
positions are typically open at once, so every sizing decision sees the
outcomes of ~10 trades that have not closed yet:

  * equity used by jc.size_position() anticipates unrealized P&L  -> positions
    are sized off a future account balance (compounding runs ahead of reality)
  * recent_pnls feeding jc.dyn_size_factor() is clairvoyant       -> the
    loss-streak de-risking triggers on losses that have not happened yet
  * the circuit breaker arms at entry-processing time using exit_ts+8h, so it
    blocks entries for the WHOLE life of a losing trade, not just the 8h after
    it closes (live arms in _close(), loop.py:196-197) -> distortion in the
    opposite direction, blocking entries live would have taken

v2 processes entries and exits as separate time-ordered events, settling every
exit with exit_ts <= now BEFORE handling an entry at `now` — mirroring the live
loop, which calls manage_exits() (loop.py:336) before try_fire() (loop.py:391).

FIDELITY CHECK (project convention: a new engine must reproduce the old one
before its differences can be trusted): credit_at="entry" degrades v2 to the
exact v1 ordering and MUST reproduce je.replay_account() bit-for-bit on the
same trades. Run `python3 replay_account_v2.py --selfcheck`.

Nothing here touches live code or live params.
"""
from __future__ import annotations

import heapq
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jony_core as jc
import jony_engine as je


def replay_v2(trades: list[dict], mo: int, cap: int,
              start_equity: float = jc.START_EQUITY_USD,
              cb_mode: str = "per_key", size_mult_fn=None,
              credit_at: str = "exit", cb_at: str | None = None,
              per_key_cap: int | None = None) -> dict:
    """credit_at: 'exit' (correct, event-ordered) or 'entry' (v1-compatible
    ordering, used only by the self-check).

    cb_at: when the circuit breaker arms — 'exit' (live behavior, _close())
    or 'entry' (v1 behavior: armed while processing the losing trade's ENTRY,
    so it blocks entries for that trade's whole life + CB_PAUSE_HOURS).
    Defaults to follow credit_at, which is what makes the self-check exact;
    set it independently to decompose the two effects.

    per_key_cap: max concurrent positions sharing a (coin,side) key. None =
    off (live behavior — only mo/cap apply, so the book can stack N identical
    same-side positions, which is what the live account does today). This is
    an IMPLEMENTABLE stand-in for what v1's clairvoyant CB accidentally
    enforced: it throttles same-key stacking using only present information."""
    cb_at = credit_at if cb_at is None else cb_at
    trades = sorted(trades, key=lambda t: t["entry_ts"])
    equity = start_equity
    peak = equity
    max_dd = 0.0
    recent_pnls: list[float] = []
    cb_until_global = -1
    cb_until_by_key: dict[str, int] = {}
    n_taken = 0
    n_skipped_cap = 0
    n_skipped_cb = 0
    n_skipped_size = 0      # qty==0: equity/free-margin too small to afford a lot
    max_concurrent = 0
    concurrency_sum = 0
    equity_curve = []

    open_positions: list[dict] = []          # {coin, exit_ts, margin}
    pending: list[tuple[int, int, dict]] = []  # heap of (exit_ts, seq, position)
    seq = 0

    def settle(until_ms: int) -> None:
        """Realize every exit at or before until_ms, in time order."""
        nonlocal equity, peak, max_dd, cb_until_global, recent_pnls
        while pending and pending[0][0] <= until_ms:
            exit_ts, _, pos = heapq.heappop(pending)
            equity += pos["pnl_usd"]
            recent_pnls.append(pos["pnl_pct"])
            del recent_pnls[:-50]
            if pos["pnl_pct"] <= 0 and cb_at == "exit":
                cb_ms = exit_ts + jc.CB_PAUSE_HOURS * 3_600_000
                if cb_mode == "per_key":
                    cb_until_by_key[pos["cb_key"]] = cb_ms
                else:
                    cb_until_global = cb_ms
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100)
            equity_curve.append((exit_ts, equity))

    for t in trades:
        now = t["entry_ts"]
        if credit_at == "exit":
            settle(now)
        open_positions = [p for p in open_positions if p["exit_ts"] > now]
        cb_key = f"{t['coin']}:{t['side']}"
        if cb_mode == "per_key":
            if now < cb_until_by_key.get(cb_key, -1):
                n_skipped_cb += 1
                continue
        elif now < cb_until_global:
            n_skipped_cb += 1
            continue
        max_concurrent = max(max_concurrent, len(open_positions))
        concurrency_sum += len(open_positions)
        if len(open_positions) >= mo:
            n_skipped_cap += 1
            continue
        if sum(1 for p in open_positions if p["coin"] == t["coin"]) >= cap:
            n_skipped_cap += 1
            continue
        if per_key_cap is not None and \
                sum(1 for p in open_positions if p["cb_key"] == cb_key) >= per_key_cap:
            n_skipped_cap += 1
            continue
        used_margin = sum(p["margin"] for p in open_positions)
        mult = size_mult_fn(t) if size_mult_fn is not None else 1.0
        qty, margin = jc.size_position(equity, used_margin, recent_pnls,
                                       t["strike"], t["entry_credit"], t["lot"],
                                       size_mult=mult,
                                       m_lot_override=t.get("m_lot"))
        if qty <= 0:
            n_skipped_size += 1
            continue
        notional = t["strike"] * qty
        premium_total = t["entry_credit"] * qty
        fee_open = jc.fee_usd(notional, premium_total)
        exit_credit = t["entry_credit"] * (1 - t["pnl_pct"])
        fee_close = jc.fee_usd(notional, exit_credit * qty)
        pnl_usd = (t["entry_credit"] - exit_credit) * qty - fee_open - fee_close
        n_taken += 1
        pos = {"coin": t["coin"], "exit_ts": t["exit_ts"], "margin": margin,
               "pnl_usd": pnl_usd, "pnl_pct": t["pnl_pct"], "cb_key": cb_key}
        open_positions.append(pos)
        if t["pnl_pct"] <= 0 and cb_at == "entry":
            # v1 behavior: the CB is armed while processing the losing trade's
            # ENTRY, so it suppresses entries for that trade's entire life.
            cb_ms = t["exit_ts"] + jc.CB_PAUSE_HOURS * 3_600_000
            if cb_mode == "per_key":
                cb_until_by_key[cb_key] = cb_ms
            else:
                cb_until_global = cb_ms
        if credit_at == "entry":
            # v1 ordering: realize immediately at entry, exactly as
            # je.replay_account does (equity, recent_pnls, curve, dd).
            equity += pnl_usd
            recent_pnls.append(t["pnl_pct"])
            del recent_pnls[:-50]
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100)
            equity_curve.append((t["exit_ts"], equity))
        else:
            seq += 1
            heapq.heappush(pending, (t["exit_ts"], seq, pos))

    if credit_at == "exit":
        settle(float("inf"))

    n_considered = n_taken + n_skipped_cap + n_skipped_size
    return {"final": equity, "max_dd": max_dd, "n_taken": n_taken,
            "n_skipped_cap": n_skipped_cap, "n_skipped_cb": n_skipped_cb,
            "n_skipped_size": n_skipped_size, "max_concurrent": max_concurrent,
            "avg_concurrent": concurrency_sum / n_considered if n_considered else 0.0,
            "curve": equity_curve,
            "return_pct": (equity - start_equity) / start_equity * 100}


def _selfcheck() -> int:
    trades = je.coin_trades("ETH") + je.coin_trades("BTC")
    tr, ho = je.split(trades, 0.70)
    bad = 0
    for name, sub in (("all", trades), ("train", tr), ("holdout", ho)):
        for mo, cap in ((10, 6), (6, 4)):
            a = je.replay_account(sub, mo, cap)
            b = replay_v2(sub, mo, cap, credit_at="entry")
            same = (abs(a["final"] - b["final"]) < 1e-9
                    and abs(a["max_dd"] - b["max_dd"]) < 1e-9
                    and a["n_taken"] == b["n_taken"]
                    and a["n_skipped_cap"] == b["n_skipped_cap"]
                    and a["curve"] == b["curve"])
            print(f"[{'OK ' if same else 'FAIL'}] {name:8s} mo={mo} cap={cap}  "
                  f"v1 final={a['final']:.6f} n={a['n_taken']}  |  "
                  f"v2(entry) final={b['final']:.6f} n={b['n_taken']}")
            bad += 0 if same else 1
    return bad


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        raise SystemExit(_selfcheck())
    trades = je.coin_trades("ETH") + je.coin_trades("BTC")
    tr, ho = je.split(trades, 0.70)
    MO, CAP = jc.MAX_OPEN_POSITIONS, jc.PER_COIN_CAP
    # A = v1 baseline; B/C isolate one effect each; D = live-faithful
    VARIANTS = [
        ("A v1 (credit@entry, cb@entry)", "entry", "entry"),
        ("B lookahead fixed only      ", "exit", "entry"),
        ("C cb-timing fixed only      ", "entry", "exit"),
        ("D both fixed = live-faithful", "exit", "exit"),
    ]
    print(f"live config MO={MO} CAP={CAP}, n_trades={len(trades)}\n")
    for name, sub in (("TRAIN", tr), ("HOLDOUT", ho)):
        print(f"--- {name} (n={len(sub)}) ---")
        print(f"{'variant':32s} {'return%':>12s} {'maxDD%':>7s} {'taken':>6s} "
              f"{'skipCB':>7s} {'skipCap':>8s} {'skipSize':>9s} {'avgConc':>8s} {'maxConc':>8s}")
        for label, ca, cb in VARIANTS:
            r = replay_v2(sub, MO, CAP, credit_at=ca, cb_at=cb)
            print(f"{label:32s} {r['return_pct']:>12.1f} {r['max_dd']:>7.1f} "
                  f"{r['n_taken']:>6d} {r['n_skipped_cb']:>7d} {r['n_skipped_cap']:>8d} "
                  f"{r['n_skipped_size']:>9d} {r['avg_concurrent']:>8.2f} {r['max_concurrent']:>8d}")
        print()
    print("quarters (live config, return% / maxDD%):")
    print(f"{'quarter':10s} " + " ".join(f"{l.strip():>26s}" for l, _, _ in VARIANTS))
    for i, q in enumerate(je.quarters(trades)):
        if not q:
            continue
        cells = []
        for _, ca, cb in VARIANTS:
            r = replay_v2(q, MO, CAP, credit_at=ca, cb_at=cb)
            cells.append(f"{r['return_pct']:+13.1f}% /{r['max_dd']:5.1f}%")
        print(f"  Q{i+1:<7d} " + " ".join(f"{c:>26s}" for c in cells))
