# Elecrow control over Wi-Fi

The same `device_*` tools can use MicroPython WebREPL instead of USB. Apps,
screenshots, snippets, music, recorded atmospheres and file writes retain their
existing limits and confirmations. Firmware flashing remains USB-only.

The owner enables the console using `/nova_remote.json` on the board with
`enabled` and `webrepl_password`. The PC keeps the matching secret in the ignored
`secrets/elecrow_network.json`, along with `enabled` and `host`. Alternatively
`ELECROW_NETWORK_CONFIG` selects that file. Credentials are redacted in results.
The runtime starts the console after joining a saved Wi-Fi network.

With this configuration, `port="auto"` uses Wi-Fi; `port="wifi"` explicitly
requires it, and an explicit COM port uses USB. A failed command is never replayed
on USB: a lost reply can mean the board already performed the action.

WebREPL is password authenticated but not encrypted. Use it on a trusted LAN,
never publish port 8266. The PC must reach the board address. An iPhone hotspot
does not automatically provide access from the home network. A changed DHCP
address requires updating `host` (a router DHCP reservation is recommended).

Power can come from a suitable independent supply; PC software still hosts the
MCP tools. Disconnecting the USB data cable does not disable the Wi-Fi tools.
