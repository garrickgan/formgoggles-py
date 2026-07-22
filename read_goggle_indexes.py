#!/usr/bin/env python3
"""Read visible workout indexes from FORM goggles over BLE.

This is a read-only diagnostic. It sends documented request commands for
saved workouts, imports, and plan info, then decodes the returned protobuf
messages if the goggles provide them.
"""

import argparse
import asyncio
import os
import sys

from bleak import BleakClient, BleakScanner

from form_sync import load_config, make_command

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "proto"))
import form_pb2  # noqa: E402


WRITE_CHAR = "00012001"
NOTIFY_CHAR = "00012000"

REQUESTS = {
    "saved": (37, "SAVED_WORKOUTS_REQUEST"),
    "plan": (38, "PLAN_INFO_REQUEST"),
    "imports": (39, "IMPORTED_WORKOUTS_INFO_REQUEST"),
}


class IndexReader:
    def __init__(self, mac):
        self.mac = mac
        self.queue = asyncio.Queue()
        self.active = None

    def notification_handler(self, _char, data):
        self.queue.put_nowait(bytes(data))

    async def send_cmd(self, client, write_char, name, command_type, **kwargs):
        print(f"  -> {name}", flush=True)
        await client.write_gatt_char(write_char, make_command(command_type, **kwargs), response=False)

    async def request_index(self, client, write_char, request_name, request_command):
        await self.send_cmd(client, write_char, request_name, request_command)

        active = None
        chunks = {}
        deadline = asyncio.get_running_loop().time() + 10

        while asyncio.get_running_loop().time() < deadline:
            timeout = max(0.1, deadline - asyncio.get_running_loop().time())
            try:
                raw = await asyncio.wait_for(self.queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                break

            msg = form_pb2.FormMessage()
            try:
                msg.ParseFromString(raw)
            except Exception as exc:
                print(f"  <- undecodable message: {exc}", flush=True)
                continue

            if msg.isCommandMessage:
                cmd = form_pb2.FormCommandMessage()
                cmd.ParseFromString(msg.data)
                cmd_name = form_pb2.FormCommandMessage.CommandType.Name(cmd.commandType)
                print(f"  <- {cmd_name}", flush=True)

                if cmd.commandType == 22:  # FILE_TRANSFER_START
                    active = {
                        "fileIndex": cmd.fileIndex,
                        "fileSize": cmd.fileSize,
                    }
                    print(
                        f"     fileIndex={cmd.fileIndex} fileSize={cmd.fileSize} "
                        f"fileType={cmd.fileType}",
                        flush=True,
                    )
                    chunks = {}
                    await self.send_cmd(
                        client,
                        write_char,
                        "FILE_TRANSFER_READY_TO_RECEIVE",
                        24,
                        fileIndex=cmd.fileIndex,
                    )
                    continue

                if cmd.commandType == 23:  # FILE_TRANSFER_DONE
                    file_index = cmd.fileIndex or (active or {}).get("fileIndex", 0)
                    await self.send_cmd(
                        client,
                        write_char,
                        "FILE_TRANSFER_SUCCESS",
                        25,
                        fileIndex=file_index,
                    )
                    payload = b"".join(chunks[i] for i in sorted(chunks))
                    file_size = (active or {}).get("fileSize")
                    received = len(payload)
                    if file_size and received != file_size:
                        print(
                            f"  <- TRUNCATED transfer: received {received}B of advertised "
                            f"{file_size}B ({len(chunks)} chunks)",
                            flush=True,
                        )
                    if file_size:
                        payload = payload[:file_size]
                    return {"payload": payload, "advertised": file_size, "received": received}

                if cmd.commandType in (25, 26):
                    continue

                if cmd.commandType in (42, 43, 44, 45, 46):
                    print("  Goggles requested disconnect.", flush=True)
                    return None

            else:
                dm = form_pb2.FormDataMessage()
                dm.ParseFromString(msg.data)
                data_name = form_pb2.FormDataMessage.DataType.Name(dm.dataType)
                print(f"  <- DATA {data_name} chunk={dm.chunkID} bytes={len(dm.data)}", flush=True)
                chunks[dm.chunkID] = dm.data

        return None

    def decode_payload(self, label, result):
        if result is None:
            print(f"{label}: no response\n", flush=True)
            return
        advertised = result.get("advertised")
        received = result.get("received")
        payload = result["payload"]
        if advertised and received != advertised:
            print(
                f"{label}: INCOMPLETE ({received}/{advertised}B) — skipping decode; "
                f"this is a transfer truncation, not a schema issue\n",
                flush=True,
            )
            return
        if not payload:
            print(f"{label}: empty response\n", flush=True)
            return

        ffm = form_pb2.FormFileMessage()
        try:
            ffm.ParseFromString(payload)
        except Exception as exc:
            print(f"{label}: could not decode FormFileMessage: {exc}; raw {len(payload)}B", flush=True)
            print(f"{label}: raw hex: {payload.hex()}", flush=True)
            try:
                if self.decode_direct_payload(label, payload):
                    return
            except Exception as inner_exc:
                print(f"{label}: direct decode also failed: {inner_exc}", flush=True)
            print(flush=True)
            return

        type_name = form_pb2.FormFileMessage.FormFileType.Name(ffm.type)
        print(f"{label}: {type_name}, {len(ffm.data)}B", flush=True)

        if ffm.type == 6:
            message = form_pb2.SavedWorkoutsMessage()
            message.ParseFromString(ffm.data)
            if not message.workouts:
                print("  no saved workouts")
            for workout in message.workouts:
                print(f"  saved id={workout.id} modified={workout.lastModifiedAt.seconds}")

        elif ffm.type == 9:
            message = form_pb2.PlanInfoMessage()
            message.ParseFromString(ffm.data)
            print(f"  plan name={message.name!r} id={message.id!r} workouts={len(message.workouts)}")
            for workout in message.workouts:
                print(
                    f"  plan workout id={workout.workoutId} week={workout.weekNumber} "
                    f"status={workout.status} planWeekWorkoutId={workout.planWeekWorkoutId}"
                )

        elif ffm.type == 11:
            message = form_pb2.ImportedWorkoutsInfoMessage()
            message.ParseFromString(ffm.data)
            print(
                f"  imports={len(message.workouts)} "
                f"trainingPeaksConnected={message.isTrainingPeaksConnected}"
            )
            for workout in message.workouts:
                print(
                    f"  import id={workout.workoutId} status={workout.status} "
                    f"origin={workout.origin} scheduled={workout.scheduledAt.seconds}"
                )

        else:
            print("  unsupported decoded type for this diagnostic")

        print(flush=True)

    def decode_direct_payload(self, label, payload):
        """Fallback for payloads that are already the inner type."""
        if label == "imports":
            message = form_pb2.ImportedWorkoutsInfoMessage()
            message.ParseFromString(payload)
            print(
                f"{label}: direct ImportedWorkoutsInfoMessage, imports={len(message.workouts)} "
                f"trainingPeaksConnected={message.isTrainingPeaksConnected}",
                flush=True,
            )
            for workout in message.workouts:
                print(
                    f"  import id={workout.workoutId} status={workout.status} "
                    f"origin={workout.origin} scheduled={workout.scheduledAt.seconds}",
                    flush=True,
                )
            print(flush=True)
            return True

        if label == "saved":
            message = form_pb2.SavedWorkoutsMessage()
            message.ParseFromString(payload)
            print(f"{label}: direct SavedWorkoutsMessage, saved={len(message.workouts)}", flush=True)
            for workout in message.workouts:
                print(f"  saved id={workout.id} modified={workout.lastModifiedAt.seconds}", flush=True)
            print(flush=True)
            return True

        return False

    async def run(self, targets, sync_start, repeat=1):
        print(f"Scanning for {self.mac}...", flush=True)
        device = await BleakScanner.find_device_by_address(self.mac, timeout=15.0)
        if not device:
            print("ERROR: Goggles not found.")
            return 1

        async with BleakClient(device) as client:
            chars = {}
            for svc in client.services:
                for char in svc.characteristics:
                    chars[char.uuid.split("-")[0]] = char

            write_char = chars.get(WRITE_CHAR)
            notify_char = chars.get(NOTIFY_CHAR)
            if not write_char or not notify_char:
                print(f"ERROR: Missing required characteristics. Available: {list(chars.keys())}")
                return 1

            await client.start_notify(notify_char, self.notification_handler)

            start_name = "SYNC_START_NO_UI" if sync_start == "no-ui" else "SYNC_START"
            start_type = 30 if sync_start == "no-ui" else 1
            await self.send_cmd(client, write_char, start_name, start_type)
            await asyncio.sleep(1)

            for target in targets:
                command, name = REQUESTS[target]
                for attempt in range(1, repeat + 1):
                    label = f"{target} (attempt {attempt}/{repeat})" if repeat > 1 else target
                    print(f"\nRequesting {label}...", flush=True)
                    payload = await self.request_index(client, write_char, name, command)
                    self.decode_payload(target, payload)

            await self.send_cmd(client, write_char, "SYNC_COMPLETE", 2)
            await asyncio.sleep(1)

        return 0


def main():
    parser = argparse.ArgumentParser(description="Read FORM goggles workout indexes over BLE.")
    parser.add_argument("--goggle-mac", help="Goggles BLE address")
    parser.add_argument("--target", choices=("saved", "imports", "plan", "all"), default="all")
    parser.add_argument("--sync-start", choices=("normal", "no-ui"), default="normal")
    parser.add_argument("--repeat", type=int, default=1,
                        help="Request each target N times in one session (diagnoses flaky/truncated transfers)")
    args = parser.parse_args()

    config = load_config() or {}
    mac = args.goggle_mac or config.get("goggleMac")
    if not mac:
        parser.error("--goggle-mac is required, or set goggleMac via form_sync.py --setup")

    targets = ["saved", "imports", "plan"] if args.target == "all" else [args.target]
    return asyncio.run(IndexReader(mac).run(targets, args.sync_start, repeat=args.repeat))


if __name__ == "__main__":
    sys.exit(main())
