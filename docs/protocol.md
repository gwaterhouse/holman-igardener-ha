# Vibrance_RGB BLE protocol notes

These notes describe our own understanding of how the controller behaves,
worked out by observing Bluetooth LE traffic to our own device. They are
incomplete and may be wrong in places.

## GATT

| Purpose | UUID |
|---|---|
| Service | `a876f000-7f10-4d70-b606-7df77c3eee0c` |
| Notify (read/notify) | `a876f003-7f10-4d70-b606-7df77c3eee0c` |
| Write | `0000f004-0000-1000-8000-00805f9b34fb` |

All frames are 20 bytes, written with response. Byte 0 is always `0x00` and
byte 1 is the command.

## Commands

| Cmd | Name | Notes |
|---|---|---|
| `0x1F` | Identify | Sent first after subscribing to notifications. The controller replies on the notify characteristic with byte 2 = `0x01`. |
| `0x22` | Session | Sent during init. Bytes 2–3 vary between sessions and have not been decoded; a fixed value is accepted. |
| `0x24` | Set time | Bytes 2–3 year (big-endian), then month, day, hour, minute, second (local time). Also used as a keepalive. |
| `0x2D` | Init slot | Same layout as control; sent for slots 0–3 during init. |
| `0x2E` | Control | Sets colour and brightness (below). |

## Control frame (`0x2E`)

```
00 2E 00 02 7F 12 00 17 3B 00 RR GG BB 00 00 00 64 00 LL 00
```

- `RR GG BB` – colour
- `LL` – brightness, `0x00`–`0xFF`
- `02 7F 12 00 17 3B 00` and `64 00` are constants whose meaning is unknown

Off is a dedicated frame: `00 2E FF` followed by 17 zero bytes. Brightness 0
is not the same as off.

## Connection sequence

1. Connect (one connection only – the phone app must be disconnected).
2. Subscribe to notify.
3. Write identify (`0x1F`) and wait for the reply.
4. Write set time (`0x24`), session (`0x22`), init slots (`0x2D` × 4).
5. Write control/off frames as required.

The controller only advertises briefly after power-on or after a disconnect,
so the integration keeps the link open, writes a `0x24` frame every 240 s as a
keepalive and reconnects when it sees a new advertisement.
