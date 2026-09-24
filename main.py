#!/usr/bin/env python3
"""Standby candidate detector, first iteration. Simple on purpose.

Rule: per-meter limit from quiet hours. A low reading counts only
as part of two low readings in a row. No clear night-day split
means declined, not zero. Missing stays missing, zeros stay apart.
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

QUIET_HOURS = {22, 23, 0, 1, 2, 3, 4, 5}
DAY_HOURS = {8, 9, 10, 11, 12, 13, 14, 15, 16}


def _key(name: str) -> int:
    m = re.search(r"(\d+)", str(name))
    return int(m.group(1)) if m else 999


def load(input_path: Path) -> pd.DataFrame:
    if input_path.is_file():
        files = [input_path]
    else:
        files = sorted(input_path.glob("meter_*.csv")) or sorted(
            p for p in input_path.glob("*.csv") if p.name != ".DS_Store"
        )
    frames = []
    for path in files:
        d = pd.read_csv(path, dtype=str)
        d["meter_id"] = path.stem
        frames.append(d[["meter_id", "timestamp", "value_kwh"]])
    df = pd.concat(frames, ignore_index=True)
    # Blank text means missing. Missing never becomes zero.
    df["value_kwh"] = pd.to_numeric(df["value_kwh"], errors="coerce")
    df["is_missing"] = df["value_kwh"].isna()
    df["ts_utc"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df["ts_local"] = [datetime.fromisoformat(s) for s in df["timestamp"]]
    df["hour"] = [t.hour for t in df["ts_local"]]
    df["weekday"] = [t.weekday() for t in df["ts_local"]]
    df["is_quiet"] = df["hour"].isin(QUIET_HOURS) | (df["weekday"] >= 5)
    df["is_day"] = (df["weekday"] < 5) & df["hour"].isin(DAY_HOURS)
    df["mean_kw"] = df["value_kwh"] * 4.0  # 15-min energy -> mean power
    return df.sort_values(["meter_id", "ts_utc"]).reset_index(drop=True)


def fit(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for mid, g in df.groupby("meter_id"):
        ok = g[~g["is_missing"] & (g["value_kwh"] > 0)]
        q = ok[ok["is_quiet"]]["value_kwh"]
        d = ok[ok["is_day"]]["value_kwh"]
        p25 = float(q.quantile(0.25)) if len(q) else float("nan")
        p50q = float(q.quantile(0.5)) if len(q) else float("nan")
        p50d = float(d.quantile(0.5)) if len(d) else float("nan")
        thr = p25 * 4.0 if len(q) else float("nan")
        ratio = p50q / p50d if p50d and p50d > 0 else float("nan")
        # Decline when there is no clear quiet floor to stand on.
        if len(q) < 200 or not (p50d > 0) or ratio != ratio or ratio > 0.5:
            status, reason = "declined", "no_distinct_quiet_regime"
        else:
            status, reason = "estimated", "ok"
        rows.append(
            {"meter_id": mid, "thr_kw": thr, "n_quiet_pos": len(q),
             "quiet_day_ratio": ratio, "status": status, "status_reason": reason}
        )
        print(f"{mid} thr={thr:.3f}kW ratio={ratio:.3f} {status}")
    return pd.DataFrame(rows).sort_values("meter_id", key=lambda s: s.map(_key))


def run(df: pd.DataFrame, thr: pd.DataFrame) -> pd.DataFrame:
    out = []
    for mid, g in df.groupby("meter_id"):
        g = g.copy()
        t = float(thr.loc[thr["meter_id"] == mid, "thr_kw"].iloc[0])
        est = thr.loc[thr["meter_id"] == mid, "status"].iloc[0] == "estimated"
        # Raw pass: low means positive and at or below the limit.
        low = (~g["is_missing"]) & (g["value_kwh"] > 0) & (g["mean_kw"] <= t) & est
        is_zero = (~g["is_missing"]) & (g["value_kwh"] == 0)
        # Dwell: only runs of 2+ low rows count. Singles are ignored.
        cand = [False] * len(g)
        run_idx: list[int] = []
        pos = list(g.index)
        for k, idx in enumerate(pos):
            if low.iloc[k]:
                run_idx.append(k)
            else:
                if len(run_idx) >= 2:
                    for j in run_idx:
                        cand[j] = True
                run_idx = []
        if len(run_idx) >= 2:
            for j in run_idx:
                cand[j] = True
        g["is_zero"] = is_zero.values
        g["is_candidate"] = cand
        g["thr_kw"] = t
        out.append(g)
    return pd.concat(out, ignore_index=True)


def summarise(df: pd.DataFrame, det: pd.DataFrame, thr: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for mid, g in det.groupby("meter_id"):
        meta = thr.loc[thr["meter_id"] == mid].iloc[0]
        n = len(g)
        n_miss = int(g["is_missing"].sum())
        n_zero = int(g["is_zero"].sum())
        est = meta["status"] == "estimated"
        n_cand = int(g["is_candidate"].sum()) if est else 0
        kwh = float(g.loc[g["is_candidate"], "value_kwh"].sum()) if est else float("nan")
        rows.append({
            "meter_id": mid,
            "period_start": str(g["timestamp"].iloc[0]),
            "period_end": str(g["timestamp"].iloc[-1]),
            "missing_share": round(n_miss / n, 4),
            "zero_load_hours": n_zero * 0.25,
            "standby_candidate_hours": n_cand * 0.25 if est else None,
            "excess_kwh": kwh if est else None,  # above a 0 kWh off baseline
            "thr_kw": round(float(meta["thr_kw"]), 3),
            "quiet_day_ratio": round(float(meta["quiet_day_ratio"]), 3),
            "status": meta["status"],
            "status_reason": meta["status_reason"],
        })
    return pd.DataFrame(rows).sort_values("meter_id", key=lambda s: s.map(_key))


def main() -> None:
    ap = argparse.ArgumentParser(description="Standby candidate detector (v1)")
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    a = ap.parse_args()
    df = load(a.input)
    thr = fit(df)
    det = run(df, thr)
    summ = summarise(df, det, thr)
    a.output.mkdir(parents=True, exist_ok=True)
    thr.to_csv(a.output / "thresholds.csv", index=False)
    summ.to_csv(a.output / "meter_summary.csv", index=False)
    print(f"wrote {a.output}/meter_summary.csv")


if __name__ == "__main__":
    main()
