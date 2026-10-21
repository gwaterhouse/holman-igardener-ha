"""Holman iGardener RGB / Vibrance_RGB BLE protocol (pure Python, no HA imports).

Our own understanding from observing BLE traffic; see docs/protocol.md.
All frames are 20 bytes, written with response to the write characteristic.
Control frame (0x2E) layout:
  [0]     0x00
  [1]     command: 0x1F identify, 0x22 session, 0x24 set-time,
          0x2D init-slot, 0x2E control
  [2]     slot/index (0x00 for control, 0x00-0x03 for init, 0xFF = off)
  [3:10]  constant header 02 7F 12 00 17 3B 00
  [10:13] R, G, B
  [13:16] 00 00 00
  [16:18] 64 00 (meaning unknown)
  [18]    brightness 0x00-0xFF
  [19]    0x00
OFF is a dedicated frame (002EFF followed by zeros), not brightness 0.
"""
from __future__ import annotations
from datetime import datetime

SERVICE_UUID = "a876f000-7f10-4d70-b606-7df77c3eee0c"
NOTIFY_UUID = "a876f003-7f10-4d70-b606-7df77c3eee0c"
WRITE_UUID = "0000f004-0000-1000-8000-00805f9b34fb"
DEVICE_NAME = "Vibrance_RGB"

_HEADER = bytes.fromhex("027f1200173b00")  # bytes 3..9 of normal control frames
_TAIL_CONST = bytes.fromhex("6400")         # bytes 16..17

# Observed 0x22 session frame. Bytes [2:4] vary between sessions and are not
# decoded; the controller accepts this fixed value.
SESSION_FRAME = bytes.fromhex(
    "0022ba7000000000000000000000000000000000"
)


def control_frame(rgb=(255, 0, 0), level=255, slot=0, command=0x2E):
    """Build a 0x2E (or 0x2D init-slot) 20-byte frame."""
    r, g, b = (max(0, min(255, int(v))) for v in rgb)
    level = max(0, min(255, int(level)))
    return (
        bytes([0x00, command, slot & 0xFF])
        + _HEADER
        + bytes([r, g, b, 0, 0, 0])
        + _TAIL_CONST
        + bytes([level, 0x00])
    )


def off_frame():
    return bytes.fromhex("002eff" + "00" * 17)


def identify_frame():
    return bytes.fromhex("001f" + "00" * 18)


def session_frame():
    """Fixed 0x22 session frame (payload meaning not decoded)."""
    return SESSION_FRAME


def set_time_frame(when: datetime):
    """0x24 frame: [2:4]=year BE, [4]=month [5]=day [6]=hour [7]=min [8]=sec.

    Pass HA-local time (dt_util.now()).
    """
    return (
        bytes([0x00, 0x24])
        + when.year.to_bytes(2, "big")
        + bytes([when.month, when.day, when.hour, when.minute, when.second])
        + bytes(11)
    )


def init_slot_frames(rgb=(255, 0, 0), level=255):
    """0x2D frames for slots 0..3, sent during connection init."""
    return [control_frame(rgb, level, slot=i, command=0x2D) for i in range(4)]


def reconnect_init_frames(when: datetime, rgb=(255, 0, 0), level=255):
    """Frames sent after the identify reply on (re)connect.

    Caller must: subscribe notify, write identify, *wait for identify
    notify*, then write the remaining frames returned here after identify
    (time, session 0x22, 002D x4). The trailing 0x2E control/off frame is
    appended by the device layer for the actual command.
    """
    return [
        set_time_frame(when),
        session_frame(),
        *init_slot_frames(rgb, level),
    ]


def parse_frame(data: bytes):
    """Best-effort decode for logging / notify frames."""
    if len(data) != 20:
        return {"raw": data.hex(), "length": len(data)}
    out = {"raw": data.hex(), "command": data[1], "slot": data[2]}
    if data[1] in (0x2D, 0x2E) and data[2] != 0xFF:
        out.update(rgb=tuple(data[10:13]), level=data[18], header=data[3:10].hex())
    if data[1] == 0x2E and data[2] == 0xFF:
        out["off"] = True
    if data[1] == 0x1F and data[2] == 0x01:
        out["identify_response"] = True
        out["mac_reversed"] = data[14:20].hex()
    if data[1] == 0x22:
        out["session_token"] = data[2:4].hex()
    if data[1] == 0x24:
        out["time"] = (
            f"{int.from_bytes(data[2:4], 'big'):04d}-"
            f"{data[4]:02d}-{data[5]:02d} "
            f"{data[6]:02d}:{data[7]:02d}:{data[8]:02d}"
        )
    return out


def is_identify_response(data: bytes) -> bool:
    """True for device notify reply to 001F (byte[2]==0x01), not our write."""
    return len(data) >= 3 and data[0] == 0x00 and data[1] == 0x1F and data[2] == 0x01
