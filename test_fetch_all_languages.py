"""
test_fetch_all_languages.py

Unit tests for fetch_all_languages.py.
Validates:
- Payload construction functions
- Response validation and parsing (including pagination / capped results)
- Target language reading and filtering
- Result organization and metadata calculation
- Prevention of overwriting protected files
- HTTP client retry and error capture logic without live network calls
"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fetch_all_languages import (
    DEFAULT_ROLE,
    DUTCH_GUID,
    OP_BIND_HELPER_SH,
    OP_BIND_SESSION_USER,
    OP_POST_SOURCE_LANG,
    OP_SEARCH,
    OP_TRIGGER_TARGET_LANG,
    OP_UPDATE_SEARCH_HELPER,
    OP_UPDATE_SESSION_OWR,
    BureauWbtvClient,
    Language,
    LanguageResult,
    SearchCounts,
    SessionState,
    build_bind_helper_payload,
    build_bind_session_user_payload,
    build_get_session_data_payload,
    build_post_source_lang_payload,
    build_render_start_form_payload,
    build_retrieve_by_ids_payload,
    build_search_payload,
    build_set_target_language_payload,
    build_trigger_target_lang_payload,
    build_update_search_helper_payload,
    build_update_session_owr_payload,
    export_results_to_csv,
    export_results_to_excel,
    extract_known_keys_from_excel,
    extract_known_guids_from_json,
    extract_record_csv_fields,
    filter_target_languages,
    load_target_languages,
    parse_search_response,
    parse_session_data,
    save_results,
    setup_logging,
    upsert_records_to_excel,
)


class TestPayloadConstruction(unittest.TestCase):
    def setUp(self) -> None:
        self.state = SessionState(
            csrf_token="dummy-csrf-token",
            search_helper_guid="1001",
            helper_guid="2002",
            session_owr_guid="3003",
            user_guid="4004",
            helper_obj={"objectType": "OpenWebregister.Helper", "guid": "2002"},
            session_owr_obj={"objectType": "OpenWebregister.SessionOWR", "guid": "3003"},
            session_user_hash="hash-session-user",
            helper_sh_hash="hash-helper-sh",
        )

    def test_build_get_session_data_payload(self) -> None:
        payload = build_get_session_data_payload()
        self.assertEqual(payload["action"], "get_session_data")
        self.assertTrue(payload["params"]["offline"])
        self.assertEqual(payload["params"]["deviceType"], "Desktop")

    def test_build_bind_session_user_payload(self) -> None:
        payload = build_bind_session_user_payload(self.state)
        self.assertEqual(payload["action"], "runtimeOperation")
        self.assertEqual(payload["operationId"], OP_BIND_SESSION_USER)
        self.assertIn("3003", payload["changes"])
        user_change = payload["changes"]["3003"]["OpenWebregister.SessionOWR_User"]
        self.assertEqual(user_change["value"], "4004")
        self.assertEqual(user_change["hash"], "hash-session-user")
        self.assertEqual(payload["objects"], [self.state.session_owr_obj])

    def test_build_bind_helper_payload(self) -> None:
        payload = build_bind_helper_payload(self.state)
        self.assertEqual(payload["action"], "runtimeOperation")
        self.assertEqual(payload["operationId"], OP_BIND_HELPER_SH)
        self.assertEqual(payload["params"]["SearchHelper"]["guid"], "1001")
        self.assertEqual(payload["params"]["Helper"]["guid"], "2002")
        helper_change = payload["changes"]["2002"]["OpenWebregister.Helper_SearchHelper"]
        self.assertEqual(helper_change["value"], "1001")
        self.assertEqual(helper_change["hash"], "hash-helper-sh")

    def test_build_update_search_helper_payload(self) -> None:
        changes = {"Rol": {"value": "Tolk"}}
        payload = build_update_search_helper_payload("1001", changes)
        self.assertEqual(payload["action"], "runtimeOperation")
        self.assertEqual(payload["operationId"], OP_UPDATE_SEARCH_HELPER)
        self.assertEqual(payload["params"]["SearchHelper"]["guid"], "1001")
        self.assertEqual(payload["changes"]["1001"], changes)

    def test_build_set_target_language_payload(self) -> None:
        payload = build_set_target_language_payload(
            search_helper_guid="1001",
            source_guid="46161896180548081",
            target_guid="46161896180548020",
            role="Tolk",
        )
        self.assertEqual(payload["operationId"], OP_UPDATE_SEARCH_HELPER)
        changes = payload["changes"]["1001"]
        self.assertEqual(changes["Rol"]["value"], "Tolk")
        self.assertEqual(changes["OpenWebregister.SearchHelper_BronTaal"]["value"], "46161896180548081")
        self.assertEqual(changes["OpenWebregister.SearchHelper_DoelTaal"]["value"], "46161896180548020")

    def test_build_trigger_target_lang_payload(self) -> None:
        payload = build_trigger_target_lang_payload(
            search_helper_guid="1001",
            source_guid="46161896180548081",
            target_guid="46161896180548020",
        )
        self.assertEqual(payload["operationId"], OP_TRIGGER_TARGET_LANG)
        self.assertEqual(payload["params"]["SearchHelper"]["guid"], "1001")
        changes = payload["changes"]["1001"]
        self.assertEqual(changes["OpenWebregister.SearchHelper_DoelTaal"]["value"], "46161896180548020")

    def test_build_search_payload(self) -> None:
        payload = build_search_payload(self.state)
        self.assertEqual(payload["operationId"], OP_SEARCH)
        self.assertEqual(payload["params"]["SearchHelper"]["guid"], "1001")
        self.assertEqual(payload["params"]["Helper"]["guid"], "2002")
        self.assertEqual(len(payload["objects"]), 2)

    def test_build_retrieve_by_ids_payload(self) -> None:
        payload = build_retrieve_by_ids_payload(["id1", "id2"])
        self.assertEqual(payload["action"], "retrieve_by_ids")
        self.assertEqual(payload["params"]["ids"], ["id1", "id2"])


class TestResponseParsing(unittest.TestCase):
    def test_parse_session_data_success(self) -> None:
        data = {
            "csrftoken": "test-csrf-123",
            "user": {"guid": "9999", "objectType": "System.User"},
            "objects": [
                {"objectType": "OpenWebregister.SearchHelper", "guid": "1001"},
                {"objectType": "OpenWebregister.Helper", "guid": "2002"},
                {"objectType": "OpenWebregister.SessionOWR", "guid": "3003"},
            ],
            "changes": {
                "3003": {
                    "OpenWebregister.SessionOWR_User": {
                        "hash": "user-hash",
                        "value": "9999",
                    }
                },
                "2002": {
                    "OpenWebregister.Helper_SearchHelper": {
                        "hash": "helper-hash",
                        "value": "1001",
                    }
                },
            },
        }
        state = parse_session_data(data)
        self.assertEqual(state.csrf_token, "test-csrf-123")
        self.assertEqual(state.search_helper_guid, "1001")
        self.assertEqual(state.helper_guid, "2002")
        self.assertEqual(state.session_owr_guid, "3003")
        self.assertEqual(state.user_guid, "9999")
        self.assertEqual(state.session_user_hash, "user-hash")
        self.assertEqual(state.helper_sh_hash, "helper-hash")

    def test_parse_session_data_missing_csrf(self) -> None:
        with self.assertRaises(ValueError):
            parse_session_data({"objects": [], "changes": {}})

    def test_parse_session_data_missing_objects(self) -> None:
        data = {
            "csrftoken": "token",
            "user": {"guid": "123"},
            "objects": [{"objectType": "OpenWebregister.SearchHelper", "guid": "1001"}],
            "changes": {},
        }
        with self.assertRaises(ValueError):
            parse_session_data(data)

    def test_parse_search_response_success(self) -> None:
        mock_response = {
            "partialObjects": [
                {"guid": "101", "attributes": {"Achternaam": {"value": "Smith"}}},
                {"guid": "102", "attributes": {"Achternaam": {"value": "Jones"}}},
            ],
            "resultGuids": ["101", "102"],
            "hasMoreItems": False,
            "changes": {
                "1001": {
                    "NumberOfResults": {"value": "2"},
                    "NumberOfResults_C1": {"value": "2"},
                    "NumberOfResults_B2": {"value": "0"},
                    "NumberOfResults_Uitwijklijst": {"value": "0"},
                }
            },
        }
        records, counts, hashes, has_more, guids = parse_search_response(mock_response, "1001")
        self.assertEqual(len(records), 2)
        self.assertEqual(counts.total, 2)
        self.assertEqual(counts.c1, 2)
        self.assertEqual(counts.b2, 0)
        self.assertFalse(has_more)
        self.assertEqual(guids, ["101", "102"])

    def test_parse_search_response_empty(self) -> None:
        mock_response = {
            "partialObjects": [],
            "resultGuids": [],
            "hasMoreItems": False,
            "changes": {
                "1001": {
                    "NumberOfResults": {"value": "0"},
                }
            },
        }
        records, counts, hashes, has_more, guids = parse_search_response(mock_response, "1001")
        self.assertEqual(len(records), 0)
        self.assertEqual(counts.total, 0)
        self.assertFalse(has_more)

    def test_parse_search_response_missing_partial_objects(self) -> None:
        with self.assertRaises(ValueError):
            parse_search_response({"changes": {}}, "1001")

    def test_parse_search_response_invalid_type(self) -> None:
        with self.assertRaises(ValueError):
            parse_search_response("not-a-dict", "1001")


class TestLanguageFilteringAndLoading(unittest.TestCase):
    def setUp(self) -> None:
        self.languages = [
            Language(guid="46161896180548081", name="Dutch"),
            Language(guid="46161896180547992", name="Abkhaz"),
            Language(guid="46161896180548020", name="Arabic Egypt"),
            Language(guid="46161896180548017", name="Arabic Algeria"),
        ]

    def test_filter_by_exact_name(self) -> None:
        res = filter_target_languages(self.languages, query="Arabic Egypt")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].guid, "46161896180548020")

    def test_filter_by_substring(self) -> None:
        res = filter_target_languages(self.languages, query="arabic")
        self.assertEqual(len(res), 2)
        names = {l.name for l in res}
        self.assertEqual(names, {"Arabic Egypt", "Arabic Algeria"})

    def test_filter_by_guid(self) -> None:
        res = filter_target_languages(self.languages, query="46161896180547992")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].name, "Abkhaz")

    def test_filter_with_limit(self) -> None:
        res = filter_target_languages(self.languages, limit=2)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0].name, "Dutch")
        self.assertEqual(res[1].name, "Abkhaz")

    def test_load_target_languages_from_file(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".json") as f:
            json.dump(
                [{"guid": "1", "name": "Lang1"}, {"guid": "2", "name": "Lang2"}],
                f,
            )
            temp_path = Path(f.name)

        try:
            langs = load_target_languages(temp_path)
            self.assertEqual(len(langs), 2)
            self.assertEqual(langs[0].name, "Lang1")
        finally:
            temp_path.unlink()


class TestResultOrganizationAndPersistence(unittest.TestCase):
    def test_language_result_to_dict_success(self) -> None:
        res = LanguageResult(
            language_name="Arabic Egypt",
            language_guid="46161896180548020",
            status="success",
            count=1,
            counts_breakdown={"total": 1, "c1": 1, "b2": 0, "uitwijklijst": 0},
            has_more=False,
            records=[{"guid": "rec1"}],
        )
        d = res.to_dict()
        self.assertEqual(d["status"], "success")
        self.assertEqual(d["count"], 1)
        self.assertEqual(len(d["records"]), 1)
        self.assertNotIn("error", d)

    def test_language_result_to_dict_error(self) -> None:
        res = LanguageResult(
            language_name="FailedLang",
            language_guid="0000",
            status="error",
            error="HTTP 500: Server Error",
        )
        d = res.to_dict()
        self.assertEqual(d["status"], "error")
        self.assertEqual(d["error"], "HTTP 500: Server Error")
        self.assertNotIn("records", d)
        self.assertNotIn("counts_breakdown", d)
        self.assertNotIn("count", d)

    def test_save_results_protects_reserved_files(self) -> None:
        res = {
            "Lang1": LanguageResult(
                language_name="Lang1",
                language_guid="1",
                status="success",
                count=0,
                records=[],
            )
        }
        meta = {"total": 1}

        with self.assertRaises(ValueError):
            save_results(Path("results.json"), res, meta)

        with self.assertRaises(ValueError):
            save_results(Path("requests.json"), res, meta)

        with self.assertRaises(ValueError):
            save_results(Path("cookies.json"), res, meta)

    def test_save_results_writes_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = Path(tmp_dir) / "custom_results.json"
            res = {
                "Arabic Egypt": LanguageResult(
                    language_name="Arabic Egypt",
                    language_guid="46161896180548020",
                    status="success",
                    count=1,
                    counts_breakdown={"total": 1, "c1": 1, "b2": 0, "uitwijklijst": 0},
                    has_more=False,
                    records=[{"guid": "rec1"}],
                ),
                "BrokenLang": LanguageResult(
                    language_name="BrokenLang",
                    language_guid="999",
                    status="error",
                    error="Connection reset",
                ),
            }
            meta = {"source": "Dutch", "total": 2}
            save_results(out_file, res, meta)

            self.assertTrue(out_file.is_file())
            with out_file.open("r", encoding="utf-8") as f:
                saved = json.load(f)

            self.assertIn("metadata", saved)
            self.assertIn("results", saved)
            self.assertEqual(saved["results"]["Arabic Egypt"]["count"], 1)
            self.assertEqual(saved["results"]["BrokenLang"]["status"], "error")
            self.assertIn("Connection reset", saved["results"]["BrokenLang"]["error"])

            # Verify Excel was generated alongside JSON
            excel_file = out_file.with_suffix(".xlsx")
            self.assertTrue(excel_file.is_file())

    def test_extract_record_csv_fields(self) -> None:
        record = {
            "guid": "43628621631559015",
            "role": "Tolk",
            "level": "c1",
            "attributes": {
                "Voorletters": {"value": "I"},
                "Achternaam": {"value": "Brinkman-Ramla"},
                "Voorvoegsel": {"value": None},
                "Klantnummer": {"value": "26683"},
                "OpenWebregister.AdresgegevensWoonadres_VertalerTolk": {
                    "attributes": {"Plaats": {"value": "Arnhem"}}
                },
            },
        }
        fields = extract_record_csv_fields(record, language_name="Arabic Syria-Lebanon")
        self.assertEqual(fields["GUID"], "43628621631559015")
        self.assertEqual(fields["Name"], "I Brinkman-Ramla")
        self.assertEqual(fields["Wbtv Number"], "26683")
        self.assertEqual(fields["City"], "Arnhem")
        self.assertEqual(fields["Role"], "Tolk")
        self.assertEqual(fields["Level"], "c1")
        self.assertEqual(fields["Language"], "Arabic Syria-Lebanon")

    def test_extract_record_csv_fields_fallback_from_registrations(self) -> None:
        """Verifies that role and level are inferred from profile details registrations when missing on record."""
        record = {
            "guid": "43628621632036213",
            "attributes": {
                "Voorletters": {"value": "A."},
                "Achternaam": {"value": "Sayed"},
                "Klantnummer": {"value": "16414"},
            },
            "details": {
                "Registrations": (
                    "Dutch -> Arabic Iraq (Interpreter, Rbtv C1 level, Oath: 2017-02-15); "
                    "Dutch -> Arabic Egypt (Interpreter, Rbtv C1 level, Oath: 2017-02-15)"
                )
            },
        }
        fields = extract_record_csv_fields(record, language_name="Arabic Egypt")
        self.assertEqual(fields["Role"], "Tolk")
        self.assertEqual(fields["Level"], "c1")

    def test_export_results_to_csv_pandas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / "output.csv"
            res = {
                "Arabic": {
                    "records": [
                        {
                            "guid": "123",
                            "role": "Tolk",
                            "level": "b2",
                            "attributes": {
                                "Voorletters": {"value": "A."},
                                "Voorvoegsel": {"value": "van"},
                                "Achternaam": {"value": "Dijk"},
                                "Klantnummer": {"value": "9999"},
                                "OpenWebregister.AdresgegevensWoonadres_VertalerTolk": {
                                    "attributes": {"Plaats": {"value": "Utrecht"}}
                                },
                            },
                        }
                    ]
                }
            }
            written = export_results_to_csv(csv_path, res)
            self.assertEqual(written, 1)
            self.assertTrue(csv_path.is_file())

            # Read back with pandas
            import pandas as pd
            df = pd.read_csv(csv_path)
            self.assertEqual(len(df), 1)
            self.assertEqual(df["Name"].iloc[0], "A. van Dijk")
            self.assertEqual(df["City"].iloc[0], "Utrecht")
            self.assertEqual(df["Level"].iloc[0], "b2")

    def test_export_results_to_excel_pandas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            excel_path = Path(tmp_dir) / "output.xlsx"
            res = {
                "Arabic": {
                    "records": [
                        {
                            "guid": "123",
                            "role": "Tolk",
                            "level": "b2",
                            "attributes": {
                                "Voorletters": {"value": "A."},
                                "Voorvoegsel": {"value": "van"},
                                "Achternaam": {"value": "Dijk"},
                                "Klantnummer": {"value": "9999"},
                                "OpenWebregister.AdresgegevensWoonadres_VertalerTolk": {
                                    "attributes": {"Plaats": {"value": "Utrecht"}}
                                },
                            },
                        }
                    ]
                }
            }
            written = export_results_to_excel(excel_path, res)
            self.assertEqual(written, 1)
            self.assertTrue(excel_path.is_file())

            # Read back with pandas
            import pandas as pd
            df = pd.read_excel(excel_path)
            self.assertEqual(len(df), 1)
            self.assertEqual(df["Name"].iloc[0], "A. van Dijk")
            self.assertEqual(df["City"].iloc[0], "Utrecht")
            self.assertEqual(df["Level"].iloc[0], "b2")

    def test_upsert_records_to_excel_pandas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            excel_path = Path(tmp_dir) / "all_languages_results.xlsx"
            rec1 = {
                "guid": "123",
                "role": "Tolk",
                "level": "b2",
                "attributes": {
                    "Voorletters": {"value": "A."},
                    "Achternaam": {"value": "Dijk"},
                    "Klantnummer": {"value": "9999"},
                    "OpenWebregister.AdresgegevensWoonadres_VertalerTolk": {
                        "attributes": {"Plaats": {"value": "Utrecht"}}
                    },
                },
            }
            # 1. Insert new record
            ins, upd = upsert_records_to_excel(excel_path, [("Arabic", rec1)])
            self.assertEqual(ins, 1)
            self.assertEqual(upd, 0)

            # 2. Update existing record (level changed to c1)
            rec1_updated = dict(rec1)
            rec1_updated["level"] = "c1"
            ins, upd = upsert_records_to_excel(excel_path, [("Arabic", rec1_updated)])
            self.assertEqual(ins, 0)
            self.assertEqual(upd, 1)

            # Verify in pandas
            import pandas as pd
            df = pd.read_excel(excel_path)
            self.assertEqual(len(df), 1)
            self.assertEqual(df["Level"].iloc[0], "c1")

            # 3. Insert second distinct record
            rec2 = {
                "guid": "456",
                "role": "Tolk",
                "level": "b2",
                "attributes": {
                    "Voorletters": {"value": "B."},
                    "Achternaam": {"value": "Bakker"},
                    "Klantnummer": {"value": "8888"},
                },
            }
            ins, upd = upsert_records_to_excel(excel_path, [("English", rec2)])
            self.assertEqual(ins, 1)
            self.assertEqual(upd, 0)

            df = pd.read_excel(excel_path)
            self.assertEqual(len(df), 2)

            # 4. Extract known keys from Excel — now returns plain GUIDs
            keys = extract_known_keys_from_excel(excel_path)
            self.assertIn("123", keys)
            self.assertIn("456", keys)
            self.assertEqual(len(keys), 2)



class TestClientSearchWorkflow(unittest.TestCase):
    @patch.object(BureauWbtvClient, "initialize_session")
    @patch.object(BureauWbtvClient, "_post_with_retry")
    def test_client_search_language_success(
        self, mock_post: MagicMock, mock_init: MagicMock
    ) -> None:
        client = BureauWbtvClient()
        client.session_state = SessionState(
            csrf_token="csrf",
            search_helper_guid="1001",
            helper_guid="2002",
            session_owr_guid="3003",
            user_guid="4004",
            helper_obj={"guid": "2002"},
            session_owr_obj={"guid": "3003"},
            session_user_hash="h1",
            helper_sh_hash="h2",
        )

        # Mock the search microflow response
        mock_post.side_effect = [
            {},  # retrieve_by_ids
            {},  # set target language
            {},  # trigger target language
            {    # search microflow
                "partialObjects": [{"guid": "rec1"}],
                "resultGuids": ["rec1"],
                "hasMoreItems": False,
                "changes": {
                    "1001": {"NumberOfResults": {"value": "1"}}
                },
            },
        ]

        target = Language(guid="46161896180548020", name="Arabic Egypt")
        result = client.search_language(target)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.count, 1)
        self.assertEqual(result.language_name, "Arabic Egypt")
        self.assertIsNotNone(result.records)
        assert result.records is not None
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]["role"], "Tolk")
        self.assertEqual(result.records[0]["level"], "c1")

    @patch.object(BureauWbtvClient, "fetch_profile_details")
    def test_enrich_records_with_details_concurrent(self, mock_fetch: MagicMock) -> None:
        """Verifies that enrich_records_with_details fetches all profiles concurrently and attaches details."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_cache = Path(tmp_dir) / "test_cache.json"
            client = BureauWbtvClient(cache_path=temp_cache)

            def fake_fetch(guid: str) -> dict[str, str]:
                res = {"Email": f"{guid}@example.com", "Registrations": "Dutch -> Arabic"}
                with client._cache_lock:
                    client._details_cache[guid] = res
                return res

            mock_fetch.side_effect = fake_fetch
            records: list[dict[str, Any]] = [{"guid": f"guid_test_{i}"} for i in range(10)]

            client.enrich_records_with_details(records, max_workers=4)

            self.assertEqual(mock_fetch.call_count, 10)
            for r in records:
                self.assertIn("details", r)
                details = r["details"]
                self.assertIsInstance(details, dict)
                self.assertEqual(details["Email"], f"{r['guid']}@example.com")

    @patch.object(BureauWbtvClient, "initialize_session")
    @patch.object(BureauWbtvClient, "_post_with_retry")
    def test_client_search_handles_error_explicitly(
        self, mock_post: MagicMock, mock_init: MagicMock
    ) -> None:
        client = BureauWbtvClient()
        client.session_state = SessionState(
            csrf_token="csrf",
            search_helper_guid="1001",
            helper_guid="2002",
            session_owr_guid="3003",
            user_guid="4004",
            helper_obj={"guid": "2002"},
            session_owr_obj={"guid": "3003"},
            session_user_hash="h1",
            helper_sh_hash="h2",
        )

        mock_post.side_effect = RuntimeError("504 Gateway Timeout")

        target = Language(guid="999", name="TimeoutLang")
        result = client.search_language(target)

        self.assertEqual(result.status, "error")
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertIn("504 Gateway Timeout", result.error)
        self.assertIsNone(result.count)
        self.assertIsNone(result.records)


class TestLoggingSetup(unittest.TestCase):
    def test_setup_logging_creates_and_writes_to_file(self) -> None:
        import logging

        with tempfile.TemporaryDirectory() as tmp_dir:
            log_file = Path(tmp_dir) / "test_run.log"
            setup_logging(log_file=log_file, verbose=True)

            logger = logging.getLogger("language_search")
            logger.info("Test log entry for unit test")

            # Flush and close all handlers before TemporaryDirectory exits on Windows
            for handler in list(logging.getLogger().handlers):
                handler.flush()
                handler.close()
                logging.getLogger().removeHandler(handler)

            self.assertTrue(log_file.is_file())
            content = log_file.read_text(encoding="utf-8")
            self.assertIn("Test log entry for unit test", content)
            self.assertIn("[INFO]", content)

    def test_monitor_setup_logging_creates_and_writes_to_file(self) -> None:
        import logging
        from monitor import setup_logging as monitor_setup_logging

        with tempfile.TemporaryDirectory() as tmp_dir:
            log_file = Path(tmp_dir) / "test_monitor.log"
            monitor_setup_logging(log_file=log_file, verbose=True)

            logger = logging.getLogger("monitor")
            logger.info("Test monitor log message")

            for handler in list(logging.getLogger().handlers):
                handler.flush()
                handler.close()
                logging.getLogger().removeHandler(handler)

            self.assertTrue(log_file.is_file())
            content = log_file.read_text(encoding="utf-8")
            self.assertIn("Test monitor log message", content)
            self.assertIn("[INFO]", content)

    def test_monitor_format_duration(self) -> None:
        from monitor import format_duration

        self.assertEqual(format_duration(5.25), "5.25s")
        self.assertEqual(format_duration(75.5), "1m 15.5s (75.50s total)")
        self.assertEqual(format_duration(3665), "1h 1m 5s")

    def test_monitor_format_alert_message_threshold(self) -> None:
        from monitor import format_alert_message

        def make_dummy(i: int) -> tuple[str, dict[str, Any]]:
            return (
                f"Lang{i}",
                {
                    "guid": f"guid_{i}",
                    "role": "Tolk",
                    "level": "c1",
                    "attributes": {
                        "Voorletters": {"value": f"A{i}."},
                        "Achternaam": {"value": f"Person{i}"},
                        "Klantnummer": {"value": f"100{i}"},
                        "OpenWebregister.AdresgegevensWoonadres_VertalerTolk": {
                            "attributes": {"Plaats": {"value": "Amsterdam"}}
                        },
                    },
                    "details": {
                        "Email": f"person{i}@test.com",
                        "Telephone 1": f"06123456{i}",
                    },
                },
            )

        # 1. Test <= 5 items: full contact details present
        items_3 = [make_dummy(i) for i in range(1, 4)]
        msg_3 = format_alert_message(items_3, elapsed_seconds=2.5)
        self.assertIn("Email: person1@test.com", msg_3)
        self.assertIn("Phone: 061234561", msg_3)
        self.assertIn("City: Amsterdam", msg_3)
        self.assertIn("Detected *3* new registration(s)", msg_3)
        self.assertNotIn("Summary Overview", msg_3)


        # 2. Test > 5 items: summary view instead of individual full contact blocks
        items_7 = [make_dummy(i) for i in range(1, 8)]
        msg_7 = format_alert_message(items_7, elapsed_seconds=4.2)
        self.assertIn("Summary Overview", msg_7)
        self.assertIn("Breakdown by Language", msg_7)
        self.assertIn("Registered Professionals", msg_7)
        # Should NOT have the multi-line contact blocks (Email, City, etc.)
        self.assertNotIn("City: Amsterdam", msg_7)
        self.assertNotIn("Email: person1@test.com", msg_7)
        self.assertIn("all_languages_results.xlsx", msg_7)

    def test_monitor_load_env_file(self) -> None:
        import os
        from monitor import load_env_file

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_file = Path(tmp_dir) / ".env"
            env_file.write_text(
                "# Sample comment\n"
                "TEST_KEY=hello_world\n"
                "TEST_QUOTED=\"spaced value\"\n",
                encoding="utf-8",
            )
            parsed = load_env_file(env_file)
            self.assertEqual(parsed.get("TEST_KEY"), "hello_world")
            self.assertEqual(parsed.get("TEST_QUOTED"), "spaced value")
            self.assertEqual(os.environ.get("TEST_KEY"), "hello_world")

            # Non-existent file returns empty dict
            empty = load_env_file(Path(tmp_dir) / "does_not_exist.env")
            self.assertEqual(empty, {})

    def test_notification_format_email_alert(self) -> None:
        from notification import format_email_alert

        def make_dummy(i: int) -> tuple[str, dict[str, Any]]:
            return (
                f"Lang{i}",
                {
                    "guid": f"guid_{i}",
                    "role": "Tolk",
                    "level": "c1",
                    "attributes": {
                        "Voorletters": {"value": f"A{i}."},
                        "Achternaam": {"value": f"Person{i}"},
                        "Klantnummer": {"value": f"100{i}"},
                        "OpenWebregister.AdresgegevensWoonadres_VertalerTolk": {
                            "attributes": {"Plaats": {"value": "Amsterdam"}}
                        },
                    },
                    "details": {
                        "Email": f"person{i}@test.com",
                        "Telephone 1": f"06123456{i}",
                    },
                },
            )

        # 1. Single record
        items_1 = [make_dummy(1)]
        sub_1, text_1, html_1 = format_email_alert(items_1, elapsed_seconds=1.5)
        self.assertIn("New Registration:", sub_1)
        self.assertIn("person1@test.com", text_1)
        self.assertIn("mailto:person1@test.com", html_1)
        self.assertIn("Amsterdam", html_1)

        # 2. Batch records (> 5)
        items_8 = [make_dummy(i) for i in range(1, 9)]
        sub_8, text_8, html_8 = format_email_alert(items_8, elapsed_seconds=3.2)
        self.assertIn("8 New Registration(s) Detected", sub_8)
        self.assertIn("Breakdown by Language", text_8)
        self.assertIn("Breakdown by Language", html_8)
        self.assertIn("all_languages_results.xlsx", text_8)
        self.assertIn("all_languages_results.xlsx", html_8)

    @patch("smtplib.SMTP_SSL")
    def test_notification_send_email_mocked(self, mock_smtp_ssl: MagicMock) -> None:
        from notification import send_email_notification

        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server

        # Successful send
        success = send_email_notification(
            subject="Test Subject",
            body="Test Body",
            html_body="<p>Test</p>",
            sender="sender@example.com",
            password="secret_pass",
            recipients=["recipient@example.com"],
        )
        self.assertTrue(success)
        mock_server.login.assert_called_once_with("sender@example.com", "secret_pass")
        mock_server.send_message.assert_called_once()

        # Missing credentials returns False without connecting
        mock_server.reset_mock()
        failed = send_email_notification(
            subject="Test",
            body="Test",
            sender="",
            password="",
            recipients="",
        )
        self.assertFalse(failed)
        mock_server.login.assert_not_called()


class TestGuidOnlyKeyBehaviour(unittest.TestCase):
    """Tests for the GUID-only tracking key introduced to avoid per-language alerts."""

    # ------------------------------------------------------------------ #
    # make_record_key                                                       #
    # ------------------------------------------------------------------ #

    def test_make_record_key_returns_guid_only(self) -> None:
        from monitor import make_record_key

        record = {"guid": "12345", "role": "Tolk"}
        key = make_record_key(record, "Arabic")
        self.assertEqual(key, "12345")

    def test_make_record_key_strips_whitespace(self) -> None:
        from monitor import make_record_key

        record = {"guid": "  99999  ", "role": "Vertaler"}
        key = make_record_key(record, "French")
        self.assertEqual(key, "99999")

    def test_make_record_key_missing_guid_returns_empty(self) -> None:
        from monitor import make_record_key

        record = {"role": "Tolk"}
        key = make_record_key(record, "Spanish")
        self.assertEqual(key, "")

    # ------------------------------------------------------------------ #
    # extract_known_keys_from_excel                                         #
    # ------------------------------------------------------------------ #

    def test_extract_known_keys_from_excel_guid_only(self) -> None:
        """extract_known_keys_from_excel now returns plain GUIDs, not compound keys."""
        import pandas as pd

        with tempfile.TemporaryDirectory() as tmp_dir:
            excel_path = Path(tmp_dir) / "test.xlsx"
            df = pd.DataFrame([
                {"GUID": "AAA", "Role": "Tolk",     "Language": "Arabic"},
                {"GUID": "AAA", "Role": "Tolk",     "Language": "French"},  # same person, second lang
                {"GUID": "BBB", "Role": "Vertaler", "Language": "German"},
            ])
            df.to_excel(excel_path, index=False)

            keys = extract_known_keys_from_excel(excel_path)

        # Should only contain the two unique GUIDs, NOT compound strings
        self.assertEqual(keys, {"AAA", "BBB"})
        self.assertNotIn("AAA:Tolk:Arabic", keys)
        self.assertNotIn("AAA:Tolk:French", keys)

    def test_extract_known_keys_from_excel_missing_file_returns_empty(self) -> None:
        from pathlib import Path
        keys = extract_known_keys_from_excel(Path("does_not_exist.xlsx"))
        self.assertEqual(keys, set())

    # ------------------------------------------------------------------ #
    # extract_known_guids_from_json                                         #
    # ------------------------------------------------------------------ #

    def test_extract_known_guids_from_json(self) -> None:
        """Reads GUIDs from a results JSON file across multiple languages."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "results.json"
            data = {
                "results": {
                    "Arabic": {
                        "records": [
                            {"guid": "GUID_1", "role": "Tolk"},
                            {"guid": "GUID_2", "role": "Tolk"},
                        ]
                    },
                    "French": {
                        "records": [
                            {"guid": "GUID_1", "role": "Tolk"},   # same person — should deduplicate
                            {"guid": "GUID_3", "role": "Vertaler"},
                        ]
                    },
                }
            }
            json_path.write_text(json.dumps(data), encoding="utf-8")

            guids = extract_known_guids_from_json(json_path)

        self.assertEqual(guids, {"GUID_1", "GUID_2", "GUID_3"})

    def test_extract_known_guids_from_json_missing_file_returns_empty(self) -> None:
        guids = extract_known_guids_from_json(Path("does_not_exist.json"))
        self.assertEqual(guids, set())

    def test_extract_known_guids_from_json_malformed_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            bad_path = Path(tmp_dir) / "bad.json"
            bad_path.write_text("{ not valid json", encoding="utf-8")
            guids = extract_known_guids_from_json(bad_path)
        self.assertEqual(guids, set())

    # ------------------------------------------------------------------ #
    # sync_and_load_known_records — auto-migration                         #
    # ------------------------------------------------------------------ #

    def test_sync_migrates_old_compound_keys_to_guids(self) -> None:
        """Old 'guid:role:lang' entries in known_records.json are auto-migrated."""
        from monitor import sync_and_load_known_records

        with tempfile.TemporaryDirectory() as tmp_dir:
            known_path = Path(tmp_dir) / "known_records.json"
            # Old format: compound keys
            old_keys = [
                "GUID_A:Tolk:Arabic",
                "GUID_A:Tolk:French",   # same person, second language
                "GUID_B:Vertaler:German",
            ]
            known_path.write_text(json.dumps(old_keys), encoding="utf-8")

            result = sync_and_load_known_records(known_path=known_path)

            # Should deduplicate and strip to plain GUIDs
            self.assertEqual(result, {"GUID_A", "GUID_B"})

            # known_records.json must be rewritten in new plain-GUID format
            saved = json.loads(known_path.read_text(encoding="utf-8"))
            self.assertCountEqual(saved, ["GUID_A", "GUID_B"])

    def test_sync_first_run_seeds_from_excel_when_known_empty(self) -> None:
        """If Excel has data and known_records has not, update known_records from Excel."""
        import pandas as pd
        from monitor import sync_and_load_known_records

        with tempfile.TemporaryDirectory() as tmp_dir:
            known_path = Path(tmp_dir) / "known_records.json"  # does NOT exist yet
            excel_path = Path(tmp_dir) / "results.xlsx"

            # Excel has GUID_1 and GUID_2
            df = pd.DataFrame([
                {"GUID": "GUID_1", "Role": "Tolk",     "Language": "Arabic"},
                {"GUID": "GUID_2", "Role": "Vertaler", "Language": "German"},
            ])
            df.to_excel(excel_path, index=False)

            result = sync_and_load_known_records(
                known_path=known_path,
                excel_path=excel_path,
            )

            # Seeds from Excel and writes to known_records.json
            self.assertEqual(result, {"GUID_1", "GUID_2"})
            self.assertTrue(known_path.is_file())
            saved = json.loads(known_path.read_text(encoding="utf-8"))
            self.assertCountEqual(saved, ["GUID_1", "GUID_2"])

    def test_sync_first_run_seeds_from_json_when_both_known_and_excel_missing(self) -> None:
        """If neither known_records nor Excel exists, seed baseline from JSON."""
        from monitor import sync_and_load_known_records

        with tempfile.TemporaryDirectory() as tmp_dir:
            known_path = Path(tmp_dir) / "known_records.json"
            json_path  = Path(tmp_dir) / "results.json"

            json_data = {
                "results": {
                    "Spanish": {"records": [{"guid": "GUID_JSON_1", "role": "Tolk"}]},
                }
            }
            json_path.write_text(json.dumps(json_data), encoding="utf-8")

            result = sync_and_load_known_records(
                known_path=known_path,
                json_path=json_path,
            )

            self.assertEqual(result, {"GUID_JSON_1"})
            self.assertTrue(known_path.is_file())

    def test_sync_intersection_detects_deletion_from_known_records(self) -> None:
        """Deleting a GUID from known_records.json excludes it so it gets re-scraped as NEW."""
        import pandas as pd
        from monitor import sync_and_load_known_records

        with tempfile.TemporaryDirectory() as tmp_dir:
            known_path = Path(tmp_dir) / "known_records.json"
            excel_path = Path(tmp_dir) / "results.xlsx"

            # Excel has GUID_1 and GUID_2
            df = pd.DataFrame([
                {"GUID": "GUID_1", "Role": "Tolk", "Language": "Arabic"},
                {"GUID": "GUID_2", "Role": "Tolk", "Language": "English"},
            ])
            df.to_excel(excel_path, index=False)

            # User deleted GUID_2 from known_records.json (only GUID_1 remains)
            known_path.write_text(json.dumps(["GUID_1"]), encoding="utf-8")

            result = sync_and_load_known_records(known_path=known_path, excel_path=excel_path)

            # GUID_2 is excluded from active keys -> will be scraped as NEW!
            self.assertEqual(result, {"GUID_1"})
            self.assertNotIn("GUID_2", result)

    def test_sync_intersection_detects_deletion_from_excel(self) -> None:
        """Deleting a row from Excel excludes it so it gets re-scraped as NEW."""
        import pandas as pd
        from monitor import sync_and_load_known_records

        with tempfile.TemporaryDirectory() as tmp_dir:
            known_path = Path(tmp_dir) / "known_records.json"
            excel_path = Path(tmp_dir) / "results.xlsx"

            # known_records has GUID_1 and GUID_2
            known_path.write_text(json.dumps(["GUID_1", "GUID_2"]), encoding="utf-8")

            # User deleted GUID_2 row from Excel (only GUID_1 remains)
            df = pd.DataFrame([{"GUID": "GUID_1", "Role": "Tolk", "Language": "Arabic"}])
            df.to_excel(excel_path, index=False)

            result = sync_and_load_known_records(known_path=known_path, excel_path=excel_path)

            # GUID_2 is excluded from active keys -> will be scraped as NEW!
            self.assertEqual(result, {"GUID_1"})
            self.assertNotIn("GUID_2", result)

    # ------------------------------------------------------------------ #
    # New-person vs new-language detection logic                            #
    # ------------------------------------------------------------------ #

    def test_new_language_for_known_person_not_flagged(self) -> None:
        """An existing person adding a new language should NOT produce a new key."""
        from monitor import make_record_key

        known_guids = {"GUID_A"}

        # Simulate the same person appearing with a new language
        record_new_lang = {"guid": "GUID_A", "role": "Tolk"}
        key = make_record_key(record_new_lang, "NewLanguage")
        self.assertIn(key, known_guids)   # should be silently skipped

    def test_brand_new_person_is_flagged(self) -> None:
        """A record with a GUID not in known_guids should be treated as new."""
        from monitor import make_record_key

        known_guids = {"GUID_A"}

        record_new_person = {"guid": "GUID_B", "role": "Tolk"}
        key = make_record_key(record_new_person, "Arabic")
        self.assertNotIn(key, known_guids)


if __name__ == "__main__":
    unittest.main()


