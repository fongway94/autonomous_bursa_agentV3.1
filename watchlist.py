# watchlist.py
"""
Curated Bursa Malaysia watchlist + user-added custom tickers.

Custom tickers are stored in SQLite (`custom_watchlist` table) but a
JSON fallback file is also kept for backwards-compat with v1 data dirs.
"""

import os
import json
from db import connect, myt_iso, DATA_DIR

CUSTOM_WATCHLIST_FILE = os.path.join(DATA_DIR, "custom_watchlist.json")


BURSA_WATCHLIST = {
    "Technology": {
        "0138.KL": "MYEG Services Berhad",
        "0166.KL": "Inari Amertron Berhad",
        "0128.KL": "Frontken Corporation Berhad",
        "5005.KL": "Unisem (M) Berhad",
        "0097.KL": "ViTrox Corporation Berhad",
        "7204.KL": "D&O Green Technologies Berhad",
        "3867.KL": "Malaysian Pacific Industries Berhad",
        "5292.KL": "UWC Berhad",
        "7022.KL": "Globetronics Technology Berhad",
        "0127.KL": "JCY International Berhad",
    },
    "Financial Services": {
        "1155.KL": "Malayan Banking Berhad (Maybank)",
        "1295.KL": "Public Bank Berhad",
        "1023.KL": "CIMB Group Holdings Berhad",
        "1015.KL": "AMMB Holdings Berhad (AmBank)",
        "1066.KL": "RHB Bank Berhad",
        "5819.KL": "Hong Leong Bank Berhad",
        "1818.KL": "Bursa Malaysia Berhad",
        "5258.KL": "Bank Islam Malaysia Berhad",
        "1163.KL": "Allianz Malaysia Berhad",
    },
    "Utilities": {
        "5347.KL": "Tenaga Nasional Berhad (TNB)",
        "4677.KL": "YTL Corporation Berhad",
        "6742.KL": "YTL Power International Berhad",
        "6033.KL": "Petronas Gas Berhad",
        "5209.KL": "Gas Malaysia Berhad",
    },
    "Construction": {
        "5398.KL": "Gamuda Berhad",
        "5263.KL": "Sunway Construction Group Berhad",
        "3336.KL": "IJM Corporation Berhad",
        "9679.KL": "WCT Holdings Berhad",
        "7161.KL": "Kerjaya Prospek Group Berhad",
        "1651.KL": "Malaysian Resources Corporation Berhad (MRCB)",
        "5253.KL": "Econpile Holdings Berhad",
    },
    "Telecommunications": {
        "4863.KL": "Telekom Malaysia Berhad (TM)",
        "6947.KL": "CelcomDigi Berhad",
        "6012.KL": "Maxis Berhad",
        "6888.KL": "Axiata Group Berhad",
        "0172.KL": "OCK Group Berhad",
    },
    "Property & REITs": {
        "8664.KL": "S P Setia Berhad",
        "8583.KL": "Mah Sing Group Berhad",
        "8206.KL": "Eco World Development Group Berhad",
        "5211.KL": "Sunway Berhad",
        "5288.KL": "Sime Darby Property Berhad",
        "5148.KL": "UEM Sunrise Berhad",
        "5249.KL": "IOI Properties Group Berhad",
        "5212.KL": "Pavilion Real Estate Investment Trust",
        "5227.KL": "IGB Real Estate Investment Trust",
    },
    "Consumer Products": {
        "4707.KL": "Nestle (Malaysia) Berhad",
        "4065.KL": "PPB Group Berhad",
        "7084.KL": "QL Resources Berhad",
        "5296.KL": "Mr D.I.Y. Group (M) Berhad",
        "3255.KL": "Heineken Malaysia Berhad",
        "2836.KL": "Carlsberg Brewery Malaysia Berhad",
        "3182.KL": "Genting Berhad",
        "4715.KL": "Genting Malaysia Berhad",
        "7052.KL": "Padini Holdings Berhad",
        "6599.KL": "AEON Co. (M) Berhad",
        "0157.KL": "Focus Point Holdings Berhad",
    },
    "Healthcare": {
        "5878.KL": "KPJ Healthcare Berhad",
        "5225.KL": "IHH Healthcare Berhad",
        "7113.KL": "Top Glove Corporation Berhad",
        "5168.KL": "Hartalega Holdings Berhad",
        "7153.KL": "Kossan Rubber Industries Berhad",
        "7106.KL": "Supermax Corporation Berhad",
    },
    "Energy": {
        "7108.KL": "Dialog Group Berhad",
        "7277.KL": "Yinson Holdings Berhad",
        "5199.KL": "Hibiscus Petroleum Berhad",
        "5132.KL": "Deleum Berhad",
        "5210.KL": "Bumi Armada Berhad",
        "5243.KL": "Velesto Energy Berhad",
        "5244.KL": "Wasco Berhad",
    },
    "Plantation": {
        "2445.KL": "Kuala Lumpur Kepong Berhad (KLK)",
        "1961.KL": "IOI Corporation Berhad",
        "5285.KL": "SD Guthrie Berhad (Sime Darby Plantation)",
        "5222.KL": "FGV Holdings Berhad",
        "5245.KL": "Sarawak Oil Palms Berhad",
    },
}


# -------------------------------------------------------------------------
# Custom tickers — SQLite-backed with JSON fallback
# -------------------------------------------------------------------------

def _ensure_custom_table():
    with connect() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS custom_watchlist ("
            " ticker TEXT PRIMARY KEY, name TEXT, sector TEXT, added_at TEXT)"
        )
    # Migrate from JSON file if present
    if os.path.exists(CUSTOM_WATCHLIST_FILE):
        try:
            with open(CUSTOM_WATCHLIST_FILE) as f:
                items = json.load(f)
            for ticker, val in (items or {}).items():
                if isinstance(val, dict):
                    name, sector = val.get("name", ticker), val.get("sector", "Custom")
                else:
                    name, sector = str(val), "Custom"
                with connect() as c:
                    c.execute(
                        "INSERT OR IGNORE INTO custom_watchlist "
                        "(ticker, name, sector, added_at) VALUES (?,?,?,?)",
                        (ticker, name, sector, myt_iso()),
                    )
        except Exception:
            pass


_ensure_custom_table()


def load_custom_watchlist_tickers() -> dict:
    with connect(readonly=True) as c:
        rows = c.execute(
            "SELECT ticker, name, sector FROM custom_watchlist"
        ).fetchall()
    return {r["ticker"]: {"name": r["name"], "sector": r["sector"]} for r in rows}


def save_custom_watchlist_tickers(custom_tickers: dict) -> bool:
    # Reset and reinsert (used for bulk overrides)
    with connect() as c:
        c.execute("DELETE FROM custom_watchlist")
        for ticker, v in (custom_tickers or {}).items():
            if isinstance(v, dict):
                c.execute(
                    "INSERT INTO custom_watchlist (ticker, name, sector, added_at) "
                    "VALUES (?,?,?,?)",
                    (ticker, v.get("name", ticker), v.get("sector", "Custom"),
                     myt_iso()),
                )
    return True


def add_custom_ticker(ticker: str, name: str, sector: str = "Custom") -> str:
    ticker = ticker.strip().upper()
    if not ticker.endswith(".KL"):
        ticker += ".KL"
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO custom_watchlist "
            "(ticker, name, sector, added_at) VALUES (?,?,?,?)",
            (ticker, name.strip(), sector.strip(), myt_iso()),
        )
    return ticker


def remove_custom_ticker(ticker: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM custom_watchlist WHERE ticker=?", (ticker,))


def get_all_tickers() -> list[str]:
    tickers = []
    for items in BURSA_WATCHLIST.values():
        tickers.extend(items.keys())
    tickers.extend(load_custom_watchlist_tickers().keys())
    return sorted(set(tickers))


def get_ticker_sector(ticker: str) -> str:
    for sector, items in BURSA_WATCHLIST.items():
        if ticker in items:
            return sector
    custom = load_custom_watchlist_tickers().get(ticker)
    return custom["sector"] if custom else "Unknown"


def get_ticker_name(ticker: str) -> str:
    for items in BURSA_WATCHLIST.values():
        if ticker in items:
            return items[ticker]
    custom = load_custom_watchlist_tickers().get(ticker)
    return custom["name"] if custom else ticker


# -------------------------------------------------------------------------
# Shariah-compliant filter (optional, user-toggle)
# -------------------------------------------------------------------------
#
# Default *non*-compliant set based on Securities Commission Malaysia
# Shariah-compliant Securities List (best-effort, may drift between
# SC's twice-yearly revisions). Users should verify with their broker.
#
# Banks (conventional interest income), brewers, gaming companies are
# typically excluded.

SHARIAH_NON_COMPLIANT = {
    # Conventional banks
    "1155.KL", "1295.KL", "1023.KL", "1015.KL", "1066.KL", "5819.KL",
    "1818.KL",  # Bursa MY itself (mixed activities)
    # Brewers
    "3255.KL", "2836.KL",
    # Gaming
    "3182.KL", "4715.KL",
    # Conventional insurance (mixed model)
    "1163.KL",
}


def is_shariah_compliant(ticker: str) -> bool:
    """Best-effort check; user can override in Settings."""
    return ticker not in SHARIAH_NON_COMPLIANT


def get_all_tickers_shariah_only() -> list[str]:
    return [t for t in get_all_tickers() if is_shariah_compliant(t)]
