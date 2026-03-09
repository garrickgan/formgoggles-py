# FORM Goggles BLE Protocol

Reverse-engineered protocol documentation for FORM smart swim goggles (tested on firmware 3.11.211).

## BLE Service Overview

FORM goggles advertise a custom GATT service with two key characteristics:

| UUID Prefix | Direction | Description |
|-------------|-----------|-------------|
| `00012001`  | Write (no response) | Command/data TX — phone writes here |
| `00012000`  | Notify | Command/data RX — goggles notify here |

All messages are serialized as Protocol Buffers.

## Message Framing

Every BLE message is a `FormMessage`:

```protobuf
message FormMessage {
  bool isCommandMessage = 1;  // true = command, false = data
  bytes data = 2;             // serialized inner message
}
```

- If `isCommandMessage = true`, `data` contains a serialized `FormCommandMessage`
- If `isCommandMessage = false`, `data` contains a serialized `FormDataMessage`

## Command Types

```protobuf
enum CommandType {
  SYNC_START = 1;
  SYNC_COMPLETE = 2;
  FILE_TRANSFER_START = 22;
  FILE_TRANSFER_DONE = 23;
  FILE_TRANSFER_READY_TO_RECEIVE = 24;
  FILE_TRANSFER_SUCCESS = 25;
  FILE_TRANSFER_FAIL = 26;
  FILE_TRANSFER_CHUNK_ID_REQUEST = 34;
  // ... see proto/form.proto for full list
}
```

## File Transfer Protocol

Pushing data to the goggles uses a chunked file transfer protocol:

```
Phone                              Goggles
  |                                  |
  |--- SYNC_START (cmd=1) --------->|
  |                                  |
  |--- FILE_TRANSFER_START (cmd=22)->|  (fileIndex, fileSize, maxChunkSize)
  |<-- READY_TO_RECEIVE (cmd=24) ---|
  |                                  |
  |--- DATA chunk 0 (dataType=9) -->|
  |--- DATA chunk 1 --------------->|
  |--- DATA chunk N --------------->|
  |                                  |
  |--- FILE_TRANSFER_DONE (cmd=23)->|
  |<-- FILE_TRANSFER_SUCCESS (25) --|  (or CHUNK_ID_REQUEST for retransmit)
  |                                  |
  |  ... repeat for each file ...    |
  |                                  |
  |--- SYNC_COMPLETE (cmd=2) ------>|
```

### FILE_TRANSFER_START fields

```protobuf
message FormCommandMessage {
  CommandType commandType = 1;  // 22 = FILE_TRANSFER_START
  uint32 fileIndex = 2;         // Sequential file index (1, 2, 3, ...)
  uint32 maxChunkSize = 3;      // Max bytes per chunk (typically 180)
  uint32 fileSize = 5;          // Total file size in bytes
}
```

### Data Chunks

Each chunk is wrapped in a `FormDataMessage`:

```protobuf
message FormDataMessage {
  DataType dataType = 1;   // 9 = FILE_TRANSFER
  uint32 fileIndex = 2;    // Matches FILE_TRANSFER_START
  uint32 chunkID = 3;      // Sequential chunk ID (0, 1, 2, ...)
  uint32 crc = 4;          // CRC (typically 0)
  bytes data = 5;          // Chunk payload
}
```

### Retransmission

If the goggles need a chunk re-sent, they respond with `FILE_TRANSFER_CHUNK_ID_REQUEST` (cmd=34) containing the `chunkID` to retransmit. Resend the chunk, then send `FILE_TRANSFER_DONE` again.

## FormFileMessage Wrapper

File transfer payloads are wrapped in a `FormFileMessage`:

```protobuf
message FormFileMessage {
  FormFileType type = 1;
  bytes data = 2;
  bool isEncrypted = 3;    // Always false for our purposes
}
```

### File Types

| Type | Name | Description |
|------|------|-------------|
| 5 | `WORKOUT_DATA` | Protobuf-encoded workout structure |
| 6 | `SAVED_WORKOUTS` | List of saved workout IDs |
| 7 | `WORKOUTS_INFO` | Workout metadata (duration, category) |
| 9 | `PLAN_INFO` | Training plan structure |
| 11 | `IMPORTED_WORKOUTS_INFO` | Imported workout status + origin |
| 15 | `UP_NEXT_WORKOUTS` | Queue of upcoming workouts |

## Pushing a Workout

To push a workout to the goggles, send 4 files in sequence:

1. **WorkoutsInfo** (type 7) — Register the workout ID with expected duration
2. **ImportedWorkoutsInfo** (type 11) — Set status=UPCOMING, origin=CUSTOM_WORKOUT
3. **WorkoutData** (type 5) — The actual workout protobuf binary
4. **UpNextWorkouts** (type 15) — Add to the "up next" queue

### WorkoutsInfoMessage

```protobuf
message WorkoutsInfoMessage {
  repeated WorkoutInfo standaloneWorkouts = 1;
  repeated WorkoutInfo planWorkouts = 2;
  repeated WorkoutInfo sampleWorkouts = 3;
  repeated WorkoutInfo importedWorkouts = 4;
  repeated WorkoutInfo personalizedWorkouts = 5;
}

message WorkoutInfo {
  string id = 1;              // Workout UUID
  uint32 expectedDuration = 2; // Seconds
}
```

### ImportedWorkoutsInfoMessage

```protobuf
message ImportedWorkoutsInfoMessage {
  repeated ImportedWorkoutInfo workouts = 1;
}

message ImportedWorkoutInfo {
  string workoutId = 1;
  ImportedWorkoutStatus status = 3;   // 10 = UPCOMING
  ImportedWorkoutOrigin origin = 6;   // 2 = CUSTOM_WORKOUT
}
```

### UpNextWorkoutsMessage

```protobuf
message UpNextWorkoutsMessage {
  repeated UpNextWorkoutInfo upNextWorkouts = 1;
}

message UpNextWorkoutInfo {
  string id = 1;
  UpNextWorkoutType type = 2;    // 1 = STANDALONE
  uint32 expectedDuration = 3;
}
```

## Workout Data Format

The workout binary (FormFileType=5) is a separate protobuf schema:

```protobuf
message WorkoutData {
  string minFirmwareVersion = 1;  // "2.0.0"
  string id = 2;                   // UUID
  string name = 3;                 // Display name on HUD
  repeated SetGroup setGroups = 4;
  bytes equipmentTypes = 5;
  string category = 6;             // "Endurance", "Sprint", etc.
  uint32 revisionNumber = 7;
  ImageInfo imageInfo = 8;
  uint32 workoutVersion = 9;
  uint32 isPublished = 10;
}
```

See `proto/workout.proto` for the complete schema including SetGroup, Set, IntervalSpec, etc.

### Enum Reference

**StrokeType**: 3=backstroke, 4=freestyle, 5=breaststroke, 6=choice, 7=butterfly, 8=IM

**EffortLevel**: 1=easy, 2=moderate, 3=fast, 6=max, 7=descend, 9=strong

**GroupType**: 1=warmup, 2=cooldown, 4=main

## FORM REST API

The FORM app communicates with `app.formathletica.com/api/v1/`. Key endpoints for workout sync:

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/workout_builder/workouts` | Create a custom workout |
| POST | `/users/me/workouts` | Save/unsave workout (`{addWorkoutId, removeWorkoutId}`) |
| GET | `/users/me/workouts/protobuf?workoutIds=ID` | Fetch server-generated protobuf binary |
| POST | `/oauth/token/refresh` | Refresh bearer token |
| GET | `/users/me/workouts` | List saved workouts |

### Authentication

All API requests require `Authorization: Bearer <token>`. Tokens expire ~30 days. Use the refresh endpoint with Basic auth to get new tokens:

```
POST /api/v1/oauth/token/refresh
Authorization: Basic <client_credentials>
Content-Type: application/json

{"refreshToken": "<refresh_token>"}
```

## Disconnect Signals

The goggles may send disconnect commands during sync:

| Command | Meaning |
|---------|---------|
| 42 | Swim started |
| 43 | Shutting down |
| 44 | Sync timeout |
| 45 | OTA timeout |
| 46 | Connection disabled |

Always handle these gracefully — disconnect and retry later.
