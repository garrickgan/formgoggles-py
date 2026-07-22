---
name: form-sync-verification
description: |
  Bounded, diagnostic-first procedure for verifying whether a pushed workout actually
  committed to FORM goggles. Use when: (1) testing changes to form_sync.py/full_sync.py
  BLE push logic, (2) investigating why transfers succeed (FILE_TRANSFER_SUCCESS) but
  workouts don't appear on the goggles, (3) any /goal loop over goggle sync. Hardware-in-
  the-loop: BLE is flaky and the device is irreplaceable mid-project, so this skill is
  as much about what NOT to do as what to do.
---

# FORM Goggles Sync Verification

## Known state (as of 2026-07)

- Server API works: create workouts, save to library, fetch protobufs.
- BLE transport works: goggles ACK every transfer (11/11 FILE_TRANSFER_SUCCESS).
- **The gap**: transport success ≠ commit. Our pushes don't appear on the goggles;
  the official app's sync does. Root cause unknown — likely missing context files
  (DEVICE_SETTINGS_V2, USER_PROFILE_V2, REMOTE_CONFIG), or unanswered goggle-initiated
  requests after SYNC_START.
- Premium entitlement blobs are cached in `~/.formgoggles_blobs/` for replay.

## Success criterion — be specific or it's noise

Never use "a workout appears in the list" as the signal. Always:

1. Create the test workout with a **unique, greppable name** including a timestamp,
   e.g. `SYNCTEST 20260706-1732`, and record the workout id returned by the server API.
2. Verify by reading the goggles' own index, not by inference:
   ```bash
   .venv/bin/python read_goggle_indexes.py --target all
   ```
   (Always use `.venv/bin/python` — the system python3 lacks bleak/protobuf.)
   Success = **that specific workout id/name** present in the relevant index
   (`imports` or `saved`) that was absent in the pre-push baseline.
3. Always capture the baseline index BEFORE pushing, so "appeared" means a diff,
   not a guess.

## Procedure (one attempt)

1. **Preflight (no hardware needed):** run with the project venv:
   ```bash
   .venv/bin/python -c "import form_sync" \
   && .venv/bin/python -c "import json;c=json.load(open('$HOME/.formgoggles.json'));assert c.get('goggleMac') and c.get('accessToken')" \
   && ls ~/.formgoggles_blobs/entitlement.bin ~/.formgoggles_blobs/subscription.bin
   ```
   Config keys are `goggleMac` / `accessToken` (camelCase). If anything is missing,
   stop and report — do not attempt BLE. Note: `accessToken` may be expired
   (`tokenExpires` field); server-API steps refresh via `refreshToken`, but
   cached-blob mode (`--entitlement-mode cached`) works without the server.
2. **Baseline:** goggles on and awake → `.venv/bin/python read_goggle_indexes.py
   --target all`. Save output to a timestamped file.
3. **Push:** one sync attempt (`form_sync.py` or `full_sync.py` depending on the
   hypothesis being tested), full output captured to a timestamped log.
4. **Verify:** re-read indexes. Diff against baseline for the specific test id.
5. **Record:** append hypothesis → change → transfer result → index diff → conclusion
   to `SYNC_LOG.md` in the repo root (create if missing). Every attempt gets an entry,
   including failures — the log IS the deliverable while the root cause is unknown.

## Hard limits (hardware-in-the-loop guardrails)

These are non-negotiable in any autonomous or /goal context:

- **Max 3 turns** per /goal loop; **max 2 BLE sync attempts** per session (BLE flakiness
  means attempt 3+ is noise, and each attempt drains/heats the device).
- **No destructive writes to the device**: no file deletions, no factory/debug service
  writes (Services 2/3 are unprobed factory/debug — never write to them), no firmware
  operations, no `--replace-id` against a workout that wasn't created by this session.
- **Never clear device state or caches** — not on the goggles, and not
  `~/.formgoggles_blobs/` (the premium entitlement blobs are irreplaceable after the
  subscription lapses).
- **No repeated official-app intervention without human confirmation.** Asking Garrick
  to run the official app's sync is a consumable diagnostic (it changes device state and
  costs a comparison baseline) — at most once, and only with explicit confirmation.
- **Stop on repeated identical failure**: if two attempts produce byte-identical
  transfer results and the same index diff (i.e., nothing), stop — the hypothesis is
  exhausted; more attempts are not evidence.
- **Timeouts are ambiguous, not failures**: a BLE timeout may mean the goggles slept.
  One retry after confirming the device is awake; then treat as environment, not code.

## If not solved: diagnostic report format

When a session/loop ends without the workout committing, produce a report (in
`SYNC_LOG.md`) containing: hypothesis tested, exact command(s) run, transfer-level
results per file, pre/post index diff (or "no diff"), any goggle-initiated messages
observed after SYNC_START (from the notification handler log), and the single most
informative next hypothesis. Do not restart with a new hypothesis in the same loop.

## Escalation order for the root cause (from prior analysis)

1. Respond to goggle-initiated requests after SYNC_START that we currently ignore
   (check notification handler logs for unanswered message types).
2. Add missing FormFileMessage types to the sync: DEVICE_SETTINGS_V2, USER_PROFILE_V2,
   REMOTE_CONFIG (full_sync.py already builds the first two — verify they're sent and
   ACKed in the right order relative to the app's sequence).
3. Capture official-app BLE traffic (`ble_sniff.py` / `frida/hook_form.js`) — requires
   human setup, propose it, don't do it autonomously.
