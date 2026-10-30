#!/usr/bin/env python3
"""
monitor.py

A simple, beginner-friendly continuous monitoring script for Bureau Wbtv.
Every hour (or a custom interval), it checks for any newly registered professionals
or newly added languages/roles.

When new records are found:
1. It formats an alert message with the professional's name, language, role, and contact info.
2. It sends an alert directly to your Telegram chat via a Telegram Bot.
3. It updates 'known_records.json' so it never alerts you twice for the same person.
4. It updates 'all_languages_results.json' and 'all_languages_results.xlsx' so your records stay fresh.

Usage:
    # 1. Quick test of your Telegram bot setup:
    uv run monitor.py --test-telegram

    # 2. Run a single check cycle and exit (useful for testing):
    uv run monitor.py --once

    # 3. Run continuously (checks every 1 hour by default):
    uv run monitor.py

    # 4. Check every 30 minutes for a specific language:
    uv run monitor.py --interval 1800 --language "Arabic"
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import queue
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Any, Optional

import requests
from dotenv import dotenv_values, load_dotenv

from notification import format_email_alert, send_email_notification

# We import the core search client and helpers from fetch_all_languages.py
from fetch_all_languages import (
    PROTECTED_FILES,
    ROLE_TOLK,
    ROLE_VERTALER,
    BureauWbtvClient,
    LanguageResult,
    extract_known_keys_from_excel,
    extract_known_guids_from_json,
    extract_record_csv_fields,
    filter_target_languages,
    load_target_languages,
    save_results,
    upsert_records_to_excel,
)


def setup_logging(
    log_file: Optional[Path] = None, verbose: bool = False
) -> None:
    """Configures logging to output to both console and a .log file."""
    # Ensure Windows console streams handle UTF-8 / emojis safely without charmap errors
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    log_level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Clear existing handlers to avoid duplicate log entries
    root_logger.handlers.clear()

    # Stream (console) handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8", mode="a")
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


# Initial console logging setup before CLI parsing
setup_logging()
logger = logging.getLogger("monitor")




# ---------------------------------------------------------------------------
# 1. .env File Loader (python-dotenv)
# ---------------------------------------------------------------------------

def load_env_file(env_path: Path = Path(".env")) -> dict[str, str]:
    """
    Loads environment variables from a .env file using python-dotenv.
    Populates os.environ and returns non-empty parsed variables as a dictionary.
    """
    if not env_path.is_file():
        return {}

    load_dotenv(dotenv_path=env_path, override=True)
    raw_vals = dotenv_values(dotenv_path=env_path)
    return {k: v for k, v in raw_vals.items() if v is not None}


# ---------------------------------------------------------------------------
# 2. Telegram Alert Sender
# ---------------------------------------------------------------------------

def send_telegram_message(token: str, chat_id: str, message: str) -> bool:
    """
    Sends a message to your Telegram account using the official Telegram Bot API.
    Returns True if sent successfully, False otherwise.
    """
    if not token or not chat_id:
        logger.error("Cannot send Telegram alert: Token or Chat ID is missing.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, json=payload, timeout=15)
        # If Markdown fails (e.g. special characters in names), retry in plain text
        if response.status_code == 400 and "can't parse" in response.text:
            payload.pop("parse_mode", None)
            response = requests.post(url, json=payload, timeout=15)

        if response.status_code == 200:
            logger.info("Telegram notification sent successfully.")
            return True
        else:
            logger.error(
                "Telegram API returned error %d: %s",
                response.status_code,
                response.text,
            )
            return False
    except Exception as exc:
        logger.error("Failed to connect to Telegram: %s", exc)
        return False


# ---------------------------------------------------------------------------
# 3. State Management (Tracking Known Records)
# ---------------------------------------------------------------------------

def make_record_key(record: dict[str, Any], language: str) -> str:
    """
    Returns the GUID of the professional as the unique tracking key.

    Using GUID-only means a notification fires only when a **completely new
    person** appears in the registry.  If an existing professional adds a new
    language or role that is NOT treated as a new registration — preventing
    duplicate alerts for the same person.
    """
    return str(record.get("guid") or "").strip()


def sync_and_load_known_records(
    known_path: Path,
    excel_path: Optional[Path] = None,
    baseline_path: Optional[Path] = None,
    json_path: Optional[Path] = None,
) -> set[str]:
    """
    Synchronizes and loads known professional GUIDs before each check cycle.

    Sources checked (all optional, each may be None / missing):
    1. ``known_path``   — ``known_records.json`` (plain GUID list).
                          **Auto-migrates** the old ``guid:role:language`` compound
                          format by stripping the suffix and keeping only the GUID.
    2. ``excel_path``   — ``all_languages_results.xlsx`` (GUID column).
    3. ``json_path``    — live output JSON (``all_languages_results.json``).
    4. ``baseline_path``— alias for ``json_path`` used when the same file serves
                          as both output and baseline; falls back to this when
                          ``json_path`` is not provided separately.

    All sources are merged via **union** so that the widest possible set of
    already-known persons is considered.  This prevents spurious alerts on
    the first run (or after ``known_records.json`` is deleted) because the
    Excel / JSON files still carry the full history.

    The merged GUID set is saved back to ``known_records.json`` in the new
    plain-GUID format before returning.
    """

    def _parse_known_file(path: Path) -> Optional[set[str]]:
        """Read known_records.json; auto-migrate old compound keys to plain GUIDs."""
        if not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return None
            guids: set[str] = set()
            for entry in data:
                raw = str(entry).strip()
                if not raw:
                    continue
                # Old format: "guid:role:language" — keep only the GUID part
                guids.add(raw.split(":")[0])
            return guids
        except Exception as e:
            logger.warning("Could not read %s (%s).", path, e)
            return None

    known_guids  = _parse_known_file(known_path)
    excel_guids  = None
    json_guids   = None

    if excel_path and excel_path.is_file():
        try:
            excel_guids = extract_known_keys_from_excel(excel_path)
        except Exception as e:
            logger.warning("Could not read GUIDs from Excel %s: %s", excel_path, e)

    effective_json_path = json_path or baseline_path

    # Determine active known keys:
    if known_guids and excel_guids:
        # Both files have records: taking the intersection ensures that if a record
        # was removed from EITHER known_records.json OR all_languages_results.xlsx,
        # it is recognized as removed and flagged as NEW!
        active_keys = known_guids & excel_guids
    elif excel_guids:
        # Excel file has data and known record has not:
        # Update the known record with Excel data instead of scraping data as new!
        active_keys = set(excel_guids)
        logger.info(
            "Known records file was empty or missing. Initializing from Excel (%d record(s)).",
            len(active_keys),
        )
    elif known_guids:
        # Only known_records has data
        active_keys = set(known_guids)
    elif effective_json_path and effective_json_path.is_file():
        # Fallback for first run if neither Excel nor known_records exists
        try:
            json_guids = extract_known_guids_from_json(effective_json_path)
            active_keys = set(json_guids) if json_guids else set()
            logger.info(
                "Initializing baseline known records from JSON (%d record(s)).",
                len(active_keys),
            )
        except Exception as e:
            logger.warning("Could not seed baseline from JSON %s: %s", effective_json_path, e)
            active_keys = set()
    else:
        active_keys = set()

    # Remove any empty strings that crept in
    active_keys.discard("")

    # Persist the merged (and migrated) GUID set
    save_known_records(known_path, active_keys)

    logger.info(
        "Active known GUIDs synced: %d unique person(s) "
        "(Known file: %s, Excel: %s, JSON: %s).",
        len(active_keys),
        len(known_guids)  if known_guids  is not None else "N/A",
        len(excel_guids)  if excel_guids  is not None else "N/A",
        len(json_guids)   if json_guids   is not None else "N/A",
    )
    return active_keys


def load_known_records(
    known_path: Path,
    excel_path: Optional[Path] = None,
    baseline_path: Optional[Path] = None,
    json_path: Optional[Path] = None,
) -> set[str]:
    """Compatibility wrapper that calls sync_and_load_known_records."""
    return sync_and_load_known_records(
        known_path=known_path,
        excel_path=excel_path,
        baseline_path=baseline_path,
        json_path=json_path,
    )


def save_known_records(known_path: Path, known_keys: set[str]) -> None:
    """Saves the set of known registration keys to a JSON file."""
    known_path.parent.mkdir(parents=True, exist_ok=True)
    with known_path.open("w", encoding="utf-8") as f:
        json.dump(sorted(list(known_keys)), f, indent=2)


def update_json_results(
    output_json: Path,
    new_found: list[tuple[str, dict[str, Any]]],
    roles: list[str],
    total_known: int,
    scan_elapsed: float,
    enrich_elapsed: float,
    cycle_elapsed: float,
) -> None:
    """Updates all_languages_results.json incrementally without re-querying everything."""
    existing_data: dict[str, Any] = {}
    if output_json.is_file():
        try:
            with output_json.open("r", encoding="utf-8") as f:
                existing_data = json.load(f)
        except Exception as e:
            logger.warning("Could not load existing JSON results: %s", e)

    results = existing_data.get("results", {})

    for lang_name, record in new_found:
        if lang_name not in results:
            results[lang_name] = {
                "language_name": lang_name,
                "status": "success",
                "count": 0,
                "records": [],
            }
        lang_entry = results[lang_name]
        records_list: list[dict[str, Any]] = lang_entry.get("records", [])

        # Check if record already exists by guid and role
        g = str(record.get("guid") or "").strip()
        r = str(record.get("role") or "").strip()
        matched = False
        for i, existing_rec in enumerate(records_list):
            if str(existing_rec.get("guid") or "").strip() == g and str(existing_rec.get("role") or "").strip() == r:
                records_list[i] = record
                matched = True
                break
        if not matched:
            records_list.append(record)
        lang_entry["count"] = len(records_list)

    existing_data["metadata"] = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "roles": roles,
        "total_known_records": total_known,
        "scan_duration_seconds": round(scan_elapsed, 2),
        "enrich_duration_seconds": round(enrich_elapsed, 2),
        "cycle_duration_seconds": round(cycle_elapsed, 2),
        "cycle_duration_formatted": format_duration(cycle_elapsed),
    }
    existing_data["results"] = results

    output_json.parent.mkdir(parents=True, exist_ok=True)
    with output_json.open("w", encoding="utf-8") as f:
        json.dump(existing_data, f, indent=4, ensure_ascii=False)
        f.write("\n")


def format_duration(seconds: float) -> str:
    """Formats seconds into human-readable duration string (e.g. '4.12s', '1m 24.3s', '1h 05m 12s')."""
    if seconds < 0:
        return "0.00s"
    if seconds < 60:
        return f"{seconds:0.2f}s"
    minutes = int(seconds // 60)
    rem_seconds = seconds % 60
    if minutes < 60:
        return f"{minutes}m {rem_seconds:0.1f}s ({seconds:0.2f}s total)"
    hours = minutes // 60
    rem_minutes = minutes % 60
    return f"{hours}h {rem_minutes}m {rem_seconds:0.0f}s"


# ---------------------------------------------------------------------------
# 4. Alert Message Formatting
# ---------------------------------------------------------------------------

def format_alert_message(
    new_items: list[tuple[str, dict[str, Any]]],
    elapsed_seconds: Optional[float] = None,
) -> str:
    """
    Formats a clean, readable Telegram alert listing newly detected professionals.
    - If 5 or fewer records: sends full contact details (email, phone, address, etc.).
    - If more than 5 records: sends a concise summary with language breakdown and professional list.
    """
    total = len(new_items)
    lines: list[str] = [
        "\U0001f6a8 *Bureau Wbtv: New Registration Alert!*",
    ]

    if total <= 5:
        lines.append(f"Detected *{total}* new registration(s):\n")
        for idx, (lang_name, record) in enumerate(new_items, start=1):
            # Extract formatted fields (Name, Wbtv Number, City, etc.)
            fields = extract_record_csv_fields(record, language_name=lang_name)
            name = fields.get("Name") or "Unknown Name"
            role = fields.get("Role") or "Unknown Role"
            level = fields.get("Level") or "-"
            wbtv = fields.get("Wbtv Number") or "-"
            city = fields.get("City") or "-"
            email = fields.get("Email") or "-"
            phone = fields.get("Telephone 1") or "-"

            block = (
                f"*{idx}. {name}*\n"
                f"   \U0001f310 Language: *{lang_name}*\n"
                f"   \U0001f4bc Role: *{role}* (Level: {level})\n"
                f"   \U0001f194 Wbtv No: {wbtv}\n"
                f"   \U0001f4cd City: {city}\n"
                f"   \U0001f4e7 Email: {email}\n"
                f"   \U0001f4de Phone: {phone}\n"
            )
            lines.append(block)
    else:
        lines.append(f"Detected *{total}* new registrations (Summary Overview):\n")

        # 1. Breakdown by language
        lang_counts: dict[str, int] = {}
        for lang_name, _ in new_items:
            lang_counts[lang_name] = lang_counts.get(lang_name, 0) + 1

        lines.append("\U0001f4ca *Breakdown by Language:*")
        for lang, count in sorted(lang_counts.items(), key=lambda x: -x[1]):
            lines.append(f"• *{lang}*: {count} new registration(s)")
        lines.append("")

        # 2. Compact professional list (up to 15 previewed)
        lines.append("\U0001f465 *Registered Professionals:*")
        max_preview = 15
        for idx, (lang_name, record) in enumerate(new_items[:max_preview], start=1):
            fields = extract_record_csv_fields(record, language_name=lang_name)
            name = fields.get("Name") or "Unknown Name"
            role = fields.get("Role") or "Tolk"
            level = fields.get("Level") or ""
            level_str = f" ({level})" if level and level != "-" else ""
            wbtv = fields.get("Wbtv Number") or ""
            wbtv_str = f" [Wbtv: {wbtv}]" if wbtv and wbtv != "-" else ""
            lines.append(f"{idx}. *{name}* — {lang_name} ({role}{level_str}){wbtv_str}")

        if total > max_preview:
            lines.append(f"... and *{total - max_preview}* more.")

        lines.append("")
        lines.append("\U0001f4c1 _Full contact details (email, phone, address) saved in all_languages_results.xlsx._\n")

    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if elapsed_seconds is not None:
        lines.append(f"\u23f0 *Detected at:* {time_str} (Scan time: {format_duration(elapsed_seconds)})")
    else:
        lines.append(f"\u23f0 *Detected at:* {time_str}")
    return "\n".join(lines)



# ---------------------------------------------------------------------------
# 5. Single Monitoring Cycle
# ---------------------------------------------------------------------------

def run_check_cycle(
    languages: list[Any],
    roles: list[str],
    known_keys: set[str],
    known_path: Path,
    scan_workers: int,
    concurrency: int,
    output_json: Path,
    output_excel: Path,
    email_enabled: bool = True,
    sender_email: Optional[str] = None,
    email_password: Optional[str] = None,
    recipient_emails: Optional[list[str] | str] = None,
    telegram_token: str = "",
    telegram_chat_id: str = "",
) -> int:
    """
    Runs one scan of the requested languages concurrently using worker threads.
    Each worker initializes its Mendix session ONCE and reuses it for all assigned languages.
    Detects new registrations, enriches them, and sends email alerts (default) and Telegram alerts (optional).
    Returns the count of new records found.
    """
    cycle_start_time = time.perf_counter()
    actual_workers = min(scan_workers, len(languages)) if languages else 1
    logger.info(
        "Starting concurrent scan across %d language(s) with %d worker(s)...",
        len(languages), actual_workers,
    )

    all_current_results: dict[str, LanguageResult] = {}
    new_found: list[tuple[str, dict[str, Any]]] = []
    results_lock = Lock()
    progress_lock = Lock()
    completed_counter = 0
    total = len(languages)

    lang_queue: queue.Queue[Any] = queue.Queue()
    for lang in languages:
        lang_queue.put(lang)

    def worker_loop(worker_id: int) -> None:
        """
        Runs on a single worker thread. Initializes one Mendix session and
        processes languages from the queue until empty.
        """
        nonlocal completed_counter
        worker_client = BureauWbtvClient()
        try:
            worker_client.initialize_session()
            while True:
                try:
                    lang = lang_queue.get_nowait()
                except queue.Empty:
                    break

                try:
                    res = worker_client.search_language_roles(lang, roles=roles)

                    # Count new records for this language
                    new_for_lang: list[tuple[str, dict[str, Any]]] = []
                    for record in res.records or []:
                        key = make_record_key(record, lang.name)
                        if key not in known_keys:
                            new_for_lang.append((lang.name, record))

                    # Thread-safe write: update shared state under the lock
                    with results_lock:
                        all_current_results[lang.name] = res
                        for item in new_for_lang:
                            key = make_record_key(item[1], item[0])
                            if key not in known_keys:
                                new_found.append(item)
                                known_keys.add(key)

                    with progress_lock:
                        completed_counter += 1
                        current_done = completed_counter

                    record_count = res.count or 0
                    if res.status == "error":
                        logger.warning(
                            "[%d/%d] ERROR for '%s': %s",
                            current_done, total, lang.name, res.error,
                        )
                    elif new_for_lang:
                        logger.info(
                            "[%d/%d] '%s' -> %d record(s), %d NEW! 🎉",
                            current_done, total, lang.name, record_count, len(new_for_lang),
                        )

                    else:
                        logger.info(
                            "[%d/%d] '%s' -> %d record(s), no new.",
                            current_done, total, lang.name, record_count,
                        )

                except Exception as exc:
                    with progress_lock:
                        completed_counter += 1
                        current_done = completed_counter
                    logger.error(
                        "[%d/%d] Worker %d error scanning '%s': %s",
                        current_done, total, worker_id, lang.name, exc,
                    )
                finally:
                    lang_queue.task_done()

        except Exception as exc:
            logger.error("Worker %d failed to initialize session: %s", worker_id, exc)
        finally:
            worker_client.close()

    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
        futures = [executor.submit(worker_loop, i + 1) for i in range(actual_workers)]
        for future in futures:
            try:
                future.result()
            except Exception as exc:
                logger.error("Unhandled worker exception: %s", exc)

    scan_elapsed = time.perf_counter() - cycle_start_time
    avg_per_lang = scan_elapsed / max(len(languages), 1)
    logger.info(
        "Scan phase completed in %s (%0.2fs/lang avg across %d worker(s)).",
        format_duration(scan_elapsed),
        avg_per_lang,
        actual_workers,
    )

    # If new records were detected, enrich their profile details and send alert!
    if new_found:
        enrich_start_time = time.perf_counter()
        logger.info("Found %d new registration(s)! Enriching contact details...", len(new_found))
        # Use a single shared client for detail enrichment (it has its own thread pool internally)
        enrich_client = BureauWbtvClient()
        try:
            records_to_enrich = [rec for (_, rec) in new_found]
            enrich_client.enrich_records_with_details(records_to_enrich, max_workers=concurrency)
        finally:
            enrich_client.close()

        enrich_elapsed = time.perf_counter() - enrich_start_time
        logger.info("Enrichment completed in %s.", format_duration(enrich_elapsed))

        cycle_elapsed_so_far = time.perf_counter() - cycle_start_time

        # 1. Primary Notification: Email Alert (Default)
        email_sent = False
        if email_enabled:
            subject, text_body, html_body = format_email_alert(
                new_found, elapsed_seconds=cycle_elapsed_so_far
            )
            email_sent = send_email_notification(
                subject=subject,
                body=text_body,
                html_body=html_body,
                sender=sender_email,
                password=email_password,
                recipients=recipient_emails,
            )
            if not email_sent:
                logger.warning(
                    "Default email alert could not be sent. Check SENDER_EMAIL, EMAIL_PASSWORD, and RECIPIENT_EMAIL in .env."
                )

        # 2. Secondary Notification: Telegram Alert (Optional)
        telegram_sent = False
        if telegram_token and telegram_chat_id:
            telegram_msg = format_alert_message(
                new_found, elapsed_seconds=cycle_elapsed_so_far
            )
            telegram_sent = send_telegram_message(telegram_token, telegram_chat_id, telegram_msg)
        else:
            logger.debug("Telegram credentials not configured (Telegram is optional).")

        # 3. Fallback: Print to console if neither channel sent an alert
        if not email_sent and not telegram_sent:
            print("\n" + "=" * 50)
            print(format_alert_message(new_found, elapsed_seconds=cycle_elapsed_so_far))
            print("=" * 50 + "\n")

        # Update Excel via pandas (only update matching rows or append new records)
        inserted_count, updated_count = upsert_records_to_excel(output_excel, new_found)
        logger.info(
            "Excel file '%s' updated via pandas: %d new inserted, %d existing updated.",
            output_excel.name, inserted_count, updated_count
        )

        # 3. Update all_languages_results.json incrementally
        cycle_total_seconds = time.perf_counter() - cycle_start_time
        try:
            update_json_results(
                output_json=output_json,
                new_found=new_found,
                roles=roles,
                total_known=len(known_keys),
                scan_elapsed=scan_elapsed,
                enrich_elapsed=enrich_elapsed,
                cycle_elapsed=cycle_total_seconds,
            )
            logger.info("JSON file '%s' incrementally updated.", output_json.name)
        except Exception as e:
            logger.warning("Could not update JSON results file: %s", e)

        # 4. Save updated known keys
        save_known_records(known_path, known_keys)

    else:
        logger.info("No new registrations found. Everything is up to date.")

    total_cycle_elapsed = time.perf_counter() - cycle_start_time
    logger.info(
        "Cycle execution finished in %s (found %d new record(s)).",
        format_duration(total_cycle_elapsed),
        len(new_found),
    )

    return len(new_found)



# ---------------------------------------------------------------------------
# 6. Main Entry Point and Continuous Loop
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Continuously monitor Bureau Wbtv for new registrations. Sends email alerts by default and optional Telegram alerts."
    )
    # Email notifications (Default)
    parser.add_argument(
        "--recipient-email",
        "--email",
        type=str,
        default=None,
        help="Recipient email address(es) for default notifications (or set RECIPIENT_EMAIL in .env).",
    )
    parser.add_argument(
        "--sender-email",
        type=str,
        default=None,
        help="Sender email address for default notifications (or set SENDER_EMAIL in .env).",
    )
    parser.add_argument(
        "--email-password",
        type=str,
        default=None,
        help="App password for sender email (or set EMAIL_PASSWORD in .env).",
    )
    parser.add_argument(
        "--disable-email",
        "--no-email",
        action="store_true",
        help="Disable default email notifications.",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Send a test email notification to verify SMTP credentials and exit.",
    )

    # Telegram notifications (Optional)
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="Optional Telegram Bot Token (or set TELEGRAM_BOT_TOKEN in .env).",
    )
    parser.add_argument(
        "--chat-id",
        type=str,
        default=None,
        help="Optional Telegram Chat ID (or set TELEGRAM_CHAT_ID in .env).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Monitoring interval in seconds (default: 3600 = 1 hour).",
    )
    parser.add_argument(
        "-l",
        "--language",
        type=str,
        default=None,
        help="Optional language name or substring to monitor (default: all languages from target_lang.json).",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=None,
        help="Limit the number of languages to check (e.g. 1 for a quick test).",
    )
    parser.add_argument(
        "--roles",
        type=str,
        nargs="+",
        default=[ROLE_TOLK],
        choices=[ROLE_TOLK, ROLE_VERTALER, "both"],
        help="Profession roles to check (default: Tolk only).",
    )
    parser.add_argument(
        "-w",
        "--scan-workers",
        type=int,
        default=8,
        help="Number of languages to scan in parallel (default: 8).",
    )
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=10,
        help="Concurrency for fetching profile details of new records (default: 10).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check cycle and exit immediately (do not loop continuously).",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test message to your Telegram chat to verify credentials and exit.",
    )
    parser.add_argument(
        "--known-file",
        type=str,
        default="known_records.json",
        help="Path to JSON file tracking known registration IDs (default: known_records.json).",
    )
    parser.add_argument(
        "--baseline-file",
        type=str,
        default="all_languages_results.json",
        help="Path to existing baseline JSON to initialize known records quietly (default: all_languages_results.json).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="all_languages_results.json",
        help="Path to output JSON file to keep updated (default: all_languages_results.json).",
    )
    parser.add_argument(
        "--excel-output",
        type=str,
        default="all_languages_results.xlsx",
        help="Path to output Excel file to keep updated (default: all_languages_results.xlsx).",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Optional path to .log file where execution logs are saved (default: None, console only).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )

    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent

    # Load environment variables (.env file takes precedence if present)
    env_vars = load_env_file(base_dir / ".env")

    # Set up logging (console by default, file only if explicitly requested)
    log_file_name = args.log_file or env_vars.get("MONITOR_LOG_FILE")
    log_path: Optional[Path] = None
    if log_file_name:
        log_path = Path(log_file_name)
        if not log_path.is_absolute():
            log_path = base_dir / log_path

        if log_path.name in PROTECTED_FILES:
            print(f"Error: Specified log file '{log_path.name}' is a protected file.")
            sys.exit(1)

    setup_logging(log_file=log_path, verbose=args.verbose)

    # Email credentials (Default channel)
    sender_email = (
        args.sender_email
        or env_vars.get("SENDER_EMAIL")
        or env_vars.get("EMAIL_SENDER")
        or os.environ.get("SENDER_EMAIL")
        or os.environ.get("EMAIL_SENDER")
        or ""
    )
    email_password = (
        args.email_password
        or env_vars.get("EMAIL_PASSWORD")
        or os.environ.get("EMAIL_PASSWORD")
        or ""
    )
    recipient_email = (
        args.recipient_email
        or env_vars.get("RECIPIENT_EMAIL")
        or env_vars.get("EMAIL_RECEIVER")
        or os.environ.get("RECIPIENT_EMAIL")
        or os.environ.get("EMAIL_RECEIVER")
        or ""
    )
    email_enabled = not args.disable_email

    # Telegram credentials (Optional channel)
    telegram_token = (
        args.token
        or env_vars.get("TELEGRAM_BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or ""
    )
    telegram_chat_id = (
        args.chat_id
        or env_vars.get("TELEGRAM_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or ""
    )
    interval_seconds = args.interval or int(env_vars.get("MONITOR_INTERVAL_SECONDS") or 3600)

    # 1. Handle --test-email command
    if args.test_email:
        print("\nTesting Email connection (Default Notification Channel)...")
        if not sender_email or not email_password or not recipient_email:
            print("Error: SENDER_EMAIL, EMAIL_PASSWORD, or RECIPIENT_EMAIL is missing.")
            print("Please configure them in your .env file or pass them as command-line arguments.")
            sys.exit(1)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        test_subject = "[Bureau Wbtv] Test Email Alert"
        test_body = (
            "Bureau Wbtv Monitor Email Connected!\n\n"
            "Your email alert configuration is working properly.\n"
            f"Timestamp: {now_str}\n"
        )
        test_html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 500px; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px;">
          <h2 style="color: #0f172a; margin-top: 0;">&#x2705; Bureau Wbtv Monitor Email Connected!</h2>
          <p style="color: #334155;">Your email notification setup is working properly.</p>
          <p style="color: #64748b; font-size: 12px;">Tested at: {now_str}</p>
        </div>
        """
        success = send_email_notification(
            subject=test_subject,
            body=test_body,
            html_body=test_html,
            sender=sender_email,
            password=email_password,
            recipients=recipient_email,
        )
        if success:
            print(f"Success! Check {recipient_email} for the test email.\n")
        else:
            print("Failed to send test email. Please check your credentials in .env.\n")
        return

    # 2. Handle --test-telegram command
    if args.test_telegram:
        print("\nTesting Telegram connection (Optional Notification Channel)...")
        if not telegram_token or not telegram_chat_id:
            print("Error: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
            print("Please fill them in .env or pass --token <TOKEN> --chat-id <CHAT_ID>.")
            sys.exit(1)

        test_msg = (
            "\u2705 *Bureau Wbtv Monitor Connected!*\n\n"
            "Your Telegram Bot notifications are working perfectly.\n"
            f"\u23f0 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        success = send_telegram_message(telegram_token, telegram_chat_id, test_msg)
        if success:
            print("Success! Check your Telegram app for the test message.\n")
        else:
            print("Failed to send test message. Please verify your Bot Token and Chat ID.\n")
        return

    # Informational check for credentials
    if not (email_enabled and sender_email and email_password and recipient_email):
        logger.warning(
            "Default email notifications not fully configured in .env (SENDER_EMAIL, EMAIL_PASSWORD, RECIPIENT_EMAIL)."
        )
    if not (telegram_token and telegram_chat_id):
        logger.info("Telegram notifications not configured (Telegram is optional).")

    # Load languages to monitor
    target_path = base_dir / "target_lang.json"
    known_path = base_dir / args.known_file
    baseline_path = base_dir / args.baseline_file
    output_json = base_dir / args.output
    output_excel = base_dir / args.excel_output

    all_langs = load_target_languages(target_path)
    selected_langs = filter_target_languages(all_langs, query=args.language, limit=args.limit)

    if not selected_langs:
        logger.error("No languages matched criteria. Exiting.")
        return

    # Normalize roles
    selected_roles: list[str] = []
    for r in args.roles:
        if r.lower() == "both":
            selected_roles = [ROLE_TOLK, ROLE_VERTALER]
            break
        selected_roles.append(r)
    if not selected_roles:
        selected_roles = [ROLE_TOLK]

    logger.info("=" * 60)
    logger.info("BUREAU WBTV CONTINUOUS MONITOR")
    logger.info("=" * 60)
    logger.info("Monitored Languages: %d", len(selected_langs))
    logger.info("Scan Workers:        %d", args.scan_workers)
    logger.info("Detail Concurrency:  %d", args.concurrency)
    logger.info("Roles:               %s", ", ".join(selected_roles))
    logger.info("Check Interval:      %d seconds (%0.1f hours)", interval_seconds, interval_seconds / 3600)
    logger.info("Log File:            %s", log_path.name if log_path else "None (Console only)")
    email_status = (
        f"ENABLED (Default) -> {recipient_email}"
        if (email_enabled and sender_email and email_password and recipient_email)
        else ("DISABLED (--disable-email)" if not email_enabled else "NOT CONFIGURED (Missing .env credentials)")
    )
    telegram_status = (
        "ENABLED (Optional)" if (telegram_token and telegram_chat_id) else "DISABLED (Optional - not configured)"
    )
    logger.info("Email Alerts:        %s", email_status)
    logger.info("Telegram Alerts:     %s", telegram_status)
    logger.info("=" * 60)

    cycle_count = 1

    try:
        while True:
            cycle_start_time = time.perf_counter()
            start_timestamp = datetime.now()
            logger.info(
                ">>> Cycle #%d started at %s",
                cycle_count,
                start_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            )

            # Sync and reload known GUIDs from Excel, JSON, and known file before each cycle
            known_keys = sync_and_load_known_records(
                known_path=known_path,
                excel_path=output_excel,
                baseline_path=baseline_path,
                json_path=output_json,
            )
            try:
                run_check_cycle(
                    languages=selected_langs,
                    roles=selected_roles,
                    known_keys=known_keys,
                    known_path=known_path,
                    scan_workers=args.scan_workers,
                    concurrency=args.concurrency,
                    output_json=output_json,
                    output_excel=output_excel,
                    email_enabled=email_enabled,
                    sender_email=sender_email,
                    email_password=email_password,
                    recipient_emails=recipient_email,
                    telegram_token=telegram_token,
                    telegram_chat_id=telegram_chat_id,
                )
            except Exception as e:
                logger.error("Unexpected error during monitoring cycle: %s", e)

            cycle_elapsed = time.perf_counter() - cycle_start_time
            end_timestamp = datetime.now()
            logger.info(
                ">>> Cycle #%d finished at %s | Run duration: %s",
                cycle_count,
                end_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                format_duration(cycle_elapsed),
            )

            if args.once:
                logger.info(
                    "Single run (--once) finished in %s. Exiting.",
                    format_duration(cycle_elapsed),
                )
                break

            logger.info(
                "Cycle #%d complete (took %s). Sleeping %d seconds until next check...",
                cycle_count,
                format_duration(cycle_elapsed),
                interval_seconds,
            )
            cycle_count += 1
            time.sleep(interval_seconds)

    except KeyboardInterrupt:
        logger.info("Monitor stopped by user (Ctrl+C). Goodbye!")



if __name__ == "__main__":
    main()
