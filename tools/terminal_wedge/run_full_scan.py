#!/usr/bin/env python3
"""Reliable entry point for the A-share terminal-wedge scanner.

Universe construction uses independent public sources:
- Shanghai main board: official SSE suggestion data.
- Shenzhen main board: Tencent batch quotes across deterministic 000/001/002/003
  code ranges, with TDX and deterministic ranges as fallbacks.
K-lines are then fetched and scored by ``scan_terminal_wedge.py``.
"""
from __future__ import annotations

import io
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import scan_terminal_wedge as core  # noqa: E402

SSE_URL = "https://www.sse.com.cn/js/common/ssesuggestdata.js"
SSE_PREFIXES = ("600", "601", "603", "605")
SZ_PREFIXES = ("000", "001", "002", "003")
TDX_SERVERS = [
    ("124.71.187.72", 7709),
    ("115.238.56.198", 7709),
    ("101.133.214.242", 7709),
    ("119.147.212.81", 7709),
    ("47.103.48.45", 7709),
    ("218.108.47.69", 7709),
]


def clean_name(name: Any) -> str:
    return re.sub(r"\s+", "", str(name or "")).strip()


def fetch_sse_mainboard() -> list[dict[str, Any]]:
    r = requests.get(
        SSE_URL,
        headers={
            "User-Agent": core.HEADERS["User-Agent"],
            "Referer": "https://www.sse.com.cn/assortment/stock/list/share/",
        },
        timeout=25,
    )
    r.raise_for_status()
    text = r.content.decode("utf-8", errors="ignore")
    pairs = re.findall(
        r"val\s*:\s*[\"'](\d{6})[\"']\s*,\s*val2\s*:\s*[\"']([^\"']+)",
        text,
    )
    rows: list[dict[str, Any]] = []
    for code, raw_name in pairs:
        name = clean_name(raw_name)
        if code.startswith(SSE_PREFIXES) and name and not core.BAD_NAME.search(name):
            rows.append({"code": code, "name": name, "exchange": "SH", "universe_source": "SSE"})
    if len(rows) < 1200:
        raise RuntimeError(f"SSE main-board list unexpectedly small: {len(rows)}")
    return rows


def generated_sz_codes() -> list[str]:
    return [f"{prefix}{i:03d}" for prefix in SZ_PREFIXES for i in range(1000)]


def fetch_sz_mainboard_tencent(batch_size: int = 80) -> list[dict[str, Any]]:
    """Validate the complete Shenzhen main-board code space in ~50 requests."""
    codes = generated_sz_codes()
    found: dict[str, dict[str, Any]] = {}
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": core.HEADERS["User-Agent"],
        "Referer": "https://gu.qq.com/",
    })
    for start in range(0, len(codes), batch_size):
        batch = codes[start:start + batch_size]
        symbols = ",".join("sz" + code for code in batch)
        r = sess.get("https://qt.gtimg.cn/q=" + symbols, timeout=20)
        r.raise_for_status()
        text = r.content.decode("gbk", errors="ignore")
        for code, payload in re.findall(r'v_sz(\d{6})="([^"]*)";', text):
            fields = payload.split("~")
            # Valid equity responses carry name/code/current quote fields. Invalid
            # symbols are empty or return too few fields.
            if len(fields) < 10:
                continue
            name = clean_name(fields[1] if len(fields) > 1 else "")
            echoed = str(fields[2] if len(fields) > 2 else "")
            if echoed != code or not name or core.BAD_NAME.search(name):
                continue
            # Avoid indices/funds that happen to share the numeric range.
            if not code.startswith(SZ_PREFIXES):
                continue
            found[code] = {
                "code": code,
                "name": name,
                "exchange": "SZ",
                "universe_source": "Tencent-batch",
            }
        if start and start % 800 == 0:
            print(f"Tencent SZ validation {start}/{len(codes)}; valid={len(found)}", flush=True)
        time.sleep(0.035)
    rows = list(found.values())
    if len(rows) < 1000:
        raise RuntimeError(f"Tencent Shenzhen main-board list unexpectedly small: {len(rows)}")
    return rows


def fetch_sz_mainboard_tdx() -> list[dict[str, Any]]:
    from pytdx.hq import TdxHq_API

    failures: list[str] = []
    for host, port in TDX_SERVERS:
        api = TdxHq_API(heartbeat=False, auto_retry=False, raise_exception=True)
        try:
            api.connect(host, port, time_out=5)
            count = int(api.get_security_count(0) or 0)
            if count <= 0:
                raise RuntimeError("TDX returned a zero security count")
            collected: dict[str, dict[str, Any]] = {}
            for start in range(0, min(count, 10000), 1000):
                chunk = api.get_security_list(0, start) or []
                if not chunk:
                    break
                for rec in chunk:
                    code = str(rec.get("code", "")).zfill(6)
                    name = clean_name(rec.get("name", ""))
                    if code.startswith(SZ_PREFIXES) and name and not core.BAD_NAME.search(name):
                        collected[code] = {
                            "code": code,
                            "name": name,
                            "exchange": "SZ",
                            "universe_source": f"TDX:{host}",
                        }
                if len(chunk) < 1000:
                    break
            api.disconnect()
            rows = list(collected.values())
            if len(rows) < 1000:
                raise RuntimeError(f"TDX Shenzhen list unexpectedly small: {len(rows)}")
            return rows
        except Exception as exc:
            failures.append(f"{host}:{port} {type(exc).__name__}: {exc}")
            try:
                api.disconnect()
            except Exception:
                pass
    raise RuntimeError("No TDX universe server succeeded: " + " | ".join(failures))


def generated_sz_fallback() -> list[dict[str, Any]]:
    # Invalid codes are discarded later by the K-line fetcher. This guarantees
    # full recall even when all code-list endpoints are temporarily unavailable.
    return [
        {"code": code, "name": code, "exchange": "SZ", "universe_source": "generated-SZ-fallback"}
        for code in generated_sz_codes()
    ]


def fetch_workbook_supplement() -> list[dict[str, Any]]:
    """Small current public list used only to add very recent symbols/names."""
    url = (
        "https://raw.githubusercontent.com/89529738/ashare-public-data/main/data/"
        "股票池合并_核准_技术指标_latest.xlsx"
    )
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        wb = pd.read_excel(io.BytesIO(r.content), dtype=str, usecols=["股票代码", "股票名称"])
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for _, row in wb.iterrows():
        match = re.search(r"(\d{6})", str(row.get("股票代码", "")))
        if not match:
            continue
        code = match.group(1)
        name = clean_name(row.get("股票名称", ""))
        if code.startswith(core.MAIN_PREFIXES) and name and not core.BAD_NAME.search(name):
            out.append({
                "code": code,
                "name": name,
                "exchange": "SH" if code.startswith("6") else "SZ",
                "universe_source": "public-workbook",
            })
    return out


def robust_universe() -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    diagnostics: list[str] = []

    try:
        sh = fetch_sse_mainboard()
        rows.extend(sh)
        diagnostics.append(f"SSE={len(sh)}")
    except Exception as exc:
        diagnostics.append(f"SSE_FAILED={type(exc).__name__}:{exc}")
        traceback.print_exc()
        # Full Shanghai prefix space as a recall-safe fallback.
        sh = [
            {"code": f"{prefix}{i:03d}", "name": f"{prefix}{i:03d}",
             "exchange": "SH", "universe_source": "generated-SH-fallback"}
            for prefix in SSE_PREFIXES for i in range(1000)
        ]
        rows.extend(sh)
        diagnostics.append(f"SSE_GENERATED={len(sh)}")

    sz: list[dict[str, Any]] = []
    try:
        sz = fetch_sz_mainboard_tencent()
        diagnostics.append(f"SZ_TENCENT={len(sz)}")
    except Exception as exc:
        diagnostics.append(f"SZ_TENCENT_FAILED={type(exc).__name__}:{exc}")
        traceback.print_exc()
        try:
            sz = fetch_sz_mainboard_tdx()
            diagnostics.append(f"SZ_TDX={len(sz)}")
        except Exception as exc2:
            diagnostics.append(f"SZ_TDX_FAILED={type(exc2).__name__}:{exc2}")
            traceback.print_exc()
            sz = generated_sz_fallback()
            diagnostics.append(f"SZ_GENERATED={len(sz)}")
    rows.extend(sz)

    supplement = fetch_workbook_supplement()
    rows.extend(supplement)
    diagnostics.append(f"SUPPLEMENT={len(supplement)}")

    u = pd.DataFrame(rows)
    u["code"] = u["code"].astype(str).str.extract(r"(\d{6})", expand=False)
    u["name"] = u["name"].map(clean_name)
    # Named, authoritative sources take precedence over generated placeholders.
    u["placeholder"] = u["name"].eq(u["code"])
    u = u.sort_values(["code", "placeholder"]).drop_duplicates("code", keep="first")
    u = u.drop(columns="placeholder").dropna(subset=["code"])
    u = u[u["code"].str.startswith(core.MAIN_PREFIXES)]
    u = u[~u["name"].str.contains(core.BAD_NAME, na=False)]
    u = u[u["name"].str.len().between(2, 12)]
    u = u.sort_values("code").reset_index(drop=True)

    print("Universe sources: " + "; ".join(diagnostics), flush=True)
    print(
        f"Universe final={len(u)}, SH={(u['exchange']=='SH').sum()}, SZ={(u['exchange']=='SZ').sum()}",
        flush=True,
    )
    if len(u) < 2500:
        raise RuntimeError(f"Combined main-board universe unexpectedly small: {len(u)}")
    return u


if __name__ == "__main__":
    core.fetch_universe = robust_universe
    core.main()
