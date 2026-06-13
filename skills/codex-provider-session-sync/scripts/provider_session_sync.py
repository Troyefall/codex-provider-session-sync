#!/usr/bin/env python3
"""Keep Codex thread metadata aligned with the configured model provider."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


APP_NAME = "codex-provider-session-sync"
STATE_TABLE = "provider_session_sync_state"
LEGACY_TRIGGERS = (
    "provider_session_sync_after_thread_insert",
    "sync_thread_provider_after_insert",
    "sync_thread_provider_after_provider_update",
)
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_RECONCILE_SECONDS = 30.0
LOCK_STALE_SECONDS = 300.0
PROVIDER_LINE = re.compile(r"^\s*model_provider\s*=\s*(.*?)\s*(?:#.*)?$")
SESSION_TYPE = re.compile(rb'"type"\s*:\s*"session_meta"')
PROVIDER_FIELD = re.compile(
    rb'("model_provider"\s*:\s*")((?:\\.|[^"\\])*)(")'
)
STATE_DB_NAME = re.compile(r"^state_(\d+)\.sqlite$")


class SyncError(RuntimeError):
    """Base exception for synchronization failures."""


class CompatibilityError(SyncError):
    """Raised when Codex storage does not match the supported schema."""


class ConcurrentWriteError(SyncError):
    """Raised when a session file changes during synchronization."""


@dataclass(frozen=True)
class FilePatch:
    path: Path
    original: bytes
    updated: bytes
    size: int
    mtime_ns: int


@dataclass
class SyncResult:
    provider: str
    provider_source: str
    database: str
    database_rows_changed: int
    session_files_changed: int
    backup_directory: Optional[str]
    legacy_triggers_removed: int

    def as_dict(self) -> Dict[str, object]:
        return {
            "provider": self.provider,
            "provider_source": self.provider_source,
            "database": self.database,
            "database_rows_changed": self.database_rows_changed,
            "session_files_changed": self.session_files_changed,
            "backup_directory": self.backup_directory,
            "legacy_triggers_removed": self.legacy_triggers_removed,
        }


def codex_home_from_env() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def runtime_root(codex_home: Path) -> Path:
    return codex_home / APP_NAME


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def log_message(codex_home: Path, message: str) -> None:
    log_dir = runtime_root(codex_home) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
    with (log_dir / "watcher.log").open("a", encoding="utf-8") as handle:
        handle.write("{} {}\n".format(timestamp, message))


def _parse_toml_string(value: str) -> str:
    value = value.strip()
    if not value:
        raise SyncError("model_provider is empty in config.toml")
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SyncError("model_provider is not a valid TOML basic string") from exc
        if not isinstance(parsed, str):
            raise SyncError("model_provider must be a string")
        return parsed
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1]
    return value.split()[0]


def read_configured_provider(codex_home: Path) -> Optional[str]:
    config_path = codex_home / "config.toml"
    if not config_path.is_file():
        return None

    with config_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            if raw_line.lstrip().startswith("["):
                break
            match = PROVIDER_LINE.match(raw_line)
            if match:
                provider = _parse_toml_string(match.group(1))
                if not provider:
                    raise SyncError("model_provider is empty in config.toml")
                return provider
    return None


def _database_sort_key(path: Path) -> Tuple[int, int]:
    match = STATE_DB_NAME.match(path.name)
    version = int(match.group(1)) if match else -1
    return version, path.stat().st_mtime_ns


def _table_columns(connection: sqlite3.Connection, table: str) -> List[str]:
    return [row[1] for row in connection.execute("PRAGMA table_info({})".format(table))]


def validate_database(connection: sqlite3.Connection, path: Path) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "threads" not in tables:
        raise CompatibilityError("{} has no threads table".format(path))
    columns = set(_table_columns(connection, "threads"))
    required = {"id", "model_provider"}
    missing = sorted(required - columns)
    if missing:
        raise CompatibilityError(
            "{} is missing required thread columns: {}".format(
                path, ", ".join(missing)
            )
        )


def discover_state_database(codex_home: Path) -> Path:
    path = None
    searched = []
    for root in (codex_home / "sqlite", codex_home):
        searched.append(str(root))
        candidates = sorted(
            root.glob("state_*.sqlite"), key=_database_sort_key, reverse=True
        )
        if candidates:
            path = candidates[0]
            break
    if path is None:
        raise CompatibilityError(
            "No Codex state_*.sqlite database was found in {}".format(
                " or ".join(searched)
            )
        )
    try:
        connection = sqlite3.connect(
            "file:{}?mode=ro".format(path.as_posix()), uri=True
        )
        try:
            validate_database(connection, path)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise CompatibilityError(
            "The newest Codex state database could not be read: {}".format(exc)
        ) from exc
    return path


def iter_session_files(codex_home: Path) -> Iterator[Path]:
    for directory_name in ("sessions", "archived_sessions"):
        root = codex_home / directory_name
        if root.is_dir():
            yield from sorted(root.rglob("*.jsonl"))


def _encoded_json_string(value: str) -> bytes:
    return json.dumps(value, ensure_ascii=True)[1:-1].encode("ascii")


def prepare_session_patch(path: Path, provider: str) -> Optional[FilePatch]:
    before = path.stat()
    original = path.read_bytes()
    after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise ConcurrentWriteError("{} changed while it was being read".format(path))

    lines = original.splitlines(keepends=True)
    session_meta_index = None
    for index, line in enumerate(lines):
        if SESSION_TYPE.search(line):
            session_meta_index = index
            try:
                payload = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CompatibilityError(
                    "{} contains malformed session_meta JSON".format(path)
                ) from exc
            if payload.get("type") != "session_meta":
                continue
            metadata = payload.get("payload")
            if not isinstance(metadata, dict) or "model_provider" not in metadata:
                raise CompatibilityError(
                    "{} session_meta has no payload.model_provider".format(path)
                )
            break

    if session_meta_index is None:
        raise CompatibilityError("{} has no session_meta record".format(path))

    line = lines[session_meta_index]
    replacement = _encoded_json_string(provider)
    match = PROVIDER_FIELD.search(line)
    if match is None:
        raise CompatibilityError(
            "{} session_meta has an unsupported model_provider layout".format(path)
        )
    current_provider = metadata.get("model_provider")
    if not isinstance(current_provider, str):
        raise CompatibilityError(
            "{} session_meta model_provider is not a string".format(path)
        )
    if match.group(2) != _encoded_json_string(current_provider):
        raise CompatibilityError(
            "{} session_meta provider could not be located safely".format(path)
        )
    updated_line = (
        line[: match.start(2)] + replacement + line[match.end(2) :]
    )
    if updated_line == line:
        return None
    lines[session_meta_index] = updated_line
    return FilePatch(
        path=path,
        original=original,
        updated=b"".join(lines),
        size=before.st_size,
        mtime_ns=before.st_mtime_ns,
    )


def prepare_session_patches(codex_home: Path, provider: str) -> List[FilePatch]:
    return [
        patch
        for patch in (
            prepare_session_patch(path, provider)
            for path in iter_session_files(codex_home)
        )
        if patch is not None
    ]


def _legacy_triggers(connection: sqlite3.Connection) -> List[str]:
    placeholders = ",".join("?" for _ in LEGACY_TRIGGERS)
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'trigger' AND name IN ({})".format(placeholders),
            LEGACY_TRIGGERS,
        )
    ]


def _state_table_exists(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (STATE_TABLE,),
        ).fetchone()
        is not None
    )


def inspect_database(
    database_path: Path, provider: str, timeout: float
) -> Tuple[int, List[str], bool]:
    connection = sqlite3.connect(str(database_path), timeout=timeout)
    try:
        validate_database(connection, database_path)
        rows = connection.execute(
            "SELECT COUNT(*) FROM threads WHERE model_provider <> ?", (provider,)
        ).fetchone()[0]
        return int(rows), _legacy_triggers(connection), _state_table_exists(connection)
    finally:
        connection.close()


def read_saved_provider(codex_home: Path) -> Optional[str]:
    state_path = runtime_root(codex_home) / "state.json"
    if not state_path.is_file():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    provider = state.get("provider")
    return provider if isinstance(provider, str) and provider else None


def _newest_database_provider(
    database_path: Path,
    different_from: Optional[str],
    timeout: float,
) -> Optional[str]:
    connection = sqlite3.connect(str(database_path), timeout=timeout)
    try:
        validate_database(connection, database_path)
        columns = set(_table_columns(connection, "threads"))
        filters = []
        parameters: List[object] = []
        if "archived" in columns:
            filters.append("archived = 0")
        if "thread_source" in columns:
            filters.append("thread_source = 'user'")
        elif "has_user_event" in columns:
            filters.append("has_user_event = 1")
        if different_from is not None:
            filters.append("model_provider <> ?")
            parameters.append(different_from)

        order_values = []
        for name in ("updated_at_ms", "updated_at", "created_at_ms", "created_at"):
            if name not in columns:
                continue
            multiplier = " * 1000" if not name.endswith("_ms") else ""
            order_values.append("{}{}".format(name, multiplier))
        order_values.append("rowid")
        order_clause = "COALESCE({}) DESC".format(", ".join(order_values))

        query = "SELECT model_provider FROM threads"
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY {} LIMIT 1".format(order_clause)
        row = connection.execute(query, parameters).fetchone()
        if row is None and "thread_source" in columns:
            relaxed_filters = [
                item for item in filters if item != "thread_source = 'user'"
            ]
            query = "SELECT model_provider FROM threads"
            if relaxed_filters:
                query += " WHERE " + " AND ".join(relaxed_filters)
            query += " ORDER BY {} LIMIT 1".format(order_clause)
            row = connection.execute(query, parameters).fetchone()
        if row and isinstance(row[0], str) and row[0]:
            return row[0]
        return None
    finally:
        connection.close()


def read_session_provider(path: Path) -> str:
    for line in path.read_bytes().splitlines():
        if not SESSION_TYPE.search(line):
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CompatibilityError(
                "{} contains malformed session_meta JSON".format(path)
            ) from exc
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        provider = payload.get("model_provider") if isinstance(payload, dict) else None
        if not isinstance(provider, str) or not provider:
            raise CompatibilityError(
                "{} session_meta has no valid payload.model_provider".format(path)
            )
        return provider
    raise CompatibilityError("{} has no session_meta record".format(path))


def _newest_session_provider(
    codex_home: Path, different_from: Optional[str]
) -> Optional[str]:
    candidates = sorted(
        iter_session_files(codex_home),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for path in candidates:
        provider = read_session_provider(path)
        if different_from is None or provider != different_from:
            return provider
    return None


def resolve_provider(
    codex_home: Path, database_path: Path, timeout: float
) -> Tuple[str, str]:
    configured = read_configured_provider(codex_home)
    if configured:
        return configured, "config"

    saved = read_saved_provider(codex_home)
    if saved:
        database_signal = _newest_database_provider(
            database_path, different_from=saved, timeout=timeout
        )
        if database_signal:
            return database_signal, "new-user-thread"
        session_signal = _newest_session_provider(codex_home, different_from=saved)
        if session_signal:
            return session_signal, "new-session"
        return saved, "saved-state"

    database_provider = _newest_database_provider(
        database_path, different_from=None, timeout=timeout
    )
    if database_provider:
        return database_provider, "newest-user-thread"
    session_provider = _newest_session_provider(codex_home, different_from=None)
    if session_provider:
        return session_provider, "newest-session"
    raise SyncError("The active Codex provider could not be determined")


def backup_database(source: Path, destination: Path, timeout: float) -> None:
    source_connection = sqlite3.connect(str(source), timeout=timeout)
    destination_connection = sqlite3.connect(str(destination))
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def create_backup(
    codex_home: Path,
    database_path: Path,
    patches: Sequence[FilePatch],
    provider: str,
    timeout: float,
) -> Path:
    backup_dir = runtime_root(codex_home) / "backups" / utc_stamp()
    backup_dir.mkdir(parents=True, exist_ok=False)
    database_relative = database_path.relative_to(codex_home)
    database_backup = backup_dir / "database" / database_relative
    database_backup.parent.mkdir(parents=True, exist_ok=True)
    backup_database(database_path, database_backup, timeout)

    backed_up_files = []
    for patch in patches:
        relative = patch.path.relative_to(codex_home)
        destination = backup_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(patch.original)
        shutil.copystat(str(patch.path), str(destination), follow_symlinks=True)
        backed_up_files.append(relative.as_posix())

    manifest = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "target_provider": provider,
        "database": database_relative.as_posix(),
        "session_files": backed_up_files,
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return backup_dir


def _is_locked_error(error: sqlite3.OperationalError) -> bool:
    text = str(error).lower()
    return "locked" in text or "busy" in text


def update_database(
    database_path: Path,
    provider: str,
    timeout: float,
    retries: int,
) -> Tuple[int, int]:
    last_error = None
    for attempt in range(retries + 1):
        connection = sqlite3.connect(str(database_path), timeout=timeout)
        try:
            connection.execute("PRAGMA busy_timeout = {}".format(int(timeout * 1000)))
            validate_database(connection, database_path)
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE threads SET model_provider = ? WHERE model_provider <> ?",
                (provider, provider),
            )
            changed = cursor.rowcount
            legacy_triggers = _legacy_triggers(connection)
            for trigger in legacy_triggers:
                connection.execute('DROP TRIGGER IF EXISTS "{}"'.format(trigger))
            connection.execute("DROP TABLE IF EXISTS {}".format(STATE_TABLE))
            connection.commit()
            return int(changed), len(legacy_triggers)
        except sqlite3.OperationalError as exc:
            connection.rollback()
            last_error = exc
            if not _is_locked_error(exc) or attempt >= retries:
                raise SyncError(
                    "Could not update the Codex state database: {}".format(exc)
                ) from exc
            time.sleep(min(0.25 * (2**attempt), 2.0))
        finally:
            connection.close()
    raise SyncError("Could not update the Codex state database: {}".format(last_error))


def _stat_matches(patch: FilePatch) -> bool:
    current = patch.path.stat()
    return current.st_size == patch.size and current.st_mtime_ns == patch.mtime_ns


def apply_session_patch(patch: FilePatch) -> None:
    if not _stat_matches(patch):
        raise ConcurrentWriteError(
            "{} changed before it could be replaced".format(patch.path)
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + patch.path.name + ".",
        suffix=".tmp",
        dir=str(patch.path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(patch.updated)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copystat(str(patch.path), str(temporary_path), follow_symlinks=True)
        if not _stat_matches(patch):
            raise ConcurrentWriteError(
                "{} changed while its replacement was prepared".format(patch.path)
            )
        os.replace(str(temporary_path), str(patch.path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class SyncLock:
    def __init__(self, codex_home: Path):
        self.path = runtime_root(codex_home) / "sync.lock"
        self.acquired = False

    def __enter__(self) -> "SyncLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.mkdir()
        except FileExistsError:
            metadata_path = self.path / "owner.json"
            stale = True
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                age = time.time() - float(metadata.get("created_at", 0))
                stale = age > LOCK_STALE_SECONDS or not _pid_is_running(
                    int(metadata.get("pid", -1))
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                stale = True
            if stale:
                shutil.rmtree(self.path, ignore_errors=True)
                self.path.mkdir()
            else:
                raise SyncError("Another provider synchronization is already running")
        (self.path / "owner.json").write_text(
            json.dumps({"pid": os.getpid(), "created_at": time.time()}),
            encoding="utf-8",
        )
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.acquired:
            shutil.rmtree(self.path, ignore_errors=True)


def sync_once(
    codex_home: Path,
    timeout: float = 5.0,
    retries: int = 4,
) -> SyncResult:
    codex_home = codex_home.expanduser().resolve()
    with SyncLock(codex_home):
        database_path = discover_state_database(codex_home)
        provider, provider_source = resolve_provider(
            codex_home, database_path, timeout
        )
        database_rows, legacy_triggers, state_table_present = inspect_database(
            database_path, provider, timeout
        )
        patches = prepare_session_patches(codex_home, provider)
        needs_database_write = (
            database_rows > 0 or bool(legacy_triggers) or state_table_present
        )
        backup_dir = None
        if needs_database_write or patches:
            backup_dir = create_backup(
                codex_home, database_path, patches, provider, timeout
            )

        for patch in patches:
            apply_session_patch(patch)
        changed_rows, removed_triggers = (
            update_database(database_path, provider, timeout, retries)
            if needs_database_write
            else (0, 0)
        )

        result = SyncResult(
            provider=provider,
            provider_source=provider_source,
            database=str(database_path),
            database_rows_changed=changed_rows,
            session_files_changed=len(patches),
            backup_directory=str(backup_dir) if backup_dir else None,
            legacy_triggers_removed=removed_triggers,
        )
        state_path = runtime_root(codex_home) / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "last_success": dt.datetime.now(dt.timezone.utc).isoformat(),
                    **result.as_dict(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return result


def storage_fingerprint(codex_home: Path) -> Tuple[object, ...]:
    config = codex_home / "config.toml"
    database = discover_state_database(codex_home)
    config_stat = config.stat() if config.exists() else None
    database_stat = database.stat()
    session_counts = []
    for name in ("sessions", "archived_sessions"):
        root = codex_home / name
        files = list(root.rglob("*.jsonl")) if root.is_dir() else []
        newest = max((path.stat().st_mtime_ns for path in files), default=0)
        session_counts.extend((len(files), newest))
    return (
        config_stat.st_mtime_ns if config_stat else 0,
        config_stat.st_size if config_stat else 0,
        str(database),
        database_stat.st_mtime_ns,
        database_stat.st_size,
        *session_counts,
    )


def watch(
    codex_home: Path,
    poll_seconds: float,
    reconcile_seconds: float,
    once: bool = False,
) -> None:
    last_fingerprint = None
    last_reconcile = 0.0
    log_message(codex_home, "Watcher started")
    while True:
        try:
            fingerprint = storage_fingerprint(codex_home)
            now = time.monotonic()
            if (
                fingerprint != last_fingerprint
                or now - last_reconcile >= reconcile_seconds
            ):
                result = sync_once(codex_home)
                log_message(
                    codex_home,
                    "Synchronized provider={} database_rows={} session_files={}".format(
                        result.provider,
                        result.database_rows_changed,
                        result.session_files_changed,
                    ),
                )
                last_fingerprint = storage_fingerprint(codex_home)
                last_reconcile = now
        except Exception as exc:
            log_message(codex_home, "Synchronization failed: {}".format(exc))
        if once:
            return
        time.sleep(max(poll_seconds, 0.25))


def status(codex_home: Path) -> Dict[str, object]:
    result: Dict[str, object] = {
        "codex_home": str(codex_home),
        "configured_provider": None,
        "resolved_provider": None,
        "provider_source": None,
        "database": None,
        "providers": {},
        "legacy_triggers": [],
        "last_success": None,
        "service": {"installed": False, "running": False},
    }
    database = discover_state_database(codex_home)
    result["configured_provider"] = read_configured_provider(codex_home)
    resolved_provider, provider_source = resolve_provider(codex_home, database, 5.0)
    result["resolved_provider"] = resolved_provider
    result["provider_source"] = provider_source
    result["database"] = str(database)
    connection = sqlite3.connect(str(database))
    try:
        validate_database(connection, database)
        result["providers"] = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT model_provider, COUNT(*) FROM threads GROUP BY model_provider"
            )
        }
        result["legacy_triggers"] = _legacy_triggers(connection)
    finally:
        connection.close()
    state_path = runtime_root(codex_home) / "state.json"
    if state_path.is_file():
        try:
            result["last_success"] = json.loads(
                state_path.read_text(encoding="utf-8")
            ).get("last_success")
        except (OSError, json.JSONDecodeError):
            pass
    try:
        from service_manager import service_status

        result["service"] = service_status(codex_home)
    except (ImportError, OSError):
        pass
    return result


def _load_service_manager():
    try:
        from service_manager import install_service, uninstall_service
    except ImportError as exc:
        raise SyncError(
            "service_manager.py must be next to provider_session_sync.py"
        ) from exc
    return install_service, uninstall_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Keep Codex conversations visible across provider switches.",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=codex_home_from_env(),
        help="Codex data directory (default: CODEX_HOME or ~/.codex)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync", help="Synchronize all sessions immediately")
    subparsers.add_parser("status", help="Show synchronization status")
    watch_parser = subparsers.add_parser("watch", help="Watch for provider changes")
    watch_parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    watch_parser.add_argument(
        "--reconcile-seconds", type=float, default=DEFAULT_RECONCILE_SECONDS
    )
    watch_parser.add_argument("--once", action="store_true")
    subparsers.add_parser("install", help="Install and start the background service")
    uninstall_parser = subparsers.add_parser(
        "uninstall", help="Stop and remove the background service"
    )
    uninstall_parser.add_argument(
        "--remove-data",
        action="store_true",
        help="Also remove runtime state and logs; backups are always retained",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = build_parser().parse_args(argv)
    codex_home = arguments.codex_home.expanduser().resolve()
    try:
        if arguments.command == "sync":
            print(json.dumps(sync_once(codex_home).as_dict(), indent=2))
        elif arguments.command == "status":
            print(json.dumps(status(codex_home), indent=2, sort_keys=True))
        elif arguments.command == "watch":
            watch(
                codex_home,
                arguments.poll_seconds,
                arguments.reconcile_seconds,
                arguments.once,
            )
        elif arguments.command == "install":
            install_service, _ = _load_service_manager()
            sync_once(codex_home)
            print(json.dumps(install_service(codex_home, Path(__file__)), indent=2))
        elif arguments.command == "uninstall":
            _, uninstall_service = _load_service_manager()
            print(
                json.dumps(
                    uninstall_service(
                        codex_home, remove_data=arguments.remove_data
                    ),
                    indent=2,
                )
            )
        return 0
    except (OSError, sqlite3.Error, SyncError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
