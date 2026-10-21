# Holman iGardener RGB (Vibrance_RGB) for Home Assistant

A custom Home Assistant integration that controls Holman iGardener RGB garden
light controllers (which advertise over Bluetooth LE as `Vibrance_RGB`)
locally, with no cloud and no phone app.

It provides a single `light` entity with on/off, RGB colour and brightness.

> **Disclaimer:** This project is an independent, community-made integration.
> It is not affiliated with, endorsed by, or supported by Holman Industries.
> "Holman" and "iGardener" are trademarks of their respective owners and are
> used here only to identify compatible hardware. Use at your own risk.

## Supported hardware

- Holman iGardener RGB light controllers that advertise as `Vibrance_RGB`
  (service UUID `a876f000-7f10-4d70-b606-7df77c3eee0c`).

Other Holman products (tap timers, etc.) are not supported.

## Requirements

- Home Assistant 2024.8 or newer with the Bluetooth integration set up.
- A Bluetooth adapter that can make **active connections** to the controller.
  An [ESPHome Bluetooth proxy](#esphome-bluetooth-proxy-recommended) is
  strongly recommended.

## Installation

### HACS (custom repository)

1. In HACS, open the menu (⋮) → **Custom repositories**.
2. Add `https://github.com/gwaterhouse/holman-igardener-ha` with category
   **Integration**.
3. Install **Holman iGardener RGB (Vibrance_RGB)** and restart Home Assistant.

### Manual

1. Copy `custom_components/holman_igardener` into your Home Assistant
   `config/custom_components/` directory.
2. Restart Home Assistant.

## Setup

1. Close the Holman phone app (or turn off Bluetooth on the phone) – the
   controller accepts only one connection.
2. Power-cycle the controller so it advertises.
3. Home Assistant should discover it automatically (**Settings → Devices &
   services**). Otherwise click **Add integration**, search for
   *Holman iGardener RGB* and pick the discovered device.

## ESPHome Bluetooth proxy (recommended)

Some onboard Bluetooth adapters cannot connect to this controller at all (for
example a Realtek `13d3:3558` adapter on HAOS with kernel 6.18 was seen to
fail). An ESP32 running ESPHome as an active Bluetooth proxy, placed within
range of the controller, works reliably.

```yaml
esphome:
  name: garden-bt-proxy
  friendly_name: Garden BT Proxy

esp32:
  board: esp32dev
  framework:
    type: esp-idf

logger:

api:
  encryption:
    key: !secret api_encryption_key

ota:
  - platform: esphome
    password: !secret ota_password

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password

esp32_ble_tracker:
  scan_parameters:
    active: true

bluetooth_proxy:
  active: true
```

The integration prefers a proxy connection over a local `hciN` adapter when
both can see the controller.

## How it works / limitations

- **Single connection:** the controller accepts one BLE connection at a time.
  While Home Assistant holds the connection the phone app cannot connect, and
  vice versa.
- **Persistent connection:** the controller only advertises briefly after
  power-on or a disconnect, so the integration keeps the connection open,
  sends a small keepalive (a time-sync frame) every 240 seconds and reconnects
  automatically when the controller advertises again.
- **Assumed state:** the controller does not report its state, so the entity
  shows the last state Home Assistant set. Changes made in the phone app or by
  the controller's own schedules are not reflected.
- **Colours** may not exactly match what the phone app shows for the same
  setting.
- Only plain colour/brightness/on/off is supported; scenes, effects and
  schedules from the app are not.

## Protocol notes

Short version: the controller exposes one custom GATT service. Commands are
20-byte frames written to a single characteristic, with replies on a notify
characteristic. After connecting, the integration sends an identify command,
waits for the reply, sets the clock, sends a session frame and initialises
four slots; after that, one frame sets colour and brightness and a separate
frame turns the light off. See [docs/protocol.md](docs/protocol.md) for
details. These notes were written from our own observations of BLE traffic.

## Troubleshooting

- **Device not discovered / "no devices found":** power-cycle the controller
  and make sure the phone app is closed. Check that a proxy or adapter is in
  range.
- **Connect fails repeatedly:** your local adapter may not be able to connect;
  add an ESPHome Bluetooth proxy near the controller. After a failed attempt
  the integration waits about 20 seconds before trying again.
- **Phone app can't connect:** expected while Home Assistant holds the
  connection. Disable the config entry temporarily to use the app.
- **Debug logging:**

  ```yaml
  logger:
    default: info
    logs:
      custom_components.holman_igardener: debug
  ```

  Debug logs include the frames written and notifications received. Logs will
  contain your device's Bluetooth address – redact it before sharing.

## License

MIT – see [LICENSE](LICENSE).
