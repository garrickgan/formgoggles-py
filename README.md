# formgoggles-py

Push custom swim workouts to FORM smart goggles over Bluetooth LE — no subscription required.

## What this does

FORM swim goggles are excellent hardware. The subscription paywall for custom workout sync is not. This tool implements the FORM BLE + API protocol so you can push structured swim workouts directly to your goggles from the command line.

**One command. Define a workout, push it, swim it.**

```bash
python3 form_sync.py \
  --token YOUR_BEARER_TOKEN \
  --goggle-mac AA:BB:CC:DD:EE:FF \
  --workout "10x100 free @moderate 20s rest"
```

## What happens

1. **Creates** the workout on FORM's server via their REST API
2. **Saves** it to your workout list
3. **Fetches** the server-generated protobuf binary
4. **Pushes** it to your goggles over Bluetooth LE

All four steps, one command, ~15 seconds.

## Setup

```bash
git clone https://github.com/yourusername/formgoggles-py.git
cd formgoggles-py
pip install -r requirements.txt

# Compile protobuf schemas
protoc --python_out=. proto/form.proto proto/workout.proto
```

### Requirements

- FORM swim goggles (tested on firmware 3.11.211)
- Python 3.9+
- Linux with BlueZ (BLE push requires `sudo` for BlueZ agent registration)
- A FORM account and bearer token (captured via mitmproxy or browser dev tools)

## Usage

### Push a simple workout

```bash
python3 form_sync.py \
  --token YOUR_TOKEN \
  --goggle-mac AA:BB:CC:DD:EE:FF \
  --workout "10x100 free @moderate 20s rest"
```

Auto-generates 200m easy warmup and cooldown around your main set.

### Push a structured workout

```bash
python3 form_sync.py \
  --token YOUR_TOKEN \
  --goggle-mac AA:BB:CC:DD:EE:FF \
  --workout "warmup: 200 free easy | main: 8x100 free @fast 15s rest, 4x50 fly @max 30s rest | cooldown: 200 free easy"
```

### Custom name

```bash
python3 form_sync.py \
  --token YOUR_TOKEN \
  --workout "10x100 free @threshold 20s rest" \
  --name "Tuesday Threshold"
```

### Server-only (skip BLE push)

```bash
python3 form_sync.py \
  --token YOUR_TOKEN \
  --workout "5x200 free @mod 30s rest" \
  --no-ble
```

Creates and saves the workout on the FORM server. Sync via the official app later.

### List saved workouts

```bash
python3 form_sync.py --token YOUR_TOKEN --workout dummy --list-workouts
```

### Replace a saved workout (max 5 limit)

```bash
python3 form_sync.py \
  --token YOUR_TOKEN \
  --workout "10x50 fly @max 30s rest" \
  --replace-id UUID_OF_WORKOUT_TO_REMOVE
```

### Auto token refresh

```bash
python3 form_sync.py \
  --token YOUR_TOKEN \
  --refresh-token YOUR_REFRESH_TOKEN \
  --workout "10x100 free @mod 20s rest"
```

Automatically refreshes the bearer token on 401 errors.

## Workout String Format

```
Sets:       NxDISTm STROKE @EFFORT RESTs rest
Sections:   warmup: ... | main: ... | cooldown: ...
Multiple:   set1, set2, set3  (comma-separated within a section)
```

| Component | Options | Default |
|-----------|---------|---------|
| N | Interval count | 1 |
| DIST | Distance in meters | required |
| STROKE | free, back, breast, fly, im, choice | free |
| EFFORT | easy, moderate, fast, strong, max, descend | moderate |
| REST | Rest between intervals in seconds | 0 |

Effort aliases: `threshold`=moderate, `hard`=fast, `sprint`=max, `warm`=easy

## Getting Your Bearer Token

The easiest way is a direct API login — no mitmproxy needed:

```bash
python3 form_sync.py --login your@email.com yourpassword
```

This prints your `accessToken` (valid 30 days) and `refreshToken` (valid 6 months).

**A free FORM account is sufficient** — no active subscription required to authenticate or use BLE sync.

Alternatively, capture it manually:

1. Install [mitmproxy](https://mitmproxy.org/)
2. Configure your phone to proxy through mitmproxy
3. Open the FORM app and trigger any sync
4. Find the `Authorization: Bearer ...` header in the captured requests to `app.formathletica.com`

Pass `--refresh-token` to auto-refresh on expiry.

## Protocol Documentation

See [PROTOCOL.md](./PROTOCOL.md) for the full reverse-engineered BLE protocol:
- GATT service/characteristic UUIDs
- Protobuf message schemas
- File transfer flow (chunked with retransmission)
- REST API endpoints

## How it was built

This tool was built by reverse engineering:
1. **BLE GATT profile** — Scanning services/characteristics, capturing sync traffic
2. **Android APK** — Decompiling to extract protobuf schemas and API endpoints
3. **REST API** — Observing app traffic via mitmproxy to map workout creation flow
4. **Protobuf binary format** — Comparing `protoc --decode_raw` output with JSON API responses

Full writeup: [Reverse Engineering FORM Swim Goggles](https://reachflowstate.ai/blog/form-goggles-reverse-engineering)

## Legal

Developed under the DMCA interoperability exception (17 U.S.C. §1201(f)). We reverse engineered only what was necessary to achieve interoperability with third-party training software. No FORM proprietary code, credentials, or user data is included in this repository.

See [LEGAL.md](./LEGAL.md) for full analysis.

## Disclaimer

This project is not affiliated with or endorsed by FORM Athletica Inc. FORM is a trademark of FORM Athletica Inc. Use at your own risk.

## License

MIT
