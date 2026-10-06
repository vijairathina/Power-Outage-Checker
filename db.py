"""
Database module for storing TNEB power outage history in SQLite.
Stores EXACTLY ONE RECORD PER MONTH if there is a power outage.
"""

import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outages.db")


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = DB_PATH):
    """Initialize database tables. Keeps exactly one record per month."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()

        # Monthly single-record table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monthly_outages (
                month_key TEXT PRIMARY KEY,        -- YYYY-MM (e.g., '2026-10')
                outage_date TEXT NOT NULL,         -- YYYY-MM-DD
                raw_date TEXT,                     -- Original date from portal
                start_time TEXT NOT NULL,          -- HH:MM (e.g. '09:00')
                end_time TEXT NOT NULL,            -- HH:MM (e.g. '14:00')
                circle TEXT,                       -- Circle name
                town TEXT,                         -- Town/Area
                substation TEXT,                   -- Substation
                feeder TEXT,                       -- Feeder
                location TEXT,                     -- Locations/Streets affected
                type_of_work TEXT,                 -- Reason/Work type
                has_outage INTEGER DEFAULT 1,      -- 1 = Outage scheduled, 0 = None
                raw_json TEXT,                     -- Full JSON if multiple slots exist
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Sync tracking table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_history (
                month_key TEXT PRIMARY KEY,        -- YYYY-MM
                last_checked_at TIMESTAMP,
                status TEXT NOT NULL               -- 'SUCCESS_WITH_OUTAGE', 'CHECKED_NO_OUTAGE'
            )
        """)
        conn.commit()


def normalize_date(raw_date_str: str) -> Tuple[str, str]:
    """Parse date like '06-10-2026' into ('2026-10-06', '2026-10')."""
    raw = raw_date_str.strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m")
        except ValueError:
            continue
    today = datetime.now()
    return raw, today.strftime("%Y-%m")


def has_month_data(month_key: str, db_path: str = DB_PATH) -> bool:
    """Returns True if we already have the power outage record stored for this month."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT month_key, has_outage FROM monthly_outages WHERE month_key = ?",
            (month_key,)
        )
        row = cursor.fetchone()
        if row and row["has_outage"] == 1:
            return True
    return False


def set_sync_status(month_key: str, status: str, db_path: str = DB_PATH):
    """Record sync attempt for the given month."""
    now_iso = datetime.now().isoformat()
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sync_history (month_key, last_checked_at, status)
            VALUES (?, ?, ?)
            ON CONFLICT(month_key) DO UPDATE SET
                last_checked_at = excluded.last_checked_at,
                status = excluded.status
        """, (month_key, now_iso, status))
        conn.commit()


def save_single_month_outage(
    records: List[Dict[str, Any]],
    month_key: str,
    circle: str,
    db_path: str = DB_PATH
) -> Optional[Dict[str, Any]]:
    """
    Saves EXACTLY ONE consolidated record for the month into monthly_outages.
    Picks the primary/earliest outage for the month and bundles any extra slots in raw_json.
    """
    if not records:
        return None

    # Sort records by date then start_time
    def sort_key(r):
        raw_d = r.get("Date Of Outage") or r.get("date") or r.get("outage_date") or ""
        iso_d, _ = normalize_date(raw_d)
        st = r.get("From Date") or r.get("start_time") or ""
        return (iso_d, st)

    sorted_records = sorted(records, key=sort_key)
    primary = sorted_records[0]

    raw_date = primary.get("Date Of Outage") or primary.get("raw_date") or primary.get("date") or ""
    outage_date, m_key = normalize_date(raw_date)
    if not month_key:
        month_key = m_key

    start_time = (primary.get("From Date") or primary.get("start_time") or "").strip()
    end_time = (primary.get("To Date") or primary.get("end_time") or "").strip()
    town = (primary.get("Town") or primary.get("town") or "").strip()
    substation = (primary.get("Substation") or primary.get("substation") or "").strip()
    feeder = (primary.get("Feeder") or primary.get("feeder") or "").strip()
    location = (primary.get("Location") or primary.get("location") or "").strip()
    type_of_work = (primary.get("Type of work") or primary.get("type_of_work") or "").strip()

    raw_json_str = json.dumps(sorted_records)

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO monthly_outages
            (month_key, outage_date, raw_date, start_time, end_time, circle, town, substation, feeder, location, type_of_work, has_outage, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(month_key) DO UPDATE SET
                outage_date = excluded.outage_date,
                raw_date = excluded.raw_date,
                start_time = excluded.start_time,
                end_time = excluded.end_time,
                circle = excluded.circle,
                town = excluded.town,
                substation = excluded.substation,
                feeder = excluded.feeder,
                location = excluded.location,
                type_of_work = excluded.type_of_work,
                has_outage = 1,
                raw_json = excluded.raw_json,
                created_at = CURRENT_TIMESTAMP
        """, (month_key, outage_date, raw_date, start_time, end_time, circle, town, substation, feeder, location, type_of_work, raw_json_str))
        conn.commit()

    return {
        "month_key": month_key,
        "outage_date": outage_date,
        "raw_date": raw_date,
        "start_time": start_time,
        "end_time": end_time,
        "circle": circle,
        "town": town,
        "substation": substation,
        "feeder": feeder,
        "location": location,
        "type_of_work": type_of_work,
        "has_outage": 1,
        "all_records": sorted_records
    }


def get_month_outage(month_key: str, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    """Retrieve the single outage record for a specific month (e.g. '2026-10')."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM monthly_outages WHERE month_key = ? AND has_outage = 1",
            (month_key,)
        )
        row = cursor.fetchone()
        if row:
            d = dict(row)
            if d.get("raw_json"):
                try:
                    d["all_records"] = json.loads(d["raw_json"])
                except Exception:
                    d["all_records"] = []
            return d
        return None


def get_all_monthly_history(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Retrieve all monthly outage records (one row per month)."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM monthly_outages
            ORDER BY month_key DESC
        """)
        results = []
        for row in cursor.fetchall():
            d = dict(row)
            if d.get("raw_json"):
                try:
                    d["all_records"] = json.loads(d["raw_json"])
                except Exception:
                    d["all_records"] = []
            results.append(d)
        return results
