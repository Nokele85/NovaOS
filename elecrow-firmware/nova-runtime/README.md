# Nova runtime — Novas Spielwiese auf dem Elecrow-Board

MicroPython-Schicht für das Elecrow AI Camera Development Board (SAD00006D,
ESP32-S3R8, 16 MB Flash, 8 MB OPI-PSRAM). Darauf schreibt, startet und
verbessert Nova eigene Python-Apps — ohne Kompilieren, ohne Flashen.

Stand: 11.09.2026 · Status-Marken: `VERIFIED` · `INFERRED` · `UNVERIFIED` · `NOT OFFERED`

## Aufteilung

| Ebene | Wer ändert sie | Wie |
|---|---|---|
| MicroPython-Firmware | nur Besitzer | `tools/elecrow_device_admin.py flash-micropython … --yes` (nur mit Sicherung < 24 h) |
| Laufzeit (`boot.py`, `main.py`, `lib/nova/`) und `/nova_secrets.json` | nur Besitzer | `tools/elecrow_device_admin.py install-runtime --yes` |
| Autostart-App | Nova nur mit `confirm=true` nach deinem Ja | MCP `device_set_autostart` |
| Apps (`/sd/apps/<name>/`, sonst `/apps/<name>/`) und `_lib/` | **Nova frei** | MCP `device_write_app`, `device_run_app`, `device_delete_app`, … |

## Board-Wissen (Quelle: Rook-Firmware, am Board erprobt)

Übernommen aus `rook-os/clients/rook-elecrow/rook_elecrow/board.h`,
`board_io.h`, `lgfx_config.h`, abgeglichen mit Elecrows Repository und dem
Schaltplan V1.0.

| Teil | Belegung | Status in dieser Laufzeit |
|---|---|---|
| AW9523 | I²C 0x5A (SDA 16, SCL 15), ID 0x23; P1_0 Panel-Strom, P1_1 **Kamerablitz**, P1_3 Hintergrundlicht, P1_4 Verstärker (HIGH = stumm), P0_4/5/6 RGB | Register 1:1 aus Rooks `board_io.h` · UNVERIFIED unter MicroPython |
| Display | ST7789 240×284, SPI SCK 14 / MOSI 13 / DC 9 / CS 10, invert, BGR | eigener Treiber · UNVERIFIED (Farben per Testbild prüfen) |
| Touch | CST816S 0x15, INT 11, um 180° gedreht | UNVERIFIED |
| TF-Karte | eigener SPI-Bus SCK 7 / MISO 6 / MOSI 8, **kein CS im Schaltplan** | INFERRED (CS fest aktiv) · Treiber `lib/sdcard.py` aus micropython-lib (MIT) |
| Lautsprecher | NS4168, I²S DATA 3 / WS 4 / BCK 5 | UNVERIFIED |
| Kamera | GC2145, Pins wie Rook; RGB565 + CIF, kein Hardware-JPEG | nur mit Kamera-Firmware (cnadler86) · UNVERIFIED |
| Mikrofon | PDM, CLK 1 / DATA 2 | **NOT OFFERED**: `machine.I2S` kann kein PDM |
| Knopf | GPIO0, active low | nicht beim Start kalibrieren (Rook) |

Erster Start am 11.09.2026 (MicroPython v1.29.0, `ESP32_GENERIC_S3-SPIRAM_OCT`):
AW9523, SD-Karte (Dummy-CS), Touch und WLAN (NTP-Zeit) VERIFIED. **`SPI(2)` hängt
den Chip in einen Interrupt-Watchdog-Reset** — schon beim Anlegen, bei 10 und 40 MHz,
reproduzierbar. Deshalb läuft das Display auf `SPI(1)` und die TF-Karte auf `SoftSPI`.
Die Farben des Startbilds sind erst mit deinem Blick aufs Display bestätigt.

Zwei Stolperfallen, die Rook schon einmal Stunden gekostet haben:
- **AW9523 zuerst**, sonst bleibt das Display dunkel und es sieht wie ein
  SPI-Fehler aus.
- **`OUTPUT1` nie auf `0xFF`**, sonst zündet der Blitz dauerhaft.

## Sicherheit

- **Safe Mode:** kein Autostart, wenn beim Einschalten ein Finger auf dem
  Display liegt, wenn `/safe` existiert oder wenn die Autostart-App dreimal
  hintereinander die ersten 20 s nicht überlebt hat.
- **Kein Watchdog.** Ein MicroPython-WDT lässt sich nicht mehr abschalten und
  würde auch Nova mitten in einer Sitzung zurücksetzen. Endlosschleifen beendet
  der Host mit Ctrl-C.
- **WLAN-Zugang:** Er liegt auf dem Board in `/nova_secrets.json` (nur SSID
  und Passwort; der Rook-API-Token nicht). Alles, was vom Board zurückkommt,
  läuft auf dem PC durch einen Filter, der die bekannten Werte schwärzt.
  `net.status()` sagt nur „online oder nicht“, nie welches Netz.
- **eFuses:** Kein Werkzeug brennt sie. MicroPython bietet dafür keine API,
  und `esptool` ruft nur das Admin-Skript auf, und auch dort nur
  `read_flash`, `erase_flash` und `write_flash`.
- **Rückweg:** vollständige 16-MB-Abbilder erprobter Rook-Firmware liegen unter
  `rook-os/backups/elecrow/`, und vor jedem MicroPython-Flash erzwingt das
  Admin-Skript eine frische Sicherung.

## Einrichten (Besitzer)

```bash
python tools/elecrow_device_admin.py import-secrets          # WLAN aus Rooks secrets.h, Werte bleiben lokal
python tools/elecrow_device_admin.py backup                   # 16 MB, ca. 6 min
python tools/elecrow_device_admin.py flash-micropython elecrow-firmware/micropython/<image>.bin --yes
python tools/elecrow_device_admin.py install-runtime --yes
```

Danach neu starten. Auf dem Display erscheinen „Nova“, die Laufzeitversion,
der Speicherort der Apps und der SD-Status. **Erst dieser Anblick belegt, dass
Stromversorgung, Display und Farben stimmen.**

## Was Nova sieht

`device_screenshot` liest den Framebuffer (halbe Auflösung) und liefert die
gezeichneten Texte wörtlich sowie Farben, Deckung und Inhaltsbereich. Auf
Benniclaw beschreibt DeepSeeks Vision-Modell `deepseek-flash` das Bild
zusätzlich in Worten (oder das in `ELECROW_VISION_MODEL` genannte). Wichtig:
nur mit `reasoning_effort: "none"` — ohne denkt das Modell und antwortete bei
knappem Budget mit leerem Text (gemessen 11.09.2026; mit dem Parameter 1,2 s
und korrekt, samt wörtlich gelesenem Text). Das PNG bleibt auf beiden Rechnern
liegen und geht nicht als Base64 ins Gesprächsmodell.
