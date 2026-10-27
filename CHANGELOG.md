# Changelog

## 0.3.0

- Mark the light unavailable when reconnect and keepalive attempts keep
  failing for 5 minutes (`UNAVAILABLE_AFTER_S`). It becomes available again
  on the next successful connection.
- Log a warning when the light goes unavailable and an info line when
  communication returns.
- Add a diagnostic connectivity binary sensor,
  `binary_sensor.holman_igardener_rgb_connectivity`. It uses the same
  outage rule and stays available, turning off when the light is
  unavailable.
- The startup delay does not start the outage clock, so a restart does not
  flash the light unavailable. A brief proxy blip stays inside the window.

## 0.2.1

- Hold the BLE link open, with a 240-second keepalive, and recover it after
  Home Assistant restarts. Back off between failed attempts, and raise a
  repair issue if the controller stays unreachable for about 15 minutes.

## 0.2.0

- Initial release.
