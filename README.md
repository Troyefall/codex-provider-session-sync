# Codex Provider Session Sync

Keep local Codex conversations visible when switching between OpenAI and custom
model providers.

Codex stores the provider name in both its SQLite thread index and each
conversation's `session_meta` record. If those values no longer match the
active provider, conversations can disappear from the sidebar even though
their files still exist. This project synchronizes those metadata values and
keeps them aligned in the background.

## Install

Python 3.9 or newer is required.

Run Codex at least once before installation so the current user's session
directories and state database exist.

### macOS and Linux

```bash
curl -fsSL https://raw.githubusercontent.com/Troyefall/codex-provider-session-sync/main/install.py | python3
```

### Windows PowerShell

```powershell
(Invoke-WebRequest https://raw.githubusercontent.com/Troyefall/codex-provider-session-sync/main/install.py).Content | py -3 -
```

The installer:

1. Installs the Skill at
   `$CODEX_HOME/skills/codex-provider-session-sync`.
2. Creates a timestamped backup before changing metadata.
3. Repairs active and archived sessions.
4. Starts a user-level background watcher.

Restart Codex once after the first installation so the Skill appears in Codex.

Installing or copying only the Skill directory does not register a background
service. Use the one-command installer above, or run the installed Skill's
`install` command.

## Per-user Scope

The project contains no fixed username, home directory, database version, or
conversation count. It resolves the current user's Codex data directory from
`CODEX_HOME`, defaulting to that user's `~/.codex`.

Each installation reads and modifies only the selected Codex home. User-level
services are isolated by macOS login domain, Linux systemd user instance, and
a user-specific Windows Scheduled Task name. Multiple operating-system users
on the same computer can therefore install independent watchers for their own
Codex data.

## Commands

Set `SKILL` to the installed Skill directory:

```bash
SKILL="${CODEX_HOME:-$HOME/.codex}/skills/codex-provider-session-sync"
python3 "$SKILL/scripts/provider_session_sync.py" status
python3 "$SKILL/scripts/provider_session_sync.py" sync
python3 "$SKILL/scripts/provider_session_sync.py" install
python3 "$SKILL/scripts/provider_session_sync.py" watch
python3 "$SKILL/scripts/provider_session_sync.py" uninstall
```

On Windows, replace `python3` with `py -3` and use the equivalent path under
`%USERPROFILE%\.codex`.

## Platform Services

- macOS: user LaunchAgent
  `io.github.troyefall.codex-provider-session-sync`.
- Linux: systemd user service
  `codex-provider-session-sync.service`.
- Windows: current-user Scheduled Task with a stable user-specific suffix.

The watcher responds to provider changes, database replacement or reindexing,
new session files, and periodic reconciliation. It supports both the current
`$CODEX_HOME/sqlite/state_*.sqlite` location and the legacy
`$CODEX_HOME/state_*.sqlite` location, preferring the current location.

When `config.toml` contains a top-level `model_provider`, that value is
authoritative. Newer Codex builds may omit it; in that case the watcher detects
a switch from the newest user thread or session and retains the last successful
provider between changes.

## Safety

The synchronizer modifies only:

- `threads.model_provider` in the active compatible `state_*.sqlite` database.
- `session_meta.payload.model_provider` in active and archived JSONL sessions.

It does not modify messages, titles, tool results, provider definitions, API
keys, or authentication files.

Before a migration, backups are written to:

```text
$CODEX_HOME/codex-provider-session-sync/backups/<UTC timestamp>/
```

The synchronizer uses parameterized SQLite statements, a process lock, SQLite
busy retries, atomic JSONL replacement, and file-change detection. It stops
without editing data if the Codex database schema or session metadata layout is
not recognized.

Versions before `1.0.2` installed a SQLite trigger. The current version removes
that trigger and other known legacy provider-sync triggers because they can
overwrite the first thread created after a provider switch.

This project works with internal Codex storage. A future Codex release may
change that format; compatibility failures are intentional and should be
reported with the Codex version and watcher log.

## Uninstall

From a checkout:

```bash
python3 uninstall.py
```

Or use the installed command:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-provider-session-sync/scripts/provider_session_sync.py" uninstall
```

Uninstalling stops and removes the background service. Conversation files and
all backups are retained.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q install.py uninstall.py skills
python3 skills/codex-provider-session-sync/scripts/validate_skill.py \
  skills/codex-provider-session-sync
```

Tests use temporary Codex directories and never modify the developer's live
conversations.

## License

MIT
