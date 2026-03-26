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
import os
import re
import sys
import threading

import requests

# BLE imports (deferred — only needed for step 4)
BLE_AVAILABLE = True
try:
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib
    from bleak import BleakClient, BleakScanner
except ImportError:
    BLE_AVAILABLE = False

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

# FORM OAuth client ID (extracted from public APK — not a secret)
OAUTH_BASIC = "YjMzMzMxMTYtYmExNi00NjNiLWFhMWYtNjIxMWE3MDg0YTZkOnlMaGhHbDVSRUpWWWFFaUlrZGl5NXE2bU9Dd3E0a0F0YjZpYmM2elNwMGVHYk5IeENQUVB6YlNJeVh5b0E2cHdHTTZFMklqQWYwcEVpZDY1b0dHRmhBZmY="

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
            # Repeat block: wraps steps from duration_value to current index
            repeat_count = int(step.get("target_value", 1))
            first_step_idx = int(step.get("duration_value", 0))
            # Find the steps in result that correspond to first_step_idx..i-1
            block_steps = []
            for j in range(first_step_idx, i):
                parsed = _parse_single_fit_step(steps[j])
                if parsed:
                    block_steps.append(parsed)
            # Remove previously added steps that are part of this repeat block
            # (they were added with count=1, now we replace with repeat_count)
            to_remove = i - first_step_idx
            result = result[:-to_remove] if to_remove <= len(result) else result
            # Attach rest from rest steps to previous active steps within block
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

    # Distance (in centimeters → meters)
    distance = 0
    if duration_type == "distance":
        raw = step.get("duration_distance") or step.get("duration_value", 0)
        if raw is not None:
            distance = int(float(raw) / 100) if float(raw) > 100 else int(raw)
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
        rest_seconds = 0
        if duration_type == "distance" and distance > 0:
            rest_seconds = int(distance * 1.2)
        elif duration_type == "time":
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
    "easy": "easy", "moderate": "moderate", "fast": "hard", "strong": "hard", "max": "hard",
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
    return {
        "strokeType": s["strokeType"],
        "equipment": [],
        "intervalsCount": s["intervalsCount"],
        "intervalDistance": s["intervalDistance"],
        "effort": {
            "level": s["effort"],
            "pace": None, "percentage": None, "rpeLevel": None, "zone": None,
        },
        "drill": None,
        "restDurationBetweenIntervalsDefined": s["restSeconds"],
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
        "name": name,
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


def make_form_file(file_type, data):
    ffm = form_pb2.FormFileMessage()
    ffm.type = file_type
    ffm.data = data
    ffm.isEncrypted = False
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

    async def push_workout(self, workout_id, workout_binary, duration_est):
        """Push workout files to goggles via BLE."""
        if not BLE_AVAILABLE:
            print("ERROR: BLE libraries not available (bleak, dbus, gi)", flush=True)
            return False

        # Build FormFileMessage payloads
        wim = form_pb2.WorkoutsInfoMessage()
        wi = wim.importedWorkouts.add()
        wi.id = workout_id
        wi.expectedDuration = duration_est
        wim_file = make_form_file(7, wim.SerializeToString())

        iwm = form_pb2.ImportedWorkoutsInfoMessage()
        iw = iwm.workouts.add()
        iw.workoutId = workout_id
        iw.status = 10  # UPCOMING
        iw.origin = 2   # CUSTOM_WORKOUT
        iwm_file = make_form_file(11, iwm.SerializeToString())

        wd_file = make_form_file(5, workout_binary)

        unm = form_pb2.UpNextWorkoutsMessage()
        un = unm.upNextWorkouts.add()
        un.id = workout_id
        un.type = 1  # STANDALONE
        un.expectedDuration = duration_est
        unm_file = make_form_file(15, unm.SerializeToString())

        # BLE setup
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

            chars = {}
            for svc in client.services:
                for char in svc.characteristics:
                    chars[char.uuid.split("-")[0]] = char

            wc = chars.get("00012001")
            nc = chars.get("00012000")
            if not wc or not nc:
                print("ERROR: Required BLE characteristics not found!", flush=True)
                return False

            await asyncio.wait_for(client.start_notify(nc, self.notification_handler), timeout=5.0)

            # SYNC_START
            await self.send_cmd(client, wc, "SYNC_START", 1)
            await self.wait_response(3.0)
            if self.disconnect_requested:
                print("ERROR: Goggles requested disconnect", flush=True)
                return False

            # Push 4 files
            await self.file_transfer(client, wc, 1, wim_file, "WorkoutsInfo")
            await self.file_transfer(client, wc, 2, iwm_file, "ImportedWorkoutsInfo")
            await self.file_transfer(client, wc, 3, wd_file, "WorkoutData")
            await self.file_transfer(client, wc, 4, unm_file, "UpNextWorkouts")

            # SYNC_COMPLETE
            await self.send_cmd(client, wc, "SYNC_COMPLETE", 2)
            await self.wait_response(3.0)

            # Count successes
            successes = sum(1 for _, d in self.received if "OK" in d)
            print(f"\nBLE sync complete: {successes}/4 transfers succeeded", flush=True)
            success = successes == 4

        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
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
        if r.status_code != 200:
            print(f"ERROR: Create workout failed ({r.status_code}): {r.text}", flush=True)
            return None
        data = r.json()
        print(f"Created workout: {data['name']} (ID: {data['id']})", flush=True)
        return data

    def save_workout(self, workout_id, replace_id=None):
        """Step 2: Save workout to user's list."""
        body = {"addWorkoutId": workout_id}
        if replace_id:
            body["removeWorkoutId"] = replace_id
        r = self._request("POST", f"{API_BASE}/users/me/workouts", json=body)
        if r.status_code == 200:
            print(f"Saved workout to user list", flush=True)
            return True
        elif r.status_code == 400 and "max" in r.text.lower():
            print(f"ERROR: Max saved workouts reached. Use --replace-id to swap one out.", flush=True)
            return False
        else:
            print(f"ERROR: Save failed ({r.status_code}): {r.text}", flush=True)
            return False

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

    # Step 1: Create on server
    print("Step 1/4: Creating workout on FORM server...", flush=True)
    payload = build_api_payload(name, sections)
    workout_data = api.create_workout(payload)
    if not workout_data:
        return 1

    workout_id = workout_data["id"]

    # Step 2: Save to user's workout list
    print("\nStep 2/4: Saving to user's workout list...", flush=True)
    if not api.save_workout(workout_id, replace_id=args.replace_id):
        if not args.replace_id:
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


def main():
    parser = argparse.ArgumentParser(
        description="formgoggles-py: Push custom workouts to FORM swim goggles",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --login your@email.com yourpassword
  %(prog)s --token TOKEN --goggle-mac AA:BB:CC:DD:EE:FF --workout "10x100 free @moderate 20s rest"
  %(prog)s --token TOKEN --goggle-mac AA:BB:CC:DD:EE:FF --workout "warmup: 200 free easy | main: 8x100 free @fast 15s rest | cooldown: 200 free easy"
  %(prog)s --token TOKEN --workout "5x200 free @mod 30s rest" --no-ble
  %(prog)s --token TOKEN --workout "10x50 fly @max 30s rest" --no-ble --name "Sprint Fly"
  %(prog)s --token TOKEN --goggle-mac AA:BB:CC:DD:EE:FF --fit-file workout.fit
  %(prog)s --token TOKEN --fit-file ~/Downloads/swim-workout.fit --no-ble
        """
    )
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
    parser.add_argument("--list-workouts", action="store_true", help="List saved workouts and exit")

    args = parser.parse_args()

    # --login: get a token and exit
    if args.login:
        return cmd_login(args.login[0], args.login[1])

    # All other commands require --token
    if not args.token:
        parser.error("--token is required (or use --login EMAIL PASSWORD to get one)")

    if args.list_workouts:
        api = FormAPI(args.token, refresh_token=getattr(args, 'refresh_token', None))
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

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main() or 0)
