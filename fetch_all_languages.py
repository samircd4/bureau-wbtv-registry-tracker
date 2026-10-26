#!/usr/bin/env python3
"""
fetch_all_languages.py

Fetches interpreter search results for target languages from
zoekeentolkvertaler.bureauwbtv.nl using HTTP requests only (requests.Session)
without browser automation.

All session state, CSRF tokens, and object identifiers/hashes are dynamically
negotiated through Mendix XAS runtime operations.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter

# Load environment variables from .env file if present
load_dotenv(Path(__file__).resolve().parent / ".env")

logger = logging.getLogger("language_search")

# URLs
BASE_URL = "https://zoekeentolkvertaler.bureauwbtv.nl/"
XAS_URL = "https://zoekeentolkvertaler.bureauwbtv.nl/xas/"

# Known domain constants from application configuration
DUTCH_GUID = "46161896180548081"
DEFAULT_ROLE = "Tolk"

# Protected file names that must never be overwritten
PROTECTED_FILES = {
    "results.json",
    "requests.json",
    "cookies.json",
    "target_lang.json",
    "fetch_all_languages.py",
    "test_fetch_all_languages.py",
    "monitor.py",
    "known_records.json",
    "pyproject.toml",
    "uv.lock",
}

# Captured Mendix operation IDs
OP_BIND_SESSION_USER = "8Qp8P7HKYV2WlaeR0KLu4A"
OP_BIND_HELPER_SH = "yKLbNrbO3FKeZsa3A89EMQ"
OP_UPDATE_SEARCH_HELPER = "9G3SoD57IFGfvNdhHXbCUA"
OP_UPDATE_SESSION_OWR = "x2smpdFV2VSJdff/q57cWA"
OP_RENDER_START_FORM = "CVd69L2xuFSTLqkmJ/Rp4g"
OP_POST_SOURCE_LANG = "S9enRLbo0FW9jmu+prznyw"
OP_TRIGGER_TARGET_LANG = "Yb+G+W4bgVqAOdRfZwJisQ"
OP_SEARCH = "CJfhZShp1VqOiKewUsJeqQ"

# Details modal operation IDs
OP_OPEN_DETAILS = "g94R0DAnaFq5+pXqD0c9oQ"
OP_GET_REGISTRATIONS = "IvBKHX+hh1yw7x1u7ldc4Q"

# Level filter operation IDs (captured from browser tab interactions)
OP_FILTER_LEVEL_ALL = "IO3a6eQ3Tl6CAslZsN1sqw"
OP_FILTER_LEVEL_C1 = "mIANdOwvi1OYQfOK3R+mtg"
OP_FILTER_LEVEL_B2 = "0bcYwNshBF+R/eEqivdLew"

# Profession/role types supported by the Bureau Wbtv registry
ROLE_TOLK = "Tolk"        # Interpreter (spoken language, C1 and B2 levels)
ROLE_VERTALER = "Vertaler"  # Translator (written language, C1 level only)
DEFAULT_ROLES: list[str] = [ROLE_TOLK]


@dataclass(frozen=True)
class Language:
    guid: str
    name: str


@dataclass
class SessionState:
    csrf_token: str
    search_helper_guid: str
    helper_guid: str
    session_owr_guid: str
    user_guid: str
    helper_obj: dict[str, Any]
    session_owr_obj: dict[str, Any]
    session_user_hash: str
    helper_sh_hash: str


@dataclass
class SearchCounts:
    total: int = 0
    c1: int = 0
    b2: int = 0
    uitwijklijst: int = 0


@dataclass
class SearchCountHashes:
    """Server-issued hash values for NumberOfResults fields, returned in search responses.
    These must be echoed back verbatim when calling a level-filter operation."""
    total: str = ""
    c1: str = ""
    b2: str = ""
    uitwijklijst: str = ""


@dataclass
class LanguageResult:
    language_name: str
    language_guid: str
    status: str  # "success" or "error"
    count: Optional[int] = None
    counts_breakdown: Optional[dict[str, Any]] = None  # Per-role dict when multiple roles searched
    has_more: Optional[bool] = None
    records: Optional[list[dict[str, Any]]] = None  # Each record tagged with "role" key when multi-role
    error: Optional[str] = None
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.status == "error":
            # Remove keys that are not applicable on error
            data.pop("records", None)
            data.pop("counts_breakdown", None)
            data.pop("count", None)
            data.pop("has_more", None)
        else:
            data.pop("error", None)
        return data


# ---------------------------------------------------------------------------
# Payload Construction Helpers (Pure functions)
# ---------------------------------------------------------------------------


def build_get_session_data_payload() -> dict[str, Any]:
    return {
        "action": "get_session_data",
        "params": {
            "offline": True,
            "referrer": None,
            "deviceType": "Desktop",
            "profile": "",
            "timezoneoffset": -360,
            "timezoneId": "UTC",
            "preferredLanguages": ["en-US", "en"],
            "version": 2,
        },
    }


def build_bind_session_user_payload(state: SessionState) -> dict[str, Any]:
    return {
        "action": "runtimeOperation",
        "operationId": OP_BIND_SESSION_USER,
        "params": {},
        "options": {},
        "changes": {
            state.session_owr_guid: {
                "OpenWebregister.SessionOWR_User": {
                    "hash": state.session_user_hash,
                    "value": state.user_guid,
                }
            }
        },
        "objects": [state.session_owr_obj],
    }


def build_bind_helper_payload(state: SessionState) -> dict[str, Any]:
    return {
        "action": "runtimeOperation",
        "operationId": OP_BIND_HELPER_SH,
        "params": {
            "SearchHelper": {"guid": state.search_helper_guid},
            "Helper": {"guid": state.helper_guid},
        },
        "options": {},
        "changes": {
            state.helper_guid: {
                "OpenWebregister.Helper_SearchHelper": {
                    "hash": state.helper_sh_hash,
                    "value": state.search_helper_guid,
                }
            },
            state.session_owr_guid: {
                "OpenWebregister.SessionOWR_User": {
                    "hash": state.session_user_hash,
                    "value": state.user_guid,
                }
            },
        },
        "objects": [state.helper_obj, state.session_owr_obj],
    }


def build_update_search_helper_payload(
    search_helper_guid: str, changes: dict[str, Any]
) -> dict[str, Any]:
    return {
        "action": "runtimeOperation",
        "operationId": OP_UPDATE_SEARCH_HELPER,
        "params": {"SearchHelper": {"guid": search_helper_guid}},
        "validationGuids": [],
        "changes": {search_helper_guid: changes},
        "objects": [],
    }


def build_update_session_owr_payload(state: SessionState) -> dict[str, Any]:
    return {
        "action": "runtimeOperation",
        "operationId": OP_UPDATE_SESSION_OWR,
        "params": {"SessionOWR": {"guid": state.session_owr_guid}},
        "options": {},
        "changes": {
            state.session_owr_guid: {
                "OpenWebregister.SessionOWR_User": {
                    "hash": state.session_user_hash,
                    "value": state.user_guid,
                }
            }
        },
        "objects": [state.session_owr_obj],
    }


def build_render_start_form_payload(
    state: SessionState, role: str = DEFAULT_ROLE
) -> dict[str, Any]:
    return {
        "action": "runtimeOperation",
        "operationId": OP_RENDER_START_FORM,
        "params": {
            "SearchHelper": {"guid": state.search_helper_guid},
            "SessionOWR": {"guid": state.session_owr_guid},
        },
        "options": {},
        "changes": {
            state.session_owr_guid: {
                "OpenWebregister.SessionOWR_User": {
                    "hash": state.session_user_hash,
                    "value": state.user_guid,
                }
            },
            state.search_helper_guid: {"Rol": {"value": role}},
        },
        "objects": [state.session_owr_obj],
    }


def build_retrieve_by_ids_payload(ids: list[str]) -> dict[str, Any]:
    return {
        "action": "retrieve_by_ids",
        "params": {"ids": ids, "schema": {}},
    }


def build_post_source_lang_payload(
    state: SessionState, source_guid: str, role: str = DEFAULT_ROLE
) -> dict[str, Any]:
    return {
        "action": "runtimeOperation",
        "operationId": OP_POST_SOURCE_LANG,
        "params": {
            "SearchHelper": {"guid": state.search_helper_guid},
            "SessionOWR": {"guid": state.session_owr_guid},
        },
        "options": {},
        "changes": {
            state.session_owr_guid: {
                "OpenWebregister.SessionOWR_User": {
                    "hash": state.session_user_hash,
                    "value": state.user_guid,
                }
            },
            state.search_helper_guid: {
                "Rol": {"value": role},
                "OpenWebregister.SearchHelper_BronTaal": {"value": source_guid},
            },
        },
        "objects": [state.session_owr_obj],
    }


def build_set_target_language_payload(
    search_helper_guid: str,
    source_guid: str,
    target_guid: str,
    role: str = DEFAULT_ROLE,
) -> dict[str, Any]:
    return build_update_search_helper_payload(
        search_helper_guid,
        {
            "Rol": {"value": role},
            "OpenWebregister.SearchHelper_BronTaal": {"value": source_guid},
            "OpenWebregister.SearchHelper_DoelTaal": {"value": target_guid},
        },
    )


def build_trigger_target_lang_payload(
    search_helper_guid: str,
    source_guid: str,
    target_guid: str,
    role: str = DEFAULT_ROLE,
) -> dict[str, Any]:
    return {
        "action": "runtimeOperation",
        "operationId": OP_TRIGGER_TARGET_LANG,
        "params": {"SearchHelper": {"guid": search_helper_guid}},
        "validationGuids": [],
        "changes": {
            search_helper_guid: {
                "Rol": {"value": role},
                "OpenWebregister.SearchHelper_BronTaal": {"value": source_guid},
                "OpenWebregister.SearchHelper_DoelTaal": {"value": target_guid},
            }
        },
        "objects": [],
    }


def build_search_payload(state: SessionState) -> dict[str, Any]:
    return {
        "action": "runtimeOperation",
        "operationId": OP_SEARCH,
        "params": {
            "SearchHelper": {"guid": state.search_helper_guid},
            "Helper": {"guid": state.helper_guid},
        },
        "options": {},
        "changes": {
            state.helper_guid: {
                "OpenWebregister.Helper_SearchHelper": {
                    "hash": state.helper_sh_hash,
                    "value": state.search_helper_guid,
                }
            },
            state.session_owr_guid: {
                "OpenWebregister.SessionOWR_User": {
                    "hash": state.session_user_hash,
                    "value": state.user_guid,
                }
            },
        },
        "objects": [state.helper_obj, state.session_owr_obj],
    }


def build_filter_level_payload(
    search_helper_guid: str,
    level_op_id: str,
    counts: SearchCounts,
    hashes: "SearchCountHashes",
) -> dict[str, Any]:
    """
    Builds the payload to activate a level filter (C1, B2, or All) on the SearchHelper.
    This mirrors clicking the 'Rbtv C1 level' / 'Rbtv B2 level' / 'Alle' tab in the browser.
    The server identifies which level to apply purely from the ``level_op_id``.
    The ``hashes`` must be the exact values returned by the preceding OP_SEARCH response.

    Args:
        search_helper_guid: The GUID of the active SearchHelper object.
        level_op_id: One of OP_FILTER_LEVEL_ALL, OP_FILTER_LEVEL_C1, OP_FILTER_LEVEL_B2.
        counts: The current result counts (returned by the preceding search response).
        hashes: Server-issued hash values for each count field (from the search response).
    """
    return {
        "action": "runtimeOperation",
        "operationId": level_op_id,
        "params": {"SearchHelper": {"guid": search_helper_guid}},
        "validationGuids": [search_helper_guid],
        "changes": {
            search_helper_guid: {
                "NumberOfResults": {"hash": hashes.total, "value": str(counts.total)},
                "NumberOfResults_C1": {"hash": hashes.c1, "value": str(counts.c1)},
                "NumberOfResults_B2": {"hash": hashes.b2, "value": str(counts.b2)},
                "NumberOfResults_Uitwijklijst": {"hash": hashes.uitwijklijst, "value": str(counts.uitwijklijst)},
            }
        },
        "objects": [],
    }


# ---------------------------------------------------------------------------
# Response Validation & Parsing Helpers
# ---------------------------------------------------------------------------


def parse_session_data(data: Any) -> SessionState:
    """Validates and extracts dynamic session credentials from get_session_data."""
    if not isinstance(data, dict):
        raise ValueError(
            f"Expected get_session_data response to be a dict, got {type(data).__name__}"
        )

    csrf_token = data.get("csrftoken")
    if not csrf_token or not isinstance(csrf_token, str):
        raise ValueError("Missing or invalid 'csrftoken' in get_session_data response")

    user_info = data.get("user")
    if not isinstance(user_info, dict) or "guid" not in user_info:
        raise ValueError("Missing 'user' object in get_session_data response")
    user_guid = str(user_info["guid"])

    raw_objects = data.get("objects", [])
    if not isinstance(raw_objects, list):
        raise ValueError("Expected 'objects' to be a list in get_session_data response")

    objects_by_type: dict[str, dict[str, Any]] = {}
    for obj in raw_objects:
        if isinstance(obj, dict) and "objectType" in obj:
            objects_by_type[obj["objectType"]] = obj

    for required_type in (
        "OpenWebregister.SearchHelper",
        "OpenWebregister.Helper",
        "OpenWebregister.SessionOWR",
    ):
        if required_type not in objects_by_type:
            raise ValueError(f"Required object '{required_type}' not found in get_session_data response")

    search_helper_obj = objects_by_type["OpenWebregister.SearchHelper"]
    helper_obj = objects_by_type["OpenWebregister.Helper"]
    session_owr_obj = objects_by_type["OpenWebregister.SessionOWR"]

    sh_guid = str(search_helper_obj["guid"])
    helper_guid = str(helper_obj["guid"])
    session_owr_guid = str(session_owr_obj["guid"])

    changes = data.get("changes", {})
    if not isinstance(changes, dict):
        raise ValueError("Expected 'changes' to be a dict in get_session_data response")

    session_user_entry = changes.get(session_owr_guid, {}).get("OpenWebregister.SessionOWR_User", {})
    session_user_hash = session_user_entry.get("hash")
    if not session_user_hash:
        raise ValueError(f"Missing 'OpenWebregister.SessionOWR_User' hash for session OWR {session_owr_guid}")

    helper_sh_entry = changes.get(helper_guid, {}).get("OpenWebregister.Helper_SearchHelper", {})
    helper_sh_hash = helper_sh_entry.get("hash")
    if not helper_sh_hash:
        raise ValueError(f"Missing 'OpenWebregister.Helper_SearchHelper' hash for helper {helper_guid}")

    return SessionState(
        csrf_token=csrf_token,
        search_helper_guid=sh_guid,
        helper_guid=helper_guid,
        session_owr_guid=session_owr_guid,
        user_guid=user_guid,
        helper_obj=helper_obj,
        session_owr_obj=session_owr_obj,
        session_user_hash=session_user_hash,
        helper_sh_hash=helper_sh_hash,
    )


def parse_search_response(
    data: Any, search_helper_guid: str
) -> tuple[list[dict[str, Any]], SearchCounts, SearchCountHashes, bool, list[str]]:
    """
    Validates and extracts search items, counts, hashes, and pagination metadata
    from an OP_SEARCH response.
    Returns: (records, counts, hashes, has_more, result_guids)
    """
    if not isinstance(data, dict):
        raise ValueError(f"Expected search response to be a dict, got {type(data).__name__}")

    if "partialObjects" not in data:
        raise ValueError(
            "The search response does not contain 'partialObjects'. "
            f"Available keys: {list(data.keys())}"
        )

    records = data["partialObjects"]
    if not isinstance(records, list):
        raise TypeError(f"Expected 'partialObjects' to be a list, got {type(records).__name__}")

    # Check for result guids and hasMoreItems (pagination / capping indicators)
    raw_result_guids = data.get("resultGuids", [])
    result_guids = [str(g) for g in raw_result_guids] if isinstance(raw_result_guids, list) else []
    has_more = bool(data.get("hasMoreItems", False))

    # Parse counts reported in changes
    counts = SearchCounts(total=len(records))
    hashes = SearchCountHashes()
    changes = data.get("changes", {})
    if isinstance(changes, dict) and search_helper_guid in changes:
        sh_changes = changes[search_helper_guid]
        if isinstance(sh_changes, dict):
            counts.total = _parse_int_val(sh_changes.get("NumberOfResults", {}).get("value"), default=len(records))
            counts.c1 = _parse_int_val(sh_changes.get("NumberOfResults_C1", {}).get("value"), default=0)
            counts.b2 = _parse_int_val(sh_changes.get("NumberOfResults_B2", {}).get("value"), default=0)
            counts.uitwijklijst = _parse_int_val(sh_changes.get("NumberOfResults_Uitwijklijst", {}).get("value"), default=0)
            # Capture hashes — required when calling level-filter operations
            hashes.total = sh_changes.get("NumberOfResults", {}).get("hash", "")
            hashes.c1 = sh_changes.get("NumberOfResults_C1", {}).get("hash", "")
            hashes.b2 = sh_changes.get("NumberOfResults_B2", {}).get("hash", "")
            hashes.uitwijklijst = sh_changes.get("NumberOfResults_Uitwijklijst", {}).get("hash", "")

    return records, counts, hashes, has_more, result_guids


def _parse_int_val(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Bureau Wbtv HTTP Client
# ---------------------------------------------------------------------------


class BureauWbtvClient:
    """
    Manages session lifecycle and executes target language searches via HTTP requests.
    """

    def __init__(
        self,
        base_url: str = BASE_URL,
        xas_url: str = XAS_URL,
        source_language_guid: str = DUTCH_GUID,
        role: str = DEFAULT_ROLE,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        cache_path: Optional[Path] = None,
    ) -> None:
        self.base_url = base_url
        self.xas_url = xas_url
        self.source_language_guid = source_language_guid
        self.role = role
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # Set default headers matching browser requests
        self.session.headers.update(
            {
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "origin": self.base_url.rstrip("/"),
                "referer": self.base_url,
                "accept": "application/json",
                "content-type": "application/json",
            }
        )
        # Establish Mendix origin cookie
        self.session.cookies.set(
            "originURI", "/login.html", domain="zoekeentolkvertaler.bureauwbtv.nl"
        )
        self.session_state: Optional[SessionState] = None
        self._details_cache: dict[str, dict[str, str]] = {}
        self._cache_path: Path = cache_path or Path("details_cache.json")
        self._cache_lock = threading.Lock()
        self._session_lock = threading.Lock()
        self._load_details_cache()

    def _load_details_cache(self) -> None:
        """Loads cached profile details from disk or falls back to Excel if available."""
        self._details_cache = dict(get_details_cache(self._cache_path))
        if self._details_cache:
            logger.info("Loaded %d profile(s) into details cache", len(self._details_cache))

    def _save_details_cache(self) -> None:
        """Saves cached profile details to disk in a thread-safe manner."""
        try:
            with self._cache_lock:
                cache_copy = dict(self._details_cache)
            with self._cache_path.open("w", encoding="utf-8") as f:
                json.dump(cache_copy, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.warning("Failed to save details cache: %s", e)

    def initialize_session(self) -> SessionState:
        """
        Executes the initial handshake and configuration sequence required by Mendix.
        """
        logger.info("Initializing fresh session with Mendix XAS runtime...")
        init_payload = build_get_session_data_payload()
        init_data = self._post_raw(init_payload, csrf=False)
        self.session_state = parse_session_data(init_data)

        # Set the dynamic CSRF token on session headers
        self.session.headers["x-csrf-token"] = self.session_state.csrf_token

        # Execute app bootstrap sequence
        state = self.session_state
        self._post_raw(build_bind_session_user_payload(state))
        self._post_raw(build_bind_helper_payload(state))

        # Select role (Tolk)
        self._post_raw(
            build_update_search_helper_payload(
                state.search_helper_guid, {"Rol": {"value": self.role}}
            )
        )
        self._post_raw(build_update_session_owr_payload(state))
        self._post_raw(build_render_start_form_payload(state, role=self.role))

        # Select source language (Dutch)
        self._post_raw(build_retrieve_by_ids_payload([self.source_language_guid]))
        self._post_raw(
            build_update_search_helper_payload(
                state.search_helper_guid,
                {
                    "Rol": {"value": self.role},
                    "OpenWebregister.SearchHelper_BronTaal": {
                        "value": self.source_language_guid
                    },
                },
            )
        )
        self._post_raw(
            build_post_source_lang_payload(
                state, source_guid=self.source_language_guid, role=self.role
            )
        )

        logger.info(
            "Session initialized successfully (SearchHelper GUID: %s)",
            state.search_helper_guid,
        )
        return self.session_state

    def _post_raw(self, payload: dict[str, Any], csrf: bool = True) -> dict[str, Any]:
        """Sends a POST request to XAS_URL and parses the JSON response."""
        resp = self.session.post(self.xas_url, json=payload, timeout=self.timeout)
        if resp.status_code in (401, 403):
            raise PermissionError(f"HTTP {resp.status_code} Unauthorized / CSRF failure from XAS")
        resp.raise_for_status()

        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"XAS returned non-JSON response (status {resp.status_code}): {resp.text[:200]}") from exc

    def _post_with_retry(
        self, payload: dict[str, Any], max_retries: Optional[int] = None
    ) -> dict[str, Any]:
        """
        Sends a POST request with bounded retries on transient errors or session invalidation.
        """
        retries = self.max_retries if max_retries is None else max_retries
        attempt = 0
        last_exc: Optional[Exception] = None

        while attempt <= retries:
            try:
                return self._post_raw(payload)
            except (requests.RequestException, PermissionError, ValueError) as exc:
                last_exc = exc
                attempt += 1
                if attempt > retries:
                    break

                backoff = self.retry_backoff * (2 ** (attempt - 1))
                logger.warning(
                    "Request failed (%s). Retrying in %.2fs (attempt %d/%d)...",
                    exc,
                    backoff,
                    attempt,
                    retries,
                )
                time.sleep(backoff)

                # If unauthorized or permission error, renew the session
                if isinstance(exc, PermissionError) or "csrf" in str(exc).lower():
                    logger.info("Renewing session due to auth/session error...")
                    with self._session_lock:
                        try:
                            self.initialize_session()
                        except Exception as reinit_exc:
                            logger.error("Failed to renew session: %s", reinit_exc)

        raise RuntimeError(f"Request failed after {retries} retries: {last_exc}") from last_exc

    def search_language(
        self, target_lang: Language, role: Optional[str] = None
    ) -> LanguageResult:
        """
        Executes the search for a single target language and role (profession type).
        Parses results and retrieves any capped/paginated records.

        Args:
            target_lang: The target language to search for.
            role: Profession type override ("Tolk" or "Vertaler"). Defaults to self.role.
        """
        if self.session_state is None:
            self.initialize_session()

        state = self.session_state
        assert state is not None

        effective_role = role if role is not None else self.role

        try:
            # Step 1: Optional retrieve_by_ids for target language (mirrors browser action)
            self._post_with_retry(build_retrieve_by_ids_payload([target_lang.guid]))

            # Step 2: Set target language on SearchHelper
            self._post_with_retry(
                build_set_target_language_payload(
                    state.search_helper_guid,
                    self.source_language_guid,
                    target_lang.guid,
                    role=effective_role,
                )
            )

            # Step 3: Trigger runtime update / validation
            self._post_with_retry(
                build_trigger_target_lang_payload(
                    state.search_helper_guid,
                    self.source_language_guid,
                    target_lang.guid,
                    role=effective_role,
                )
            )

            # Step 4: Execute search microflow
            search_data = self._post_with_retry(build_search_payload(state))

            # Step 5: Parse search response (records, counts, hashes, has_more, result_guids)
            records, counts, count_hashes, has_more, result_guids = parse_search_response(
                search_data, state.search_helper_guid
            )

            # Check if results are capped / paginated (e.g. resultGuids has items not in partialObjects)
            existing_guids = {str(item.get("guid")) for item in records if isinstance(item, dict) and "guid" in item}
            missing_guids = [g for g in result_guids if g not in existing_guids]

            if missing_guids:
                logger.info(
                    "Language %s (%s): %d additional guids detected in resultGuids; retrieving...",
                    target_lang.name,
                    effective_role,
                    len(missing_guids),
                )
                fetched_objects = self._retrieve_objects_by_ids(missing_guids)
                records.extend(fetched_objects)

            # Tag records with role and default level if not already set
            for rec in records:
                if isinstance(rec, dict):
                    if "role" not in rec and effective_role:
                        rec["role"] = effective_role
                    if "level" not in rec:
                        rec["level"] = "c1"

            result = LanguageResult(
                language_name=target_lang.name,
                language_guid=target_lang.guid,
                status="success",
                count=len(records),
                counts_breakdown=asdict(counts),
                has_more=has_more,
                records=records,
            )
            # Stash hashes on the result for use by search_language_roles()
            result._count_hashes = count_hashes  # type: ignore[attr-defined]
            return result

        except Exception as exc:
            logger.error(
                "Failed search for language '%s' (GUID %s) role '%s': %s",
                target_lang.name,
                target_lang.guid,
                effective_role,
                exc,
            )
            return LanguageResult(
                language_name=target_lang.name,
                language_guid=target_lang.guid,
                status="error",
                error=str(exc),
            )

    def search_language_roles(
        self,
        target_lang: Language,
        roles: Optional[list[str]] = None,
    ) -> LanguageResult:
        """
        Searches a target language across multiple profession types (Tolk and/or Vertaler).
        All records from all roles are combined. Each record is tagged with a
        ``"role"`` key (``"Tolk"`` or ``"Vertaler"``) so they can be identified.
        The ``counts_breakdown`` is a dict keyed by role name.

        Args:
            target_lang: The target language to search.
            roles: List of roles to search. Defaults to DEFAULT_ROLES (Tolk + Vertaler).

        Returns:
            A combined LanguageResult with all records from all roles, each tagged.
        """
        effective_roles = roles if roles is not None else DEFAULT_ROLES

        combined_records: list[dict[str, Any]] = []
        combined_counts: dict[str, dict[str, Any]] = {}
        any_success = False
        errors: list[str] = []
        any_has_more = False

        for role in effective_roles:
            logger.debug(
                "Searching language '%s' for role '%s'...",
                target_lang.name,
                role,
            )
            res = self.search_language(target_lang, role=role)

            if res.status == "success":
                any_success = True
                if res.has_more:
                    any_has_more = True

                role_counts = res.counts_breakdown or {}
                combined_counts[role] = role_counts

                # For Tolk: identify which records are B2 vs C1 via a filtered sub-search.
                # For Vertaler: there is no B2 tier — all are implicitly C1.
                b2_guids: set[str] = set()
                if role == ROLE_TOLK and role_counts.get("b2", 0) > 0:
                    counts_obj = SearchCounts(
                        total=role_counts.get("total", 0),
                        c1=role_counts.get("c1", 0),
                        b2=role_counts.get("b2", 0),
                        uitwijklijst=role_counts.get("uitwijklijst", 0),
                    )
                    # Retrieve hashes that were captured from the search response
                    count_hashes: SearchCountHashes = getattr(
                        res, "_count_hashes", SearchCountHashes()
                    )
                    b2_guids = self._get_b2_guids(counts_obj, count_hashes)
                    logger.debug(
                        "Language '%s' Tolk: identified %d B2 GUID(s)",
                        target_lang.name,
                        len(b2_guids),
                    )

                # Tag each record with its role and level
                for rec in res.records or []:
                    tagged = dict(rec)  # shallow copy to avoid mutating the original
                    tagged["role"] = role
                    if role == ROLE_VERTALER:
                        tagged["level"] = "c1"  # Vertaler only exists at C1
                    else:
                        guid = str(rec.get("guid", ""))
                        tagged["level"] = "b2" if guid in b2_guids else "c1"
                    combined_records.append(tagged)
            else:
                errors.append(f"{role}: {res.error}")

        if not any_success:
            return LanguageResult(
                language_name=target_lang.name,
                language_guid=target_lang.guid,
                status="error",
                error="; ".join(errors),
            )

        return LanguageResult(
            language_name=target_lang.name,
            language_guid=target_lang.guid,
            status="success",
            count=len(combined_records),
            counts_breakdown=combined_counts,
            has_more=any_has_more,
            records=combined_records,
        )

    def _get_b2_guids(self, counts: SearchCounts, hashes: SearchCountHashes) -> set[str]:
        """
        Fires the B2 level filter operation followed by a re-search to determine
        which record GUIDs belong to the Rbtv B2 level.
        After collecting the GUIDs, resets the filter to 'All'.

        Returns:
            A set of GUID strings for records at the B2 level.
            Returns an empty set if counts.b2 == 0 (no B2 records exist).
        """
        if counts.b2 == 0:
            return set()

        state = self.session_state
        assert state is not None

        b2_guids: set[str] = set()
        try:
            # Activate B2 level filter on the SearchHelper
            self._post_with_retry(
                build_filter_level_payload(
                    state.search_helper_guid, OP_FILTER_LEVEL_B2, counts, hashes
                )
            )
            # Re-run the search — server now returns only B2 records
            b2_search_data = self._post_with_retry(build_search_payload(state))

            b2_records = b2_search_data.get("partialObjects", [])
            result_guids = b2_search_data.get("resultGuids", [])

            # Collect GUIDs from partialObjects
            for rec in b2_records if isinstance(b2_records, list) else []:
                if isinstance(rec, dict) and "guid" in rec:
                    b2_guids.add(str(rec["guid"]))

            # Also include any overflow GUIDs listed in resultGuids
            for g in result_guids if isinstance(result_guids, list) else []:
                b2_guids.add(str(g))

        except Exception as exc:
            logger.warning("Could not determine B2 GUIDs (level tagging skipped): %s", exc)
        finally:
            # Always reset filter to All so subsequent searches are not affected
            try:
                self._post_with_retry(
                    build_filter_level_payload(
                        state.search_helper_guid, OP_FILTER_LEVEL_ALL, counts, hashes
                    )
                )
            except Exception as reset_exc:
                logger.warning("Failed to reset level filter to All: %s", reset_exc)

        return b2_guids

    def _retrieve_objects_by_ids(self, guids: list[str], chunk_size: int = 50) -> list[dict[str, Any]]:
        """Batches retrieve_by_ids calls for missing object GUIDs."""
        all_objects: list[dict[str, Any]] = []
        for i in range(0, len(guids), chunk_size):
            chunk = guids[i : i + chunk_size]
            payload = build_retrieve_by_ids_payload(chunk)
            data = self._post_with_retry(payload)
            objs = data.get("objects", [])
            if isinstance(objs, list):
                all_objects.extend(objs)
        return all_objects

    def fetch_profile_details(self, guid: str) -> dict[str, str]:
        """
        Fetches contact details, all registered languages, and oath dates
        for an interpreter/translator GUID using Mendix XAS runtime operations.
        Results are cached in memory and in details_cache.json.
        """
        with self._cache_lock:
            if guid in self._details_cache:
                return self._details_cache[guid]

        state = self.session_state
        if not state:
            with self._session_lock:
                if not self.session_state:
                    self.initialize_session()
                state = self.session_state
            assert state is not None

        phone1 = "-"
        phone2 = "-"
        email = "-"
        website = "-"

        try:
            # 1. OP_OPEN_DETAILS (Contactgegevens)
            payload_open = {
                "action": "runtimeOperation",
                "operationId": OP_OPEN_DETAILS,
                "params": {"CurrentObject": {"guid": guid}},
                "options": {},
                "changes": {
                    state.session_owr_guid: {
                        "OpenWebregister.SessionOWR_User": {
                            "hash": state.session_user_hash,
                            "value": state.user_guid,
                        }
                    }
                },
                "objects": [
                    {
                        "objectType": "OpenWebregister.SessionOWR",
                        "guid": state.session_owr_guid,
                        "attributes": {
                            "OpenWebregister.SessionOWR_User": {
                                "readonly": True,
                                "value": None,
                            }
                        },
                        "hash": state.session_owr_obj.get("hash"),
                    }
                ],
            }
            res_open = self._post_with_retry(payload_open)
            for obj in res_open.get("objects", []):
                if obj.get("objectType") == "OpenWebregister.Contactgegevens":
                    attrs = obj.get("attributes", {})
                    phone1 = (attrs.get("Telefoon1") or {}).get("value") or "-"
                    phone2 = (attrs.get("Telefoon2") or {}).get("value") or "-"
                    email = (attrs.get("Email") or {}).get("value") or "-"
                    website = (attrs.get("Website") or {}).get("value") or "-"
        except Exception as e:
            logger.warning("Failed to fetch contact details for GUID %s: %s", guid, e)

        registered_languages: list[str] = []
        reg_strs: list[str] = []
        oath_list: list[str] = []

        try:
            # 2. OP_GET_REGISTRATIONS (Languages & Oath)
            payload_reg = {
                "action": "runtimeOperation",
                "operationId": OP_GET_REGISTRATIONS,
                "params": {
                    "VertalerTolk": {"guid": guid},
                    "CurrentObject": {"guid": guid},
                },
                "options": {
                    "offset": 0,
                    "amount": 100,
                    "sort": [],
                    "wantCount": False,
                    "extraXpath": "",
                },
                "changes": {},
                "objects": [],
            }
            res_reg = self._post_with_retry(payload_reg)
            oath_dates: dict[str, str] = {}
            registrations: list[dict[str, str]] = []

            for obj in res_reg.get("objects", []):
                ot = obj.get("objectType")
                attrs = obj.get("attributes", {})
                if ot == "OpenWebregister.Beediging":
                    oath_dates[obj.get("guid")] = (
                        (attrs.get("DatumBeediging") or {}).get("value") or ""
                    )
                elif ot == "OpenWebregister.Registratie":
                    src = (attrs.get("Brontaal_EN") or {}).get("value") or ""
                    tgt = (attrs.get("Doeltaal_EN") or {}).get("value") or ""
                    role_en = (attrs.get("Rol_EN") or {}).get("value") or ""
                    lvl = (attrs.get("TypeRegistratie_EN") or {}).get("value") or ""
                    b_guid = str(
                        (attrs.get("OpenWebregister.Registratie_Beediging") or {}).get("value") or ""
                    )

                    if tgt and tgt not in registered_languages:
                        registered_languages.append(tgt)

                    registrations.append(
                        {
                            "source": src,
                            "target": tgt,
                            "role": role_en,
                            "level": lvl,
                            "b_guid": b_guid,
                        }
                    )

            for r in registrations:
                oath = oath_dates.get(r["b_guid"]) or "-"
                if oath != "-" and oath not in oath_list:
                    oath_list.append(oath)
                reg_strs.append(
                    f"{r['source']} -> {r['target']} ({r['role']}, {r['level']}, Oath: {oath})"
                )

        except Exception as e:
            logger.warning("Failed to fetch registrations for GUID %s: %s", guid, e)

        result = {
            "Telephone 1": phone1,
            "Telephone 2": phone2,
            "Email": email,
            "Website": website,
            "Registered Languages": ", ".join(registered_languages),
            "Registrations": "; ".join(reg_strs),
            "Date of Oath": ", ".join(oath_list) if oath_list else "-",
        }

        with self._cache_lock:
            self._details_cache[guid] = result
        return result

    def enrich_records_with_details(
        self, records: list[dict[str, Any]], max_workers: int = 10
    ) -> None:
        """
        Enriches records in place with their detailed profile information
        (contact details, all registered languages, and date of oath).
        Uses concurrent worker threads for fast network retrieval.
        """
        unique_guids: list[str] = []
        for r in records:
            g = str(r.get("guid") or "")
            if g and g not in unique_guids:
                unique_guids.append(g)

        with self._cache_lock:
            needed = [g for g in unique_guids if g not in self._details_cache]

        logger.info(
            "Enriching %d records (%d unique profiles, %d need network fetch)...",
            len(records),
            len(unique_guids),
            len(needed),
        )

        if needed:
            workers = min(max_workers, len(needed)) if max_workers > 1 else 1
            if workers > 1:
                completed = 0
                total_needed = len(needed)
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    future_to_guid = {
                        executor.submit(self.fetch_profile_details, g): g
                        for g in needed
                    }
                    for future in as_completed(future_to_guid):
                        completed += 1
                        g = future_to_guid[future]
                        try:
                            future.result()
                        except Exception as exc:
                            logger.warning("Failed to fetch details for GUID %s: %s", g, exc)

                        if completed % 10 == 0 or completed == total_needed:
                            logger.info("  [%d/%d] Fetching profile details...", completed, total_needed)

                        if completed % 25 == 0:
                            self._save_details_cache()
            else:
                for idx, g in enumerate(needed, start=1):
                    if idx % 10 == 0 or idx == len(needed):
                        logger.info("  [%d/%d] Fetching profile details...", idx, len(needed))
                    self.fetch_profile_details(g)
                    if idx % 25 == 0:
                        self._save_details_cache()

            self._save_details_cache()

        # Attach details into record dictionaries
        with self._cache_lock:
            for r in records:
                g = str(r.get("guid") or "")
                if g in self._details_cache:
                    r["details"] = self._details_cache[g]

    def close(self) -> None:
        self._save_details_cache()
        self.session.close()


# ---------------------------------------------------------------------------
# Data Handling & Orchestration
# ---------------------------------------------------------------------------


def load_target_languages(path: Path) -> list[Language]:
    if not path.is_file():
        raise FileNotFoundError(f"Target language file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise TypeError(f"Expected list in {path}, got {type(data).__name__}")

    languages: list[Language] = []
    for item in data:
        if isinstance(item, dict) and "guid" in item and "name" in item:
            languages.append(Language(guid=str(item["guid"]), name=str(item["name"])))
        else:
            raise ValueError(f"Invalid language item: {item}")

    return languages


def filter_target_languages(
    languages: list[Language],
    query: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[Language]:
    filtered = languages
    if query:
        q = query.strip().lower()
        # Match exact GUID, or case-insensitive substring/equality on name
        filtered = [
            lang
            for lang in languages
            if lang.guid == q or q in lang.name.lower()
        ]

    if limit is not None and limit > 0:
        filtered = filtered[:limit]

    return filtered


_GLOBAL_DETAILS_CACHE: Optional[dict[str, dict[str, str]]] = None


def get_details_cache(cache_path: Optional[Path] = None) -> dict[str, dict[str, str]]:
    """Loads and caches details in memory. Falls back to reading from Excel or JSON if details_cache.json is absent."""
    global _GLOBAL_DETAILS_CACHE
    if _GLOBAL_DETAILS_CACHE is not None and cache_path is None:
        return _GLOBAL_DETAILS_CACHE

    target_path = cache_path or (Path(__file__).resolve().parent / "details_cache.json")
    if target_path.is_file():
        try:
            with target_path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict) and loaded:
                    if cache_path is None:
                        _GLOBAL_DETAILS_CACHE = loaded
                    return loaded
        except Exception as e:
            logger.warning("Could not read details cache from %s: %s", target_path, e)

    # Fallback: Recover cached contact details from existing Excel file using pandas
    excel_fallback = target_path.parent / "all_languages_results.xlsx"
    if excel_fallback.is_file():
        try:
            df = pd.read_excel(excel_fallback, dtype=str).fillna("")
            recovered: dict[str, dict[str, str]] = {}
            detail_cols = [
                "Telephone 1", "Telephone 2", "Email", "Website",
                "Registered Languages", "Registrations", "Date of Oath"
            ]
            for _, row in df.iterrows():
                guid = str(row.get("GUID") or "").strip()
                if not guid or guid in recovered:
                    continue
                entry = {c: str(row.get(c) or "").strip() for c in detail_cols}
                if any(v and v != "-" for v in entry.values()):
                    recovered[guid] = entry
            if recovered:
                logger.info("Loaded %d profile details from %s via pandas", len(recovered), excel_fallback.name)
                if cache_path is None:
                    _GLOBAL_DETAILS_CACHE = recovered
                return recovered
        except Exception as err:
            logger.debug("Could not read fallback details from Excel: %s", err)

    # Fallback: Recover from all_languages_results.json
    json_fallback = target_path.parent / "all_languages_results.json"
    if json_fallback.is_file():
        try:
            with json_fallback.open("r", encoding="utf-8") as f:
                data = json.load(f)
                results = data.get("results", {})
                recovered_json: dict[str, dict[str, str]] = {}
                for _, lang_data in results.items():
                    for rec in lang_data.get("records", []):
                        g = str(rec.get("guid") or "").strip()
                        details = rec.get("details")
                        if g and details and isinstance(details, dict):
                            recovered_json[g] = details
                if recovered_json:
                    logger.info("Loaded %d profile details from %s", len(recovered_json), json_fallback.name)
                    if cache_path is None:
                        _GLOBAL_DETAILS_CACHE = recovered_json
                    return recovered_json
        except Exception as err:
            logger.debug("Could not read fallback details from JSON: %s", err)

    return {}


def extract_record_csv_fields(
    record: dict[str, Any],
    language_name: str = "",
    details_cache: Optional[dict[str, dict[str, str]]] = None,
) -> dict[str, str]:
    """Extracts CSV row fields with English column names from a search record, falling back to cache for details."""
    guid = str(record.get("guid") or "").strip()
    role = str(record.get("role") or "").strip()
    level = str(record.get("level") or "").strip()

    attrs = record.get("attributes") or {}
    voorletters = ((attrs.get("Voorletters") or {}).get("value") or "").strip()
    voorvoegsel = ((attrs.get("Voorvoegsel") or {}).get("value") or "").strip()
    achternaam = ((attrs.get("Achternaam") or {}).get("value") or "").strip()

    name_parts = [p for p in (voorletters, voorvoegsel, achternaam) if p]
    name = " ".join(name_parts)

    wbtv_number = str((attrs.get("Klantnummer") or {}).get("value") or "").strip()

    addr_obj = attrs.get("OpenWebregister.AdresgegevensWoonadres_VertalerTolk") or {}
    addr_attrs = addr_obj.get("attributes") or {} if isinstance(addr_obj, dict) else {}
    city = str((addr_attrs.get("Plaats") or {}).get("value") or "").strip()

    # Check direct record details first, then fallback to cache
    details = record.get("details")
    if not details and guid:
        cache = details_cache if details_cache is not None else get_details_cache()
        details = cache.get(guid)

    details = details or {}
    phone1 = str(details.get("Telephone 1") or "-")
    phone2 = str(details.get("Telephone 2") or "-")
    email = str(details.get("Email") or "-")
    website = str(details.get("Website") or "-")
    reg_langs = str(details.get("Registered Languages") or "")
    registrations = str(details.get("Registrations") or "")
    date_of_oath = str(details.get("Date of Oath") or "-")

    # Fallback: if role or level is missing from record, infer from profile registration details
    if not role or not level:
        target_reg = ""
        if registrations and language_name:
            for item in registrations.split(";"):
                if f"-> {language_name}" in item or f"{language_name} ->" in item:
                    target_reg = item
                    break

        reg_to_check = target_reg or registrations

        if not role:
            if "Interpreter" in reg_to_check or "Tolk" in reg_to_check:
                role = "Tolk"
            elif "Translator" in reg_to_check or "Vertaler" in reg_to_check:
                role = "Vertaler"

        if not level:
            if "B2" in reg_to_check or "b2" in reg_to_check:
                level = "b2"
            elif "C1" in reg_to_check or "c1" in reg_to_check:
                level = "c1"

    return {
        "GUID": guid,
        "Name": name,
        "Wbtv Number": wbtv_number,
        "City": city,
        "Role": role,
        "Level": level,
        "Language": language_name,
        "Telephone 1": phone1,
        "Telephone 2": phone2,
        "Email": email,
        "Website": website,
        "Registered Languages": reg_langs,
        "Registrations": registrations,
        "Date of Oath": date_of_oath,
    }


TABULAR_COLUMNS = [
    "GUID",
    "Name",
    "Wbtv Number",
    "City",
    "Role",
    "Level",
    "Language",
    "Telephone 1",
    "Telephone 2",
    "Email",
    "Website",
    "Registered Languages",
    "Registrations",
    "Date of Oath",
]


def export_results_to_excel(
    excel_path: Path,
    results: dict[str, Any],
    cache: Optional[dict[str, dict[str, str]]] = None,
) -> int:
    """
    Exports language search results to an Excel (.xlsx) file using pandas and openpyxl with English headers:
    GUID, Name, Wbtv Number, City, Role, Level, Language,
    Telephone 1, Telephone 2, Email, Website, Registered Languages, Registrations, Date of Oath
    """
    if excel_path.name in PROTECTED_FILES:
        raise ValueError(
            f"Output Excel file name '{excel_path.name}' is protected and must not be overwritten."
        )

    details_cache = cache if cache is not None else get_details_cache()

    rows: list[dict[str, str]] = []
    for lang_name, lang_data in results.items():
        records = getattr(lang_data, "records", None)
        if records is None and isinstance(lang_data, dict):
            records = lang_data.get("records", [])

        for record in records or []:
            rows.append(extract_record_csv_fields(record, lang_name, details_cache=details_cache))

    df = pd.DataFrame(rows, columns=TABULAR_COLUMNS)
    excel_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(excel_path, index=False, engine="openpyxl")

    logger.info("Exported %d records to Excel via pandas: %s", len(df), excel_path)
    return len(df)


def extract_known_keys_from_excel(excel_path: Path) -> set[str]:
    """Reads all existing registration keys (guid:role:language) from Excel using pandas."""
    if not excel_path.is_file():
        return set()
    try:
        df = pd.read_excel(excel_path, dtype=str).fillna("")
        keys: set[str] = set()
        for _, row in df.iterrows():
            g = str(row.get("GUID") or "").strip()
            r = str(row.get("Role") or "").strip()
            l = str(row.get("Language") or "").strip()
            if g and l:
                keys.add(f"{g}:{r}:{l}")
        return keys
    except Exception as e:
        logger.warning("Could not read known keys from Excel %s: %s", excel_path, e)
        return set()


def upsert_records_to_excel(
    excel_path: Path,
    items: list[tuple[str, dict[str, Any]]],
    cache: Optional[dict[str, dict[str, str]]] = None,
) -> tuple[int, int]:
    """
    Incrementally updates or appends records in an Excel file using pandas.
    - If a record with (GUID, Role, Language) or (GUID, Language) already exists in Excel:
      it updates that row in-place (updating Level, contact details, Name, etc.).
    - If it does not exist: it appends a new row to Excel.
    Returns (inserted_count, updated_count).
    """
    if not items:
        return (0, 0)

    details_cache = cache if cache is not None else get_details_cache()

    if excel_path.is_file():
        try:
            df = pd.read_excel(excel_path, dtype=str).fillna("")
        except Exception as e:
            logger.warning("Could not read existing Excel %s (%s). Creating new DataFrame.", excel_path, e)
            df = pd.DataFrame(columns=TABULAR_COLUMNS)
    else:
        df = pd.DataFrame(columns=TABULAR_COLUMNS)

    for col in TABULAR_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    inserted = 0
    updated = 0

    # Build row index maps for fast lookup
    guid_role_lang_map: dict[tuple[str, str, str], int] = {}
    guid_lang_map: dict[tuple[str, str], int] = {}

    for idx, (g_val, r_val, l_val) in enumerate(
        zip(df["GUID"], df["Role"], df["Language"])
    ):
        g = str(g_val or "").strip()
        r = str(r_val or "").strip()
        l = str(l_val or "").strip()
        if g and l:
            guid_role_lang_map[(g, r, l)] = idx
            guid_lang_map[(g, l)] = idx

    new_rows: list[dict[str, str]] = []

    for lang_name, record in items:
        row_fields = extract_record_csv_fields(record, language_name=lang_name, details_cache=details_cache)
        g = str(row_fields.get("GUID") or "").strip()
        r = str(row_fields.get("Role") or "").strip()
        l = str(row_fields.get("Language") or "").strip()

        match_idx = guid_role_lang_map.get((g, r, l))
        if match_idx is None:
            match_idx = guid_lang_map.get((g, l))

        if match_idx is not None:
            # Update existing row in place
            for col in TABULAR_COLUMNS:
                new_val = row_fields.get(col, "")
                if new_val:
                    df.at[match_idx, col] = new_val
            updated += 1
        else:
            # Append new row
            new_rows.append(row_fields)
            inserted += 1

    if new_rows:
        new_df = pd.DataFrame(new_rows, columns=TABULAR_COLUMNS)
        df = pd.concat([df, new_df], ignore_index=True)

    excel_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(excel_path, index=False, engine="openpyxl")
    logger.info(
        "Excel updated via pandas: %d inserted, %d updated in %s",
        inserted, updated, excel_path
    )
    return (inserted, updated)


def export_results_to_csv(
    csv_path: Path,
    results: dict[str, Any],
    cache: Optional[dict[str, dict[str, str]]] = None,
) -> int:
    """
    Exports language search results to a CSV file using pandas with English headers:
    GUID, Name, Wbtv Number, City, Role, Level, Language,
    Telephone 1, Telephone 2, Email, Website, Registered Languages, Registrations, Date of Oath
    """
    if csv_path.name in PROTECTED_FILES:
        raise ValueError(
            f"Output CSV file name '{csv_path.name}' is protected and must not be overwritten."
        )

    details_cache = cache if cache is not None else get_details_cache()

    rows: list[dict[str, str]] = []
    for lang_name, lang_data in results.items():
        records = getattr(lang_data, "records", None)
        if records is None and isinstance(lang_data, dict):
            records = lang_data.get("records", [])

        for record in records or []:
            rows.append(extract_record_csv_fields(record, lang_name, details_cache=details_cache))

    df = pd.DataFrame(rows, columns=TABULAR_COLUMNS)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    logger.info("Exported %d records to CSV via pandas: %s", len(df), csv_path)
    return len(df)


def save_results(
    output_path: Path,
    results: dict[str, Any],
    metadata: dict[str, Any],
    excel_path: Optional[Path] = None,
    csv_path: Optional[Path] = None,
) -> None:
    """Saves structured results to a JSON file and an Excel (.xlsx) file, attaching cached details and refusing to overwrite protected files."""
    if output_path.name in PROTECTED_FILES:
        raise ValueError(
            f"Output file name '{output_path.name}' is protected and must not be overwritten."
        )

    details_cache = get_details_cache()

    # Ensure cached contact details are attached to each record
    serialized_results: dict[str, Any] = {}
    for name, res in results.items():
        if hasattr(res, "to_dict"):
            res_dict = res.to_dict()
        elif isinstance(res, dict):
            res_dict = dict(res)
        else:
            continue

        records = res_dict.get("records") or []
        for rec in records:
            if isinstance(rec, dict) and not rec.get("details"):
                g = str(rec.get("guid") or "").strip()
                if g and g in details_cache:
                    rec["details"] = details_cache[g]

        serialized_results[name] = res_dict

    output_payload = {
        "metadata": metadata,
        "results": serialized_results,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=4, ensure_ascii=False)
        f.write("\n")

    logger.info("Saved results to %s", output_path)

    # Automatically generate corresponding Excel (.xlsx) file
    target_excel_path = excel_path or output_path.with_suffix(".xlsx")
    export_results_to_excel(target_excel_path, serialized_results, cache=details_cache)
    logger.info("Saved Excel results to %s", target_excel_path)

    # If csv_path is explicitly provided, export to CSV as well
    if csv_path:
        export_results_to_csv(csv_path, serialized_results, cache=details_cache)
        logger.info("Saved CSV results to %s", csv_path)



def setup_logging(
    log_file: Optional[Path] = None, verbose: bool = False
) -> None:
    """Configures logging to output to both console and a .log file."""
    log_level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Clear existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # Stream (console) handler
    console_handler = logging.StreamHandler()
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


# ---------------------------------------------------------------------------
# Main Routine
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch interpreter search results for target languages via HTTP only (no Playwright)."
    )
    parser.add_argument(
        "-l",
        "--language",
        type=str,
        default=None,
        help="Filter target language by name substring or GUID (e.g. 'Arabic Egypt' or '46161896180548020').",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=None,
        help="Limit number of languages to process (e.g. 1 for verification).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="all_languages_results.json",
        help="Path to output JSON file (default: all_languages_results.json).",
    )
    parser.add_argument(
        "--excel-output",
        type=str,
        default=None,
        help="Path to output Excel (.xlsx) file (default: same name as output with .xlsx extension).",
    )
    parser.add_argument(
        "--export-excel",
        type=str,
        default=None,
        metavar="JSON_FILE",
        help="Export an existing results JSON file directly to Excel (.xlsx) and exit.",
    )
    parser.add_argument(
        "--csv-output",
        type=str,
        default=None,
        help="Path to output CSV file if CSV is also desired (optional).",
    )
    parser.add_argument(
        "--export-csv",
        type=str,
        default=None,
        metavar="JSON_FILE",
        help="Export an existing results JSON file directly to CSV and exit.",
    )
    parser.add_argument(
        "--no-details",
        action="store_true",
        help="Skip fetching contact details and registrations from profile modal (fetches basic fields only).",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default="fetch_all_languages.log",
        help="Path to .log file where execution logs are saved (default: fetch_all_languages.log).",
    )
    parser.add_argument(
        "-t",
        "--target-file",
        type=str,
        default="target_lang.json",
        help="Path to target_lang.json file (default: target_lang.json in workspace).",
    )
    parser.add_argument(
        "-d",
        "--delay",
        type=float,
        default=1.0,
        help="Delay in seconds between language searches (default: 1.0).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Maximum retries for transient HTTP failures (default: 3).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP request timeout in seconds (default: 30.0).",
    )
    parser.add_argument(
        "--roles",
        type=str,
        nargs="+",
        default=DEFAULT_ROLES,
        choices=[ROLE_TOLK, ROLE_VERTALER, "both"],
        metavar="ROLE",
        help=(
            f"Profession types to search (default: {ROLE_TOLK}). "
            f"Choices: {ROLE_TOLK}, {ROLE_VERTALER}, both. "
            f"Example: --roles both (or --both)"
        ),
    )
    parser.add_argument(
        "--both",
        action="store_true",
        help="Search both Tolk (Interpreters) and Vertaler (Translators).",
    )
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=10,
        help="Number of concurrent worker threads for profile details enrichment (default: 10).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )

    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    log_path = Path(args.log_file)
    if not log_path.is_absolute():
        log_path = base_dir / log_path

    if log_path.name in PROTECTED_FILES:
        print(f"Error: Specified log file '{log_path.name}' is a protected file.")
        raise SystemExit(1)

    setup_logging(log_file=log_path, verbose=args.verbose)

    target_path = Path(args.target_file)
    if not target_path.is_absolute():
        target_path = base_dir / target_path

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = base_dir / output_path

    if output_path.name in PROTECTED_FILES:
        logger.error(
            "Specified output file '%s' is a protected file. Please specify a different output path.",
            output_path.name,
        )
        raise SystemExit(1)

    excel_path = Path(args.excel_output) if args.excel_output else output_path.with_suffix(".xlsx")
    if not excel_path.is_absolute():
        excel_path = base_dir / excel_path

    if excel_path.name in PROTECTED_FILES:
        logger.error(
            "Specified Excel output file '%s' is a protected file. Please specify a different output path.",
            excel_path.name,
        )
        raise SystemExit(1)

    csv_path: Optional[Path] = None
    if args.csv_output:
        csv_path = Path(args.csv_output)
        if not csv_path.is_absolute():
            csv_path = base_dir / csv_path
        if csv_path.name in PROTECTED_FILES:
            logger.error(
                "Specified CSV output file '%s' is a protected file. Please specify a different output path.",
                csv_path.name,
            )
            raise SystemExit(1)

    # If --export-excel is passed, export existing JSON directly and exit
    if args.export_excel:
        export_source = Path(args.export_excel)
        if not export_source.is_absolute():
            export_source = base_dir / export_source

        if not export_source.is_file():
            logger.error("JSON file for Excel export not found: %s", export_source)
            raise SystemExit(1)

        logger.info("Loading existing JSON results from %s...", export_source)
        with export_source.open(encoding="utf-8") as f:
            data = json.load(f)

        records_count = export_results_to_excel(excel_path, data.get("results", {}))
        print("\n" + "=" * 60)
        print("EXCEL EXPORT SUMMARY")
        print("=" * 60)
        print(f"Source JSON:        {export_source}")
        print(f"Excel Output:       {excel_path}")
        print(f"Total rows written: {records_count}")
        print("=" * 60)
        return

    # If --export-csv is passed, export existing JSON directly to CSV and exit
    if args.export_csv:
        target_csv = csv_path or output_path.with_suffix(".csv")
        export_source = Path(args.export_csv)
        if not export_source.is_absolute():
            export_source = base_dir / export_source

        if not export_source.is_file():
            logger.error("JSON file for CSV export not found: %s", export_source)
            raise SystemExit(1)

        logger.info("Loading existing JSON results from %s...", export_source)
        with export_source.open(encoding="utf-8") as f:
            data = json.load(f)

        records_count = export_results_to_csv(target_csv, data.get("results", {}))
        print("\n" + "=" * 60)
        print("CSV EXPORT SUMMARY")
        print("=" * 60)
        print(f"Source JSON:        {export_source}")
        print(f"CSV Output:         {target_csv}")
        print(f"Total rows written: {records_count}")
        print("=" * 60)
        return

    # 1. Load target languages
    all_languages = load_target_languages(target_path)
    selected_languages = filter_target_languages(
        all_languages, query=args.language, limit=args.limit
    )

    if not selected_languages:
        logger.warning("No languages matched the specified criteria.")
        return

    logger.info(
        "Processing %d language(s) (out of %d total languages)...",
        len(selected_languages),
        len(all_languages),
    )

    client = BureauWbtvClient(
        timeout=args.timeout,
        max_retries=args.retries,
    )

    start_time = datetime.now(timezone.utc)
    results: dict[str, LanguageResult] = {}
    successful_count = 0
    failed_count = 0
    total_records = 0

    if args.both or "both" in [r.lower() for r in args.roles]:
        selected_roles: list[str] = [ROLE_TOLK, ROLE_VERTALER]
    else:
        selected_roles = args.roles
    logger.info("Searching for profession role(s): %s", ", ".join(selected_roles))

    try:
        client.initialize_session()

        for idx, lang in enumerate(selected_languages, start=1):
            logger.info(
                "[%d/%d] Searching language: '%s' (GUID: %s)...",
                idx,
                len(selected_languages),
                lang.name,
                lang.guid,
            )

            # Search language across selected role(s) (tags role and b2/c1 level on every record)
            res = client.search_language_roles(lang, roles=selected_roles)

            results[lang.name] = res

            if res.status == "success":
                successful_count += 1
                rec_count = res.count or 0
                total_records += rec_count
                logger.info(
                    "[%d/%d] SUCCESS for '%s': found %d record(s)",
                    idx,
                    len(selected_languages),
                    lang.name,
                    rec_count,
                )
            else:
                failed_count += 1
                logger.error(
                    "[%d/%d] ERROR for '%s': %s",
                    idx,
                    len(selected_languages),
                    lang.name,
                    res.error,
                )

            # Delay between requests to respect rate limits
            if idx < len(selected_languages) and args.delay > 0:
                time.sleep(args.delay)

        # Enrich records with details if not disabled
        if not args.no_details:
            all_records = [rec for res in results.values() for rec in (res.records or [])]
            client.enrich_records_with_details(all_records, max_workers=args.concurrency)

    finally:
        client.close()

    end_time = datetime.now(timezone.utc)
    metadata = {
        "source_language": "Dutch",
        "source_language_guid": DUTCH_GUID,
        "roles_searched": selected_roles,
        "total_languages_requested": len(selected_languages),
        "successful_searches": successful_count,
        "failed_searches": failed_count,
        "total_records_found": total_records,
        "started_at": start_time.isoformat(),
        "completed_at": end_time.isoformat(),
        "elapsed_seconds": round((end_time - start_time).total_seconds(), 2),
    }

    save_results(output_path, results, metadata, excel_path=excel_path, csv_path=csv_path)

    print("\n" + "=" * 60)
    print("FETCH SUMMARY")
    print("=" * 60)
    print(f"Total languages processed: {len(selected_languages)}")
    print(f"Successful:                {successful_count}")
    print(f"Failed:                    {failed_count}")
    print(f"Total records found:       {total_records}")
    print(f"JSON output saved to:      {output_path}")
    print(f"Excel output saved to:     {excel_path}")
    if csv_path:
        print(f"CSV output saved to:       {csv_path}")
    print(f"Elapsed time:              {metadata['elapsed_seconds']}s")
    print(f"Log saved to:              {log_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
