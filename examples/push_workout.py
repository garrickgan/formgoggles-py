#!/usr/bin/env python3
"""
Example: Push a workout to FORM goggles using the form_sync module.

This shows the programmatic API — use form_sync.py CLI for normal usage.
"""

import asyncio
import base64
import sys
import os

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from form_sync import FormAPI, BLESync, parse_workout_string, build_api_payload, generate_name, calc_duration_estimate

# === Configuration ===
# Get these from mitmproxy or browser dev tools (see README.md)
TOKEN = "YOUR_BEARER_TOKEN_HERE"
REFRESH_TOKEN = "YOUR_REFRESH_TOKEN_HERE"  # Optional
GOGGLE_MAC = "AA:BB:CC:DD:EE:FF"  # Your goggles' BLE MAC address


async def main():
    # 1. Parse a workout string
    workout_str = "warmup: 200 free easy | main: 10x100 free @moderate 20s rest | cooldown: 200 free easy"
    sections = parse_workout_string(workout_str)
    name = "My Custom Workout"

    # 2. Create on FORM server
    api = FormAPI(TOKEN, refresh_token=REFRESH_TOKEN)
    payload = build_api_payload(name, sections)
    workout_data = api.create_workout(payload)
    if not workout_data:
        print("Failed to create workout")
        return

    workout_id = workout_data["id"]
    print(f"Created: {workout_id}")

    # 3. Save to user's workout list
    if not api.save_workout(workout_id):
        print("Failed to save (you may need --replace-id if at max 5)")
        return

    # 4. Fetch server-generated protobuf
    workout_binary = api.fetch_protobuf(workout_id)
    if not workout_binary:
        print("Failed to fetch protobuf")
        return

    # 5. Push to goggles via BLE
    ble = BLESync(GOGGLE_MAC)
    duration = calc_duration_estimate(sections)
    success = await ble.push_workout(workout_id, workout_binary, duration)

    if success:
        print(f"Workout '{name}' is on your goggles!")
    else:
        print("BLE push failed — workout is still saved on server")


if __name__ == "__main__":
    asyncio.run(main())
