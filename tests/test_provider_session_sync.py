from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "codex-provider-session-sync" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import provider_session_sync as sync
import service_manager


def create_database(path: Path, providers=("OpenAI", "openai")) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                model_provider TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.executemany(
            "INSERT INTO threads(id, model_provider, title) VALUES (?, ?, ?)",
            [
                ("thread-{}".format(index), provider, "Title {}".format(index))
                for index, provider in enumerate(providers)
            ],
        )
        connection.commit()
    finally:
        connection.close()


def session_bytes(provider: str, message: str = "Keep this message unchanged.") -> bytes:
    metadata = {
        "timestamp": "2026-06-12T00:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": "thread-1",
            "model_provider": provider,
            "base_instructions": {
                "text": 'The phrase "model_provider" may occur in conversation text.'
            },
        },
    }
    message_record = {
        "timestamp": "2026-06-12T00:00:01Z",
        "type": "response_item",
        "payload": {"role": "user", "content": message},
    }
    return (
        json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        + b"\n"
        + json.dumps(message_record, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


class CodexFixture:
    def __init__(self, root: Path, configured: str, stored: str):
        self.root = root
        self.root.mkdir(parents=True)
        (self.root / "config.toml").write_text(
            'model_provider = "{}"\n\n[model_providers.{}]\nname = "{}"\n'.format(
                configured, configured, configured
            ),
            encoding="utf-8",
        )
        create_database(self.root / "state_5.sqlite", (stored, stored))
        active = self.root / "sessions" / "2026" / "06" / "12"
        active.mkdir(parents=True)
        self.active_session = active / "active.jsonl"
        self.active_session.write_bytes(session_bytes(stored))
        archived = self.root / "archived_sessions"
        archived.mkdir()
        self.archived_session = archived / "archived.jsonl"
        self.archived_session.write_bytes(session_bytes(stored, "Archived body"))


class ProviderSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / ".codex"

    def fixture(self, configured="custom", stored="OpenAI") -> CodexFixture:
        return CodexFixture(self.root, configured, stored)

    def test_provider_migrations(self):
        cases = (
            ("openai", "OpenAI"),
            ("OpenAI", "openai"),
            ("custom", "OpenAI"),
            ("provider-with-dashes", "custom"),
        )
        for case_number, (configured, stored) in enumerate(cases):
            with self.subTest(configured=configured, stored=stored):
                case_root = self.root.parent / "case-{}".format(case_number)
                fixture = CodexFixture(case_root, configured, stored)
                result = sync.sync_once(case_root)
                self.assertEqual(result.provider, configured)
                self.assertEqual(result.database_rows_changed, 2)
                self.assertEqual(result.session_files_changed, 2)
                connection = sqlite3.connect(str(case_root / "state_5.sqlite"))
                try:
                    providers = {
                        row[0]
                        for row in connection.execute(
                            "SELECT model_provider FROM threads"
                        )
                    }
                finally:
                    connection.close()
                self.assertEqual(providers, {configured})

    def test_only_session_provider_bytes_change(self):
        fixture = self.fixture()
        before = fixture.active_session.read_bytes()
        result = sync.sync_once(self.root)
        after = fixture.active_session.read_bytes()
        self.assertEqual(result.session_files_changed, 2)
        expected = before.replace(
            b'"model_provider":"OpenAI"',
            b'"model_provider":"custom"',
            1,
        )
        self.assertEqual(after, expected)
        self.assertIn(b"Keep this message unchanged.", after)

    def test_backup_contains_database_sessions_and_manifest(self):
        fixture = self.fixture()
        result = sync.sync_once(self.root)
        backup = Path(result.backup_directory)
        self.assertTrue((backup / "state_5.sqlite").is_file())
        self.assertEqual(
            (backup / fixture.active_session.relative_to(self.root)).read_bytes(),
            session_bytes("OpenAI"),
        )
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["target_provider"], "custom")
        self.assertEqual(len(manifest["session_files"]), 2)

    def test_sync_is_idempotent(self):
        self.fixture()
        first = sync.sync_once(self.root)
        second = sync.sync_once(self.root)
        self.assertIsNotNone(first.backup_directory)
        self.assertEqual(second.database_rows_changed, 0)
        self.assertEqual(second.session_files_changed, 0)
        self.assertIsNone(second.backup_directory)

    def test_insert_trigger_assigns_current_provider(self):
        self.fixture()
        sync.sync_once(self.root)
        connection = sqlite3.connect(str(self.root / "state_5.sqlite"))
        try:
            connection.execute(
                "INSERT INTO threads(id, model_provider, title) VALUES (?, ?, ?)",
                ("new-thread", "different", "New"),
            )
            connection.commit()
            provider = connection.execute(
                "SELECT model_provider FROM threads WHERE id = 'new-thread'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(provider, "custom")

    def test_reinstalls_state_and_trigger_after_reindex(self):
        self.fixture()
        sync.sync_once(self.root)
        database = self.root / "state_5.sqlite"
        connection = sqlite3.connect(str(database))
        try:
            connection.execute(
                "DROP TRIGGER provider_session_sync_after_thread_insert"
            )
            connection.execute("DROP TABLE provider_session_sync_state")
            connection.execute("UPDATE threads SET model_provider = 'OpenAI'")
            connection.commit()
        finally:
            connection.close()
        result = sync.sync_once(self.root)
        self.assertEqual(result.database_rows_changed, 2)
        state = sync.status(self.root)
        self.assertTrue(state["trigger_installed"])
        self.assertEqual(state["providers"], {"custom": 2})

    def test_malformed_session_meta_fails_before_database_change(self):
        fixture = self.fixture()
        fixture.active_session.write_bytes(
            b'{"type":"session_meta","payload":{"model_provider":"OpenAI"}\n'
        )
        with self.assertRaises(sync.CompatibilityError):
            sync.sync_once(self.root)
        connection = sqlite3.connect(str(self.root / "state_5.sqlite"))
        try:
            providers = {
                row[0]
                for row in connection.execute("SELECT model_provider FROM threads")
            }
        finally:
            connection.close()
        self.assertEqual(providers, {"OpenAI"})

    def test_unknown_database_schema_fails_closed(self):
        self.root.mkdir()
        (self.root / "config.toml").write_text(
            'model_provider = "custom"\n', encoding="utf-8"
        )
        connection = sqlite3.connect(str(self.root / "state_9.sqlite"))
        connection.execute("CREATE TABLE something_else(value TEXT)")
        connection.commit()
        connection.close()
        with self.assertRaises(sync.CompatibilityError):
            sync.sync_once(self.root)

    def test_does_not_fall_back_to_an_older_database(self):
        self.root.mkdir()
        (self.root / "config.toml").write_text(
            'model_provider = "custom"\n', encoding="utf-8"
        )
        create_database(self.root / "state_5.sqlite")
        connection = sqlite3.connect(str(self.root / "state_6.sqlite"))
        connection.execute("CREATE TABLE future_schema(value TEXT)")
        connection.commit()
        connection.close()
        with self.assertRaises(sync.CompatibilityError):
            sync.discover_state_database(self.root)

    def test_missing_configuration_fails(self):
        self.root.mkdir()
        create_database(self.root / "state_5.sqlite")
        with self.assertRaises(sync.SyncError):
            sync.sync_once(self.root)

    def test_locked_database_reports_failure(self):
        self.fixture()
        database = self.root / "state_5.sqlite"
        blocker = sqlite3.connect(str(database))
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            with self.assertRaises(sync.SyncError):
                sync.update_database(database, "custom", timeout=0.01, retries=0)
        finally:
            blocker.rollback()
            blocker.close()

    def test_concurrent_session_write_is_detected(self):
        fixture = self.fixture()
        patch = sync.prepare_session_patch(fixture.active_session, "custom")
        fixture.active_session.write_bytes(
            fixture.active_session.read_bytes() + b'{"new":"record"}\n'
        )
        with self.assertRaises(sync.ConcurrentWriteError):
            sync.apply_session_patch(patch)

    def test_status_is_read_only(self):
        self.fixture()
        state = sync.status(self.root)
        self.assertEqual(state["configured_provider"], "custom")
        self.assertEqual(state["providers"], {"OpenAI": 2})
        self.assertFalse(state["trigger_installed"])


class ServiceDefinitionTests(unittest.TestCase):
    def test_windows_task_restarts_on_failure(self):
        xml = service_manager._windows_task_xml(
            Path("C:/Users/Test/.codex"),
            Path("C:/Users/Test/provider_session_sync.py"),
        )
        self.assertIn("<RestartOnFailure>", xml)
        self.assertEqual(
            service_manager.WINDOWS_TASK, "Codex Provider Session Sync"
        )

    def test_systemd_quoting(self):
        self.assertEqual(
            service_manager._systemd_quote('/tmp/a "quoted" path'),
            '"/tmp/a \\"quoted\\" path"',
        )


if __name__ == "__main__":
    unittest.main()
