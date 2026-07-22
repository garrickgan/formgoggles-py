#!/usr/bin/env python3
"""
Full sync to FORM goggles — mirrors the official app's sync behavior.

Sends ALL data the official app sends: device settings, subscription,
entitlement, feature flags, remote config, ALL workouts, user profile.

Usage:
  python3 full_sync.py [--new-workout "4x100 free @moderate 20s rest"]
"""

import argparse
import asyncio
import base64
import json
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "proto")

from form_sync import (
    API_BASE, FormAPI, BLESync, load_config, load_entitlement_blobs,
    save_entitlement_blobs, make_form_file, make_command, make_data_chunk,
    form_pb2, BLOB_CACHE_DIR, BLE_AVAILABLE,
    parse_workout_string, build_api_payload, generate_name, calc_duration_estimate,
)

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    pass


def build_device_settings_v2(device_info):
    """Build DeviceSettingsV2Message from API device data."""
    ds = form_pb2.DeviceSettingsV2Message()
    ds.timestamp.seconds = int(time.time())

    import calendar
    local_offset = time.timezone if time.daylight == 0 else time.altzone
    ds.timezoneOffsetMs = -local_offset * 1000
    ds.isDST = bool(time.daylight and time.localtime().tm_isdst)

    brightness = device_info.get("brightness", 50)
    brightness_map = {0: 1, 25: 2, 50: 3, 75: 4, 100: 5}
    ds.brightness = brightness_map.get(brightness, 3)

    orientation = device_info.get("orientation", "left")
    ds.orientation = 1 if orientation == "left" else 2

    ds.shortRestDetection = device_info.get("shortRestDetection", False)

    return ds


def build_user_profile_v2(user_info):
    """Build UserProfileV2Message from API user data."""
    up = form_pb2.UserProfileV2Message()

    gender = user_info.get("gender", "male")
    gender_map = {"male": 1, "female": 2, "unspecified": 3, "non_binary": 4}
    up.gender = gender_map.get(gender, 3)

    height_cm = user_info.get("height", 180)
    weight_kg = user_info.get("weight", 75)
    is_imperial = user_info.get("measurementSystem") == "imperial"
    up.isImperial = is_imperial

    if is_imperial:
        up.height = int(round(height_cm / 2.54))
        up.weight = int(round(weight_kg * 2.20462))
    else:
        up.height = int(round(height_cm))
        up.weight = int(round(weight_kg))

    birthdate = user_info.get("birthdate")
    if birthdate:
        try:
            bd = datetime.strptime(birthdate, "%Y-%m-%d")
            today = datetime.now()
            age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
            up.age = age
        except ValueError:
            up.age = 30
    else:
        up.age = 30

    up.language = 1  # ENGLISH

    secret_key = user_info.get("secretKey", "")
    if secret_key:
        up.secretKey = secret_key

    hr_zones = user_info.get("heartRateZones", {})
    hr_preference = user_info.get("heartRateZonePreference", "automatic")
    if hr_preference == "automatic":
        z = up.heartRateZones.add()
        z.zoneNumber = 1
        z.maxBpm = 0
    else:
        zone_names = ["zoneOne", "zoneTwo", "zoneThree", "zoneFour", "zoneFive"]
        for i, zname in enumerate(zone_names, 1):
            zdata = hr_zones.get(zname)
            if zdata:
                z = up.heartRateZones.add()
                z.zoneNumber = i
                z.maxBpm = zdata.get("max", 0)

    return up


async def full_sync(goggle_mac, all_workout_ids, all_workout_binaries,
                    entitlement_bundle, device_settings_bytes, user_profile_bytes,
                    remote_config_bytes, new_workout_id=None, duration_est=300):
    """Full sync matching the official app's file ordering."""

    files = []

    # 1. DEVICE_SETTINGS_V2 (type 2) — app sends this FIRST
    if device_settings_bytes:
        files.append((make_form_file(2, device_settings_bytes), "DeviceSettingsV2"))

    # 2. SUBSCRIPTION_INFO (type 8)
    if entitlement_bundle:
        sub = entitlement_bundle.get("subscription_bytes")
        if sub:
            files.append((make_form_file(8, sub, encrypted=True), "SubscriptionInfo"))

    # 3. REMOTE_CONFIG (type 16)
    if remote_config_bytes:
        files.append((make_form_file(16, remote_config_bytes), "RemoteConfig"))

    # 4. DEVICE_ENTITLEMENT (type 10)
    if entitlement_bundle:
        ent = entitlement_bundle.get("entitlement_bytes")
        if ent:
            files.append((make_form_file(10, ent, encrypted=True), "DeviceEntitlement"))

    # 5. FEATURE_FLAGS_V2 (type 17)
    if entitlement_bundle:
        flags = entitlement_bundle.get("feature_flags")
        if flags:
            ff_msg = form_pb2.FeatureFlagsV2Message()
            for flag_name in flags:
                if len(flag_name) <= 30:
                    f = ff_msg.featureFlag.add()
                    f.name = flag_name
            files.append((make_form_file(17, ff_msg.SerializeToString()), "FeatureFlagsV2"))

    # 6. WORKOUT_DATA (type 5) — each workout binary
    for wid in all_workout_ids:
        binary = all_workout_binaries.get(wid)
        if binary:
            files.append((make_form_file(5, binary), f"WorkoutData({wid[:8]})"))

    # 7. WORKOUTS_INFO (type 7) — ALL workouts as standaloneWorkouts
    wim = form_pb2.WorkoutsInfoMessage()
    for wid in all_workout_ids:
        wi = wim.standaloneWorkouts.add()
        wi.id = wid
        wi.expectedDuration = duration_est
    files.append((make_form_file(7, wim.SerializeToString()), "WorkoutsInfo(all)"))

    # 8. SAVED_WORKOUTS (type 6) — ALL workout IDs
    now_seconds = int(time.time())
    swm = form_pb2.SavedWorkoutsMessage()
    for wid in all_workout_ids:
        sw = swm.workouts.add()
        sw.id = wid
        sw.lastModifiedAt.seconds = now_seconds
    files.append((make_form_file(6, swm.SerializeToString()), "SavedWorkouts(all)"))

    # 9. UP_NEXT_WORKOUTS (type 15)
    unm = form_pb2.UpNextWorkoutsMessage()
    if new_workout_id:
        un = unm.upNextWorkouts.add()
        un.id = new_workout_id
        un.type = 1  # STANDALONE
        un.expectedDuration = duration_est
    files.append((make_form_file(15, unm.SerializeToString()), "UpNextWorkouts"))

    # 10. USER_PROFILE_V2 (type 13) — app sends this LAST
    if user_profile_bytes:
        files.append((make_form_file(13, user_profile_bytes), "UserProfileV2"))

    # BLE transfer
    print(f"\nScanning for {goggle_mac}...", flush=True)
    device = await BleakScanner.find_device_by_address(goggle_mac, timeout=15.0)
    if not device:
        print("ERROR: Goggles not found!", flush=True)
        return False

    print(f"Found: {device.name}", flush=True)

    ble = BLESync(goggle_mac)
    disconnected = False

    client = BleakClient(device)
    try:
        await client.connect()
        print("Connected to goggles", flush=True)
        await asyncio.sleep(1.0)

        chars = {}
        for svc in client.services:
            for char in svc.characteristics:
                chars[char.uuid.split("-")[0]] = char

        wc = chars.get("00012001")
        nc = chars.get("00012000")
        if not wc or not nc:
            print("ERROR: Required BLE characteristics not found!", flush=True)
            return False

        print("Starting notifications...", flush=True)
        await asyncio.wait_for(client.start_notify(nc, ble.notification_handler), timeout=15.0)

        print("Sending SYNC_START...", flush=True)
        await ble.send_cmd(client, wc, "SYNC_START", 1)
        await ble.wait_response(3.0)
        if ble.disconnect_requested:
            print("ERROR: Goggles requested disconnect", flush=True)
            return False

        for idx, (fdata, label) in enumerate(files, 1):
            await ble.file_transfer(client, wc, idx, fdata, label)
            if ble.disconnect_requested:
                print("ERROR: Goggles disconnected during transfer", flush=True)
                return False

        await ble.send_cmd(client, wc, "SYNC_COMPLETE", 2)
        await ble.wait_response(3.0)

        total = len(files)
        successes = sum(1 for _, d in ble.received if "OK" in d)
        print(f"\nBLE sync complete: {successes}/{total} transfers succeeded", flush=True)
        return successes == total

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def main():
    parser = argparse.ArgumentParser(description="Full sync to FORM goggles")
    parser.add_argument("--new-workout", help="New workout string to create and include")
    parser.add_argument("--name", help="Workout name (auto-generated if omitted)")
    parser.add_argument("--replace-id", help="Replace this saved workout ID")
    parser.add_argument("--entitlement-mode", choices=("server", "cached"), default="server")
    parser.add_argument("--goggle-mac", help="Override goggle MAC address")
    args = parser.parse_args()

    config = load_config()
    if not config:
        print("ERROR: No config. Run: python3 form_sync.py --setup")
        return 1

    token = config.get("accessToken")
    refresh = config.get("refreshToken")
    goggle_mac = args.goggle_mac or config.get("goggleMac")
    if not token or not goggle_mac:
        print("ERROR: Missing token or goggle MAC in config")
        return 1

    api = FormAPI(token, refresh)

    # Optionally create a new workout
    new_workout_id = None
    if args.new_workout:
        sections = parse_workout_string(args.new_workout)
        name = args.name or generate_name(sections)
        payload = build_api_payload(name, sections)
        wd = api.create_workout(payload)
        if not wd:
            return 1
        new_workout_id = wd["id"]
        result = api.save_workout(new_workout_id, replace_id=args.replace_id)
        if result != "ok":
            print(f"WARNING: Could not save to library: {result}")

    # Fetch user profile
    print("Fetching user profile...", flush=True)
    r = api._request("GET", f"{API_BASE}/users/me")
    user_info = r.json() if r.status_code == 200 else {}
    print(f"  secretKey: {user_info.get('secretKey', 'N/A')}", flush=True)

    # Fetch device info
    print("Fetching device info...", flush=True)
    r = api._request("GET", f"{API_BASE}/users/me/devices")
    devices = r.json() if r.status_code == 200 else []
    device_info = devices[0] if devices else {}
    device_id = device_info.get("id")
    print(f"  firmware: {device_info.get('firmwareVersion', 'N/A')}", flush=True)
    device_info["shortRestDetection"] = user_info.get("shortRestDetection", False)

    # Build device settings
    ds = build_device_settings_v2(device_info)
    ds_bytes = ds.SerializeToString()
    print(f"  DeviceSettingsV2: {len(ds_bytes)}B", flush=True)

    # Build user profile
    up = build_user_profile_v2(user_info)
    up_bytes = up.SerializeToString()
    print(f"  UserProfileV2: {len(up_bytes)}B (secretKey={bool(up.secretKey)})", flush=True)

    # Fetch remote config
    print("Fetching remote config...", flush=True)
    rc_bytes = None
    if device_id:
        r = api._request("GET", f"https://app.formathletica.com/api/v2/users/me/devices/{device_id}/remote_config/protobuf")
        if r.status_code == 200:
            rc_data = r.json()
            if "binary" in rc_data:
                rc_bytes = base64.b64decode(rc_data["binary"])
                print(f"  RemoteConfig: {len(rc_bytes)}B", flush=True)
            else:
                print("  RemoteConfig: no binary field", flush=True)
        else:
            print(f"  RemoteConfig: {r.status_code}", flush=True)

    # Fetch all saved workouts
    print("\nFetching all saved workouts...", flush=True)
    saved = api.list_saved_workouts()
    all_ids = [w["id"] for w in saved]
    print(f"  {len(saved)} workouts:", flush=True)
    for w in saved:
        print(f"    {w['id'][:8]}...  {w.get('name', '?')}", flush=True)

    all_binaries = api.fetch_all_protobufs(all_ids)
    print(f"  {len(all_binaries)} protobufs fetched", flush=True)

    # Entitlement data
    if args.entitlement_mode == "server":
        print("\nFetching entitlement data...", flush=True)
        bundle = api.fetch_entitlement_bundle()
        sub = bundle.get("subscription_bytes")
        ent = bundle.get("entitlement_bytes")
        print(f"  Subscription: {len(sub)}B" if sub else "  Subscription: None", flush=True)
        print(f"  Entitlement: {len(ent)}B" if ent else "  Entitlement: None", flush=True)
        save_entitlement_blobs(bundle)
    else:
        print(f"\nLoading cached entitlement blobs...", flush=True)
        bundle = load_entitlement_blobs()

    # Run full sync
    print(f"\n{'='*60}", flush=True)
    print(f"STARTING FULL BLE SYNC", flush=True)
    print(f"  Files: DeviceSettings + Subscription + RemoteConfig + Entitlement + FeatureFlags + {len(all_ids)} workouts + WorkoutsInfo + SavedWorkouts + UpNext + UserProfile", flush=True)
    print(f"{'='*60}", flush=True)

    ok = await full_sync(
        goggle_mac=goggle_mac,
        all_workout_ids=all_ids,
        all_workout_binaries=all_binaries,
        entitlement_bundle=bundle,
        device_settings_bytes=ds_bytes,
        user_profile_bytes=up_bytes,
        remote_config_bytes=rc_bytes,
        new_workout_id=new_workout_id,
    )

    if ok:
        print(f"\nFull sync succeeded!", flush=True)
        return 0
    else:
        print(f"\nFull sync failed.", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
