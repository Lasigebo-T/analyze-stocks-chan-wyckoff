#!/usr/bin/env python3
"""Reliable entry point for the terminal-wedge scanner.

Builds the Shanghai main-board universe from the official SSE suggestion file
and the Shenzhen main-board universe from the TDX security table, then delegates
the K-line pattern scan to ``scan_terminal_wedge.py``.
"""
from __future__ import annotations

import re
import sys
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
    r.encoding = "utf-8"
    pairs = re.findall(
        r"val\s*:\s*[\"'](\d{6})[\"']\s*,\s*val2\s*:\s*[\"']([^\"']+)",
        r.text,
    )
    rows: list[dict[str, Any]] = []
    for code, raw_name in pairs:
        name = clean_name(raw_name)
        if code.startswith(SSE_PREFIXES) and not core.BAD_NAME.search(name):
            rows.append({"code": code, "name": name, "exchange": "SH", "universe_source": "SSE"})
    if len(rows) < 1200:
        raise RuntimeError(f"SSE main-board list unexpectedly small: {len(rows)}")
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
            empty_main_chunks = 0
            # Shenzhen main-board codes are located near the front of market 0's
            # code table. 12,000 entries leaves ample margin while avoiding the
            # unrelated fund/bond tail of the 20k+ table.
            for start in range(0, min(count, 12000), 1000):
                chunk = api.get_security_list(0, start) or []
                if not chunk:
                    break
                found = 0
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
                        found += 1
                empty_main_chunks = empty_main_chunks + 1 if found == 0 else 0
                if start >= 6000 and empty_main_chunks >= 3:
                    break
                if len(chunk) < 1000:
                    break
            api.disconnect()
            rows = list(collected.values())
            if len(rows) < 1000:
                raise RuntimeError(f"TDX Shenzhen main-board list unexpectedly small: {len(rows)}")
            return rows
        except Exception as exc:
            failures.append(f"{host}:{port} {type(exc).__name__}: {exc}")
            try:
                api.disconnect()
            except Exception:
                pass
    raise RuntimeError("No TDX universe server succeeded: " + " | ".join(failures))


def fetch_workbook_supplement() -> list[dict[str, Any]]:
    """Small, current public list used only to supplement recent symbols."""
    url = (
        "https://raw.githubusercontent.com/89529738/ashare-public-data/main/data/"
        "股票池合并_核准_技术指标_latest.xlsx"
    )
    try:
        wb = pd.read_excel(url, dtype=str, usecols=["股票代码", "股票名称"])
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

    try:
        sz = fetch_sz_mainboard_tdx()
        rows.extend(sz)
        diagnostics.append(f"SZ_TDX={len(sz)}")
    except Exception as exc:
        diagnostics.append(f"SZ_TDX_FAILED={type(exc).__name__}:{exc}")
        traceback.print_exc()

    supplement = fetch_workbook_supplement()
    rows.extend(supplement)
    diagnostics.append(f"SUPPLEMENT={len(supplement)}")

    # Last-resort deterministic Shanghai code ranges. Invalid codes are harmless:
    # the K-line fetcher drops them. This path normally contributes nothing because
    # the official SSE list is available.
    if not any(str(x.get("code", "")).startswith(SSE_PREFIXES) for x in rows):
        for prefix in SSE_PREFIXES:
            rows.extend(
                {"code": f"{prefix}{i:03d}", "name": f"{prefix}{i:03d}",
                 "exchange": "SH", "universe_source": "generated-fallback"}
                for i in range(1000)
            )
        diagnostics.append("SSE_GENERATED=4000")

    if not rows:
        raise RuntimeError("All universe sources failed")

    u = pd.DataFrame(rows)
    u["code"] = u["code"].astype(str).str.extract(r"(\d{6})", expand=False)
    u["name"] = u["name"].map(clean_name)
    u = u.dropna(subset=["code"]).drop_duplicates("code", keep="first")
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
