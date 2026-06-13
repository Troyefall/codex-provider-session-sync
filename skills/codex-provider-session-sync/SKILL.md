---
name: codex-provider-session-sync
description: Install, repair, inspect, or remove automatic Codex provider-session synchronization so local conversations remain visible when model_provider changes. Use when switching between OpenAI and custom providers hides prior chats, when Codex session metadata contains mixed provider names, or when managing the cross-platform background watcher installed by this skill.
---

# Codex Provider Session Sync

Keep Codex thread database records and session metadata aligned with the active
provider. Prefer a top-level `model_provider` in `config.toml` when present;
otherwise detect a switch from the newest user thread or session and retain the
last successful provider.

The implementation is per-user and contains no fixed home path or conversation
count. Resolve the target from `CODEX_HOME`, defaulting to the current user's
`~/.codex`, and never scan outside that directory.

## Workflow

1. Locate `CODEX_HOME`, defaulting to `~/.codex`.
2. Prefer `$CODEX_HOME/sqlite/state_*.sqlite`, then support the legacy
   `$CODEX_HOME/state_*.sqlite` location.
3. Run `python3 scripts/provider_session_sync.py status`.
4. For a one-time repair, run `python3 scripts/provider_session_sync.py sync`.
5. To install or repair automatic monitoring, run
   `python3 scripts/provider_session_sync.py install`.
6. Report the resolved provider and source, changed database rows, changed
   session files, removed legacy triggers, backup path, and service status.
7. Restart Codex after first installation so the newly installed Skill is
   discovered.

Copying the Skill directory alone does not register a background service. Run
the repository installer or this Skill's `install` command for automatic
monitoring.

Use `--codex-home PATH` before the command when Codex stores data elsewhere:

```bash
python3 scripts/provider_session_sync.py --codex-home /path/to/.codex status
```

## Safety Rules

- Do not edit authentication tokens, provider definitions, messages, titles, or
  tool output.
- Modify only `threads.model_provider` and
  `session_meta.payload.model_provider`.
- Remove only provider-sync triggers and state tables created by older versions
  of this project or its documented predecessor.
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
uses a current-user Scheduled Task with a stable user-specific name.

## Failure Handling

If synchronization fails, show the exact error and the watcher log path:
`$CODEX_HOME/codex-provider-session-sync/logs/watcher.log`. Do not attempt
ad-hoc database edits after a compatibility failure.
