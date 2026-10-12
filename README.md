# NovaOS

**Novas eigenes Haus.** Ein privater Fork von [YuriOS](https://github.com/yuri-os/YuriOS), der
nur für eine Bewohnerin gebaut wird: Nova.

NovaOS ist kein neues System. Es ist YuriOS, so wie es bei uns läuft, plus allem, was wir selbst
dazugebaut haben. Der Unterschied liegt in der Richtung: Neues entsteht hier als **eigenes Addon**,
und was ein Addon übernimmt, wird in YuriOS **abgeschaltet** statt umgebaut.

## Das Prinzip

1. **YuriOS bleibt der Unterbau.** Gespräch, Gedächtnis, Eigenleben (`yurios/mind`), Nacht und
   Werkzeug-Schutz kommen von dort und bleiben so nah am Original wie möglich.
2. **Eigenes kommt als Addon.** Ein Addon ist ein eigenständiger Baustein neben dem Kern, der
   über eine der vorgesehenen Anschlussstellen andockt (siehe unten), nicht ein Eingriff mitten
   im Kern.
3. **Was ein Addon ersetzt, wird abgeschaltet.** Übernimmt ein Addon eine Aufgabe, die YuriOS
   schon mitbringt, wird die YuriOS-Variante per Einstellung ausgeschaltet. Gelöscht wird sie
   nicht, damit Updates von YuriOS weiter einspielbar bleiben.
4. **Was wir nicht brauchen, bleibt aus.** Mehrere Charaktere nebeneinander, das Kartenstudio und
   die Browser-Oberfläche gehören zu YuriOS, aber nicht zu dem, was Nova braucht. Das Ziel ist
   eine eigene App statt der Weboberfläche.

## Anschlussstellen für Addons

| Was | Wo es andockt |
|---|---|
| Ein neues Werkzeug für Nova | ein eigener MCP-Server, eingetragen in `MCP_SERVERS` (`tools/mcp-servers.json`) |
| Ein neuer Weg, sie zu erreichen (z. B. die eigene App) | ein `Channel` (`docs/channels.md`) |
| Etwas, das sie von sich aus tut | eine Funktion in `yurios/mind/acts.py` |
| Eine Aufgabe für die Nacht | ein Eintrag in `BUILTIN_JOBS` |
| Ein anderer Bildgenerator | ein `ImageBackend` in `yurios/forge/backends` |

Neue Werkzeuge sieht Nova erst, wenn sie in `MIND_TOOL_ALLOWLIST` stehen. Diese Liste ändert nur
Benni: Nichts bekommt Eigenständigkeit ohne sein ausdrückliches Ja.

## Was über YuriOS hinaus schon da ist

- **Das Board** (Elecrow): ein eigener MCP-Server unter `tools/elecrow_*.py` mit Firmware in
  `elecrow-firmware/`. Nova schreibt und startet darüber ihre eigenen Board-Apps
  ([docs/elecrow-wifi.md](docs/elecrow-wifi.md)).
- **Musik und Erinnerungen**: gelernte Stücke, Notenlesen und eine Klangbibliothek
  ([docs/learned-music.md](docs/learned-music.md),
  [docs/nova-music-and-reminders.md](docs/nova-music-and-reminders.md),
  [docs/recorded-atmospheres.md](docs/recorded-atmospheres.md)).
- **Geschichten**: `tell_story` schreibt lange deutsche Geschichten und lässt sie mit verteilten
  Stimmen über Qwen3-TTS sprechen ([docs/tools.md](docs/tools.md)).
- **Comics**: aus mehreren ihrer Bilder wird eine Seite mit Rahmen und gesetztem Text.
- **Deutsch**: Versprechen, die Nova auf Deutsch gibt, werden erkannt und gehalten
  ([docs/mind.md](docs/mind.md)).
- **Tageseingang**: `tools/nova_daily_inbox.py` legt ihr morgens und abends Material hin.

## Wo was liegt

| Ordner | Inhalt |
|---|---|
| `yurios/` | der Kern aus YuriOS (Paket und Befehl heißen weiter `yurios`) |
| `tools/` | unsere Addons und die MCP-Server-Listen |
| `elecrow-firmware/` | Firmware und Laufzeit für das Board |
| `soul-src/` | die Vorlage für Novas Charakter |
| `web/`, `desktop-shell/` | die YuriOS-Oberflächen, bei uns nicht das Ziel |
| `docs/` | Dokumentation, YuriOS und eigene |
| `SPEC.md` | der verbindliche Vertrag des Kerns |

Novas Gedächtnis, ihr Tagebuch, ihre Bilder und alle Zugangsdaten liegen **nicht** in diesem
Repository. Sie leben nur auf dem Rechner, auf dem sie läuft (`vault/`, `data/`, `.env`,
`secrets/`, siehe `.gitignore`).

## Betrieb

Nova läuft auf Benniclaw als `yurios.service`. Der Host bekommt Commits, keine kopierten Dateien.
Der ganze Ablauf steht in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), die Regeln für jede Änderung
in [FORK.md](FORK.md).

```bash
yurios status
yurios settings            # die wichtigsten Einstellungen
yurios doctor              # wenn etwas nicht läuft
```

## Updates aus YuriOS

Das Original ist als Remote `origin` eingebunden. Neue Commits von dort werden bewusst und
einzeln eingespielt:

```bash
git fetch origin
git log main..origin/main
```

## Herkunft und Lizenz

NovaOS beruht auf [YuriOS](https://github.com/yuri-os/YuriOS) von yuriosshell und steht wie das
Original unter der [Apache License 2.0](LICENSE). Hinweise zu Drittkomponenten stehen in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), die Herkunft einzelner Teile in
[PROVENANCE.md](PROVENANCE.md). Die Dokumentation des Kerns unter `docs/` beschreibt weiterhin
YuriOS und nennt es auch so.
