# FORM Goggles Sync Log

Bounded verification runs per `.claude/skills/form-sync-verification`. Every attempt recorded, including failures.

---

## 2026-10-06 — Field report (issue #5): same "ACK ≠ commit" on a second user's goggles

**Source:** [garrickgan/formgoggles-py#5](https://github.com/garrickgan/formgoggles-py/issues/5), reported by another user on macOS (no BlueZ agent; device already paired). No hardware run on our side.

**Observed (reporter's logs):**
- Web UI "Direct BLE" (`menu=imports`, no entitlement files): WorkoutsInfo, ImportedWorkoutsInfo, WorkoutData, UpNextWorkouts — **4/4 `FILE_TRANSFER_SUCCESS`**, workout not on goggles.
- CLI `--direct-ble --direct-ble-menu imports` (default `--entitlement-mode server`, reporter's own account blobs: subscription 144B, entitlement 16B, 65 flags) — **7/7 `FILE_TRANSFER_SUCCESS`**, workout not on goggles.
- Reporter's Premium status unknown; no index read (`read_goggle_indexes.py`) before/after.

**Conclusion:** independent reproduction of the 2026-07-21 result on a different device/account/OS — the gap is not specific to our goggles or setup. The tool was reporting these runs as "Done! Workout pushed directly to goggles", which is what led to the bug report. Output now states the ACK-only result and points to the Premium library-save path; no protocol change.


## 2026-07-21 (evening) — "Undecodable imports payload" root-caused: transfer truncation, not schema

**Question:** Baseline/verify reads failed to decode the imports index (237B of advertised 2789B). Was the protobuf schema stale?

**Method:** Instrumented `read_goggle_indexes.py` to (a) dump raw hex on decode failure, (b) detect truncated transfers (received vs advertised `fileSize`), and (c) `--repeat N` to request the same index multiple times per session. Ran `--target imports --repeat 3` (`logs/imports-repeat-20260721-*.log`).

**Findings:**
1. All 3 requests in one session decoded cleanly: `IMPORTED_WORKOUTS_INFO`, `imports=0`, `isTrainingPeaksConnected=True`. Schema is correct — an empty imports index is 6 bytes on the wire.
2. The earlier failure advertised `fileSize=2789` but the goggles sent only 2 chunks (231+6=237B), interleaved with an `UNKNOWN_CMD`, then `FILE_TRANSFER_DONE`. **It was a truncated transfer of a non-empty index**, not a schema mismatch. Likely the goggles had ~2.7KB of TrainingPeaks-imported workouts at that moment and failed mid-stream; by evening the index was empty (TP imports expire/clear on their own schedule — reads are the only thing we did).
3. `UNKNOWN_CMD` (command type 33) also appears in response to a *repeat* `IMPORTED_WORKOUTS_INFO_REQUEST` in the same session — the goggles then answer normally. Reading as "duplicate/unexpected command" — harmless.

**Code changes (reader only, no protocol changes):**
- Truncated transfers now reported as `INCOMPLETE (received/advertisedB)` and decode is skipped — no more misleading "could not decode FormFileMessage" on partial data.
- Raw hex dumped on genuine decode failures.
- New `--repeat N` flag for single-session repeat diagnostics.

**Remaining loose end:** if a future read catches the imports index non-empty again, `--repeat` will show whether large transfers consistently truncate after chunk 1 (flow-control gap in the reader) or whether it was a one-off BLE flake. If consistent, next step is per-chunk flow control (`FILE_TRANSFER_CHUNK_ID_REQUEST`, cmd 34) in the receive path.


## 2026-07-21 — Is the loophole patched? (full verification pass)

**Question:** Reddit user MtBakerScum reported the method may have been patched (encryption seen in BLE snoop). Garrick asked to verify current state and produce an honest answer.

### Server-side checks (no hardware)

| Check | Command / method | Result |
|---|---|---|
| OAuth refresh token still honored | `POST /api/v1/oauth/token/refresh` with stored `refreshToken` | ✅ HTTP 200, new access token expires 2026-08-21. Saved to `~/.formgoggles.json`. Prior token had expired 2026-06-06 (6 weeks stale) — this, not a patch, was why the API path appeared dead. |
| Create workout via API | `form_sync.py --no-ble` | ✅ Workout created: `SYNCTEST 20260721-1940`, ID `019f8744-6a64-7004-ac74-a0918624fcb2` |
| Save workout to library | `POST /api/v1/users/me/workouts` | ❌ HTTP `403 subscription_required` — **server-enforced Premium gate** |

**Cached blobs:** `~/.formgoggles_blobs/entitlement.bin` (32B) and `subscription.bin` (160B) are high-entropy ciphertext — opaque without FORM's keys. `feature_flags.json` is plaintext (59 flags).

### BLE verification (goggles awake, one push attempt)

1. **Baseline index read** — `read_goggle_indexes.py --target all` (`logs/baseline-20260721-194655.log`)
   - `saved`: **no response** (consistent with empty/non-Premium library)
   - `imports`: received 237B payload but **fails to decode** against both `FormFileMessage` and `ImportedWorkoutsInfoMessage` schemas
   - `plan`: full training plan present (workouts through week 24)
   - Test workout ID absent from baseline (as expected)

2. **Push** — `form_sync.py --direct-ble --entitlement-mode cached` (`logs/push-20260721-1947*.log`)
   - Created `SYNCTEST-PUSH 20260721-1947`, ID `019f874b-2897-7551-a30e-23f22892ab11`, fetched 264B protobuf
   - Sent: SubscriptionInfo, DeviceEntitlement, FeatureFlagsV2, WorkoutsInfo, ImportedWorkoutsInfo, WorkoutData, UpNextWorkouts
   - **7/7 transfers ACKed `FILE_TRANSFER_SUCCESS`**

3. **Verify** — re-read indexes (`logs/verify-20260721-1947*.log`)
   - Pushed workout ID **NOT present** in any index
   - `saved` still no-response; `imports` still undecodable (identical 237B failure)

### Conclusion

- **Transport works exactly as before**: goggles ACK every transfer, including cached entitlement blobs. No new BLE-level rejection.
- **Commit still fails exactly as before**: pushed workout never appears in the goggle's own index. This matches the pre-existing "ACK ≠ commit" gap documented in the skill since 2026-07-06 — **no behavioral change detected**.
- **The server side is what closed**: workout creation still works, but saving to the library (the path that made workouts appear via cloud sync) is now hard-gated behind Premium with `403 subscription_required`. That is server-enforced, not a protocol loophole.
- The undecodable 237B `imports` response is worth noting but is **not** evidence of a patch — our schema may simply be incomplete; it failed identically before and after the push.

**Honest answer for the Reddit thread:** FORM didn't patch a BLE exploit — the goggles still accept transfers. What changed (or was always enforced) is that saving a custom workout to your FORM library requires an active Premium subscription, checked server-side. Without Premium there is no remaining path: the direct-BLE transfer ACKs but never commits, and that was true even while the "loophole" appeared to work.

**Not pursued (out of scope):** any attempt to bypass the Premium gate (e.g., forging/replaying entitlement data to defeat the server check). The cached blobs are encrypted; defeating that is circumvention of a paid access control, not interoperability.

### Most informative next hypothesis (if this project continues legitimately)
Capture official-app BLE traffic (`ble_sniff.py` / `frida/hook_form.js`) while Premium is active to see what the official sync sends after `SYNC_START` that we don't — specifically which message makes the goggle *commit* rather than merely ACK. Requires human setup and an active subscription; propose, don't run autonomously.
