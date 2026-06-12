---
name: codex-provider-session-sync
description: Install, repair, inspect, or remove automatic Codex provider-session synchronization so local conversations remain visible when model_provider changes. Use when switching between OpenAI and custom providers hides prior chats, when Codex session metadata contains mixed provider names, or when managing the cross-platform background watcher installed by this skill.
---

# Codex Provider Session Sync

Keep Codex thread database records and session metadata aligned with the active
`model_provider` in `config.toml`.

## Workflow

1. Locate `CODEX_HOME`, defaulting to `~/.codex`.
2. Run `python3 scripts/provider_session_sync.py status`.
3. For a one-time repair, run `python3 scripts/provider_session_sync.py sync`.
4. To install or repair automatic monitoring, run
   `python3 scripts/provider_session_sync.py install`.
5. Report the configured provider, changed database rows, changed session files,
   backup path, and service status.
6. Restart Codex after first installation so the newly installed Skill is
   discovered.

Use `--codex-home PATH` before the command when Codex stores data elsewhere:

```bash
python3 scripts/provider_session_sync.py --codex-home /path/to/.codex status
```

## Safety Rules

- Do not edit authentication tokens, provider definitions, messages, titles, or
  tool output.
- Modify only `threads.model_provider` and
  `session_meta.payload.model_provider`.
- Preserve and report timestamped backups under
  `$CODEX_HOME/codex-provider-session-sync/backups`.
- Stop on an unknown SQLite schema, malformed session metadata, or concurrent
  file modification.
- Never delete backups during uninstall.
- Treat `state_*.sqlite` and session JSONL files as Codex internal storage that
  may change between releases.

## Commands

- `sync`: Repair existing active and archived conversations immediately.
- `watch`: Run the foreground watcher for diagnostics.
- `status`: Inspect configured and stored providers without changing data.
- `install`: Synchronize immediately and install the platform background service.
- `uninstall`: Remove the service while retaining conversations and backups.

macOS uses a user LaunchAgent, Linux uses a systemd user service, and Windows
uses a current-user Scheduled Task.

## Failure Handling

If synchronization fails, show the exact error and the watcher log path:
`$CODEX_HOME/codex-provider-session-sync/logs/watcher.log`. Do not attempt
ad-hoc database edits after a compatibility failure.
