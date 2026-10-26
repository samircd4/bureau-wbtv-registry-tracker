# Run Commands Guide

This document lists all execution commands for running tests and fetching language search results using `uv run`.

---

## 1. Environment Setup

Ensure all project dependencies are installed and in sync:

```powershell
uv sync
```

---

## 2. Run Unit Tests (Offline / Mocked)

Runs the comprehensive test suite (payload construction, response validation, pagination handling, CSV export, logging, and file protection) without making live network calls.

### Run All 29 Unit Tests
```powershell
uv run -m unittest test_fetch_all_languages.py -v
```

### Run a Specific Test Class
```powershell
# Test payload construction
uv run -m unittest test_fetch_all_languages.TestPayloadConstruction -v

# Test response parsing and error handling
uv run -m unittest test_fetch_all_languages.TestResponseParsing -v

# Test language filtering and loading logic
uv run -m unittest test_fetch_all_languages.TestLanguageFilteringAndLoading -v

# Test CSV export and result persistence
uv run -m unittest test_fetch_all_languages.TestResultOrganizationAndPersistence -v

# Test client HTTP search workflow and mocked API interactions
uv run -m unittest test_fetch_all_languages.TestClientSearchWorkflow -v

# Test logging configuration and file handler creation
uv run -m unittest test_fetch_all_languages.TestLoggingSetup -v
```

### Run an Individual Test Method
```powershell
uv run -m unittest test_fetch_all_languages.TestClientSearchWorkflow.test_client_search_language_success -v
```

---

## 3. Single-Language Verification

Test the HTTP flow with a single language before running the full list.

### By Exact Language Name
```powershell
uv run fetch_all_languages.py --language "Arabic Egypt" --output test_arabic_egypt.json
```

### By Substring Search
Searches all languages containing "arabic" (case-insensitive):
```powershell
uv run fetch_all_languages.py --language "arabic" --output test_arabic_all.json
```

### By Language GUID
```powershell
uv run fetch_all_languages.py --language 46161896180548020 --output test_guid.json
```

### Single Language for Both Roles (Interpreters + Translators)
```powershell
uv run fetch_all_languages.py --language "Arabic Egypt" --both --output test_arabic_both.json
```

---

## 4. Profession Roles (Interpreters vs Translators vs Both)

The Bureau Wbtv portal distinguishes between **Spoken Interpreters** (*Tolk*) and **Written Translators** (*Vertaler*).

### Interpreters Only (*Tolk* - Default)
```powershell
uv run fetch_all_languages.py --roles Tolk --language "French" --output test_french_tolk.json
```

### Translators Only (*Vertaler*)
```powershell
uv run fetch_all_languages.py --roles Vertaler --language "French" --output test_french_vertaler.json
```

### Both Roles Combined
Searches both Interpreters and Translators, tagging each professional record with their corresponding role:
```powershell
# Using the shortcut flag --both
uv run fetch_all_languages.py --both --limit 5 --output test_both_5.json

# Using explicit --roles both
uv run fetch_all_languages.py --roles both --limit 5 --output test_both_5.json

# Or specifying both roles by name
uv run fetch_all_languages.py --roles Tolk Vertaler --limit 5 --output test_both_5.json
```

---

## 5. Fast Mode: Skip Modal Details (`--no-details`)

By default, the script executes secondary microflows for each professional to fetch contact details (Telephone 1 & 2, Email, Website) and full registration details (source/target language pairs, register levels, and oath dates).

To run a fast scrape that fetches only basic registry fields (Initials, Last Name, Surname Prefix, Wbtv Number, City):

```powershell
# Fast test run on first 5 languages
uv run fetch_all_languages.py --no-details --limit 5 --output fast_test.json

# Fast full scrape for both roles
uv run fetch_all_languages.py --both --no-details --output all_languages_basic.json --delay 0.5
```

---

## 6. Limit-Based Test Runs

Process only the first $N$ languages to verify sequential execution and rate delay:

```powershell
# First 3 languages with a 0.5s delay
uv run fetch_all_languages.py --limit 3 --output test_first_3.json --delay 0.5

# First 10 languages with both roles and custom CSV name
uv run fetch_all_languages.py --limit 10 --both --output test_top_10.json --csv-output test_top_10.csv
```

---

## 7. Full Language Search Run (All 430 Languages)

Fetches results for all target languages from `target_lang.json`.

### Default Settings
Searches Interpreters (*Tolk*) with full contact enrichment, 1.0s delay, producing both `all_languages_results.json` and `all_languages_results.xlsx`:

```powershell
uv run fetch_all_languages.py
```

### Full Run for Both Roles (Interpreters + Translators)
```powershell
uv run fetch_all_languages.py --both --output all_professionals.json --excel-output all_professionals.xlsx --delay 1.0
```

### Full Run with Conservative Delay & Custom Log File
```powershell
uv run fetch_all_languages.py --output all_languages_results.json --delay 1.5 --log-file full_run_2026.log
```

### Full Run with Verbose Debug Output
```powershell
uv run fetch_all_languages.py --output all_languages_results.json --delay 1.0 --verbose --log-file debug_run.log
```

---

## 8. Excel & CSV Export & Offline Conversion

Every execution automatically generates a corresponding `.xlsx` Excel file alongside the `.json` output using `pandas` and `openpyxl`.

### Custom Excel Output Path During Scrape
```powershell
uv run fetch_all_languages.py --limit 5 --output results_5.json --excel-output custom_export.xlsx
```

### Offline Conversion (Instant Excel Generation from Existing JSON)
Converts an already-downloaded JSON results file directly to Excel **without making any network requests**:

```powershell
# Converts all_languages_results.json into all_languages_results.xlsx (default filename)
uv run fetch_all_languages.py --export-excel all_languages_results.json

# Converts existing JSON to a custom Excel filename
uv run fetch_all_languages.py --export-excel all_languages_results.json --excel-output custom_report.xlsx
```

### Offline CSV Generation (Optional)
If CSV format is also needed, you can export directly to CSV:
```powershell
uv run fetch_all_languages.py --export-csv all_languages_results.json --csv-output custom_report.csv
```

---

## 9. Network Reliability & Rate Limiting

Fine-tune request pacing and timeout thresholds for different network conditions:

### Aggressive / Fast Execution (Local / Small Tests)
```powershell
uv run fetch_all_languages.py --limit 5 --delay 0.2 --timeout 15.0
```

### High-Resilience Mode (Slow / Unstable Networks)
Increases timeout to 60s and retries to 5 attempts with backoff:
```powershell
uv run fetch_all_languages.py --delay 2.0 --retries 5 --timeout 60.0
```

### Custom Target Language List
Specify an alternate language JSON file instead of the default `target_lang.json`:
```powershell
uv run fetch_all_languages.py --target-file custom_languages.json --output custom_results.json
```

---

## 10. Command-Line Reference

View all available options and flag descriptions:

```powershell
uv run fetch_all_languages.py --help
```

### Key CLI Options

| Flag | Short | Default | Description |
| :--- | :--- | :--- | :--- |
| `--language` | `-l` | `None` | Filter by language name, substring, or GUID |
| `--limit` | `-n` | `None` | Max number of languages to process |
| `--output` | `-o` | `all_languages_results.json` | Path to output JSON file |
| `--excel-output` | | `None` (auto `.xlsx`) | Path to output Excel file (defaults to output JSON name with `.xlsx`) |
| `--export-excel` | | `None` | Export an existing results JSON file directly to Excel (.xlsx) offline and exit |
| `--csv-output` | | `None` | Optional path to output CSV file if CSV is also desired |
| `--export-csv` | | `None` | Export an existing results JSON file directly to CSV offline and exit |
| `--no-details` | | `False` | Skip modal profile enrichment (fetches basic name/city fields only) |
| `--roles` | | `['Tolk']` | Profession types to search (`Tolk`, `Vertaler`, or `both`) |
| `--both` | | `False` | Shortcut to search both `Tolk` (Interpreters) and `Vertaler` (Translators) |
| `--log-file` | | `fetch_all_languages.log` | Path to `.log` file where execution logs are saved |
| `--target-file` | `-t` | `target_lang.json` | Path to target language list JSON |
| `--concurrency` | `-c` | `10` | Number of concurrent worker threads for fetching profile details |
| `--delay` | `-d` | `1.0` | Pause in seconds between search requests |
| `--retries` | | `3` | Maximum retry attempts for transient HTTP failures |
| `--timeout` | | `30.0` | Timeout per HTTP request in seconds |
| `--verbose` | `-v` | `False` | Enable debug logs on console and log file |

---

## 11. Protected Output Files

To prevent accidental data loss, `fetch_all_languages.py` automatically blocks writing to reserved files:
- `fetch_all_languages.py`
- `test_fetch_all_languages.py`
- `target_lang.json`
- `monitor.py`
- `known_records.json`
- `pyproject.toml`
- `uv.lock`

---


## 12. Data Schema & Dutch Field Glossary

The results returned by the Bureau Wbtv portal use Dutch terminology in their entity models and attribute keys. Below is an explanation of each field found in the output records:

### Professional Record (`OpenWebregister.VertalerTolk`)

```json
{
  "objectType": "OpenWebregister.VertalerTolk",
  "guid": "43628621631685657",
  "attributes": {
    "Voorletters": {
      "readonly": true,
      "value": "A"
    },
    "Achternaam": {
      "readonly": true,
      "value": "Sedrak"
    },
    "Voorvoegsel": {
      "readonly": true,
      "value": null
    },
    "Klantnummer": {
      "readonly": true,
      "value": "40015"
    },
    "OpenWebregister.AdresgegevensWoonadres_VertalerTolk": {
      "objectType": "OpenWebregister.AdresgegevensWoonadres",
      "guid": "12947849170211434",
      "attributes": {
        "Plaats": {
          "readonly": true,
          "value": "-"
        }
      }
    }
  }
}
```

| Field Key | Dutch Meaning | English Explanation | Example / Notes |
| :--- | :--- | :--- | :--- |
| `objectType` | *Vertaler / Tolk* | Entity class: **Translator** (*Vertaler*) or **Interpreter** (*Tolk*). | `"OpenWebregister.VertalerTolk"` |
| `guid` | Global Unique ID | Mendix internal database ID for this specific professional record. | `"43628621631685657"` |
| `Voorletters` | *Voorletters* | **Initials** (first name letters). | `"A"`, `"S."`, `"H"` |
| `Achternaam` | *Achternaam* | **Last Name / Family Name / Surname**. | `"Sedrak"`, `"Ball-Ponne"` |
| `Voorvoegsel` | *Voorvoegsel* (Tussenvoegsel) | **Surname Prefix / Particle** placed before the surname in Dutch names. `null` if none. | `"van"`, `"de"`, `"van der"`, or `null` |
| `Klantnummer` | *Klantnummer* | **Client / Registration ID** (official Bureau Wbtv register number). | `"40015"`, `"25446"` |
| `AdresgegevensWoonadres_VertalerTolk` | *Adresgegevens Woonadres* | Association linking the professional to their **Residential / Registered Address**. | Sub-object containing address details |
| `Plaats` | *Plaats* | **City / Town of Residence**. Set to `"-"` when undisclosed for privacy. | `"AMSTERDAM"`, `"UTRECHT"`, `"-"` |

---

### Filter & Classification Fields

These Dutch terms are used in query parameters and result breakdowns:

| Term | English Translation | Description |
| :--- | :--- | :--- |
| **`Rol`** | Role | Service type searched: `"Tolk"` (Spoken Interpreter) or `"Vertaler"` (Written Translator). |
| **`BronTaal`** | Source Language | The starting language for interpretation (fixed to `"Dutch"`, GUID `46161896180548081`). |
| **`DoelTaal`** | Target Language | The target / destination language of the translation or interpretation. |
| **`NumberOfResults`** | Total Results | Total count of registered professionals matching the criteria. |
| **`NumberOfResults_C1`** | Level C1 Count | Sworn/certified professionals certified at CEFR level C1 (highest register level). |
| **`NumberOfResults_B2`** | Level B2 Count | Sworn/certified professionals certified at CEFR level B2. |
| **`NumberOfResults_Uitwijklijst`**| Contingency List Count | Number of interpreters on the fallback / contingency list (*Uitwijklijst*). |

---

### Excel & CSV Export Column Schema

Each generated `.xlsx` (or `.csv`) file contains the following English columns produced by `export_results_to_excel`:

| Column | Description | Source / Format |
| :--- | :--- | :--- |
| `GUID` | Mendix record ID | Professional's unique entity GUID |
| `Name` | Full formatted name | Combined `Initials + Prefix + Last Name` (e.g. `A. van Dijk`) |
| `Wbtv Number` | Bureau Wbtv client ID | `Klantnummer` |
| `City` | Residence location | `Plaats` (or `-` if private) |
| `Role` | Profession type | `Tolk` or `Vertaler` |
| `Level` | Certification tier | `C1`, `B2`, or `Uitwijklijst` |
| `Language` | Target language searched | e.g. `Arabic Egypt`, `Spanish` |
| `Telephone 1` | Primary phone | Extracted from contact profile modal |
| `Telephone 2` | Secondary phone | Extracted from contact profile modal |
| `Email` | Email address | Extracted from contact profile modal |
| `Website` | Website URL | Extracted from contact profile modal |
| `Registered Languages` | Certified languages | Comma-separated list of target languages |
| `Registrations` | Language pairs & oaths | Formatted details: `Dutch -> English (Tolk, C1, Oath: 2018-05-12)` |
| `Date of Oath` | Swearing-in date(s) | Comma-separated oath dates |



---

## 12. Continuous Monitoring with Email (Default) & Telegram (Optional) Alerts (monitor.py)

The `monitor.py` script runs continuously (every 1 hour by default) and checks for newly registered professionals or newly added languages/roles.

- **Email Alerts (Default)**: Sends a formatted email (with rich HTML profile cards and plain text fallback) to the specified recipients.
- **Telegram Alerts (Optional)**: If Telegram credentials are configured, sends instant alert messages directly to your Telegram chat.
- **Console Fallback**: If neither channel is configured, alert messages print to the console.

---

### Step 1: Configure Your Credentials in `.env`

Copy `.env.example` to `.env`:

```powershell
Copy-Item .env.example .env
```

Open `.env` and fill in your credentials:

```dotenv
# ==============================================================================
# Default Notification Channel: Email (SMTP SSL)
# ==============================================================================
SENDER_EMAIL=your_email@gmail.com
EMAIL_PASSWORD=your_16_character_app_password
RECIPIENT_EMAIL=recipient@example.com

# ==============================================================================
# Optional Notification Channel: Telegram Bot
# ==============================================================================
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
TELEGRAM_CHAT_ID=987654321

# ==============================================================================
# Monitoring Interval (3600 seconds = 1 hour)
# ==============================================================================
MONITOR_INTERVAL_SECONDS=3600
```

> **Note for Gmail users**: Use a Google **App Password** (Google Account > Security > 2-Step Verification > App passwords) instead of your primary password.

---

### Step 2: Test Your Notifications

Verify your alert delivery before starting continuous monitoring:

```powershell
# 1. Test Default Email Connection
uv run monitor.py --test-email

# 2. Test Optional Telegram Connection
uv run monitor.py --test-telegram

# 3. Or test email module directly
uv run notification.py
```

---

### Step 3: Run the Monitor

#### Run Once (Single Check Cycle)
```powershell
uv run monitor.py --once
```

#### Run Continuously (Every 1 Hour)
```powershell
uv run monitor.py
```

#### Send Alerts to a Specific Recipient
```powershell
uv run monitor.py --recipient-email alerts@example.com
```

#### Monitor a Specific Language
```powershell
uv run monitor.py --language Arabic
```

#### Custom Check Interval (Every 30 Minutes)
```powershell
uv run monitor.py --interval 1800
```

#### Check Both Interpreters and Translators Once
```powershell
uv run monitor.py --roles both --once
```

#### Disable Email and Run with Telegram / Console Only
```powershell
uv run monitor.py --disable-email
```

---

### How It Works

| Step | What Happens |
| :--- | :--- |
| **First run** | Reads `all_languages_results.json` to build a quiet baseline -- no spam |
| **Each cycle** | Searches all languages and compares to `known_records.json` |
| **New record found** | Enriches contact details, sends **Email alert (default)** and **Telegram alert (optional)**, updates state |
| **No new records** | Sleeps until next cycle |

---

### Monitor CLI Reference

| Flag | Short | Default | Description |
| :--- | :--- | :--- | :--- |
| `--recipient-email` | `--email` | `.env` / `RECIPIENT_EMAIL` | Recipient email address(es) for default notifications |
| `--sender-email` | | `.env` / `SENDER_EMAIL` | Sender email address for default notifications |
| `--email-password` | | `.env` / `EMAIL_PASSWORD` | App password for sender email |
| `--disable-email` | `--no-email` | `False` | Disable default email notifications |
| `--test-email` | | `False` | Test email connection and exit |
| `--token` | | `.env` / `TELEGRAM_BOT_TOKEN` | Optional Telegram Bot Token |
| `--chat-id` | | `.env` / `TELEGRAM_CHAT_ID` | Optional Telegram Chat ID |
| `--test-telegram` | | `False` | Test Telegram connection and exit |
| `--interval` | | `3600` | Seconds between checks |
| `--language` | `-l` | All | Filter by language name or substring |
| `--limit` | `-n` | All | Max number of languages to check per cycle |
| `--roles` | | `['Tolk']` | `Tolk`, `Vertaler`, or `both` |
| `--scan-workers` | `-w` | `8` | Number of worker threads scanning languages in parallel |
| `--concurrency` | `-c` | `10` | Concurrency for fetching profile details of new records |
| `--once` | | `False` | Run once and exit |
| `--known-file` | | `known_records.json` | State tracking file |
| `--baseline-file` | | `all_languages_results.json` | Baseline for first-run seeding |
| `--output` | | `all_languages_results.json` | JSON file to update on new findings |
| `--excel-output` | | `all_languages_results.xlsx` | Excel file to update on new findings |
| `--log-file` | | `None` | Optional path to `.log` file (console only by default) |
| `--verbose` | `-v` | `False` | Enable debug-level logging |

