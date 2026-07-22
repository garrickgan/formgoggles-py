#!/usr/bin/env python3
"""
Connect to goggles, send SYNC_START, and log ALL responses for 10 seconds
before sending any files. This checks if the goggles send requests we need
to handle before pushing workout data.
"""

import asyncio
import sys
from bleak import BleakClient, BleakScanner

GOGGLE_MAC = "0848D23D-1E74-DB69-F3CD-918127FFEFD4"

sys.path.insert(0, "proto")
import form_pb2

NOTIFY_CHAR = "00012000-8589-4d81-9803-a7a8ab3b0c06"
WRITE_CHAR = "00012001-8589-4d81-9803-a7a8ab3b0c06"


def decode_msg(data):
    try:
        fm = form_pb2.FormMessage()
        fm.ParseFromString(data)
        if fm.isCommandMessage:
            cmd = form_pb2.FormCommandMessage()
            cmd.ParseFromString(fm.data)
            name = form_pb2.FormCommandMessage.CommandType.Name(cmd.commandType)
            extras = []
            if cmd.fileIndex: extras.append(f"fileIndex={cmd.fileIndex}")
            if cmd.maxChunkSize: extras.append(f"maxChunk={cmd.maxChunkSize}")
            if cmd.fileSize: extras.append(f"fileSize={cmd.fileSize}")
            if cmd.fileName: extras.append(f"fileName={cmd.fileName}")
            if cmd.fileType:
                ft = form_pb2.FormCommandMessage.FileType.Name(cmd.fileType)
                extras.append(f"fileType={ft}")
            if cmd.isCompressed: extras.append("compressed")
            if cmd.userKey: extras.append(f"userKey={cmd.userKey[:20]}...")
            if cmd.bleLogPassword: extras.append(f"bleLogPass={cmd.bleLogPassword}")
            extra = " " + " ".join(extras) if extras else ""
            return f"CMD: {name}{extra}"
        else:
            dm = form_pb2.FormDataMessage()
            dm.ParseFromString(fm.data)
            name = form_pb2.FormDataMessage.DataType.Name(dm.dataType)
            extras = []
            if dm.fileIndex: extras.append(f"fileIndex={dm.fileIndex}")
            if dm.chunkID: extras.append(f"chunkID={dm.chunkID}")
            if dm.crc: extras.append(f"crc={dm.crc}")
            if dm.data: extras.append(f"data={len(dm.data)}B")
            extra = " " + " ".join(extras) if extras else ""
            return f"DATA: {name}{extra}"
    except Exception as e:
        return f"RAW: {data.hex()} (err: {e})"


def make_cmd(cmd_type):
    cmd = form_pb2.FormCommandMessage()
    cmd.commandType = cmd_type
    fm = form_pb2.FormMessage()
    fm.isCommandMessage = True
    fm.data = cmd.SerializeToString()
    return fm.SerializeToString()


async def main():
    print(f"Scanning for {GOGGLE_MAC}...")
    device = await BleakScanner.find_device_by_address(GOGGLE_MAC, timeout=15.0)
    if not device:
        print("ERROR: Goggles not found.")
        return

    print(f"Found: {device.name}")
    disconnected = asyncio.Event()
    all_messages = []

    def on_disconnect(client):
        print("\n*** DISCONNECTED ***")
        disconnected.set()

    def on_notify(sender, data):
        raw = bytes(data)
        decoded = decode_msg(raw)
        ts = asyncio.get_event_loop().time()
        all_messages.append((ts, decoded, raw))
        print(f"  [{len(all_messages):2d}] <- {decoded}")

    client = BleakClient(device, disconnected_callback=on_disconnect)
    try:
        await client.connect()
        print("Connected!")

        await client.start_notify(NOTIFY_CHAR, on_notify)
        print("Notifications active. Waiting 2s for unsolicited messages...\n")
        await asyncio.sleep(2.0)

        if disconnected.is_set():
            return

        # Send SYNC_START
        print(">>> Sending SYNC_START")
        await client.write_gatt_char(WRITE_CHAR, make_cmd(1), response=False)
        print("Waiting 10s for ALL goggles responses...\n")

        for i in range(20):
            await asyncio.sleep(0.5)
            if disconnected.is_set():
                break

        if disconnected.is_set():
            return

        # Now try SYNC_START_NO_UI
        print("\n>>> Sending SYNC_START_NO_UI")
        await client.write_gatt_char(WRITE_CHAR, make_cmd(30), response=False)
        print("Waiting 10s...\n")

        for i in range(20):
            await asyncio.sleep(0.5)
            if disconnected.is_set():
                break

        # Send SYNC_COMPLETE
        if not disconnected.is_set():
            print("\n>>> Sending SYNC_COMPLETE")
            await client.write_gatt_char(WRITE_CHAR, make_cmd(2), response=False)
            await asyncio.sleep(3.0)

        # Summary
        print("\n" + "=" * 60)
        print(f"SUMMARY: {len(all_messages)} messages received")
        print("=" * 60)
        if all_messages:
            t0 = all_messages[0][0]
            for ts, decoded, raw in all_messages:
                print(f"  +{ts-t0:6.2f}s  {decoded}")
                print(f"           hex: {raw.hex()}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if client.is_connected:
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
