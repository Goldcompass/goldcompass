#!/usr/bin/env python3
"""
Gold Compass - スポット価格自動更新スクリプト

やること:
  1. metalpriceapi.com (無料枠あり、月100リクエスト) から金・プラチナ・パラジウム・銀の
     JPY建てスポット価格とUSD/JPYレートを取得する
  2. index.html 内の <script type="application/json" id="spot-data"> ブロックを
     最新の値に書き換える(前回値との差分から前日比を計算する)
  3. 取得に失敗した場合は index.html を一切変更せずに終了する
     (自動化がサイトを壊さないことを最優先する)

必要な環境変数:
  METALPRICEAPI_KEY - metalpriceapi.com (https://metalpriceapi.com/) の無料APIキー

このスクリプトは標準ライブラリのみで動作します(pip installは不要)。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

INDEX_HTML = os.path.join(os.path.dirname(__file__), "..", "index.html")
TROY_OUNCE_G = 31.1034768
TAX_RATE = 1.10

# metalpriceapi.com のシンボル -> サイト内部キー
SYMBOL_MAP = {
    "XAU": "gold",
    "XPT": "platinum",
    "XPD": "palladium",
    "XAG": "silver",
}
# 銀だけ kg 表示、それ以外は g 表示
UNIT_G = {"gold": 1, "platinum": 1, "palladium": 1, "silver": 1000}


def fetch_rates(api_key: str) -> dict:
    """metalpriceapi.com から JPY建てレートを取得する。失敗時は例外を投げる。"""
    currencies = ",".join(list(SYMBOL_MAP.keys()) + ["USD"])
    url = (
        "https://api.metalpriceapi.com/v1/latest"
        f"?api_key={api_key}&base=JPY&currencies={currencies}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "goldcompass-bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    if not payload.get("success"):
        raise RuntimeError(f"API error: {payload.get('error')}")

    rates = payload.get("rates") or {}
    missing = [s for s in SYMBOL_MAP if s not in rates]
    if missing:
        raise RuntimeError(f"missing symbols in response: {missing}")
    return rates


def compute_metal_prices(rates: dict) -> dict:
    """rates (JPY建て、1JPYあたりの各シンボル量) から円/g・円/kgの税込税抜価格を計算する。"""
    result = {}
    for symbol, key in SYMBOL_MAP.items():
        per_jpy = rates[symbol]
        if not per_jpy or per_jpy <= 0:
            raise RuntimeError(f"invalid rate for {symbol}: {per_jpy}")
        jpy_per_ounce = 1.0 / per_jpy
        jpy_per_gram = jpy_per_ounce / TROY_OUNCE_G
        unit_grams = UNIT_G[key]
        price_notax = jpy_per_gram * unit_grams
        price_tax = price_notax * TAX_RATE
        result[key] = {
            "price_notax": round(price_notax),
            "price_tax": round(price_tax),
        }
    return result


def compute_usdjpy(rates: dict) -> float | None:
    usd_per_jpy = rates.get("USD")
    if not usd_per_jpy or usd_per_jpy <= 0:
        return None
    return 1.0 / usd_per_jpy


def load_previous_data(html: str) -> dict | None:
    m = re.search(
        r'<script type="application/json" id="spot-data">\s*(\{.*?\})\s*</script>',
        html,
        re.S,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def build_new_data(previous: dict | None, new_prices: dict, usdjpy: float | None) -> dict:
    now_jst = datetime.now(timezone(timedelta(hours=9)))
    metals = {}
    prev_metals = (previous or {}).get("metals", {})
    for key, p in new_prices.items():
        prev = prev_metals.get(key)
        if prev and prev.get("price_tax"):
            change_yen = p["price_tax"] - prev["price_tax"]
            change_pct = (change_yen / prev["price_tax"]) * 100 if prev["price_tax"] else 0.0
        else:
            change_yen = 0
            change_pct = 0.0
        metals[key] = {
            "price_tax": p["price_tax"],
            "price_notax": p["price_notax"],
            "change_yen": change_yen,
            "change_pct": round(change_pct, 2),
        }

    return {
        "updated_jst": now_jst.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "usdjpy": round(usdjpy, 2) if usdjpy else (previous or {}).get("usdjpy"),
        "metals": metals,
    }


def write_new_data(html: str, new_data: dict) -> str:
    new_json = json.dumps(new_data, ensure_ascii=False, indent=2)
    # インデントをテンプレート側の2スペースに合わせて整形
    new_json_indented = "\n".join(
        ("  " + line if line.strip() else line) for line in new_json.splitlines()
    )
    replacement = (
        '<script type="application/json" id="spot-data">\n'
        f"{new_json_indented}\n"
        "</script>"
    )
    return re.sub(
        r'<script type="application/json" id="spot-data">\s*\{.*?\}\s*</script>',
        replacement.replace("\\", "\\\\"),
        html,
        count=1,
        flags=re.S,
    )


def main() -> int:
    api_key = os.environ.get("METALPRICEAPI_KEY")
    if not api_key:
        print("METALPRICEAPI_KEY is not set - skipping update (no changes made).")
        return 0

    if not os.path.exists(INDEX_HTML):
        print(f"index.html not found at {INDEX_HTML}")
        return 1

    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    try:
        rates = fetch_rates(api_key)
        new_prices = compute_metal_prices(rates)
        usdjpy = compute_usdjpy(rates)
    except (urllib.error.URLError, RuntimeError, ValueError, KeyError) as e:
        # 取得・計算に失敗した場合はサイトを壊さないよう、何も変更せず正常終了する
        print(f"Failed to fetch/compute spot prices, leaving index.html unchanged: {e}")
        return 0

    previous = load_previous_data(html)
    new_data = build_new_data(previous, new_prices, usdjpy)
    updated_html = write_new_data(html, new_data)

    if updated_html == html:
        print("No spot-data block found or no change - nothing written.")
        return 0

    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(updated_html)

    print("index.html updated with new spot prices:")
    print(json.dumps(new_data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
