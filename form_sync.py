#!/usr/bin/env python3
"""
formgoggles-py: Push custom workouts to FORM swim goggles.

Usage:
  python3 form_sync.py --token TOKEN --goggle-mac AA:BB:CC:DD:EE:FF --workout "10x100 free @moderate 20s rest"
  python3 form_sync.py --token TOKEN --goggle-mac AA:BB:CC:DD:EE:FF --workout "warmup: 200 free easy | main: 10x100 free @mod 20s rest | cooldown: 200 free easy"
  python3 form_sync.py --token TOKEN --goggle-mac AA:BB:CC:DD:EE:FF --workout "4x50 free @fast 15s rest, 8x100 free @mod 20s rest, 4x50 free @easy 10s rest"

Workout string format:
  Sets:       NxDISTm STROKE @EFFORT RESTs rest
  Sections:   warmup: ... | main: ... | cooldown: ...
  Multiple:   set1, set2, set3  (comma-separated within a section)

  N         = interval count (default 1)
  DIST      = distance in meters (required)
  STROKE    = free/back/breast/fly/im/choice (default: free)
  @EFFORT   = easy/moderate/fast/strong/max/descend (default: moderate)
  RESTs     = rest between intervals in seconds (default: 0)

If no warmup/cooldown sections given, auto-generates 200m easy warmup + 200m easy cooldown.
"""

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

import requests

# BLE imports (deferred — only needed for step 4)
BLE_AVAILABLE = False
DBUS_AVAILABLE = False
try:
    from bleak import BleakClient, BleakScanner
    BLE_AVAILABLE = True
except ImportError:
    pass
try:
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib
    DBUS_AVAILABLE = True
except ImportError:
    pass

# Import compiled protobuf modules
# Run: protoc --python_out=. proto/form.proto proto/workout.proto
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "proto"))
try:
    import form_pb2
except ImportError:
    print("ERROR: form_pb2 not found. Run: protoc --python_out=proto proto/form.proto", file=sys.stderr)
    sys.exit(1)

API_BASE = "https://app.formathletica.com/api/v1"
AGENT_PATH = "/formgoggles/agent"

# Direct BLE transfers are ACKed (FILE_TRANSFER_SUCCESS) but the goggles have
# never been observed to commit them — see SYNC_LOG.md. Shown wherever a direct
# push "succeeds" so users don't mistake an ACK for the workout being on-device.
DIRECT_BLE_CAVEAT = (
    "Direct BLE is experimental: the goggles acknowledge the transfer, but workouts "
    "pushed this way are not currently known to appear on the goggles (see SYNC_LOG.md). "
    "The only path known to work is saving to your FORM library (requires FORM Premium) "
    "and syncing with the FORM app."
)

# FORM OAuth client ID (extracted from public APK — not a secret)
OAUTH_BASIC = "YjMzMzMxMTYtYmExNi00NjNiLWFhMWYtNjIxMWE3MDg0YTZkOnlMaGhHbDVSRUpWWWFFaUlrZGl5NXE2bU9Dd3E0a0F0YjZpYmM2elNwMGVHYk5IeENQUVB6YlNJeVh5b0E2cHdHTTZFMklqQWYwcEVpZDY1b0dHRmhBZmY="

# ===== Config File =====

CONFIG_PATH = Path.home() / ".formgoggles.json"
BLOB_CACHE_DIR = Path.home() / ".formgoggles_blobs"


def load_config():
    """Load config from ~/.formgoggles.json. Returns dict or None."""
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_config(data):
    """Save/merge config to ~/.formgoggles.json."""
    existing = load_config() or {}
    existing.update(data)
    CONFIG_PATH.write_text(json.dumps(existing, indent=2) + "\n")
    CONFIG_PATH.chmod(0o600)


def delete_config():
    """Delete ~/.formgoggles.json."""
    try:
        CONFIG_PATH.unlink()
        return True
    except FileNotFoundError:
        return False


def save_entitlement_blobs(bundle):
    BLOB_CACHE_DIR.mkdir(exist_ok=True)
    saved = []
    if bundle.get("subscription_bytes"):
        p = BLOB_CACHE_DIR / "subscription.bin"
        p.write_bytes(bundle["subscription_bytes"])
        saved.append(f"subscription ({len(bundle['subscription_bytes'])}B)")
    if bundle.get("entitlement_bytes"):
        p = BLOB_CACHE_DIR / "entitlement.bin"
        p.write_bytes(bundle["entitlement_bytes"])
        saved.append(f"entitlement ({len(bundle['entitlement_bytes'])}B)")
    if bundle.get("feature_flags"):
        p = BLOB_CACHE_DIR / "feature_flags.json"
        p.write_text(json.dumps(bundle["feature_flags"]))
        saved.append(f"feature_flags ({len(bundle['feature_flags'])} flags)")
    return saved


def load_entitlement_blobs():
    sub_path = BLOB_CACHE_DIR / "subscription.bin"
    ent_path = BLOB_CACHE_DIR / "entitlement.bin"
    flags_path = BLOB_CACHE_DIR / "feature_flags.json"
    bundle = {
        "subscription_bytes": sub_path.read_bytes() if sub_path.exists() else None,
        "entitlement_bytes": ent_path.read_bytes() if ent_path.exists() else None,
        "feature_flags": json.loads(flags_path.read_text()) if flags_path.exists() else None,
    }
    return bundle


# ===== FIT File Parser =====

def _detect_stroke(step_name):
    """Detect stroke type from a FIT workout step name."""
    if not step_name:
        return "freestyle"
    name = step_name.lower()
    for keyword, stroke in [
        ("fly", "butterfly"), ("butterfly", "butterfly"),
        ("back", "backstroke"), ("backstroke", "backstroke"),
        ("breast", "breaststroke"), ("breaststroke", "breaststroke"),
        ("im", "im"),
        ("choice", "choice"),
        ("free", "freestyle"), ("freestyle", "freestyle"),
    ]:
        if keyword in name:
            return stroke
    return "freestyle"


def _speed_to_effort(speed_mm_s):
    """Convert FIT speed target (mm/s) to FORM effort level."""
    if speed_mm_s > 1800:
        return "max"
    elif speed_mm_s > 1500:
        return "strong"
    elif speed_mm_s > 1200:
        return "fast"
    elif speed_mm_s > 900:
        return "moderate"
    return "easy"


def _fit_intensity_to_effort(intensity, target_type=None, target_high=None):
    """Map FIT intensity + target to FORM effort."""
    if intensity in ("warmup", "warm_up"):
        return "easy"
    if intensity in ("cooldown", "cool_down"):
        return "easy"
    if intensity == "rest":
        return "easy"
    # active intensity
    if target_type == "speed" and target_high is not None and target_high > 0:
        return _speed_to_effort(target_high)
    return "moderate"


def _get_field(message, field_name, default=None):
    """Safely get a field value from a fitparse message."""
    field = message.get(field_name)
    if field is not None:
        val = field.value if hasattr(field, 'value') else field
        if val is not None:
            return val
    return default


def parse_fit_file(filepath):
    """Parse a FIT workout file and return (sections, wkt_name).

    sections has the same format as parse_workout_string():
    {"warmup": [...], "main": [...], "cooldown": [...]}
    wkt_name is the workout name from the FIT file, or None.
    """
    try:
        from fitparse import FitFile
    except ImportError:
        print("ERROR: fitparse not installed. Run: pip install fitparse", file=sys.stderr)
        sys.exit(1)

    fitfile = FitFile(filepath)
    fitfile.parse()

    # Check sport type from workout message
    wkt_name = None
    for message in fitfile.get_messages("workout"):
        sport = _get_field(message, "sport")
        wkt_name_val = _get_field(message, "wkt_name")
        if wkt_name_val:
            wkt_name = str(wkt_name_val)
        if sport is not None:
            sport_val = sport if isinstance(sport, int) else getattr(sport, 'raw_value', None)
            if sport_val is not None and sport_val != 5:
                sport_name = str(sport)
                print(f"WARNING: FIT file sport is '{sport_name}' (not swimming). Proceeding anyway.", flush=True)

    # Parse workout steps
    steps = []
    for message in fitfile.get_messages("workout_step"):
        step = {}
        for field in message.fields:
            step[field.name] = field.value
        steps.append(step)

    if not steps:
        print("WARNING: No workout steps found in FIT file.", flush=True)
        return {"warmup": [], "main": [], "cooldown": []}, wkt_name

    # Resolve repeat blocks and build flat list of sets
    raw_sets = _resolve_fit_steps(steps)

    # Categorize into sections
    sections = {"warmup": [], "main": [], "cooldown": []}
    for s in raw_sets:
        section = s.pop("_section", "main")
        sections[section].append(s)

    # If no warmup/cooldown detected, auto-generate
    if not sections["warmup"] and not sections["cooldown"] and sections["main"]:
        sections["warmup"] = [{
            "intervalsCount": 1, "intervalDistance": 200,
            "strokeType": "freestyle", "effort": "easy", "restSeconds": 0,
        }]
        sections["cooldown"] = [{
            "intervalsCount": 1, "intervalDistance": 200,
            "strokeType": "freestyle", "effort": "easy", "restSeconds": 0,
        }]

    return sections, wkt_name


def _resolve_fit_steps(steps):
    """Resolve FIT workout steps (including repeats) into flat set list."""
    result = []
    i = 0
    while i < len(steps):
        step = steps[i]
        duration_type = str(step.get("duration_type", "")).lower().replace(" ", "_")

        if duration_type == "repeat_until_steps_cmplt":
            # Repeat block: wraps steps from duration_step to current index
            repeat_count = step.get("repeat_steps") or step.get("target_value") or 1
            repeat_count = int(repeat_count)
            first_step_idx = step.get("duration_step") or step.get("duration_value") or 0
            first_step_idx = int(first_step_idx)
            # Re-parse the steps in this repeat block
            block_steps = []
            for j in range(first_step_idx, i):
                parsed = _parse_single_fit_step(steps[j])
                if parsed:
                    block_steps.append(parsed)
            # Remove previously added steps that are part of this repeat block
            # Count how many result items came from steps[first_step_idx..i-1]
            to_remove = sum(1 for j in range(first_step_idx, i) if _parse_single_fit_step(steps[j]))
            if to_remove > 0 and to_remove <= len(result):
                result = result[:-to_remove]
            # Attach rest within the block, then set repeat count
            block_sets = _attach_rest_to_sets(block_steps)
            for s in block_sets:
                s["intervalsCount"] = repeat_count
                result.append(s)
            i += 1
            continue

        parsed = _parse_single_fit_step(step)
        if parsed:
            result.append(parsed)
        i += 1

    # Final pass: attach rest steps to previous active steps
    result = _attach_rest_to_sets(result)
    return result


def _parse_single_fit_step(step):
    """Parse a single FIT workout step into an intermediate set dict."""
    duration_type = str(step.get("duration_type", "")).lower().replace(" ", "_")
    intensity = str(step.get("intensity", "active")).lower().replace(" ", "_")

    if duration_type == "repeat_until_steps_cmplt":
        return None

    # Distance — fitparse gives meters directly (not centimeters like raw FIT SDK)
    distance = 0
    if duration_type == "distance":
        raw = step.get("duration_distance") or step.get("duration_value", 0)
        if raw is not None:
            distance = int(float(raw))
    elif duration_type == "time":
        raw = step.get("duration_time") or step.get("duration_value", 0)
        # Time-based step — no distance; estimate or skip
        time_s = float(raw) / 1000 if float(raw) > 1000 else float(raw)
        if intensity == "rest":
            return {
                "_is_rest": True,
                "_rest_seconds": int(time_s),
                "_section": _intensity_to_section(intensity),
            }
        # For active time-based steps, rough estimate: time / 1.2 = meters
        distance = max(25, int(time_s / 1.2))
    elif duration_type == "open":
        distance = 0

    if intensity == "rest":
        if duration_type == "distance" and distance > 0:
            # Distance-based "rest" is actually a recovery swim (e.g. 200m easy)
            step_name = step.get("wkt_step_name", "")
            stroke = _detect_stroke(step_name)
            return {
                "intervalsCount": 1,
                "intervalDistance": distance,
                "strokeType": stroke,
                "effort": "easy",
                "restSeconds": 0,
                "_section": _intensity_to_section(intensity),
                "_is_rest": False,
            }
        # Time-based rest
        rest_seconds = 0
        if duration_type == "time":
            raw = step.get("duration_time") or step.get("duration_value", 0)
            rest_seconds = int(float(raw) / 1000) if float(raw) > 1000 else int(float(raw))
        return {
            "_is_rest": True,
            "_rest_seconds": rest_seconds,
            "_section": _intensity_to_section(intensity),
        }

    # Effort mapping
    target_type = str(step.get("target_type", "open")).lower()
    target_high = step.get("custom_target_value_high")
    if target_high is not None:
        target_high = float(target_high)
    effort = _fit_intensity_to_effort(intensity, target_type, target_high)

    # Stroke from step name
    step_name = step.get("wkt_step_name", "")
    stroke = _detect_stroke(step_name)

    section = _intensity_to_section(intensity)

    return {
        "intervalsCount": 1,
        "intervalDistance": max(distance, 25) if distance > 0 else 100,
        "strokeType": stroke,
        "effort": effort,
        "restSeconds": 0,
        "_section": section,
        "_is_rest": False,
    }


def _intensity_to_section(intensity):
    """Map FIT intensity to workout section."""
    intensity = str(intensity).lower().replace(" ", "_")
    if intensity in ("warmup", "warm_up"):
        return "warmup"
    if intensity in ("cooldown", "cool_down"):
        return "cooldown"
    return "main"


def _attach_rest_to_sets(items):
    """Attach rest steps to the previous active step."""
    result = []
    for item in items:
        if item.get("_is_rest"):
            if result:
                result[-1]["restSeconds"] = item["_rest_seconds"]
        else:
            # Clean up internal keys
            clean = {k: v for k, v in item.items() if not k.startswith("_")}
            clean["_section"] = item.get("_section", "main")
            result.append(clean)
    return result


# ===== Workout String Parser =====

EFFORT_MAP = {
    "easy": "easy", "warm": "easy", "e": "easy",
    "moderate": "moderate", "mod": "moderate", "threshold": "moderate", "m": "moderate",
    "fast": "fast", "hard": "fast", "f": "fast",
    "strong": "strong", "str": "strong",
    "max": "max", "sprint": "max", "all-out": "max", "allout": "max",
    "descend": "descend", "desc": "descend",
}

EFFORT_TO_PROTO = {
    "easy": 1, "moderate": 2, "fast": 3, "max": 6, "descend": 7, "strong": 9,
}

STROKE_MAP = {
    "free": "freestyle", "freestyle": "freestyle", "fr": "freestyle",
    "back": "backstroke", "backstroke": "backstroke", "bk": "backstroke",
    "breast": "breaststroke", "breaststroke": "breaststroke", "br": "breaststroke",
    "fly": "butterfly", "butterfly": "butterfly", "bt": "butterfly",
    "im": "im",
    "choice": "choice", "ch": "choice",
}

INTENSITY_MAP = {
    "easy": "low", "moderate": "moderate", "fast": "high", "strong": "high", "max": "high",
    "descend": "moderate",
}


def parse_set(text):
    """Parse a single set like '10x100 free @moderate 20s rest'."""
    text = text.strip()
    if not text:
        return None

    # Extract rest: "20s rest" or "20s" at end
    rest_seconds = 0
    rest_match = re.search(r'(\d+)\s*s\s*(?:rest)?(?:\s*$)', text, re.IGNORECASE)
    if rest_match:
        rest_seconds = int(rest_match.group(1))
        text = text[:rest_match.start()].strip()

    # Extract effort: "@moderate" or bare "easy"/"fast"/etc.
    effort = "moderate"
    effort_match = re.search(r'@(\S+)', text)
    if effort_match:
        raw = effort_match.group(1).lower()
        effort = EFFORT_MAP.get(raw, "moderate")
        text = text[:effort_match.start()].strip() + " " + text[effort_match.end():].strip()
        text = text.strip()
    else:
        # Check for bare effort words
        for word in text.lower().split():
            if word in EFFORT_MAP:
                effort = EFFORT_MAP[word]
                text = re.sub(r'\b' + word + r'\b', '', text, count=1, flags=re.IGNORECASE).strip()
                break

    # Extract NxDIST
    count = 1
    distance = 100
    nx_match = re.match(r'(\d+)\s*x\s*(\d+)\s*m?', text, re.IGNORECASE)
    if nx_match:
        count = int(nx_match.group(1))
        distance = int(nx_match.group(2))
        text = text[nx_match.end():].strip()
    else:
        dist_match = re.match(r'(\d+)\s*m?', text, re.IGNORECASE)
        if dist_match:
            distance = int(dist_match.group(1))
            text = text[dist_match.end():].strip()

    # Extract stroke from remaining text
    stroke = "freestyle"
    for word in text.lower().split():
        if word in STROKE_MAP:
            stroke = STROKE_MAP[word]
            break

    return {
        "intervalsCount": count,
        "intervalDistance": distance,
        "strokeType": stroke,
        "effort": effort,
        "restSeconds": rest_seconds,
    }


def parse_section(text):
    """Parse comma-separated sets within a section."""
    sets = []
    for part in text.split(","):
        s = parse_set(part)
        if s:
            sets.append(s)
    return sets


def parse_workout_string(workout_str):
    """Parse full workout string into sections.

    Returns dict with keys: warmup, main, cooldown, each containing list of sets.
    """
    workout_str = workout_str.strip()
    sections = {"warmup": [], "main": [], "cooldown": []}

    # Check for section markers (warmup: ... | main: ... | cooldown: ...)
    if re.search(r'\b(warmup|warm-up|wu|main|cooldown|cool-down|cd)\s*:', workout_str, re.IGNORECASE):
        parts = re.split(r'\|', workout_str)
        for part in parts:
            part = part.strip()
            section_match = re.match(r'(warmup|warm-up|wu|main|cooldown|cool-down|cd)\s*:\s*(.*)', part, re.IGNORECASE)
            if section_match:
                section_name = section_match.group(1).lower()
                section_body = section_match.group(2)
                if section_name in ("warmup", "warm-up", "wu"):
                    sections["warmup"] = parse_section(section_body)
                elif section_name == "main":
                    sections["main"] = parse_section(section_body)
                elif section_name in ("cooldown", "cool-down", "cd"):
                    sections["cooldown"] = parse_section(section_body)
            else:
                sections["main"].extend(parse_section(part))
    else:
        # No sections — everything is main, auto-generate warmup/cooldown
        sections["main"] = parse_section(workout_str)
        sections["warmup"] = [{
            "intervalsCount": 1, "intervalDistance": 200,
            "strokeType": "freestyle", "effort": "easy", "restSeconds": 0,
        }]
        sections["cooldown"] = [{
            "intervalsCount": 1, "intervalDistance": 200,
            "strokeType": "freestyle", "effort": "easy", "restSeconds": 0,
        }]

    return sections


def calc_total_distance(sections):
    total = 0
    for sets in sections.values():
        for s in sets:
            total += s["intervalsCount"] * s["intervalDistance"]
    return total


def calc_duration_estimate(sections):
    """Rough duration estimate: ~1.2s/m swim + rest time."""
    total_seconds = 0
    for sets in sections.values():
        for s in sets:
            swim_time = s["intervalsCount"] * s["intervalDistance"] * 1.2
            rest_time = (s["intervalsCount"] - 1) * s["restSeconds"]
            total_seconds += swim_time + rest_time
    return int(total_seconds)


def generate_name(sections):
    """Generate a workout name from the main set."""
    main = sections.get("main", [])
    if not main:
        return "Custom Workout"
    s = main[0]
    stroke_short = {"freestyle": "Free", "backstroke": "Back", "breaststroke": "Breast",
                    "butterfly": "Fly", "im": "IM", "choice": "Choice"}.get(s["strokeType"], "")
    if s["intervalsCount"] > 1:
        return f"{s['intervalsCount']}x{s['intervalDistance']} {stroke_short}"
    return f"{s['intervalDistance']}m {stroke_short}"


def dominant_effort(sections):
    """Most common effort across main sets."""
    efforts = [s["effort"] for s in sections.get("main", [])]
    if not efforts:
        return "moderate"
    return max(set(efforts), key=efforts.count)


# ===== API Payload Builder =====

GROUP_TYPE_MAP = {"warmup": "warmup", "main": "main", "cooldown": "cooldown"}

POOL_LENGTHS = [
    {"distance": 25, "measurement": "m"},
    {"distance": 50, "measurement": "m"},
    {"distance": 25, "measurement": "yd"},
]


def build_api_set(s):
    """Convert parsed set dict to Form API set payload."""
    count = s["intervalsCount"]
    # FORM requires rest to be 0 if only 1 interval
    rest = s["restSeconds"] if count > 1 else 0
    return {
        "strokeType": s["strokeType"],
        "equipment": [],
        "intervalsCount": count,
        "intervalDistance": s["intervalDistance"],
        "effort": {
            "level": s["effort"],
            "pace": None, "percentage": None, "rpeLevel": None, "zone": None,
        },
        "drill": None,
        "restDurationBetweenIntervalsDefined": rest,
        "restDurationAfterDefined": 0,
        "description": "",
        "notes": None,
        "endDrill": None,
        "endStrokeType": None,
        "headCoachFocusMode": None,
        "intervals": [],
        "restDurationBetweenIntervalsTakeoff": None,
    }


def build_api_payload(name, sections):
    """Build the full POST payload for /api/v1/workout_builder/workouts."""
    set_groups = []
    for section_key in ("warmup", "main", "cooldown"):
        sets = sections.get(section_key, [])
        if not sets:
            continue
        set_groups.append({
            "groupType": GROUP_TYPE_MAP[section_key],
            "roundsCount": 1,
            "sets": [build_api_set(s) for s in sets],
            "restDurationAfterDefined": 0,
        })

    distance = calc_total_distance(sections)
    duration_est = calc_duration_estimate(sections)
    intensity = INTENSITY_MAP.get(dominant_effort(sections), "moderate")

    return {
        "name": name[:50],
        "lengthDistances": POOL_LENGTHS,
        "setGroups": set_groups,
        "durationMin": duration_est,
        "categories": ["endurance"],
        "intensityLevel": intensity,
        "description": f"Custom workout — {distance}m total.",
    }


# ===== BLE Protocol =====

def make_command(cmd_type, **kwargs):
    cmd = form_pb2.FormCommandMessage()
    cmd.commandType = cmd_type
    for k, v in kwargs.items():
        setattr(cmd, k, v)
    msg = form_pb2.FormMessage()
    msg.isCommandMessage = True
    msg.data = cmd.SerializeToString()
    return msg.SerializeToString()


def make_form_file(file_type, data, encrypted=False):
    ffm = form_pb2.FormFileMessage()
    ffm.type = file_type
    ffm.data = data
    ffm.isEncrypted = encrypted
    return ffm.SerializeToString()


def make_data_chunk(data_type, file_index, chunk_id, data, crc=0):
    dm = form_pb2.FormDataMessage()
    dm.dataType = data_type
    dm.fileIndex = file_index
    dm.chunkID = chunk_id
    dm.data = data
    dm.crc = crc
    msg = form_pb2.FormMessage()
    msg.isCommandMessage = False
    msg.data = dm.SerializeToString()
    return msg.SerializeToString()


class BLESync:
    """Handles BLE connection and file transfer to FORM goggles."""

    def __init__(self, mac):
        self.mac = mac
        self.received = []
        self.disconnect_requested = False
        self.ready_event = asyncio.Event()
        self.chunk_request_event = asyncio.Event()
        self.last_chunk_requested = -1

    @staticmethod
    def _encode_varint(value):
        """Encode an integer as a protobuf varint."""
        result = []
        while value > 0x7F:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return bytes(result)

    def notification_handler(self, char, data: bytearray):
        raw = bytes(data)
        decoded = "?"
        try:
            fm = form_pb2.FormMessage()
            fm.ParseFromString(raw)
            if fm.isCommandMessage:
                cmd = form_pb2.FormCommandMessage()
                cmd.ParseFromString(fm.data)
                name = form_pb2.FormCommandMessage.CommandType.Name(cmd.commandType)
                decoded = f"CMD: {name}"
                ct = cmd.commandType
                if ct == 24:
                    self.ready_event.set()
                elif ct == 26:
                    self.last_chunk_requested = cmd.chunkID
                    self.chunk_request_event.set()
                elif ct == 25:
                    decoded += " [OK]"
                elif ct in (42, 43, 44, 45, 46):
                    self.disconnect_requested = True
                    decoded += " [DISCONNECT]"
            else:
                dm = form_pb2.FormDataMessage()
                dm.ParseFromString(fm.data)
                name = form_pb2.FormDataMessage.DataType.Name(dm.dataType)
                decoded = f"DATA: {name} {len(dm.data) if dm.data else 0}B"
        except Exception as e:
            decoded = f"ERR: {e}"
        print(f"  <- {decoded}", flush=True)
        self.received.append((raw, decoded))

    async def send_cmd(self, client, char, name, cmd_type, **kwargs):
        data = make_command(cmd_type, **kwargs)
        try:
            await asyncio.wait_for(client.write_gatt_char(char, data, response=False), timeout=5.0)
            return True
        except Exception as e:
            print(f"  ! Send error ({name}): {e}", flush=True)
            return False

    async def send_data(self, client, char, data):
        try:
            await asyncio.wait_for(client.write_gatt_char(char, data, response=False), timeout=5.0)
            return True
        except Exception as e:
            print(f"  ! Data error: {e}", flush=True)
            return False

    async def wait_response(self, timeout=5.0):
        count = len(self.received)
        elapsed = 0
        while elapsed < timeout:
            await asyncio.sleep(0.2)
            elapsed += 0.2
            if len(self.received) > count:
                await asyncio.sleep(0.5)
                return True
        return False

    async def file_transfer(self, client, wc, file_index, file_data, label="", max_chunk=180):
        total_size = len(file_data)
        num_chunks = (total_size + max_chunk - 1) // max_chunk
        print(f"  Sending {label} ({total_size}B, {num_chunks} chunks)...", flush=True)

        self.ready_event.clear()
        await self.send_cmd(client, wc, "FILE_TRANSFER_START", 22,
                            fileIndex=file_index, fileSize=total_size, maxChunkSize=max_chunk)
        try:
            await asyncio.wait_for(self.ready_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

        for chunk_id in range(num_chunks):
            offset = chunk_id * max_chunk
            chunk_data = file_data[offset:offset + max_chunk]
            chunk_msg = make_data_chunk(9, file_index, chunk_id, chunk_data)
            await self.send_data(client, wc, chunk_msg)
            await asyncio.sleep(0.05)

        self.chunk_request_event.clear()
        await self.send_cmd(client, wc, "FILE_TRANSFER_DONE", 23, fileIndex=file_index)
        await self.wait_response(5.0)

        # Handle retransmit requests
        retries = 0
        while self.chunk_request_event.is_set() and retries < 5:
            self.chunk_request_event.clear()
            cid = self.last_chunk_requested
            print(f"  Retransmitting chunk {cid}", flush=True)
            offset = cid * max_chunk
            chunk_data = file_data[offset:offset + max_chunk]
            chunk_msg = make_data_chunk(9, file_index, cid, chunk_data)
            await self.send_data(client, wc, chunk_msg)
            await self.send_cmd(client, wc, "FILE_TRANSFER_DONE", 23, fileIndex=file_index)
            await self.wait_response(3.0)
            retries += 1

    async def push_workout(self, workout_id, workout_binary, duration_est, menu="imports", sync_start="normal", entitlement_bundle=None, entitlement_mode="server"):
        """Push workout files to goggles via BLE."""
        if not BLE_AVAILABLE:
            print("ERROR: bleak library not available. Install with: pip install bleak", flush=True)
            return False

        # Build FormFileMessage payloads
        import time
        now_seconds = int(time.time())

        if menu not in ("imports", "saved", "all"):
            raise ValueError(f"Unsupported direct BLE menu: {menu}")
        if sync_start not in ("normal", "no-ui"):
            raise ValueError(f"Unsupported sync start mode: {sync_start}")

        # 1. WorkoutsInfo (type 7) — register in the target visible category.
        wim = form_pb2.WorkoutsInfoMessage()
        categories = []
        if menu in ("saved", "all"):
            categories.append("standaloneWorkouts")
        if menu in ("imports", "all"):
            categories.append("importedWorkouts")
        if menu == "all":
            categories += ["planWorkouts", "sampleWorkouts", "personalizedWorkouts"]
        for category in categories:
            wi = getattr(wim, category).add()
            wi.id = workout_id
            wi.expectedDuration = duration_est
        wim_file = make_form_file(7, wim.SerializeToString())

        index_files = [(1, wim_file, "WorkoutsInfo")]

        if menu in ("saved", "all"):
            swm = form_pb2.SavedWorkoutsMessage()
            sw = swm.workouts.add()
            sw.id = workout_id
            sw.lastModifiedAt.seconds = now_seconds
            index_files.append((len(index_files) + 1, make_form_file(6, swm.SerializeToString()), "SavedWorkouts"))

        if menu in ("imports", "all"):
            iwm = form_pb2.ImportedWorkoutsInfoMessage()
            # In "all" mode, add both FORM_WORKOUT and CUSTOM_WORKOUT origins
            origins = [1, 2] if menu == "all" else [2]  # 1=FORM_WORKOUT, 2=CUSTOM_WORKOUT
            for origin in origins:
                iw = iwm.workouts.add()
                iw.workoutId = workout_id
                iw.status = 10  # UPCOMING
                iw.origin = origin
                iw.scheduledAt.seconds = now_seconds
                iw.statusLastModified.seconds = now_seconds
                iw.lastSync.seconds = now_seconds
                iw.expiresAt.seconds = now_seconds + 14 * 24 * 60 * 60
            iwm_file = make_form_file(11, iwm.SerializeToString())
            index_files.append((len(index_files) + 1, iwm_file, "ImportedWorkoutsInfo"))

        if menu == "all":
            pim = form_pb2.PlanInfoMessage()
            pim.name = "Custom Plan"
            pim.id = workout_id
            pim.planStartTimeUTC.seconds = now_seconds
            pim.planWeekDuration = 7
            pim.planCurrentWeekCursor = 1
            pw = pim.workouts.add()
            pw.workoutId = workout_id
            pw.planWeekWorkoutId = 1
            pw.weekNumber = 1
            pw.status = 10  # UPCOMING
            index_files.append((len(index_files) + 1, make_form_file(9, pim.SerializeToString()), "PlanInfo"))

        # 5. WorkoutData (type 5)
        wd_file = make_form_file(5, workout_binary)

        # 6. UpNextWorkouts (type 15)
        unm = form_pb2.UpNextWorkoutsMessage()
        up_next_types = []
        if menu in ("saved", "all"):
            up_next_types.append(1)  # STANDALONE
        if menu == "all":
            up_next_types.append(2)  # PLAN
        if menu in ("imports", "all"):
            up_next_types.append(3)  # IMPORT
        if menu == "all":
            up_next_types.append(4)  # HEAD_COACH
        for wtype in up_next_types:
            un = unm.upNextWorkouts.add()
            un.id = workout_id
            un.type = wtype
            un.expectedDuration = duration_est
            if wtype == 2:
                un.planWeekWorkoutId = 1
        unm_file = make_form_file(15, unm.SerializeToString())

        # Entitlement/subscription/feature-flags data (pushed before workout data,
        # matching the order the official FORM app uses during sync).
        entitlement_files = []
        if entitlement_mode == "spoof":
            # Send fabricated unencrypted subscription/entitlement data.
            # Guess at the protobuf schema based on the server's JSON API fields.
            # Subscription: try {isSubscribed: true, expiryDate: far future}
            import time as _t
            far_future = int(_t.time()) + 365 * 24 * 3600
            # field 1 (bool) = true, field 2 (uint64) = far_future timestamp
            sub_proto = b"\x08\x01\x10" + self._encode_varint(far_future)
            entitlement_files.append((
                make_form_file(8, sub_proto, encrypted=False),
                "SubscriptionInfo(spoofed)",
            ))
            # Entitlement: try {isEntitled: true}
            ent_proto = b"\x08\x01"
            entitlement_files.append((
                make_form_file(10, ent_proto, encrypted=False),
                "DeviceEntitlement(spoofed)",
            ))
            # Feature flags: send all known flags
            ff_msg = form_pb2.FeatureFlagsV2Message()
            all_flags = [
                "externalWorkoutBuilder", "plans", "head-coach", "head-coach-v2",
                "headcoach-plan", "headcoach-insights-v2", "swim-straight",
                "swim-straight-launch", "workout-text-parser", "up-next",
                "recommended-focus", "form-score", "training-peaks-mvp",
                "auto-update", "real-time-pace", "manual-heart-rate-zones",
            ]
            for flag_name in all_flags:
                f = ff_msg.featureFlag.add()
                f.name = flag_name
            entitlement_files.append((
                make_form_file(17, ff_msg.SerializeToString()),
                "FeatureFlagsV2(all)",
            ))
        elif entitlement_mode == "none":
            pass  # Skip all entitlement data
        elif entitlement_bundle:
            sub_bytes = entitlement_bundle.get("subscription_bytes")
            if sub_bytes:
                entitlement_files.append((
                    make_form_file(8, sub_bytes, encrypted=True),
                    "SubscriptionInfo",
                ))
            ent_bytes = entitlement_bundle.get("entitlement_bytes")
            if ent_bytes:
                entitlement_files.append((
                    make_form_file(10, ent_bytes, encrypted=True),
                    "DeviceEntitlement",
                ))
            flags = entitlement_bundle.get("feature_flags")
            if flags:
                ff_msg = form_pb2.FeatureFlagsV2Message()
                for flag_name in flags:
                    if len(flag_name) <= 30:
                        f = ff_msg.featureFlag.add()
                        f.name = flag_name
                entitlement_files.append((
                    make_form_file(17, ff_msg.SerializeToString()),
                    "FeatureFlagsV2",
                ))

        # BLE setup — DBus/BlueZ agent only needed on Linux for pairing
        glib_loop = None
        if DBUS_AVAILABLE:
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            bus = dbus.SystemBus()

            class AutoAgent(dbus.service.Object):
                @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
                def Release(self): pass
                @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
                def AuthorizeService(self, d, u): pass
                @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
                def RequestPinCode(self, d): return "0000"
                @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
                def RequestPasskey(self, d): return dbus.UInt32(0)
                @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
                def DisplayPasskey(self, d, p, e): pass
                @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
                def RequestConfirmation(self, d, p): pass
                @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
                def RequestAuthorization(self, d): pass
                @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
                def Cancel(self): pass

            agent = AutoAgent(bus, AGENT_PATH)
            mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
            try:
                mgr.UnregisterAgent(AGENT_PATH)
            except Exception:
                pass
            mgr.RegisterAgent(AGENT_PATH, "DisplayYesNo")
            mgr.RequestDefaultAgent(AGENT_PATH)

            glib_loop = GLib.MainLoop()
            threading.Thread(target=glib_loop.run, daemon=True).start()
            await asyncio.sleep(0.5)
        else:
            print("Note: DBus not available (macOS) — skipping BlueZ agent (not needed if already paired)", flush=True)

        print(f"Scanning for {self.mac}...", flush=True)
        device = await BleakScanner.find_device_by_address(self.mac, timeout=15.0)
        if not device:
            print("ERROR: Goggles not found!", flush=True)
            return False

        client = BleakClient(device)
        success = False
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
                print(f"  Available characteristics: {list(chars.keys())}", flush=True)
                return False

            print("Starting notifications...", flush=True)
            await asyncio.wait_for(client.start_notify(nc, self.notification_handler), timeout=15.0)

            # This only changes the session start command; it does not send
            # entitlement, subscription, feature-flag, or remote-config data.
            start_cmd_name = "SYNC_START_NO_UI" if sync_start == "no-ui" else "SYNC_START"
            start_cmd_type = 30 if sync_start == "no-ui" else 1
            print(f"Starting sync with {start_cmd_name}...", flush=True)
            await self.send_cmd(client, wc, start_cmd_name, start_cmd_type)
            await self.wait_response(3.0)
            if self.disconnect_requested:
                print("ERROR: Goggles requested disconnect", flush=True)
                return False

            # Push entitlement data first (matches official app sync order),
            # then workout indexes, workout payload, and up-next pointer.
            files = []
            for ent_data, ent_label in entitlement_files:
                files.append((len(files) + 1, ent_data, ent_label))
            for idx, fdata, label in index_files:
                files.append((len(files) + 1, fdata, label))
            files.append((len(files) + 1, wd_file, "WorkoutData"))
            files.append((len(files) + 1, unm_file, "UpNextWorkouts"))
            for idx, fdata, label in files:
                await self.file_transfer(client, wc, idx, fdata, label)

            # SYNC_COMPLETE
            await self.send_cmd(client, wc, "SYNC_COMPLETE", 2)
            await self.wait_response(3.0)

            # Count successes
            total = len(files)
            successes = sum(1 for _, d in self.received if "OK" in d)
            print(f"\nBLE sync complete: {successes}/{total} transfers succeeded", flush=True)
            success = successes == total

        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            if glib_loop:
                glib_loop.quit()

        return success

    async def full_sync(self, all_workout_ids, all_workout_binaries, entitlement_bundle,
                        new_workout_id=None, duration_est=300, sync_start="normal"):
        """Full sync that mirrors the official FORM app behavior.

        Sends ALL workouts (not just the new one), plus entitlement data,
        matching the order and completeness of the official app's sync.
        """
        if not BLE_AVAILABLE:
            print("ERROR: bleak library not available", flush=True)
            return False

        import time
        now_seconds = int(time.time())

        files = []

        # 1. Subscription + Entitlement + Feature Flags (app sends these first)
        if entitlement_bundle:
            sub_bytes = entitlement_bundle.get("subscription_bytes")
            if sub_bytes:
                files.append((make_form_file(8, sub_bytes, encrypted=True), "SubscriptionInfo"))
            ent_bytes = entitlement_bundle.get("entitlement_bytes")
            if ent_bytes:
                files.append((make_form_file(10, ent_bytes, encrypted=True), "DeviceEntitlement"))
            flags = entitlement_bundle.get("feature_flags")
            if flags:
                ff_msg = form_pb2.FeatureFlagsV2Message()
                for flag_name in flags:
                    if len(flag_name) <= 30:
                        f = ff_msg.featureFlag.add()
                        f.name = flag_name
                files.append((make_form_file(17, ff_msg.SerializeToString()), "FeatureFlagsV2"))

        # 2. WorkoutsInfo — ALL workouts, registered as standaloneWorkouts
        wim = form_pb2.WorkoutsInfoMessage()
        for wid in all_workout_ids:
            wi = wim.standaloneWorkouts.add()
            wi.id = wid
            wi.expectedDuration = duration_est
        files.append((make_form_file(7, wim.SerializeToString()), "WorkoutsInfo(all)"))

        # 3. SavedWorkouts — ALL workout IDs
        swm = form_pb2.SavedWorkoutsMessage()
        for wid in all_workout_ids:
            sw = swm.workouts.add()
            sw.id = wid
            sw.lastModifiedAt.seconds = now_seconds
        files.append((make_form_file(6, swm.SerializeToString()), "SavedWorkouts(all)"))

        # 4. WorkoutData — binary for EACH workout
        for wid in all_workout_ids:
            binary = all_workout_binaries.get(wid)
            if binary:
                files.append((make_form_file(5, binary), f"WorkoutData({wid[:8]})"))

        # 5. UpNextWorkouts — point to the new workout if specified
        unm = form_pb2.UpNextWorkoutsMessage()
        if new_workout_id:
            un = unm.upNextWorkouts.add()
            un.id = new_workout_id
            un.type = 1  # STANDALONE
            un.expectedDuration = duration_est
        files.append((make_form_file(15, unm.SerializeToString()), "UpNextWorkouts"))

        # BLE connection and transfer
        glib_loop = None
        if DBUS_AVAILABLE:
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            bus = dbus.SystemBus()
            AGENT_PATH = "/com/formgoggles/agent"
            class PairingAgent(dbus.service.Object):
                @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
                def Release(self): pass
                @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
                def RequestConfirmation(self, device, passkey): return
                @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
                def AuthorizeService(self, device): return
            agent = PairingAgent(bus, AGENT_PATH)
            mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
            try:
                mgr.UnregisterAgent(AGENT_PATH)
            except Exception:
                pass
            mgr.RegisterAgent(AGENT_PATH, "DisplayYesNo")
            mgr.RequestDefaultAgent(AGENT_PATH)
            glib_loop = GLib.MainLoop()
            threading.Thread(target=glib_loop.run, daemon=True).start()
            await asyncio.sleep(0.5)
        else:
            print("Note: DBus not available (macOS) — skipping BlueZ agent", flush=True)

        print(f"Scanning for {self.mac}...", flush=True)
        device = await BleakScanner.find_device_by_address(self.mac, timeout=15.0)
        if not device:
            print("ERROR: Goggles not found!", flush=True)
            return False

        client = BleakClient(device)
        success = False
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
            await asyncio.wait_for(client.start_notify(nc, self.notification_handler), timeout=15.0)

            start_cmd_type = 30 if sync_start == "no-ui" else 1
            start_cmd_name = "SYNC_START_NO_UI" if sync_start == "no-ui" else "SYNC_START"
            print(f"Starting sync with {start_cmd_name}...", flush=True)
            await self.send_cmd(client, wc, start_cmd_name, start_cmd_type)
            await self.wait_response(3.0)
            if self.disconnect_requested:
                print("ERROR: Goggles requested disconnect", flush=True)
                return False

            for idx, (fdata, label) in enumerate(files, 1):
                await self.file_transfer(client, wc, idx, fdata, label)

            await self.send_cmd(client, wc, "SYNC_COMPLETE", 2)
            await self.wait_response(3.0)

            total = len(files)
            successes = sum(1 for _, d in self.received if "OK" in d)
            print(f"\nBLE sync complete: {successes}/{total} transfers succeeded", flush=True)
            success = successes == total

        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            if glib_loop:
                glib_loop.quit()

        return success


# ===== API Client =====

class FormAPI:
    def __init__(self, token, refresh_token=None):
        self.token = token
        self.refresh_token = refresh_token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _try_refresh(self):
        """Attempt to refresh the access token."""
        if not self.refresh_token:
            return False
        r = requests.post(f"{API_BASE}/oauth/token/refresh",
                          headers={"Content-Type": "application/json",
                                   "Authorization": f"Basic {OAUTH_BASIC}"},
                          json={"refreshToken": self.refresh_token})
        if r.status_code == 200:
            data = r.json()
            self.token = data["accessToken"]["token"]
            self.refresh_token = data["refreshToken"]["token"]
            self.headers["Authorization"] = f"Bearer {self.token}"
            print(f"Token refreshed (expires {data['accessToken']['expires']})", flush=True)
            # Persist new tokens to config file
            save_config({
                "accessToken": self.token,
                "refreshToken": self.refresh_token,
                "tokenExpires": data["accessToken"]["expires"],
            })
            return True
        return False

    def _request(self, method, url, **kwargs):
        """Make a request with automatic token refresh on 401."""
        r = requests.request(method, url, headers=self.headers, **kwargs)
        if r.status_code == 401 and self._try_refresh():
            r = requests.request(method, url, headers=self.headers, **kwargs)
        return r

    def create_workout(self, payload):
        """Step 1: Create workout on FORM server."""
        r = self._request("POST", f"{API_BASE}/workout_builder/workouts", json=payload)
        if r.status_code not in (200, 201):
            print(f"ERROR: Create workout failed ({r.status_code}): {r.text}", flush=True)
            return None
        data = r.json()
        print(f"Created workout: {data['name']} (ID: {data['id']})", flush=True)
        return data

    def save_workout(self, workout_id, replace_id=None):
        """Step 2: Save workout to user's list. Returns 'ok', 'subscription_required', 'max_reached', or 'error'."""
        body = {"addWorkoutId": workout_id}
        if replace_id:
            body["removeWorkoutId"] = replace_id
        r = self._request("POST", f"{API_BASE}/users/me/workouts", json=body)
        if r.status_code == 200:
            print(f"Saved workout to user list", flush=True)
            return "ok"
        elif r.status_code == 403 and "subscription_required" in r.text.lower():
            print(f"ERROR: FORM Premium is required to save/import workouts to your FORM library.", flush=True)
            print(f"Note: --direct-ble skips this save, but is experimental and not known to work.", flush=True)
            return "subscription_required"
        elif r.status_code == 400 and "max" in r.text.lower():
            print(f"ERROR: Max saved workouts reached. Use --replace-id to swap one out.", flush=True)
            return "max_reached"
        else:
            print(f"ERROR: Save failed ({r.status_code}): {r.text}", flush=True)
            return "error"

    def fetch_protobuf(self, workout_id):
        """Step 3: Fetch server-generated protobuf binary."""
        r = self._request("GET", f"{API_BASE}/users/me/workouts/protobuf",
                          params={"workoutIds": workout_id})
        if r.status_code != 200:
            print(f"ERROR: Fetch protobuf failed ({r.status_code}): {r.text}", flush=True)
            return None
        data = r.json()
        if not data:
            print("ERROR: Empty protobuf response", flush=True)
            return None
        binary = base64.b64decode(data[0]["binary"])
        print(f"Fetched protobuf: {len(binary)}B", flush=True)
        return binary

    def list_saved_workouts(self):
        """List user's saved workouts (useful for finding replace-id)."""
        r = self._request("GET", f"{API_BASE}/users/me/workouts")
        if r.status_code != 200:
            return []
        data = r.json()
        workouts = data if isinstance(data, list) else data.get("workouts", [])
        return workouts

    def fetch_all_protobufs(self, workout_ids):
        """Fetch protobuf binaries for multiple workout IDs at once."""
        if not workout_ids:
            return {}
        ids_str = ",".join(workout_ids)
        r = self._request("GET", f"{API_BASE}/users/me/workouts/protobuf",
                          params={"workoutIds": ids_str})
        if r.status_code != 200:
            return {}
        result = {}
        for item in r.json():
            result[item["id"]] = base64.b64decode(item["binary"])
        return result

    def get_device_id(self):
        """Get the first registered device ID."""
        r = self._request("GET", f"{API_BASE}/users/me/devices")
        if r.status_code != 200:
            return None
        devices = r.json()
        if not devices:
            return None
        return devices[0]["id"]

    def fetch_subscription_protobuf(self):
        """Fetch encrypted subscription protobuf blob from server."""
        r = self._request("GET", f"{API_BASE}/users/me/subscriptions/protobuf")
        if r.status_code != 200:
            return None
        data = r.json()
        b64 = data.get("encryptedBinary", "")
        if not b64:
            return None
        return base64.b64decode(b64)

    def fetch_entitlement_protobuf(self, device_id):
        """Fetch encrypted device entitlement protobuf blob from server."""
        r = self._request("GET", f"{API_BASE}/devices/{device_id}/entitlements/protobuf")
        if r.status_code != 200:
            return None
        data = r.json()
        b64 = data.get("encryptedBinary", "")
        if not b64:
            return None
        return base64.b64decode(b64)

    def fetch_feature_flags(self):
        """Fetch feature flags list from server."""
        r = self._request("GET", f"{API_BASE}/users/me/feature_flags")
        if r.status_code != 200:
            return None
        data = r.json()
        return data.get("enabledFeatures", [])

    def fetch_entitlement_bundle(self):
        """Fetch all entitlement-related data for BLE sync.

        Returns dict with subscription_bytes, entitlement_bytes, and feature_flags list,
        or None values for anything that fails.
        """
        device_id = self.get_device_id()
        if not device_id:
            print("WARNING: No device registered on FORM account", flush=True)

        sub = self.fetch_subscription_protobuf()
        ent = self.fetch_entitlement_protobuf(device_id) if device_id else None
        flags = self.fetch_feature_flags()

        return {
            "subscription_bytes": sub,
            "entitlement_bytes": ent,
            "feature_flags": flags,
        }


# ===== Web UI =====

UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>formgoggles-py</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  background: #111; color: #e0e0e0; min-height: 100vh; display: flex; flex-direction: column;
}
a { color: #00B4D8; }
.container { max-width: 720px; margin: 0 auto; padding: 24px 16px; flex: 1; width: 100%; }
header { text-align: center; margin-bottom: 32px; }
header h1 { font-size: 1.75rem; font-weight: 700; color: #fff; }
header h1 span { font-size: 1.5rem; margin-right: 8px; }
header p { color: #888; margin-top: 4px; font-size: 0.9rem; }
.tabs { display: flex; gap: 0; margin-bottom: 0; border-bottom: 1px solid #333; }
.tab {
  padding: 10px 20px; cursor: pointer; font-size: 0.9rem; color: #888;
  border-bottom: 2px solid transparent; transition: all 0.15s;
}
.tab:hover { color: #ccc; }
.tab.active { color: #00B4D8; border-bottom-color: #00B4D8; }
.tab-content { display: none; padding: 20px 0; }
.tab-content.active { display: block; }
.drop-zone {
  border: 2px dashed #333; border-radius: 12px; padding: 48px 24px; text-align: center;
  cursor: pointer; transition: all 0.2s; position: relative;
}
.drop-zone:hover, .drop-zone.dragover { border-color: #00B4D8; background: rgba(0,180,216,0.05); }
.drop-zone p { color: #888; margin-bottom: 8px; }
.drop-zone .accent { color: #00B4D8; font-weight: 600; }
.drop-zone input[type="file"] {
  position: absolute; inset: 0; opacity: 0; cursor: pointer;
}
textarea {
  width: 100%; background: #1a1a1a; border: 1px solid #333; border-radius: 8px;
  color: #e0e0e0; padding: 12px; font-family: inherit; font-size: 0.95rem;
  resize: vertical; min-height: 60px; transition: border-color 0.15s;
}
textarea:focus { outline: none; border-color: #00B4D8; }
.syntax-ref {
  margin-top: 12px; padding: 12px; background: #1a1a1a; border-radius: 8px;
  font-size: 0.8rem; color: #666; line-height: 1.6;
}
.syntax-ref code { color: #888; background: #222; padding: 1px 5px; border-radius: 3px; }

/* Preview */
.preview { margin-top: 24px; }
.preview h3 { font-size: 1rem; color: #fff; margin-bottom: 12px; }
.preview-card {
  background: #1a1a1a; border-radius: 12px; padding: 20px; border: 1px solid #222;
}
.preview-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
.preview-name {
  background: transparent; border: 1px solid transparent; color: #fff; font-size: 1.1rem;
  font-weight: 600; padding: 4px 8px; border-radius: 6px; font-family: inherit;
}
.preview-name:hover { border-color: #333; }
.preview-name:focus { outline: none; border-color: #00B4D8; }
.preview-stats { font-size: 0.85rem; color: #888; }
.section-label {
  font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em;
  color: #00B4D8; margin: 12px 0 6px;
}
.set-row {
  display: flex; justify-content: space-between; padding: 6px 0;
  font-size: 0.9rem; border-bottom: 1px solid #1f1f1f;
}
.set-row:last-child { border-bottom: none; }
.set-detail { color: #888; font-size: 0.85rem; }

/* Actions */
.actions { margin-top: 24px; display: flex; gap: 12px; flex-wrap: wrap; }
.btn {
  padding: 10px 20px; border-radius: 8px; font-size: 0.9rem; font-weight: 600;
  cursor: pointer; border: none; transition: all 0.15s; font-family: inherit;
}
.btn:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-primary { background: #00B4D8; color: #000; }
.btn-primary:hover:not(:disabled) { background: #00c8ef; }
.btn-secondary { background: #222; color: #e0e0e0; border: 1px solid #333; }
.btn-secondary:hover:not(:disabled) { background: #2a2a2a; border-color: #444; }

/* Progress */
.progress { margin-top: 20px; }
.step {
  display: flex; align-items: center; gap: 10px; padding: 8px 0;
  font-size: 0.9rem; color: #666;
}
.step.active { color: #00B4D8; }
.step.done { color: #4ade80; }
.step.error { color: #f87171; }
.step.warn { color: #fbbf24; }
.step-icon { width: 20px; text-align: center; font-size: 0.8rem; }
.spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid #333; border-top-color: #00B4D8; border-radius: 50%; animation: spin 0.6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Status */
.status-msg {
  margin-top: 16px; padding: 12px 16px; border-radius: 8px; font-size: 0.9rem;
}
.status-msg.success { background: rgba(74,222,128,0.1); color: #4ade80; border: 1px solid rgba(74,222,128,0.2); }
.status-msg.error { background: rgba(248,113,113,0.1); color: #f87171; border: 1px solid rgba(248,113,113,0.2); }
.status-msg.warn { background: rgba(251,191,36,0.1); color: #fbbf24; border: 1px solid rgba(251,191,36,0.2); }

/* Saved workouts */
.saved-workouts { margin-top: 32px; }
.saved-workouts h3 { font-size: 1rem; color: #fff; margin-bottom: 12px; }
.workout-list { display: flex; flex-direction: column; gap: 6px; }
.workout-item {
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 14px; background: #1a1a1a; border-radius: 8px; border: 1px solid #222;
  font-size: 0.85rem; cursor: pointer; transition: border-color 0.15s;
}
.workout-item:hover { border-color: #333; }
.workout-item.selected { border-color: #00B4D8; }
.workout-item .wname { color: #e0e0e0; }
.workout-item .wid { color: #555; font-size: 0.75rem; font-family: monospace; }

footer {
  text-align: center; padding: 24px 16px; font-size: 0.8rem; color: #555;
  border-top: 1px solid #1a1a1a;
}
footer a { color: #666; }

.hidden { display: none !important; }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1><span>🥽</span> formgoggles-py</h1>
    <p>Push swim workouts to your FORM goggles</p>
  </header>

  <div class="tabs">
    <div class="tab active" data-tab="fit">FIT File</div>
    <div class="tab" data-tab="string">Workout String</div>
  </div>

  <div id="tab-fit" class="tab-content active">
    <div class="drop-zone" id="drop-zone">
      <input type="file" id="fit-input" accept=".fit">
      <p><span class="accent">Drop a .fit file here</span> or click to browse</p>
      <p style="font-size:0.8rem;color:#555">Supports TrainingPeaks, Garmin Connect, Final Surge, Today&rsquo;s Plan</p>
    </div>
  </div>

  <div id="tab-string" class="tab-content">
    <textarea id="workout-input" placeholder='e.g. 10x100 free @moderate 20s rest&#10;or: warmup: 200 free easy | main: 8x100 free @fast 15s rest | cooldown: 200 free easy'></textarea>
    <div class="syntax-ref">
      <strong style="color:#888">Quick reference:</strong><br>
      <code>NxDIST stroke @effort RESTs rest</code><br>
      Sections: <code>warmup: ... | main: ... | cooldown: ...</code><br>
      Multiple sets: <code>set1, set2, set3</code> (comma-separated)<br>
      Strokes: <code>free back breast fly im choice</code><br>
      Effort: <code>easy moderate fast strong max descend</code> &mdash; aliases: <code>threshold=moderate hard=fast sprint=max</code>
    </div>
  </div>

  <div id="preview" class="preview hidden">
    <h3>Workout Preview</h3>
    <div class="preview-card">
      <div class="preview-header">
        <input type="text" class="preview-name" id="preview-name" value="">
        <div class="preview-stats" id="preview-stats"></div>
      </div>
      <div id="preview-sections"></div>
    </div>
  </div>

  <div id="actions" class="actions hidden">
    <button class="btn btn-primary" id="btn-sync" onclick="doSync(HAS_BLE)"></button>
    <button class="btn btn-secondary hidden" id="btn-direct" onclick="doSync(true, true)" title="Experimental: transfers are acknowledged but workouts are not currently known to appear on the goggles">Direct BLE (experimental)</button>
  </div>

  <div id="progress" class="progress hidden"></div>
  <div id="status" class="hidden"></div>

  <div id="saved-section" class="saved-workouts">
    <h3>Saved Workouts</h3>
    <div id="workout-list" class="workout-list">
      <p style="color:#555;font-size:0.85rem">Loading...</p>
    </div>
  </div>
</div>

<footer>
  <a href="https://github.com/yourusername/formgoggles-py" target="_blank">GitHub</a>
  &nbsp;&middot;&nbsp; Running locally on your machine &mdash; your credentials never leave this computer.
</footer>

<script>
const HAS_BLE = !!__HAS_BLE__;
let currentSections = null;
let selectedReplaceId = null;

// Tabs
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
  });
});

// Drop zone
const dropZone = document.getElementById('drop-zone');
const fitInput = document.getElementById('fit-input');
['dragenter','dragover'].forEach(e => dropZone.addEventListener(e, ev => { ev.preventDefault(); dropZone.classList.add('dragover'); }));
['dragleave','drop'].forEach(e => dropZone.addEventListener(e, ev => { ev.preventDefault(); dropZone.classList.remove('dragover'); }));
dropZone.addEventListener('drop', ev => { if (ev.dataTransfer.files.length) uploadFit(ev.dataTransfer.files[0]); });
fitInput.addEventListener('change', () => { if (fitInput.files.length) uploadFit(fitInput.files[0]); });

async function uploadFit(file) {
  const form = new FormData();
  form.append('file', file);
  try {
    const r = await fetch('/api/parse-fit', { method: 'POST', body: form });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Parse failed');
    showPreview(data);
  } catch(e) { showStatus('Error: ' + e.message, true); }
}

// Workout string
const wInput = document.getElementById('workout-input');
let parseTimer;
wInput.addEventListener('input', () => { clearTimeout(parseTimer); parseTimer = setTimeout(parseString, 600); });
wInput.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); parseString(); } });

async function parseString() {
  const text = wInput.value.trim();
  if (!text) return;
  try {
    const r = await fetch('/api/parse-string', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({workout: text})
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Parse failed');
    showPreview(data);
  } catch(e) { showStatus('Error: ' + e.message, true); }
}

function showPreview(data) {
  currentSections = data.sections;
  document.getElementById('preview-name').value = data.name;
  document.getElementById('preview-stats').textContent = data.totalDistance + 'm \\u00b7 ~' + Math.floor(data.estimatedDuration/60) + 'min';
  const container = document.getElementById('preview-sections');
  container.innerHTML = '';
  const strokeMap = {freestyle:'Free',backstroke:'Back',breaststroke:'Breast',butterfly:'Fly',im:'IM',choice:'Choice'};
  for (const key of ['warmup','main','cooldown']) {
    const sets = data.sections[key];
    if (!sets || !sets.length) continue;
    container.innerHTML += '<div class="section-label">' + key + '</div>';
    for (const s of sets) {
      const stroke = strokeMap[s.strokeType] || s.strokeType;
      const label = s.intervalsCount > 1 ? s.intervalsCount + 'x' + s.intervalDistance + 'm' : s.intervalDistance + 'm';
      const detail = stroke + ' @' + s.effort + (s.restSeconds ? ' / ' + s.restSeconds + 's rest' : '');
      container.innerHTML += '<div class="set-row"><span>' + label + ' ' + stroke + '</span><span class="set-detail">@' + s.effort + (s.restSeconds ? ' / ' + s.restSeconds + 's rest' : '') + '</span></div>';
    }
  }
  document.getElementById('preview').classList.remove('hidden');
  document.getElementById('actions').classList.remove('hidden');
  document.getElementById('progress').classList.add('hidden');
  document.getElementById('status').classList.add('hidden');
  document.getElementById('btn-sync').textContent = HAS_BLE ? 'Push to Goggles' : 'Create & Save';
  document.getElementById('btn-direct').classList.toggle('hidden', !HAS_BLE);
}

function doSync(withBle, directBle = false) {
  const name = document.getElementById('preview-name').value.trim() || 'Custom Workout';
  const body = { sections: currentSections, name: name, ble: withBle, directBle: directBle };
  if (selectedReplaceId) body.replaceId = selectedReplaceId;
  document.getElementById('btn-sync').disabled = true;
  document.getElementById('btn-direct').disabled = true;
  const prog = document.getElementById('progress');
  prog.classList.remove('hidden');
  prog.innerHTML = '';
  document.getElementById('status').classList.add('hidden');

  const es = new EventSource('/api/sync?' + new URLSearchParams(body).toString());
  // We can't POST with EventSource, so we use a fetch to start the sync and read the stream
  es.close();

  fetch('/api/sync', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  }).then(async response => {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split('\\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const ev = JSON.parse(line.slice(6));
            updateProgress(ev);
          } catch(e) {}
        }
      }
    }
    // Process remaining buffer
    if (buffer.startsWith('data: ')) {
      try { updateProgress(JSON.parse(buffer.slice(6))); } catch(e) {}
    }
  }).catch(e => {
    showStatus('Connection error: ' + e.message, true);
  }).finally(() => {
    document.getElementById('btn-sync').disabled = false;
    document.getElementById('btn-direct').disabled = false;
    loadWorkouts();
  });
}

function updateProgress(ev) {
  const prog = document.getElementById('progress');
  const id = 'step-' + ev.step;
  let el = document.getElementById(id);
  if (!el) {
    el = document.createElement('div');
    el.id = id;
    el.className = 'step';
    prog.appendChild(el);
  }
  if (ev.status === 'done') {
    el.className = 'step done';
    el.innerHTML = '<span class="step-icon">&#10003;</span> ' + ev.message;
    if (ev.workoutId) {
      showStatus('Workout created! ID: ' + ev.workoutId, false);
    }
  } else if (ev.status === 'warn') {
    el.className = 'step warn';
    el.innerHTML = '<span class="step-icon">!</span> ' + ev.message;
    showStatus(ev.detail || ev.message, false, true);
  } else if (ev.status === 'error') {
    el.className = 'step error';
    el.innerHTML = '<span class="step-icon">&#10007;</span> ' + ev.message;
    showStatus(ev.message, true);
  } else {
    el.className = 'step active';
    el.innerHTML = '<span class="step-icon"><span class="spinner"></span></span> ' + ev.message;
  }
}

function showStatus(msg, isError, isWarn = false) {
  const el = document.getElementById('status');
  el.className = 'status-msg ' + (isError ? 'error' : isWarn ? 'warn' : 'success');
  el.textContent = msg;
  el.classList.remove('hidden');
}

// Saved workouts
async function loadWorkouts() {
  try {
    const r = await fetch('/api/workouts');
    const workouts = await r.json();
    const list = document.getElementById('workout-list');
    if (!workouts.length) {
      list.innerHTML = '<p style="color:#555;font-size:0.85rem">No saved workouts</p>';
      return;
    }
    list.innerHTML = '';
    for (const w of workouts) {
      const div = document.createElement('div');
      div.className = 'workout-item' + (selectedReplaceId === w.id ? ' selected' : '');
      div.innerHTML = '<span class="wname">' + (w.name||'Untitled') + '</span><span class="wid">' + (w.id||'').slice(0,8) + '...</span>';
      div.addEventListener('click', () => {
        if (selectedReplaceId === w.id) { selectedReplaceId = null; }
        else { selectedReplaceId = w.id; }
        document.querySelectorAll('.workout-item').forEach(i => i.classList.remove('selected'));
        if (selectedReplaceId) div.classList.add('selected');
      });
      list.appendChild(div);
    }
  } catch(e) {
    document.getElementById('workout-list').innerHTML = '<p style="color:#555;font-size:0.85rem">Could not load workouts</p>';
  }
}

loadWorkouts();
</script>
</body>
</html>"""


def run_ui(args):
    """Start the local web UI."""
    try:
        from flask import Flask, request, Response, jsonify
    except ImportError:
        print("ERROR: Flask is required for the web UI. Install it with:", file=sys.stderr)
        print("  pip install flask>=3.0.0", file=sys.stderr)
        sys.exit(1)

    app = Flask(__name__)
    api = FormAPI(args.token, refresh_token=args.refresh_token)
    has_ble = bool(args.goggle_mac)

    @app.route("/")
    def index():
        html = UI_HTML.replace("__HAS_BLE__", "true" if has_ble else "false")
        return Response(html, content_type="text/html")

    @app.route("/api/parse-fit", methods=["POST"])
    def api_parse_fit():
        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        f = request.files["file"]
        tmp = tempfile.NamedTemporaryFile(suffix=".fit", delete=False)
        try:
            f.save(tmp.name)
            tmp.close()
            sections, wkt_name = parse_fit_file(tmp.name)
            name = wkt_name or generate_name(sections)
            return jsonify({
                "name": name,
                "sections": sections,
                "totalDistance": calc_total_distance(sections),
                "estimatedDuration": calc_duration_estimate(sections),
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    @app.route("/api/parse-string", methods=["POST"])
    def api_parse_string():
        data = request.get_json()
        if not data or not data.get("workout"):
            return jsonify({"error": "No workout string provided"}), 400
        try:
            sections = parse_workout_string(data["workout"])
            name = generate_name(sections)
            return jsonify({
                "name": name,
                "sections": sections,
                "totalDistance": calc_total_distance(sections),
                "estimatedDuration": calc_duration_estimate(sections),
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/sync", methods=["POST"])
    def api_sync():
        data = request.get_json()
        if not data or not data.get("sections"):
            return jsonify({"error": "No workout data"}), 400

        sections = data["sections"]
        name = data.get("name", "Custom Workout")
        with_ble = data.get("ble", False) and has_ble
        direct_ble = data.get("directBle", False) and has_ble
        replace_id = data.get("replaceId")

        def generate():
            # Step 1: Create workout
            yield f"data: {json.dumps({'step': 1, 'status': 'creating', 'message': 'Creating workout on FORM server...'})}\n\n"
            payload = build_api_payload(name, sections)
            workout_data = api.create_workout(payload)
            if not workout_data:
                yield f"data: {json.dumps({'step': 1, 'status': 'error', 'message': 'Failed to create workout on FORM server. Check terminal for details.'})}\n\n"
                return
            workout_id = workout_data["id"]
            yield f"data: {json.dumps({'step': 1, 'status': 'done', 'message': 'Workout created: ' + workout_data.get('name', name)})}\n\n"

            if direct_ble:
                # Direct BLE: skip server save, fetch protobuf immediately, push to goggles.
                yield f"data: {json.dumps({'step': 2, 'status': 'done', 'message': 'Skipped server save (direct BLE mode)'})}\n\n"

                yield f"data: {json.dumps({'step': 3, 'status': 'fetching', 'message': 'Fetching protobuf (pre-save)...'})}\n\n"
                workout_binary = api.fetch_protobuf(workout_id)
                if not workout_binary:
                    yield f"data: {json.dumps({'step': 3, 'status': 'error', 'message': 'Failed to fetch protobuf before saving. The API may have changed.'})}\n\n"
                    return
                yield f"data: {json.dumps({'step': 3, 'status': 'done', 'message': 'Protobuf fetched (' + str(len(workout_binary)) + ' bytes)'})}\n\n"

                yield f"data: {json.dumps({'step': 4, 'status': 'pushing', 'message': 'Pushing directly to goggles via BLE...'})}\n\n"
                try:
                    ble = BLESync(args.goggle_mac)
                    duration_est = calc_duration_estimate(sections)
                    ok = asyncio.run(ble.push_workout(
                        workout_id,
                        workout_binary,
                        duration_est,
                        menu=args.direct_ble_menu,
                        sync_start=args.direct_ble_sync_start,
                    ))
                    if ok:
                        yield f"data: {json.dumps({'step': 5, 'status': 'warn', 'message': 'Transfers acknowledged by goggles, but not confirmed on device (not saved to FORM library)', 'detail': DIRECT_BLE_CAVEAT, 'workoutId': workout_id})}\n\n"
                    else:
                        yield f"data: {json.dumps({'step': 4, 'status': 'error', 'message': 'BLE push failed. The goggles may have rejected the transfer.'})}\n\n"
                except Exception as e:
                    yield f"data: {json.dumps({'step': 4, 'status': 'error', 'message': 'BLE error: ' + str(e)})}\n\n"
                return

            # Standard flow: save to server first
            # Step 2: Save to user list
            yield f"data: {json.dumps({'step': 2, 'status': 'saving', 'message': 'Saving to workout list...'})}\n\n"
            save_result = api.save_workout(workout_id, replace_id=replace_id)
            if save_result != "ok":
                if save_result == "subscription_required":
                    msg = "FORM Premium is required to save workouts to your library, which is currently the only way known to get a workout onto the goggles."
                elif save_result == "max_reached":
                    msg = "Max saved workouts reached (5). Select a workout to replace."
                else:
                    msg = "Failed to save workout. Check terminal for details."
                yield f"data: {json.dumps({'step': 2, 'status': 'error', 'message': msg})}\n\n"
                return
            yield f"data: {json.dumps({'step': 2, 'status': 'done', 'message': 'Saved to workout list'})}\n\n"

            if not with_ble:
                yield f"data: {json.dumps({'step': 5, 'status': 'done', 'message': 'Done! Workout saved on server. Sync via the FORM app or re-run with --goggle-mac for BLE push.', 'workoutId': workout_id})}\n\n"
                return

            # Step 3: Fetch protobuf
            yield f"data: {json.dumps({'step': 3, 'status': 'fetching', 'message': 'Fetching protobuf binary...'})}\n\n"
            workout_binary = api.fetch_protobuf(workout_id)
            if not workout_binary:
                yield f"data: {json.dumps({'step': 3, 'status': 'error', 'message': 'Failed to fetch protobuf'})}\n\n"
                return
            yield f"data: {json.dumps({'step': 3, 'status': 'done', 'message': 'Protobuf fetched (' + str(len(workout_binary)) + ' bytes)'})}\n\n"

            # Step 4: BLE push
            yield f"data: {json.dumps({'step': 4, 'status': 'pushing', 'message': 'Pushing to goggles via BLE...'})}\n\n"
            try:
                ble = BLESync(args.goggle_mac)
                duration_est = calc_duration_estimate(sections)
                ok = asyncio.run(ble.push_workout(workout_id, workout_binary, duration_est))
                if ok:
                    yield f"data: {json.dumps({'step': 5, 'status': 'done', 'message': 'Done! Workout is on your goggles.', 'workoutId': workout_id})}\n\n"
                else:
                    yield f"data: {json.dumps({'step': 4, 'status': 'error', 'message': 'BLE push had issues. Workout is saved on server (ID: ' + workout_id + ').'})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'step': 4, 'status': 'error', 'message': 'BLE error: ' + str(e)})}\n\n"

        return Response(generate(), content_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.route("/api/workouts")
    def api_workouts():
        workouts = api.list_saved_workouts()
        return jsonify([{"id": w.get("id", ""), "name": w.get("name", "Untitled"), "origin": w.get("origin", "")} for w in workouts])

    print(f"Starting web UI at http://localhost:5050", flush=True)
    print(f"BLE push: {'enabled (' + args.goggle_mac + ')' if has_ble else 'disabled (no --goggle-mac)'}", flush=True)
    webbrowser.open("http://localhost:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)


# ===== Main =====

def print_workout_plan(name, sections):
    """Pretty-print the parsed workout."""
    total = calc_total_distance(sections)
    dur = calc_duration_estimate(sections)
    print(f"\n{'='*50}", flush=True)
    print(f"  {name}", flush=True)
    print(f"  {total}m | ~{dur//60}min", flush=True)
    print(f"{'='*50}", flush=True)
    for section_key in ("warmup", "main", "cooldown"):
        sets = sections.get(section_key, [])
        if not sets:
            continue
        label = section_key.upper()
        print(f"\n  {label}:", flush=True)
        for s in sets:
            stroke_short = {"freestyle": "Free", "backstroke": "Back", "breaststroke": "Breast",
                            "butterfly": "Fly", "im": "IM", "choice": "Choice"}.get(s["strokeType"], "?")
            effort = s["effort"]
            rest_str = f" / {s['restSeconds']}s rest" if s["restSeconds"] else ""
            if s["intervalsCount"] > 1:
                print(f"    {s['intervalsCount']}x{s['intervalDistance']}m {stroke_short} @{effort}{rest_str}", flush=True)
            else:
                print(f"    {s['intervalDistance']}m {stroke_short} @{effort}{rest_str}", flush=True)
    print(flush=True)


async def run(args):
    # Parse workout from FIT file or string
    if args.fit_file:
        sections, wkt_name = parse_fit_file(args.fit_file)
        name = args.name or wkt_name or generate_name(sections)
    else:
        sections = parse_workout_string(args.workout)
        name = args.name or generate_name(sections)
    print_workout_plan(name, sections)

    api = FormAPI(args.token, refresh_token=args.refresh_token)
    direct_ble = getattr(args, "direct_ble", False)

    if direct_ble:
        total_steps = 4
        step_label = lambda n: f"Step {n}/{total_steps}"

        # Step 1: Create on server
        print(f"{step_label(1)}: Creating workout on FORM server...", flush=True)
        payload = build_api_payload(name, sections)
        workout_data = api.create_workout(payload)
        if not workout_data:
            return 1
        workout_id = workout_data["id"]

        # Step 2: Fetch protobuf before the Premium-gated library save.
        print(f"\n{step_label(2)}: Fetching protobuf (pre-save)...", flush=True)
        workout_binary = api.fetch_protobuf(workout_id)
        if not workout_binary:
            print("ERROR: Could not fetch protobuf before saving. The API may have changed.", flush=True)
            return 1

        # Step 3: Fetch entitlement/subscription/feature-flags data from server.
        ent_mode = getattr(args, "entitlement_mode", "server")
        bundle = None
        if ent_mode == "server":
            print(f"\n{step_label(3)}: Fetching entitlement data from server...", flush=True)
            bundle = api.fetch_entitlement_bundle()
            sub = bundle["subscription_bytes"]
            ent = bundle["entitlement_bytes"]
            flags_count = len(bundle["feature_flags"]) if bundle["feature_flags"] else 0
            print(f"  Subscription protobuf: {'yes' if sub else 'no'}{f' ({len(sub)}B)' if sub else ''}", flush=True)
            if sub:
                print(f"    hex: {sub.hex()}", flush=True)
            print(f"  Entitlement protobuf: {'yes' if ent else 'no'}{f' ({len(ent)}B)' if ent else ''}", flush=True)
            if ent:
                print(f"    hex: {ent.hex()}", flush=True)
            print(f"  Feature flags: {flags_count}", flush=True)
            saved = save_entitlement_blobs(bundle)
            if saved:
                print(f"  Cached to {BLOB_CACHE_DIR}: {', '.join(saved)}", flush=True)
        elif ent_mode == "cached":
            print(f"\n{step_label(3)}: Loading cached entitlement blobs from {BLOB_CACHE_DIR}...", flush=True)
            bundle = load_entitlement_blobs()
            sub_ok = f"{len(bundle['subscription_bytes'])}B" if bundle["subscription_bytes"] else "MISSING"
            ent_ok = f"{len(bundle['entitlement_bytes'])}B" if bundle["entitlement_bytes"] else "MISSING"
            flags_count = len(bundle["feature_flags"]) if bundle["feature_flags"] else 0
            print(f"  Subscription blob: {sub_ok}", flush=True)
            print(f"  Entitlement blob: {ent_ok}", flush=True)
            print(f"  Feature flags: {flags_count}", flush=True)
            if not bundle["subscription_bytes"] and not bundle["entitlement_bytes"]:
                print("  WARNING: No cached blobs found! Run with --entitlement-mode server while premium is active first.", flush=True)
        elif ent_mode == "spoof":
            print(f"\n{step_label(3)}: Using spoofed subscription data (unencrypted)...", flush=True)
        else:
            print(f"\n{step_label(3)}: Skipping entitlement data...", flush=True)

        # Step 4: Push directly to goggles via BLE
        print(f"\n{step_label(4)}: Pushing to goggles via BLE ({args.goggle_mac})...", flush=True)
        ble = BLESync(args.goggle_mac)
        duration_est = calc_duration_estimate(sections)
        push_mode = "server" if ent_mode == "cached" else ent_mode
        ok = await ble.push_workout(
            workout_id,
            workout_binary,
            duration_est,
            menu=args.direct_ble_menu,
            sync_start=args.direct_ble_sync_start,
            entitlement_bundle=bundle,
            entitlement_mode=push_mode,
        )

        if ok:
            print(f"\nGoggles acknowledged all transfers for '{name}'.", flush=True)
            print("Note: This workout is NOT saved to your FORM library.", flush=True)
            print(f"Warning: {DIRECT_BLE_CAVEAT}", flush=True)
            print("Check what's actually on the goggles with: python3 read_goggle_indexes.py --target imports", flush=True)
            return 0
        else:
            print(f"\nBLE push failed. The goggles may have rejected the transfer.", flush=True)
            print(f"Workout ID: {workout_id}", flush=True)
            return 1

    # Standard flow (requires Premium for server save)
    # Step 1: Create on server
    print("Step 1/4: Creating workout on FORM server...", flush=True)
    payload = build_api_payload(name, sections)
    workout_data = api.create_workout(payload)
    if not workout_data:
        return 1

    workout_id = workout_data["id"]

    # Step 2: Save to user's workout list
    print("\nStep 2/4: Saving to user's workout list...", flush=True)
    save_result = api.save_workout(workout_id, replace_id=args.replace_id)
    if save_result != "ok":
        if save_result == "max_reached" and not args.replace_id:
            print("\nSaved workouts:", flush=True)
            for w in api.list_saved_workouts():
                wid = w.get("id", "?")
                wname = w.get("name", "?")
                print(f"  {wid}  {wname}", flush=True)
            print("\nRe-run with --replace-id <ID> to swap one out.", flush=True)
        return 1

    # Step 3: Fetch server protobuf
    print("\nStep 3/4: Fetching server-generated protobuf...", flush=True)
    workout_binary = api.fetch_protobuf(workout_id)
    if not workout_binary:
        return 1

    # Step 4: Push to goggles
    if args.no_ble:
        print("\nStep 4/4: Skipping BLE push (--no-ble)", flush=True)
        print(f"\nWorkout ID: {workout_id}", flush=True)
        print("Workout is saved on server. Sync via FORM app or re-run without --no-ble.", flush=True)
        return 0

    print(f"\nStep 4/4: Pushing to goggles ({args.goggle_mac})...", flush=True)
    ble = BLESync(args.goggle_mac)
    duration_est = calc_duration_estimate(sections)
    ok = await ble.push_workout(workout_id, workout_binary, duration_est)

    if ok:
        print(f"\nDone! Workout '{name}' is on your goggles.", flush=True)
        return 0
    else:
        print(f"\nBLE push had issues. Workout is saved on server (ID: {workout_id}).", flush=True)
        return 1


def cmd_login(email, password):
    """Authenticate with FORM API and print tokens. No subscription required."""
    r = requests.post(
        f"{API_BASE}/oauth/token",
        headers={"Authorization": f"Basic {OAUTH_BASIC}", "Content-Type": "application/json"},
        json={"email": email, "password": password},
        timeout=10,
    )
    if r.status_code != 200:
        print(f"Login failed ({r.status_code}): {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    access = data["accessToken"]
    refresh = data["refreshToken"]
    print(f"accessToken:  {access['token']}")
    print(f"  expires:    {access['expires']}")
    print(f"refreshToken: {refresh['token']}")
    print(f"  expires:    {refresh['expires']}")
    print(f"\nPass to form_sync.py:")
    print(f"  --token {access['token']} --refresh-token {refresh['token']}")
    return 0


def cmd_setup():
    """Interactive setup wizard."""
    print("\n\U0001f97d formgoggles-py setup")
    print("=" * 24)
    print("This will save your FORM credentials locally so you don't need to pass flags every time.\n")

    email = input("FORM email: ").strip()
    if not email:
        print("Aborted.", file=sys.stderr)
        return 1

    try:
        import getpass as _gp
        password = _gp.getpass("FORM password: ")
    except (EOFError, ImportError):
        print("(password will be visible — not running in a terminal)")
        password = input("FORM password: ")

    if not password:
        print("Aborted.", file=sys.stderr)
        return 1

    # Authenticate
    r = requests.post(
        f"{API_BASE}/oauth/token",
        headers={"Authorization": f"Basic {OAUTH_BASIC}", "Content-Type": "application/json"},
        json={"email": email, "password": password},
        timeout=10,
    )
    if r.status_code != 200:
        print(f"\nLogin failed ({r.status_code}): {r.text}", file=sys.stderr)
        return 1

    data = r.json()
    access = data["accessToken"]
    refresh = data["refreshToken"]
    print(f"\u2713 Authenticated as {email}")

    config = {
        "accessToken": access["token"],
        "refreshToken": refresh["token"],
        "tokenExpires": access["expires"],
        "email": email,
    }

    # BLE setup
    goggle_mac = None
    ble_answer = input("\nDo you want to set up Bluetooth LE push? (requires goggles nearby) [y/N]: ").strip().lower()
    if ble_answer in ("y", "yes"):
        try:
            from bleak import BleakScanner as _Scanner
        except ImportError:
            print("BLE libraries not installed. Install with: sudo apt install python3-dbus && pip install bleak")
            print("Skipping BLE setup.")
            _Scanner = None

        if _Scanner is not None:
            print("\nTurn on your goggles and make sure they're not connected to your phone.")
            print("Scanning for FORM goggles...", flush=True)

            async def _scan():
                devices = await _Scanner.discover(timeout=15)
                found = []
                for d in devices:
                    name = d.name or ""
                    # Match FORM devices by name or service UUID prefix
                    uuids = [str(u) for u in (d.metadata.get("uuids", []) if hasattr(d, 'metadata') and d.metadata else [])]
                    if "form" in name.lower() or any(u.startswith("00012000") for u in uuids):
                        found.append(d)
                return found

            found = asyncio.run(_scan())
            if found:
                for i, d in enumerate(found):
                    print(f"  Found: {d.name or 'Unknown'} ({d.address})")
                if len(found) == 1:
                    confirm = input(f"Use this device? [Y/n]: ").strip().lower()
                    if confirm in ("", "y", "yes"):
                        goggle_mac = found[0].address
                else:
                    choice = input(f"Enter device number (1-{len(found)}): ").strip()
                    try:
                        idx = int(choice) - 1
                        if 0 <= idx < len(found):
                            goggle_mac = found[idx].address
                    except ValueError:
                        pass
                if goggle_mac:
                    print(f"\u2713 Goggle MAC: {goggle_mac}")
            else:
                print("No FORM goggles found. You can add your goggle MAC later by running --setup again or editing ~/.formgoggles.json")

    if goggle_mac:
        config["goggleMac"] = goggle_mac

    save_config(config)

    print(f"\n\u2713 Setup complete! Config saved to {CONFIG_PATH}")
    print(f"\nYou can now run:")
    print(f"  python3 form_sync.py --ui                    # Web UI")
    print(f"  python3 form_sync.py --fit-file workout.fit   # FIT file import")
    print(f"  python3 form_sync.py --workout \"10x100 free\"  # Workout string")
    print(f"\nNo need to pass --token or --goggle-mac anymore.")
    return 0


def cmd_config():
    """Print current config (tokens masked)."""
    config = load_config()
    if not config:
        print(f"No config found at {CONFIG_PATH}")
        print("Run 'python3 form_sync.py --setup' to get started.")
        return 1

    def mask(val):
        if not val or len(val) < 12:
            return val
        return val[:6] + "..." + val[-4:]

    print(f"Config: {CONFIG_PATH}\n")
    if config.get("email"):
        print(f"  email:        {config['email']}")
    if config.get("accessToken"):
        print(f"  accessToken:  {mask(config['accessToken'])}")
    if config.get("tokenExpires"):
        print(f"  tokenExpires: {config['tokenExpires']}")
    if config.get("refreshToken"):
        print(f"  refreshToken: {mask(config['refreshToken'])}")
    if config.get("goggleMac"):
        print(f"  goggleMac:    {config['goggleMac']}")
    else:
        print(f"  goggleMac:    (not set)")
    return 0


def cmd_logout():
    """Delete config file."""
    if delete_config():
        print(f"Logged out. Deleted {CONFIG_PATH}")
    else:
        print(f"No config file found at {CONFIG_PATH}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="formgoggles-py: Push custom workouts to FORM swim goggles",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --login your@email.com yourpassword
  %(prog)s --token TOKEN --goggle-mac AA:BB:CC:DD:EE:FF --workout "10x100 free @moderate 20s rest"
  %(prog)s --token TOKEN --goggle-mac AA:BB:CC:DD:EE:FF --workout "warmup: 200 free easy | main: 8x100 free @fast 15s rest | cooldown: 200 free easy"
  %(prog)s --token TOKEN --goggle-mac AA:BB:CC:DD:EE:FF --workout "10x100 free @mod 20s rest" --direct-ble
  %(prog)s --token TOKEN --workout "5x200 free @mod 30s rest" --no-ble
  %(prog)s --token TOKEN --workout "10x50 fly @max 30s rest" --no-ble --name "Sprint Fly"
  %(prog)s --token TOKEN --goggle-mac AA:BB:CC:DD:EE:FF --fit-file workout.fit
  %(prog)s --token TOKEN --fit-file ~/Downloads/swim-workout.fit --no-ble
  %(prog)s --token TOKEN --ui
  %(prog)s --token TOKEN --goggle-mac AA:BB:CC:DD:EE:FF --ui
        """
    )
    parser.add_argument("--setup", action="store_true", help="Run interactive setup wizard")
    parser.add_argument("--config", action="store_true", help="Print current saved config")
    parser.add_argument("--logout", action="store_true", help="Delete saved config (~/.formgoggles.json)")
    parser.add_argument("--login", nargs=2, metavar=("EMAIL", "PASSWORD"),
                        help="Get a bearer token from your FORM credentials (no subscription required)")
    parser.add_argument("--token", help="FORM API bearer token")
    parser.add_argument("--refresh-token", help="FORM API refresh token (auto-refreshes on 401)")
    parser.add_argument("--goggle-mac", help="Goggles BLE MAC address (e.g. AA:BB:CC:DD:EE:FF)")
    workout_group = parser.add_mutually_exclusive_group()
    workout_group.add_argument("--workout", help="Workout description string")
    workout_group.add_argument("--fit-file", help="Path to a FIT workout file (.fit)")
    parser.add_argument("--name", help="Workout name (auto-generated if omitted)")
    parser.add_argument("--replace-id", help="Workout ID to remove when saving (if at max)")
    parser.add_argument("--no-ble", action="store_true", help="Skip BLE push (create + save on server only)")
    parser.add_argument("--direct-ble", action="store_true",
                        help="Experimental: push workout to goggles without saving to FORM library")
    parser.add_argument("--direct-ble-menu", choices=("imports", "saved", "all"), default="imports",
                        help="Visible goggles menu to target in direct BLE mode (default: imports)")
    parser.add_argument("--direct-ble-sync-start", choices=("normal", "no-ui"), default="normal",
                        help="BLE session start command for direct BLE mode (default: normal)")
    parser.add_argument("--entitlement-mode", choices=("server", "cached", "spoof", "none"), default="server",
                        help="Entitlement data: server=fetch from API (saves to cache), cached=replay saved blobs, spoof=fabricated, none=skip")
    parser.add_argument("--list-workouts", action="store_true", help="List saved workouts and exit")
    parser.add_argument("--ui", action="store_true", help="Start local web UI at http://localhost:5050")

    args = parser.parse_args()

    # --setup / --config / --logout: standalone commands
    if args.setup:
        return cmd_setup()
    if args.config:
        return cmd_config()
    if args.logout:
        return cmd_logout()

    # --login: get a token and exit
    if args.login:
        return cmd_login(args.login[0], args.login[1])

    # Load config file — CLI flags override config values
    config = load_config()
    if not args.token:
        if config and config.get("accessToken"):
            args.token = config["accessToken"]
        else:
            print("No config found. Run 'python3 form_sync.py --setup' to get started, or pass --token.")
            return 1
    if not args.refresh_token and config and config.get("refreshToken"):
        args.refresh_token = config["refreshToken"]
    if not args.goggle_mac and config and config.get("goggleMac"):
        args.goggle_mac = config["goggleMac"]

    if args.ui:
        return run_ui(args)

    if args.list_workouts:
        api = FormAPI(args.token, refresh_token=args.refresh_token)
        workouts = api.list_saved_workouts()
        print(f"\nSaved workouts ({len(workouts)}):", flush=True)
        for w in workouts:
            wid = w.get("id", "?")
            wname = w.get("name", "?")
            origin = w.get("origin", "?")
            print(f"  {wid}  {wname}  ({origin})", flush=True)
        return 0

    if not args.workout and not args.fit_file:
        parser.error("--workout or --fit-file is required")

    if args.direct_ble:
        if args.no_ble:
            parser.error("--direct-ble and --no-ble are mutually exclusive")
        if not args.goggle_mac:
            parser.error("--direct-ble requires --goggle-mac (or set it via --setup)")

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main() or 0)
