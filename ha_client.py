"""
Home Assistant REST API Client.
Updates sensor state and rich attributes, keeping the month's outage information
active throughout the month.
"""

import sys
from datetime import datetime
from typing import Any, Dict, List, Optional
import requests

# Ensure safe printing on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def format_lovelace_markdown(outage: Optional[Dict[str, Any]], month_str: str) -> str:
    """Generate clean Markdown text suitable for a Home Assistant Lovelace card."""
    if not outage or not outage.get("has_outage"):
        return f"### ⚡ TNEB Power Status ({month_str})\n🟢 **No power outage notified** for {month_str}."

    o_date = outage.get("outage_date", "")
    start_t = outage.get("start_time", "")
    end_t = outage.get("end_time", "")
    town = outage.get("town", "")
    loc = outage.get("location", "")
    work = outage.get("type_of_work", "")

    today_str = datetime.now().strftime("%Y-%m-%d")
    is_today = (o_date == today_str)
    is_past = (o_date < today_str)

    badge = "🔴 **TODAY**" if is_today else ("⚪ *(Completed)*" if is_past else "🟡 **Upcoming**")

    md = [
        f"### ⚡ TNEB Scheduled Outage ({month_str})",
        "",
        f"- {badge} **{o_date}** ({start_t} – {end_t})",
        f"  - **Area / Town:** {town}",
    ]
    if loc:
        md.append(f"  - **Locations:** {loc}")
    if work:
        md.append(f"  - **Work:** {work}")

    # If there are additional slots in the month
    all_recs = outage.get("all_records", [])
    if len(all_recs) > 1:
        md.append("")
        md.append(f"*(Note: {len(all_recs)} feeder sections scheduled in this month)*")

    return "\n".join(md)


def push_to_home_assistant(
    ha_config: Dict[str, Any],
    month_outage: Optional[Dict[str, Any]],
    all_history: List[Dict[str, Any]],
    month_key: str,
    circle: str,
    filter_applied: Optional[str] = None
) -> bool:
    """
    Sends the single monthly outage record to Home Assistant as a sensor.
    Retains all outage details in attributes throughout the entire month.
    """
    if not ha_config.get("enabled", False):
        print("[Home Assistant] Integration disabled in config. Skipping HA push.")
        return False

    url = ha_config.get("url", "").rstrip("/")
    token = ha_config.get("token", "").strip()
    entity_id = ha_config.get("entity_id", "sensor.tneb_power_outage")

    if not url or not token:
        print("[Home Assistant] Missing 'url' or 'token' in config. Cannot send data.")
        return False

    endpoint = f"{url}/api/states/{entity_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    today_str = datetime.now().strftime("%Y-%m-%d")

    if month_outage and month_outage.get("has_outage"):
        o_date = month_outage.get("outage_date", "")
        start_t = month_outage.get("start_time", "")
        end_t = month_outage.get("end_time", "")
        town = month_outage.get("town", "")
        loc = month_outage.get("location", "")
        substation = month_outage.get("substation", "")
        feeder = month_outage.get("feeder", "")
        work = month_outage.get("type_of_work", "")
        has_outage = True

        if o_date == today_str:
            state = f"Outage Today: {start_t} - {end_t}"
        elif o_date > today_str:
            state = f"{o_date} {start_t} - {end_t}"
        else:
            state = f"Completed ({o_date})"
    else:
        o_date = None
        start_t = None
        end_t = None
        town = None
        loc = None
        substation = None
        feeder = None
        work = None
        has_outage = False
        state = "No Outage"

    card_markdown = format_lovelace_markdown(month_outage, month_key)

    attributes = {
        "friendly_name": f"TNEB Power Outage ({circle})",
        "icon": "mdi:transmission-tower-off" if has_outage else "mdi:transmission-tower",
        "date": o_date,
        "start_time": start_t,
        "end_time": end_t,
        "time": f"{start_t} - {end_t}" if (start_t and end_t) else None,
        "town": town,
        "substation": substation,
        "feeder": feeder,
        "location": loc,
        "type_of_work": work,
        "current_month": month_key,
        "circle": circle,
        "filter": filter_applied or "All",
        "has_outage": has_outage,
        "monthly_history_count": len(all_history),
        "card_markdown": card_markdown,
        "last_updated": datetime.now().isoformat(),
    }

    payload = {
        "state": state,
        "attributes": attributes,
    }

    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=10)
        if response.status_code in (200, 201):
            print(f"[Home Assistant] Successfully updated '{entity_id}' -> State: '{state}'.")

            if ha_config.get("send_persistent_notification", False) and has_outage:
                send_notification(url, token, month_outage, circle)

            return True
        else:
            print(f"[Home Assistant] Failed to update entity. Status {response.status_code}: {response.text}")
            return False
    except Exception as e:
        print(f"[Home Assistant] Connection error: {e}")
        return False


def send_notification(url: str, token: str, outage: Dict[str, Any], circle: str):
    """Sends a persistent notification to Home Assistant."""
    endpoint = f"{url}/api/services/persistent_notification/create"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    date = outage.get("outage_date")
    start = outage.get("start_time")
    end = outage.get("end_time")
    town = outage.get("town")

    msg = f"Scheduled power outage for **{town}** ({circle}) on **{date}** from **{start}** to **{end}**."
    payload = {
        "title": "⚡ Scheduled Power Outage Alert",
        "message": msg,
        "notification_id": f"tneb_outage_{date}",
    }
    try:
        requests.post(endpoint, headers=headers, json=payload, timeout=8)
    except Exception:
        pass


def test_connection(ha_config: Dict[str, Any]) -> bool:
    """Test connectivity and authentication with the Home Assistant instance."""
    url = ha_config.get("url", "").rstrip("/")
    token = ha_config.get("token", "").strip()

    if not url or not token:
        print("[Home Assistant Test] URL or Token is missing.")
        return False

    endpoint = f"{url}/api/"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.get(endpoint, headers=headers, timeout=8)
        if r.status_code == 200:
            data = r.json()
            print(f"[Home Assistant Test] Success! Connected to Home Assistant (API message: {data.get('message')}).")
            return True
        else:
            print(f"[Home Assistant Test] Failed with status {r.status_code}: {r.text}")
            return False
    except Exception as e:
        print(f"[Home Assistant Test] Connection failed: {e}")
        return False


def fetch_ha_sensor_outage(ha_config: Dict[str, Any], month_key: str) -> Optional[Dict[str, Any]]:
    """
    Fetches the current state and attributes from Home Assistant sensor.tneb_power_outage.
    If Home Assistant already contains a recorded outage for `month_key` with has_outage=True,
    returns the record dictionary. This prevents wiping monthly outages when syncing across devices.
    """
    if not ha_config or not ha_config.get("enabled", False):
        return None

    url = ha_config.get("url", "").rstrip("/")
    token = ha_config.get("token", "").strip()
    entity_id = ha_config.get("entity_id", "sensor.tneb_power_outage")

    if not url or not token:
        return None

    endpoint = f"{url}/api/states/{entity_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        r = requests.get(endpoint, headers=headers, timeout=8)
        if r.status_code == 200:
            data = r.json()
            attrs = data.get("attributes", {})
            has_outage = attrs.get("has_outage", False)
            sensor_month = attrs.get("current_month", "")
            o_date = attrs.get("date")

            if has_outage and o_date and (sensor_month == month_key or str(o_date).startswith(month_key)):
                return {
                    "outage_date": o_date,
                    "raw_date": attrs.get("raw_date", o_date),
                    "start_time": attrs.get("start_time", ""),
                    "end_time": attrs.get("end_time", ""),
                    "circle": attrs.get("circle", ""),
                    "town": attrs.get("town", ""),
                    "substation": attrs.get("substation", ""),
                    "feeder": attrs.get("feeder", ""),
                    "location": attrs.get("location", ""),
                    "type_of_work": attrs.get("type_of_work", ""),
                    "month_key": month_key,
                    "has_outage": 1,
                }
    except Exception as e:
        print(f"[Home Assistant Sync] Could not check existing sensor state: {e}")

    return None

