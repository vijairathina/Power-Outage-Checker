#!/usr/bin/env python3
"""
TNEB / TNPDCL Scheduled Power Outage Scraper & Home Assistant Sync
Cross-platform: Windows & Linux (Raspberry Pi, Ubuntu, Debian, etc.)

Features:
- Daily check capability (standalone cron or continuous daemon)
- Monthly caching: Once outage info for the month is obtained, skips portal queries for the rest of the month
- Stores JUST ONE RECORD PER MONTH in SQLite database (outages.db)
- Tracks Date, Start Time, End Time, Town, Substation, Feeder, and Location
- Native Home Assistant integration via REST API (keeps monthly data active and updates sensor)
"""

import argparse
import asyncio
from datetime import datetime, timedelta
import os
import platform
import re
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple

# Ensure safe printing on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup
from playwright.async_api import Browser, Page, async_playwright

import config as cfg_module
import db
import ha_client

# Try importing ddddocr (primary fast OCR solver)
try:
    import ddddocr
    _HAS_DDDDOCR = True
except ImportError:
    _HAS_DDDDOCR = False

# Try importing pytesseract & cv2 (fallback OCR solver)
try:
    import cv2
    import pytesseract
    _HAS_TESSERACT = True
except ImportError:
    _HAS_TESSERACT = False

URL = "https://www.tnebltd.gov.in/outages/viewshutdown.xhtml"


def configure_tesseract():
    """Auto-detect Tesseract binary path on Windows / Linux if available."""
    if not _HAS_TESSERACT:
        return
    if shutil.which("tesseract"):
        return

    if platform.system() == "Windows":
        common_paths = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
        ]
        for p in common_paths:
            if os.path.isfile(p):
                pytesseract.pytesseract.tesseract_cmd = p
                return


configure_tesseract()
_ddddocr_instance = ddddocr.DdddOcr(show_ad=False) if _HAS_DDDDOCR else None


def solve_captcha_from_bytes(image_bytes: bytes) -> str:
    """Solve CAPTCHA using ddddocr first; fallback to OpenCV + Tesseract."""
    if _ddddocr_instance is not None:
        try:
            result = _ddddocr_instance.classification(image_bytes)
            cleaned = re.sub(r"[^A-Za-z0-9]", "", result or "").strip()
            if cleaned and len(cleaned) >= 4:
                return cleaned
        except Exception as e:
            print(f"[OCR] ddddocr attempt failed: {e}")

    if _HAS_TESSERACT:
        try:
            nparr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is not None:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                resized = cv2.resize(gray, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
                _, thresh = cv2.threshold(resized, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                denoised = cv2.medianBlur(thresh, 3)
                custom_config = r"--oem 3 --psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
                text = pytesseract.image_to_string(denoised, config=custom_config)
                cleaned = re.sub(r"[^A-Za-z0-9]", "", text).strip()
                if cleaned:
                    return cleaned
        except Exception as e:
            print(f"[OCR] pytesseract attempt failed: {e}")

    return ""


async def launch_adaptive_browser(p, headless: bool = True) -> Browser:
    """Cross-platform browser launcher."""
    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]

    if platform.system() == "Windows":
        channels = ["msedge", "chrome", "chromium", None]
    else:
        channels = ["chromium", "chrome", "msedge", None]

    custom_paths = [
        os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"),
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for cp in custom_paths:
        if cp and os.path.exists(cp):
            try:
                browser = await p.chromium.launch(
                    executable_path=cp,
                    headless=headless,
                    args=launch_args,
                )
                print(f"[Browser] Launched with system binary: {cp}")
                return browser
            except Exception:
                pass

    for channel in channels:
        try:
            kwargs = {"headless": headless, "args": launch_args}
            if channel:
                kwargs["channel"] = channel
            browser = await p.chromium.launch(**kwargs)
            channel_name = channel if channel else "playwright-bundled"
            print(f"[Browser] Launched successfully with {channel_name} (headless={headless})")
            return browser
        except Exception:
            continue

    raise RuntimeError("Could not find or launch any compatible browser.")


async def select_circle(page: Page, circle_name: str) -> bool:
    """Select target circle from PrimeFaces SelectOneMenu dropdown."""
    circle_upper = circle_name.strip().upper()

    try:
        dropdown = await page.wait_for_selector('div[id*="appcat"], label[id*="appcat_label"]', timeout=8000)
        if dropdown:
            await dropdown.click()
            await page.wait_for_timeout(300)
            option = await page.wait_for_selector(
                f'.ui-selectonemenu-item:has-text("{circle_upper}"), li[data-label*="{circle_upper}"]',
                timeout=4000
            )
            if option:
                await option.click()
                print(f"[Select] Selected circle '{circle_name}' via UI.")
                return True
    except Exception:
        pass

    try:
        found = await page.evaluate(f"""() => {{
            const select = document.querySelector('select[id*="appcat_input"]') || document.querySelector('select');
            if (!select) return false;
            for (let opt of select.options) {{
                if (opt.text.toUpperCase().includes('{circle_upper}')) {{
                    select.value = opt.value;
                    select.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return true;
                }}
            }}
            return false;
        }}""")
        if found:
            print(f"[Select] Selected circle '{circle_name}' via JS evaluation.")
            return True
    except Exception as e:
        print(f"[Select] Failed to select circle: {e}")

    return False


async def scrape_portal(
    circle: str = "KRISHNAGIRI",
    max_retries: int = 5,
    headless: bool = True,
) -> Optional[List[Dict[str, Any]]]:
    """Scrapes raw outage records directly from TNEB portal."""
    async with async_playwright() as p:
        browser = await launch_adaptive_browser(p, headless=headless)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        for attempt in range(1, max_retries + 1):
            print(f"\n--- [Attempt {attempt}/{max_retries}] Connecting to TNEB Outages portal ---")
            try:
                await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"[Navigation Error] {e}. Retrying...")
                await page.wait_for_timeout(2000)
                continue

            try:
                await page.wait_for_selector('img[id*="imgCaptchaId"], img[src*="captcha"]', timeout=15000)
            except Exception:
                print("[Warning] Page did not load captcha element in time. Retrying...")
                continue

            # Select Circle
            selected = await select_circle(page, circle)
            if not selected:
                print(f"[Warning] Circle '{circle}' could not be selected.")

            # Solve CAPTCHA
            captcha_img = await page.query_selector('img[id*="imgCaptchaId"], img[src*="captcha"]')
            if not captcha_img:
                print("[Error] CAPTCHA image element not found.")
                continue

            img_bytes = await captcha_img.screenshot()
            solved_text = solve_captcha_from_bytes(img_bytes)
            print(f"[OCR] Recognized CAPTCHA: '{solved_text}'")

            if not solved_text or len(solved_text) < 4:
                print("[OCR] Confidence low, refreshing captcha...")
                refresh_btn = await page.query_selector('button[id*="j_idt65"], a[onclick*="generateCaptchaText"]')
                if refresh_btn:
                    await refresh_btn.click()
                    await page.wait_for_timeout(1500)
                continue

            # Fill CAPTCHA
            target_input = await page.query_selector('input[id*="cap"], input[name*="cap"]')
            if not target_input:
                target_input = await page.query_selector('input[type="text"]')
            if target_input:
                await target_input.fill("")
                await target_input.fill(solved_text)

            # Submit
            submit_btn = await page.query_selector('button[id*="submit3"], button:has-text("Submit")')
            if not submit_btn:
                print("[Error] Submit button not found.")
                continue

            await submit_btn.click()
            print("[Submit] Form submitted, awaiting response...")
            await page.wait_for_timeout(4000)

            content = await page.content()
            soup = BeautifulSoup(content, "html.parser")

            error_tags = soup.find_all(class_=lambda c: c and ("ui-message" in c or "ui-messages" in c))
            error_text = " ".join([e.get_text(strip=True) for e in error_tags]).lower()
            if "invalid captcha" in error_text or "enter text shown in the image" in error_text:
                print(f"[CAPTCHA Rejected] Portal message: '{error_text}'. Retrying...")
                continue

            # Parse Outage Table
            tables = soup.find_all("table")
            for table in tables:
                rows = table.find_all("tr")
                data: List[List[str]] = []
                for row in rows:
                    cells = [td.get_text(strip=True) for td in row.find_all(["th", "td"])]
                    if cells:
                        data.append(cells)

                if len(data) > 1:
                    headers = data[0]
                    rows_data = data[1:]
                    col_count = len(headers)
                    records = []
                    for r in rows_data:
                        if len(r) >= col_count:
                            row_dict = {headers[i]: r[i] for i in range(col_count)}
                            records.append(row_dict)

                    if records:
                        print(f"[Success] Found {len(records)} outage records for circle '{circle}'.")
                        await browser.close()
                        return records

            if "no records" in content.lower() or "no data" in content.lower():
                print(f"[Info] Portal loaded: No scheduled shutdowns currently posted for '{circle}'.")
                await browser.close()
                return []

        await browser.close()
        print("[End] Maximum attempts reached without retrieving records.")
        return None


def run_sync_cycle(
    circle: str,
    area_filter: Optional[str],
    force: bool = False,
    headless: bool = True,
    ha_cfg: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Executes a single sync evaluation:
    1. Checks if current month data is already stored in SQLite (ONE RECORD PER MONTH).
    2. If yes and not forced, skips portal query.
    3. If no, scrapes portal, extracts matching outage, and saves 1 record for this month into SQLite.
    4. Pushes latest state and monthly outage to Home Assistant.
    """
    db.init_db()
    current_month_key = datetime.now().strftime("%Y-%m")
    already_obtained = db.has_month_data(current_month_key)

    month_record: Optional[Dict[str, Any]] = None

    if already_obtained and not force:
        print(f"\n[Monthly Cache] Outage information for current month ({current_month_key}) is ALREADY obtained.")
        print("[Monthly Cache] Skipping portal query for the rest of this month. (Use --force to override)")
        month_record = db.get_month_outage(current_month_key)
    else:
        reason = "Forced refresh" if force else "No record found yet for this month"
        print(f"\n[Scraper] Querying portal ({reason})...")
        raw_records = asyncio.run(scrape_portal(circle=circle, headless=headless))

        if raw_records is None:
            print("[Scraper] Scrape failed. Using cached database record if available.")
            month_record = db.get_month_outage(current_month_key)
        elif len(raw_records) == 0:
            print(f"[Scraper] No outages posted yet by TNEB for month {current_month_key}.")
            db.set_sync_status(current_month_key, "CHECKED_NO_OUTAGE")
            month_record = None
        else:
            # Filter for the target area/town (e.g., Hosur)
            matching_records = raw_records
            if area_filter and area_filter.strip():
                pat = re.compile(area_filter.strip(), re.IGNORECASE)
                matching_records = [
                    r for r in raw_records
                    if pat.search(f"{r.get('Town', '')} {r.get('Substation', '')} {r.get('Feeder', '')} {r.get('Location', '')}")
                ]

            if matching_records:
                # Save EXACTLY ONE record for this month
                month_record = db.save_single_month_outage(
                    records=matching_records,
                    month_key=current_month_key,
                    circle=circle,
                )
                db.set_sync_status(current_month_key, "SUCCESS_WITH_OUTAGE")
                print(f"[DB] Successfully stored 1 outage record for month {current_month_key} ({month_record['outage_date']} {month_record['start_time']}-{month_record['end_time']}).")
            else:
                print(f"[Scraper] No outages matched filter '{area_filter}' in circle '{circle}' for month {current_month_key}.")
                db.set_sync_status(current_month_key, "CHECKED_NO_OUTAGE")
                month_record = None

    all_history = db.get_all_monthly_history()

    # Push to Home Assistant
    if ha_cfg and ha_cfg.get("enabled", False):
        ha_client.push_to_home_assistant(
            ha_config=ha_cfg,
            month_outage=month_record,
            all_history=all_history,
            month_key=current_month_key,
            circle=circle,
            filter_applied=area_filter,
        )
    else:
        print("[Home Assistant] Integration disabled or not configured in config.json.")

    return month_record


async def run_daemon_loop(args, config: Dict[str, Any]):
    """Runs continuously as a background daemon, executing once daily."""
    target_time_str = args.check_time or config.get("daily_check_time", "08:00")
    print(f"\n[Daemon] Starting background service. Daily check scheduled at: {target_time_str} every day.")

    while True:
        now = datetime.now()
        print(f"\n[Daemon] === Running daily check at {now.strftime('%Y-%m-%d %H:%M:%S')} ===")
        try:
            effective_filter = None if args.all else (args.area_filter or config.get("filter"))
            run_sync_cycle(
                circle=args.circle or config.get("circle", "KRISHNAGIRI"),
                area_filter=effective_filter,
                force=args.force,
                headless=args.headless,
                ha_cfg=config.get("home_assistant"),
            )
        except Exception as e:
            print(f"[Daemon Error] Exception during sync cycle: {e}")

        now = datetime.now()
        try:
            target_hour, target_min = map(int, target_time_str.split(":"))
        except Exception:
            target_hour, target_min = 8, 0

        next_run = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)

        sleep_seconds = (next_run - now).total_seconds()
        print(f"[Daemon] Next daily check scheduled for: {next_run.strftime('%Y-%m-%d %H:%M:%S')} (in {sleep_seconds/3600:.1f} hours).")
        await asyncio.sleep(sleep_seconds)


def print_single_outage(title: str, outage: Optional[Dict[str, Any]]):
    """Pretty prints a single monthly outage record."""
    print("\n" + "=" * 90)
    print(f" {title}")
    print("=" * 90)
    if not outage or not outage.get("has_outage"):
        print("  No scheduled power outage for this month.")
        return

    print(f"  Month:        {outage.get('month_key')}")
    print(f"  Date:         {outage.get('outage_date')}")
    print(f"  Time:         {outage.get('start_time')} - {outage.get('end_time')}")
    print(f"  Town / Area:  {outage.get('town')}")
    print(f"  Substation:   {outage.get('substation')}")
    print(f"  Feeder:       {outage.get('feeder')}")
    print(f"  Location:     {outage.get('location')}")
    print(f"  Work Type:    {outage.get('type_of_work')}")

    all_recs = outage.get("all_records", [])
    if len(all_recs) > 1:
        print(f"\n  [Additional Feeder Sections in {outage.get('month_key')}]:")
        for idx, extra in enumerate(all_recs[1:], 1):
            ex_date = extra.get("Date Of Outage") or extra.get("outage_date")
            ex_st = extra.get("From Date") or extra.get("start_time")
            ex_et = extra.get("To Date") or extra.get("end_time")
            ex_town = extra.get("Town") or extra.get("town")
            ex_loc = extra.get("Location") or extra.get("location")
            print(f"    {idx}. {ex_date} ({ex_st}-{ex_et}) - {ex_town}: {ex_loc}")


def main():
    config = cfg_module.load_config()

    parser = argparse.ArgumentParser(
        description="TNEB Power Outage Scraper & Home Assistant Sync (Windows & Linux)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--circle",
        type=str,
        default=config.get("circle", "KRISHNAGIRI"),
        help="Circle name (e.g., KRISHNAGIRI, CHENNAI - CENTRAL, etc.)",
    )
    parser.add_argument(
        "--filter",
        dest="area_filter",
        type=str,
        default=config.get("filter", "Hosur|Mathigiri|Mookandapalli|Zuzuvadi|Bagalur|Sipcot"),
        help="Regex filter for area/town/substation/feeder",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Display all circle records without area filtering",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force portal scrape even if current month data is already cached",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Display all stored historical monthly outages from SQLite (one record per month)",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously in background, performing checks once daily",
    )
    parser.add_argument(
        "--time",
        dest="check_time",
        type=str,
        default=config.get("daily_check_time", "08:00"),
        help="Time for daily check in daemon mode (HH:MM format, 24-hr)",
    )
    parser.add_argument(
        "--test-ha",
        action="store_true",
        help="Test connectivity to Home Assistant instance",
    )
    parser.add_argument(
        "--ha-url",
        type=str,
        default=None,
        help="Home Assistant URL (e.g. http://192.168.1.100:8123)",
    )
    parser.add_argument(
        "--ha-token",
        type=str,
        default=None,
        help="Home Assistant Long-Lived Access Token",
    )
    parser.add_argument(
        "--csv",
        type=str,
        default=None,
        help="Optional path to export results as CSV",
    )
    parser.add_argument(
        "--json",
        type=str,
        default=None,
        help="Optional path to export results as JSON",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="Run browser in headless mode",
    )
    parser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Run browser with visible UI for debugging",
    )

    args = parser.parse_args()

    # Override HA config from CLI flags if given
    if args.ha_url:
        config["home_assistant"]["url"] = args.ha_url
        config["home_assistant"]["enabled"] = True
    if args.ha_token:
        config["home_assistant"]["token"] = args.ha_token
        config["home_assistant"]["enabled"] = True

    # Test HA option
    if args.test_ha:
        print("[Test] Testing Home Assistant connection...")
        ha_client.test_connection(config.get("home_assistant", {}))
        return

    # View History option (One record per month)
    if args.history:
        db.init_db()
        history = db.get_all_monthly_history()
        print("\n" + "=" * 90)
        print(f" MONTHLY POWER OUTAGE HISTORY (SQLite: 1 record per month) - ({len(history)} records)")
        print("=" * 90)
        if not history:
            print("  No monthly records stored yet.")
        else:
            cols = ["month_key", "outage_date", "start_time", "end_time", "town", "location", "type_of_work"]
            df = pd.DataFrame(history)
            avail = [c for c in cols if c in df.columns]
            print(df[avail].to_string(index=False))

        if args.csv and history:
            pd.DataFrame(history).to_csv(args.csv, index=False)
            print(f"[Export] Saved history to CSV: {args.csv}")
        if args.json and history:
            pd.DataFrame(history).to_json(args.json, orient="records", indent=2)
            print(f"[Export] Saved history to JSON: {args.json}")
        return

    # Daemon mode
    if args.daemon:
        asyncio.run(run_daemon_loop(args, config))
        return

    # Standard Single Daily Run
    effective_filter = None if args.all else args.area_filter
    month_record = run_sync_cycle(
        circle=args.circle,
        area_filter=effective_filter,
        force=args.force,
        headless=args.headless,
        ha_cfg=config.get("home_assistant"),
    )

    current_month_key = datetime.now().strftime("%Y-%m")
    print_single_outage(f"POWER OUTAGE RECORD FOR THIS MONTH ({current_month_key})", month_record)

    if args.csv and month_record:
        pd.DataFrame([month_record]).to_csv(args.csv, index=False)
        print(f"\n[Export] Saved result to CSV: {args.csv}")

    if args.json and month_record:
        pd.DataFrame([month_record]).to_json(args.json, orient="records", indent=2)
        print(f"[Export] Saved result to JSON: {args.json}")


if __name__ == "__main__":
    main()