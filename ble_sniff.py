#!/usr/bin/env python3
"""
BLE traffic logger for FORM goggles.

Passively monitors BLE traffic between the official FORM app and goggles
during a sync session. Captures and decodes all FormFileMessage payloads,
saving raw binaries to disk for later analysis with protoc --decode_raw.

Usage:
  1. Disconnect the FORM app from your goggles (turn off phone BT)
  2. Run this script — it connects to the goggles and waits
  3. Turn phone BT back on and trigger a sync from the FORM app
  4. The script logs all traffic it sees on the notify characteristic

  python3 ble_sniff.py --goggle-mac AA:BB:CC:DD:EE:FF
  python3 ble_sniff.py  # uses MAC from ~/.formgoggles.json

Note: This is a passive listener on the goggles' notify characteristic.
It captures what the goggles send back (responses, acks, file info) during
a sync initiated by the official app. For full capture of what the app
sends TO the goggles, you'd need a BLE sniffer (nRF Sniffer + Wireshark).

However, this tool can also operate in "intercept" mode: it connects to
the goggles BEFORE the app does, acting as the sync initiator. The idea
is to start a sync session, then observe what the goggles request or
send back. This captures the goggles' side of the conversation.

For full bidirectional capture, use --wireshark-hint for instructions.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# BLE
try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("ERROR: bleak not installed. Run: pip install bleak", file=sys.stderr)
    sys.exit(1)

# Protobuf
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "proto"))
try:
    import form_pb2
except ImportError:
    print("ERROR: form_pb2 not found. Run: protoc --python_out=proto proto/form.proto", file=sys.stderr)
    sys.exit(1)

FORM_FILE_TYPE_NAMES = {
    0: "RESERVED",
    1: "SWIM_PASSPORT",
    2: "DEVICE_SETTINGS_V2",
    3: "DEVICE_DASHBOARDS_V2",
    4: "SWIM_SPA_SETTINGS",
    5: "WORKOUT_DATA",
    6: "SAVED_WORKOUTS",
    7: "WORKOUTS_INFO",
    8: "SUBSCRIPTION_INFO",
    9: "PLAN_INFO",
    10: "DEVICE_ENTITLEMENT",
    11: "IMPORTED_WORKOUTS_INFO",
    12: "SWIM_STATS",
    13: "USER_PROFILE_V2",
    14: "HEAD_COACH_INSIGHTS",
    15: "UP_NEXT_WORKOUTS",
    16: "REMOTE_CONFIG",
    17: "FEATURE_FLAGS_V2",
}


class BLESniffer:
    def __init__(self, mac, output_dir):
        self.mac = mac
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.message_count = 0
        self.file_buffers = {}  # file_index -> list of chunk bytes
        self.file_meta = {}    # file_index -> {size, started_at}
        self.captured_files = []

    def _save_raw(self, prefix, data):
        """Save raw bytes to output directory."""
        path = self.output_dir / f"{prefix}.bin"
        path.write_bytes(data)
        return path

    def notification_handler(self, char, data: bytearray):
        """Handle all BLE notifications from goggles."""
        raw = bytes(data)
        self.message_count += 1
        ts = time.strftime("%H:%M:%S")

        try:
            fm = form_pb2.FormMessage()
            fm.ParseFromString(raw)

            if fm.isCommandMessage:
                cmd = form_pb2.FormCommandMessage()
                cmd.ParseFromString(fm.data)
                cmd_name = form_pb2.FormCommandMessage.CommandType.Name(cmd.commandType)
                ct = cmd.commandType

                print(f"[{ts}] CMD: {cmd_name} (type={ct})", flush=True)

                # Log interesting command fields
                if cmd.fileIndex:
                    print(f"        fileIndex={cmd.fileIndex}", flush=True)
                if cmd.fileSize:
                    print(f"        fileSize={cmd.fileSize}", flush=True)
                if cmd.maxChunkSize:
                    print(f"        maxChunkSize={cmd.maxChunkSize}", flush=True)
                if cmd.chunkID:
                    print(f"        chunkID={cmd.chunkID}", flush=True)

                # Track file transfer state
                if ct == 22:  # FILE_TRANSFER_START
                    self.file_buffers[cmd.fileIndex] = []
                    self.file_meta[cmd.fileIndex] = {
                        "size": cmd.fileSize,
                        "started_at": time.time(),
                    }
                elif ct == 25:  # FILE_TRANSFER_SUCCESS
                    fi = cmd.fileIndex
                    if fi in self.file_buffers:
                        assembled = b"".join(self.file_buffers[fi])
                        self._process_completed_file(fi, assembled)
                        del self.file_buffers[fi]

            else:
                dm = form_pb2.FormDataMessage()
                dm.ParseFromString(fm.data)
                dt_name = form_pb2.FormDataMessage.DataType.Name(dm.dataType)

                if dm.dataType == 9:  # FILE_TRANSFER
                    fi = dm.fileIndex
                    if fi in self.file_buffers:
                        self.file_buffers[fi].append(dm.data)
                    print(f"[{ts}] DATA: {dt_name} file={fi} chunk={dm.chunkID} {len(dm.data)}B", flush=True)
                else:
                    print(f"[{ts}] DATA: {dt_name} {len(dm.data) if dm.data else 0}B", flush=True)
                    # Save non-file-transfer data messages
                    path = self._save_raw(f"data_{self.message_count:04d}_{dt_name}", dm.data or b"")
                    print(f"        saved: {path}", flush=True)

        except Exception as e:
            print(f"[{ts}] RAW: {len(raw)}B (decode failed: {e})", flush=True)
            self._save_raw(f"raw_{self.message_count:04d}", raw)

    def _process_completed_file(self, file_index, assembled_data):
        """Process a fully received file transfer."""
        ts = time.strftime("%H:%M:%S")

        # Try to parse as FormFileMessage
        try:
            ffm = form_pb2.FormFileMessage()
            ffm.ParseFromString(assembled_data)
            type_name = FORM_FILE_TYPE_NAMES.get(ffm.type, f"UNKNOWN_{ffm.type}")
            inner_data = ffm.data
            encrypted = ffm.isEncrypted

            print(f"\n[{ts}] === FILE COMPLETE: index={file_index} ===", flush=True)
            print(f"        FormFileType: {type_name} ({ffm.type})", flush=True)
            print(f"        Inner data: {len(inner_data)}B", flush=True)
            print(f"        Encrypted: {encrypted}", flush=True)

            # Save the full FormFileMessage
            wrapper_path = self._save_raw(f"file_{file_index:02d}_{type_name}_wrapper", assembled_data)
            print(f"        wrapper saved: {wrapper_path}", flush=True)

            # Save just the inner data (for protoc --decode_raw)
            if inner_data:
                inner_path = self._save_raw(f"file_{file_index:02d}_{type_name}_inner", inner_data)
                print(f"        inner saved:   {inner_path}", flush=True)

            # Try to decode known types
            self._try_decode(type_name, ffm.type, inner_data)

            self.captured_files.append({
                "index": file_index,
                "type": ffm.type,
                "type_name": type_name,
                "size": len(assembled_data),
                "inner_size": len(inner_data),
                "encrypted": encrypted,
            })

            # Highlight the entitlement-related types
            if ffm.type in (8, 10, 16, 17):
                print(f"\n        *** ENTITLEMENT-RELATED FILE CAPTURED: {type_name} ***", flush=True)
                print(f"        Run: protoc --decode_raw < {inner_path}", flush=True)
                print(f"        Hex: {inner_data.hex()}", flush=True)

        except Exception as e:
            print(f"\n[{ts}] === FILE COMPLETE: index={file_index} (not a FormFileMessage: {e}) ===", flush=True)
            path = self._save_raw(f"file_{file_index:02d}_raw", assembled_data)
            print(f"        saved: {path}", flush=True)

        print(flush=True)

    def _try_decode(self, type_name, type_num, data):
        """Try to decode known protobuf types."""
        decoders = {
            5: ("WorkoutData", None),  # workout.proto, separate import
            6: ("SavedWorkoutsMessage", form_pb2.SavedWorkoutsMessage),
            7: ("WorkoutsInfoMessage", form_pb2.WorkoutsInfoMessage),
            9: ("PlanInfoMessage", form_pb2.PlanInfoMessage),
            11: ("ImportedWorkoutsInfoMessage", form_pb2.ImportedWorkoutsInfoMessage),
            15: ("UpNextWorkoutsMessage", form_pb2.UpNextWorkoutsMessage),
        }

        if type_num in decoders:
            name, cls = decoders[type_num]
            if cls and data:
                try:
                    msg = cls()
                    msg.ParseFromString(data)
                    print(f"        decoded {name}:", flush=True)
                    for line in str(msg).strip().split("\n"):
                        print(f"          {line}", flush=True)
                except Exception as e:
                    print(f"        decode failed: {e}", flush=True)

        # For unknown types, show raw field analysis
        if type_num in (8, 10, 16, 17) and data:
            print(f"        raw hex: {data.hex()}", flush=True)
            print(f"        raw bytes: {list(data)}", flush=True)

    async def sniff(self, duration=300):
        """Connect to goggles and log all BLE traffic."""
        print(f"BLE Sniffer for FORM Goggles", flush=True)
        print(f"Output directory: {self.output_dir}", flush=True)
        print(f"Duration: {duration}s", flush=True)
        print(flush=True)

        print(f"Scanning for {self.mac}...", flush=True)
        device = await BleakScanner.find_device_by_address(self.mac, timeout=15.0)
        if not device:
            print("ERROR: Goggles not found. Make sure they're on and not connected to another device.", flush=True)
            return False

        client = BleakClient(device)
        try:
            await client.connect()
            print("Connected to goggles", flush=True)
            await asyncio.sleep(1.0)

            # Find characteristics
            chars = {}
            for svc in client.services:
                for char in svc.characteristics:
                    short_uuid = char.uuid.split("-")[0]
                    chars[short_uuid] = char
                    props = ", ".join(char.properties)
                    print(f"  Characteristic: {char.uuid} [{props}]", flush=True)

            nc = chars.get("00012000")  # notify
            wc = chars.get("00012001")  # write

            if not nc:
                print("ERROR: Notify characteristic (00012000) not found!", flush=True)
                return False

            print(f"\nStarting notification listener...", flush=True)
            await asyncio.wait_for(client.start_notify(nc, self.notification_handler), timeout=15.0)
            print("Listening for BLE traffic.", flush=True)
            print("Now trigger a sync from the FORM app on your phone.", flush=True)
            print(f"Will listen for {duration}s. Press Ctrl+C to stop early.\n", flush=True)

            try:
                await asyncio.sleep(duration)
            except asyncio.CancelledError:
                pass

        except KeyboardInterrupt:
            print("\nStopped by user.", flush=True)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        # Summary
        print(f"\n{'='*60}", flush=True)
        print(f"CAPTURE SUMMARY", flush=True)
        print(f"{'='*60}", flush=True)
        print(f"Total messages: {self.message_count}", flush=True)
        print(f"Files captured: {len(self.captured_files)}", flush=True)
        for f in self.captured_files:
            marker = " *** ENTITLEMENT ***" if f["type"] in (8, 10, 16, 17) else ""
            print(f"  [{f['index']}] {f['type_name']} — {f['inner_size']}B inner{marker}", flush=True)
        print(f"\nAll files saved to: {self.output_dir}/", flush=True)

        entitlement_files = [f for f in self.captured_files if f["type"] in (8, 10, 16, 17)]
        if entitlement_files:
            print(f"\nEntitlement files captured! Decode with:", flush=True)
            for f in entitlement_files:
                inner_path = self.output_dir / f"file_{f['index']:02d}_{f['type_name']}_inner.bin"
                print(f"  protoc --decode_raw < {inner_path}", flush=True)
        else:
            print(f"\nNo entitlement files (types 8,10,16,17) captured.", flush=True)
            print("The official app may not have synced, or it sends entitlement", flush=True)
            print("data TO the goggles (write characteristic) rather than FROM them.", flush=True)
            print("For full bidirectional capture, use an nRF Sniffer + Wireshark.", flush=True)

        return True


def main():
    parser = argparse.ArgumentParser(
        description="BLE traffic logger for FORM goggles — captures sync traffic for protocol analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Usage:
  %(prog)s                                    # Use MAC from ~/.formgoggles.json
  %(prog)s --goggle-mac AA:BB:CC:DD:EE:FF     # Specify MAC directly
  %(prog)s --duration 600                      # Listen for 10 minutes
  %(prog)s --output-dir ./premium_capture      # Custom output directory

After capturing a Premium sync, decode entitlement files:
  protoc --decode_raw < captures/file_XX_SUBSCRIPTION_INFO_inner.bin
  protoc --decode_raw < captures/file_XX_DEVICE_ENTITLEMENT_inner.bin
        """,
    )
    parser.add_argument("--goggle-mac", help="Goggles BLE MAC address")
    parser.add_argument("--duration", type=int, default=300, help="Listen duration in seconds (default: 300)")
    parser.add_argument("--output-dir", default="captures", help="Directory for captured files (default: captures)")
    parser.add_argument("--wireshark-hint", action="store_true",
                        help="Print instructions for full bidirectional capture with nRF Sniffer")

    args = parser.parse_args()

    if args.wireshark_hint:
        print("""
Full Bidirectional BLE Capture
==============================

This script only sees traffic FROM the goggles (notify characteristic).
To capture what the FORM app sends TO the goggles, you need a BLE sniffer.

Option 1: nRF Sniffer + Wireshark
  - Nordic nRF52840 dongle (~$10) with sniffer firmware
  - Wireshark with nRF Sniffer plugin
  - Filter: btatt.handle == 0xNNNN (the write characteristic handle)
  - Export GATT write payloads as raw bytes

Option 2: Android HCI snoop log
  - Enable Bluetooth HCI snoop log in Android developer options
  - Trigger a sync from the FORM app
  - Pull the log: adb pull /data/misc/bluetooth/logs/btsnoop_hci.log
  - Open in Wireshark, filter for ATT writes to the goggles

Option 3: mitmproxy for REST + this script for BLE
  - Captures the full picture: REST API calls + BLE responses
  - Run mitmproxy on your network, configure phone to proxy
  - Simultaneously run this script connected to goggles
""")
        return 0

    # Load goggle MAC from config if not provided
    mac = args.goggle_mac
    if not mac:
        config_path = Path.home() / ".formgoggles.json"
        try:
            config = json.loads(config_path.read_text())
            mac = config.get("goggleMac")
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    if not mac:
        parser.error("--goggle-mac is required (or run form_sync.py --setup first)")

    sniffer = BLESniffer(mac, args.output_dir)
    try:
        asyncio.run(sniffer.sniff(args.duration))
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
