#!/usr/bin/env python3
"""Screen Shanghai/Shenzhen main-board shares for a descending terminal wedge.

The target is a larger downtrend whose final section contains an alternating
L-H-L-H-L sequence: lower highs, marginal lower lows, converging rails, wave-4
price overlap, waning fifth-wave momentum, and (optionally) an upside breakout.
Outputs a ranked CSV/JSON/Markdown report and annotated daily candlestick charts.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
MAIN_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")
BAD_NAME = re.compile(r"(?:\*?ST|退市|退$|^N|^C)", re.I)
_tls = threading.local()


def session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        retry = Retry(total=2, connect=2, read=2, backoff_factor=0.35,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(["GET"]))
        s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16))
        s.headers.update(HEADERS)
        _tls.session = s
    return s


def market_prefix(code: str) -> str:
    return "sh" if code.startswith("6") else "sz"


def secid(code: str) -> str:
    return ("1." if code.startswith("6") else "0.") + code


def num(x: Any, default: float = math.nan) -> float:
    try:
        value = float(x)
        return value if math.isfinite(value) else default
    except Exception:
        return default


def fetch_universe() -> pd.DataFrame:
    url = "https://82.push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": 6000, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f3", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f12,f14,f2,f3,f5,f6,f8,f9,f20,f21",
    }
    rows: list[dict[str, Any]] = []
    try:
        payload = session().get(url, params=params, timeout=20).json()
        diff = ((payload or {}).get("data") or {}).get("diff") or []
        for r in diff:
            rows.append({
                "code": str(r.get("f12", "")).zfill(6), "name": str(r.get("f14", "")),
                "last": num(r.get("f2")), "pct": num(r.get("f3")),
                "volume": num(r.get("f5")), "amount": num(r.get("f6")),
                "turnover": num(r.get("f8")), "pe": num(r.get("f9")),
                "market_cap": num(r.get("f20")), "float_cap": num(r.get("f21")),
            })
    except Exception as exc:
        print(f"[universe] Eastmoney failed: {exc}", flush=True)

    if not rows:
        # Public 2026-07-31 indicator workbook, used only as a stock-list fallback.
        raw = "https://raw.githubusercontent.com/89529738/ashare-public-data/main/data/股票池合并_核准_技术指标_latest.xlsx"
        wb = pd.read_excel(raw)
        code_col = next((c for c in wb.columns if "代码" in str(c)), wb.columns[0])
        name_col = next((c for c in wb.columns if "名称" in str(c)), wb.columns[1])
        rows = [{"code": str(v[code_col]).split(".")[0].zfill(6), "name": str(v[name_col])}
                for _, v in wb.iterrows()]

    u = pd.DataFrame(rows).drop_duplicates("code")
    u = u[u["code"].str.startswith(MAIN_PREFIXES)]
    u = u[~u["name"].str.contains(BAD_NAME, na=False)]
    u = u[u["name"].str.len().between(2, 12)]
    if "last" in u:
        u = u[(u["last"].isna()) | (u["last"] >= 1.0)]
    return u.reset_index(drop=True)


def _frame(rows: list[list[Any]], source: str) -> pd.DataFrame | None:
    parsed = []
    for r in rows:
        if len(r) < 6:
            continue
        try:
            parsed.append((pd.to_datetime(r[0]), float(r[1]), float(r[2]),
                           float(r[3]), float(r[4]), float(r[5]),
                           float(r[6]) if len(r) > 6 and str(r[6]) not in ("", "-") else math.nan))
        except Exception:
            continue
    if len(parsed) < 120:
        return None
    df = pd.DataFrame(parsed, columns=["date", "open", "close", "high", "low", "volume", "amount"])
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    for c in ("open", "close", "high", "low", "volume"):
        df = df[pd.to_numeric(df[c], errors="coerce").notna()]
    df = df[(df[["open", "close", "high", "low"]] > 0).all(axis=1)]
    if len(df) < 120:
        return None
    df.attrs["source"] = source
    return df.reset_index(drop=True)


def fetch_tencent(code: str, count: int) -> pd.DataFrame | None:
    symbol = market_prefix(code) + code
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    payload = session().get(url, params={"param": f"{symbol},day,,,{count},qfq"}, timeout=12).json()
    node = ((payload or {}).get("data") or {}).get(symbol) or {}
    rows = node.get("qfqday") or node.get("day") or []
    return _frame(rows, "Tencent-qfq")


def fetch_eastmoney(code: str, count: int) -> pd.DataFrame | None:
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid(code), "klt": 101, "fqt": 1, "lmt": count, "end": 20500101,
        "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }
    payload = session().get(url, params=params, timeout=12).json()
    klines = ((payload or {}).get("data") or {}).get("klines") or []
    return _frame([str(x).split(",") for x in klines], "Eastmoney-qfq")


def fetch_kline(code: str, count: int) -> pd.DataFrame | None:
    for fn in (fetch_tencent, fetch_eastmoney):
        try:
            df = fn(code, count)
            if df is not None:
                return df.tail(count).reset_index(drop=True)
        except Exception:
            pass
    return None


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    prev = out["close"].shift(1)
    tr = pd.concat([(out["high"] - out["low"]), (out["high"] - prev).abs(),
                    (out["low"] - prev).abs()], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    delta = out["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi"] = 100 - 100 / (1 + rs)
    ema12 = out["close"].ewm(span=12, adjust=False).mean()
    ema26 = out["close"].ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    out["macd_hist"] = 2 * (dif - dea)
    out["vol20"] = out["volume"].rolling(20).mean()
    out["ma20"] = out["close"].rolling(20).mean()
    out["ma60"] = out["close"].rolling(60).mean()
    return out


def pivots(df: pd.DataFrame, window: int = 4) -> list[tuple[int, str, float]]:
    n = len(df)
    raw: list[tuple[int, str, float]] = []
    hi, lo = df["high"].to_numpy(), df["low"].to_numpy()
    for i in range(window, n - window):
        hwin, lwin = hi[i-window:i+window+1], lo[i-window:i+window+1]
        if hi[i] >= np.nanmax(hwin) and np.count_nonzero(hwin == hi[i]) == 1:
            raw.append((i, "H", float(hi[i])))
        if lo[i] <= np.nanmin(lwin) and np.count_nonzero(lwin == lo[i]) == 1:
            raw.append((i, "L", float(lo[i])))
    raw.sort(key=lambda x: x[0])
    if not raw:
        return []

    atr_pct = float((df["atr"] / df["close"]).tail(120).median())
    min_move = max(0.025, min(0.075, 1.15 * atr_pct if math.isfinite(atr_pct) else 0.035))
    zz: list[tuple[int, str, float]] = []
    for p in raw:
        if not zz:
            zz.append(p); continue
        last = zz[-1]
        if p[0] == last[0]:
            continue
        if p[1] == last[1]:
            better = p[2] > last[2] if p[1] == "H" else p[2] < last[2]
            if better:
                zz[-1] = p
            continue
        move = abs(p[2] / last[2] - 1)
        if move >= min_move:
            zz.append(p)
    return zz


def line(x1: int, y1: float, x2: int, y2: float) -> tuple[float, float]:
    slope = (y2 - y1) / max(1, x2 - x1)
    return slope, y1 - slope * x1


def val(slope: float, intercept: float, x: int | float) -> float:
    return slope * x + intercept


@dataclass
class Candidate:
    code: str
    name: str
    stage: str
    score: float
    last_date: str
    last_close: float
    upper_rail: float
    lower_rail: float
    distance_to_upper_pct: float
    breakout_pct: float
    breakout_volume_ratio: float
    invalidation_low5: float
    low5_date: str
    low5_age: int
    prior_decline_pct: float
    contraction_ratio: float
    wave5_vs_wave3: float
    rsi_low3: float
    rsi_low5: float
    macd_low3: float
    macd_low5: float
    wave5_volume_vs_wave3: float
    market_cap_bil: float
    turnover: float
    source: str
    p1_i: int
    p2_i: int
    p3_i: int
    p4_i: int
    p5_i: int
    p1: float
    p2: float
    p3: float
    p4: float
    p5: float
    upper_slope: float
    upper_intercept: float
    lower_slope: float
    lower_intercept: float


def scan_pattern(df: pd.DataFrame, meta: dict[str, Any]) -> Candidate | None:
    df = indicators(df.tail(340).reset_index(drop=True))
    ps = pivots(df)
    if len(ps) < 5:
        return None
    best: Candidate | None = None
    n = len(df)
    for j in range(max(0, len(ps) - 18), len(ps) - 4):
        seq = ps[j:j+5]
        if [x[1] for x in seq] != ["L", "H", "L", "H", "L"]:
            continue
        (i1, _, p1), (i2, _, p2), (i3, _, p3), (i4, _, p4), (i5, _, p5) = seq
        gaps = np.diff([i1, i2, i3, i4, i5])
        total = i5 - i1
        if total < 28 or total > 210 or gaps.min() < 3 or i5 < n - 38:
            continue
        if not (p2 > p4 and p1 > p3 and p5 <= p3 * 1.012):
            continue
        if p4 <= p1 * 0.985:  # wave 4 should overlap wave-1 price territory
            continue

        us, ui = line(i2, p2, i4, p4)
        ls, li = np.polyfit(np.array([i1, i3, i5], dtype=float), np.array([p1, p3, p5]), 1)
        if not (us < 0 and ls < 0 and us < ls * 1.05):
            continue
        width1 = val(us, ui, i1) - val(ls, li, i1)
        width5 = val(us, ui, i5) - val(ls, li, i5)
        width_now = val(us, ui, n-1) - val(ls, li, n-1)
        if min(width1, width5, width_now) <= 0:
            continue
        contraction = width5 / width1
        if not 0.08 <= contraction <= 0.82:
            continue

        # Rail adherence prevents an arbitrary five-pivot sequence being labelled a wedge.
        rail_err = np.mean([
            abs(p1 - val(ls, li, i1)) / p1,
            abs(p3 - val(ls, li, i3)) / p3,
            abs(p5 - val(ls, li, i5)) / p5,
            abs(p2 - val(us, ui, i2)) / p2,
            abs(p4 - val(us, ui, i4)) / p4,
        ])
        if rail_err > 0.018:
            continue

        before = df.iloc[max(0, i1-120):i1]
        if len(before) < 20:
            continue
        prior_high = float(before["high"].max())
        prior_decline = 100 * (prior_high / p5 - 1)
        if prior_decline < 18:
            continue

        wave3 = p2 - p3
        wave5 = p4 - p5
        if wave3 <= 0 or wave5 <= 0:
            continue
        attenuation = wave5 / wave3
        if attenuation > 1.25:
            continue

        upper_now, lower_now = val(us, ui, n-1), val(ls, li, n-1)
        close = float(df["close"].iloc[-1])
        dist = 100 * (close / upper_now - 1)
        if dist > 18 or close < lower_now * 0.94:
            continue
        above = df["close"].tail(4).to_numpy() > np.array([val(us, ui, x) for x in range(n-4, n)])
        vol_ratio = float(df["volume"].tail(3).mean() / df["vol20"].iloc[-1]) if df["vol20"].iloc[-1] else math.nan
        if above.sum() >= 3 and dist >= 1.0:
            stage = "confirmed_breakout"
        elif above[-1] and dist >= 0:
            stage = "initial_breakout"
        elif dist >= -3.5:
            stage = "testing_upper_rail"
        else:
            stage = "inside_wedge"

        rsi3, rsi5 = num(df["rsi"].iloc[i3]), num(df["rsi"].iloc[i5])
        macd3, macd5 = num(df["macd_hist"].iloc[i3]), num(df["macd_hist"].iloc[i5])
        bullish_rsi = math.isfinite(rsi3) and math.isfinite(rsi5) and rsi5 >= rsi3 + 1.0
        bullish_macd = math.isfinite(macd3) and math.isfinite(macd5) and macd5 > macd3
        seg3 = df.iloc[i2:i3+1]["volume"].mean()
        seg5 = df.iloc[i4:i5+1]["volume"].mean()
        wave_vol = float(seg5 / seg3) if seg3 else math.nan

        score = 36.0
        score += max(0, min(14, (0.82 - contraction) / 0.74 * 14))
        score += max(0, min(8, (1.25 - attenuation) / 1.25 * 8))
        score += 8 if bullish_rsi else 0
        score += 8 if bullish_macd else 0
        score += 6 if math.isfinite(wave_vol) and wave_vol <= 0.92 else (3 if math.isfinite(wave_vol) and wave_vol <= 1.08 else 0)
        score += min(7, max(0, (prior_decline - 18) / 5))
        score += {"confirmed_breakout": 13, "initial_breakout": 10,
                  "testing_upper_rail": 7, "inside_wedge": 2}[stage]
        if stage in ("confirmed_breakout", "initial_breakout") and math.isfinite(vol_ratio):
            score += min(5, max(0, (vol_ratio - 0.9) * 5))
        score -= min(8, max(0, (n - 1 - i5 - 20) * 0.35))
        score = round(max(0, min(100, score)), 2)

        c = Candidate(
            code=str(meta["code"]), name=str(meta["name"]), stage=stage, score=score,
            last_date=str(df["date"].iloc[-1].date()), last_close=round(close, 3),
            upper_rail=round(upper_now, 3), lower_rail=round(lower_now, 3),
            distance_to_upper_pct=round(dist, 2), breakout_pct=round(max(0, dist), 2),
            breakout_volume_ratio=round(vol_ratio, 2) if math.isfinite(vol_ratio) else math.nan,
            invalidation_low5=round(p5, 3), low5_date=str(df["date"].iloc[i5].date()),
            low5_age=n-1-i5, prior_decline_pct=round(prior_decline, 2),
            contraction_ratio=round(contraction, 3), wave5_vs_wave3=round(attenuation, 3),
            rsi_low3=round(rsi3, 2), rsi_low5=round(rsi5, 2),
            macd_low3=round(macd3, 4), macd_low5=round(macd5, 4),
            wave5_volume_vs_wave3=round(wave_vol, 3) if math.isfinite(wave_vol) else math.nan,
            market_cap_bil=round(num(meta.get("market_cap"), 0) / 1e9, 2),
            turnover=round(num(meta.get("turnover")), 2), source=str(df.attrs.get("source", "unknown")),
            p1_i=i1, p2_i=i2, p3_i=i3, p4_i=i4, p5_i=i5,
            p1=round(p1, 3), p2=round(p2, 3), p3=round(p3, 3), p4=round(p4, 3), p5=round(p5, 3),
            upper_slope=float(us), upper_intercept=float(ui), lower_slope=float(ls), lower_intercept=float(li),
        )
        if best is None or c.score > best.score:
            best = c
    return best


def analyse(meta: dict[str, Any], count: int) -> tuple[Candidate | None, pd.DataFrame | None, str | None]:
    code = str(meta["code"])
    try:
        df = fetch_kline(code, count)
        if df is None:
            return None, None, "no_kline"
        candidate = scan_pattern(df, meta)
        return candidate, df if candidate else None, None
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def candle_chart(df: pd.DataFrame, c: Candidate, path: Path) -> None:
    d = indicators(df.tail(340).reset_index(drop=True))
    start = max(0, c.p1_i - 35)
    d = d.iloc[start:].reset_index(drop=True)
    shift = start
    fig = plt.figure(figsize=(12, 8), dpi=150)
    gs = fig.add_gridspec(4, 1, hspace=0.05)
    ax = fig.add_subplot(gs[:3, 0]); av = fig.add_subplot(gs[3, 0], sharex=ax)
    for x, row in d.iterrows():
        up = row["close"] >= row["open"]
        color = "#d62728" if up else "#2ca02c"
        ax.vlines(x, row["low"], row["high"], color=color, linewidth=0.7)
        bottom = min(row["open"], row["close"])
        height = max(abs(row["close"] - row["open"]), row["close"] * 0.001)
        ax.add_patch(Rectangle((x-0.32, bottom), 0.64, height, facecolor=color, edgecolor=color, linewidth=0.5))
        av.bar(x, row["volume"], width=0.65, color=color, alpha=0.75)
    xs = np.arange(len(d)) + shift
    ax.plot(np.arange(len(d)), c.upper_slope*xs + c.upper_intercept, "--", linewidth=1.3, label="upper rail (2-4)")
    ax.plot(np.arange(len(d)), c.lower_slope*xs + c.lower_intercept, "--", linewidth=1.3, label="lower rail (1-3-5)")
    for label, idx, price in [("1",c.p1_i,c.p1),("2",c.p2_i,c.p2),("3",c.p3_i,c.p3),("4",c.p4_i,c.p4),("5",c.p5_i,c.p5)]:
        x = idx - shift
        if 0 <= x < len(d):
            ax.scatter([x], [price], s=26, zorder=5)
            ax.annotate(label, (x, price), xytext=(0, -16 if label in ("1","3","5") else 9),
                        textcoords="offset points", ha="center", fontsize=10, fontweight="bold")
    ax.set_title(f"{c.code} {c.name} | {c.stage} | score={c.score} | {c.last_date}")
    ax.legend(loc="upper right", fontsize=8); ax.grid(alpha=0.18); av.grid(alpha=0.12)
    ticks = np.linspace(0, len(d)-1, min(8, len(d)), dtype=int)
    av.set_xticks(ticks); av.set_xticklabels([d["date"].iloc[t].strftime("%Y-%m-%d") for t in ticks], rotation=30, ha="right", fontsize=8)
    av.set_ylabel("Volume"); ax.set_ylabel("Price")
    plt.setp(ax.get_xticklabels(), visible=False)
    fig.tight_layout(); fig.savefig(path, bbox_inches="tight"); plt.close(fig)


def write_summary(out: Path, ranked: pd.DataFrame, universe_n: int, errors: list[str]) -> None:
    lines = [
        "# A-share descending terminal-wedge scan",
        "",
        f"- Universe after main-board/ST filters: **{universe_n}**",
        f"- Candidates retained: **{len(ranked)}**",
        f"- Last data date: **{ranked['last_date'].max() if len(ranked) else 'N/A'}**",
        "- Interpretation: confirmed/initial breakout > testing upper rail > still inside wedge.",
        "- Invalidation is the fifth-wave low; a close back inside the wedge after breakout weakens the setup.",
        "",
    ]
    if len(ranked):
        cols = ["code","name","stage","score","last_close","upper_rail","distance_to_upper_pct",
                "invalidation_low5","low5_age","prior_decline_pct","contraction_ratio",
                "wave5_vs_wave3","rsi_low3","rsi_low5","breakout_volume_ratio"]
        lines += ["## Top 30", "", ranked[cols].head(30).to_markdown(index=False), ""]
    if errors:
        lines += ["## Diagnostics", "", f"K-line/parse errors: {len(errors)} (see errors.log)", ""]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=340)
    ap.add_argument("--max-workers", type=int, default=8)
    ap.add_argument("--min-score", type=float, default=50)
    ap.add_argument("--top-charts", type=int, default=15)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--output", default="terminal_wedge_output")
    args = ap.parse_args()
    out = Path(args.output); charts = out / "charts"
    charts.mkdir(parents=True, exist_ok=True)

    universe = fetch_universe()
    if args.limit:
        universe = universe.head(args.limit)
    print(f"Universe: {len(universe)}", flush=True)
    metas = universe.to_dict("records")
    candidates: list[Candidate] = []
    frames: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {pool.submit(analyse, m, args.count): m for m in metas}
        for fut in as_completed(futures):
            m = futures[fut]
            c, df, err = fut.result()
            done += 1
            if c is not None and c.score >= args.min_score:
                candidates.append(c)
                if df is not None:
                    frames[c.code] = df
                print(f"MATCH {c.code} {c.name} {c.stage} {c.score}", flush=True)
            if err and err != "no_kline":
                errors.append(f"{m['code']} {m['name']}: {err}")
            if done % 200 == 0:
                print(f"Progress {done}/{len(metas)}; matches={len(candidates)}", flush=True)
            if done % 500 == 0:
                time.sleep(random.uniform(0.2, 0.5))

    records = [asdict(c) for c in candidates]
    ranked = pd.DataFrame(records)
    if len(ranked):
        stage_order = {"confirmed_breakout": 0, "initial_breakout": 1, "testing_upper_rail": 2, "inside_wedge": 3}
        ranked["stage_order"] = ranked["stage"].map(stage_order)
        ranked = ranked.sort_values(["stage_order","score","low5_age"], ascending=[True,False,True]).drop(columns="stage_order")
    ranked.to_csv(out / "terminal_wedge_candidates.csv", index=False, encoding="utf-8-sig")
    (out / "terminal_wedge_candidates.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    universe.to_csv(out / "scanned_universe.csv", index=False, encoding="utf-8-sig")
    (out / "errors.log").write_text("\n".join(errors), encoding="utf-8")
    write_summary(out, ranked, len(universe), errors)

    for _, row in ranked.head(args.top_charts).iterrows() if len(ranked) else []:
        code = str(row["code"]).zfill(6)
        c = next(x for x in candidates if x.code == code)
        df = frames.get(code)
        if df is not None:
            try:
                candle_chart(df, c, charts / f"{code}_{c.name}.png")
            except Exception as exc:
                errors.append(f"chart {code}: {exc}")
    print(f"Finished: {len(ranked)} candidates -> {out}", flush=True)


if __name__ == "__main__":
    main()
