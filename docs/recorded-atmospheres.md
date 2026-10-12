# Novas aufgenommene Atmosphären

SPEC §26.4. Echte Aufnahmen ergänzen den bisherigen synthetischen Generator.
Nova sucht zuerst mit `device_sound_library(query="gespraeche fern")`.
Die Namen beschreiben Ort, Quelle, Entfernung, Modus und Variante:
`stadt__menschen__gespraeche__fern__hintergrund__001_<hash>.pcm`.
Originalbeschreibung und englische Suchwörter stehen zusätzlich im Katalog.

`device_soundscape(layers=[{"files": ["exakter Katalogname"], "gain": 40,
"gap_min": 5, "gap_max": 20}], level=35, minutes=30, title="Gasthaus")`
lädt nur ausgewählte Dateien. Eine Ebene enthält bis zu vier Varianten eines
Hintergrunds oder Ereignisses, nicht beides. Es gibt höchstens sechs Ebenen.
Hintergründe wechseln mit 250-ms-Überblendungen. Ereignisse bekommen wechselnde
Pausen, Varianten und Lautstärken. Der bestehende Lautstärkeknopf gilt weiterhin.
`device_stop_music` beendet nur Musik; `device_ambience(scene="off")` oder
`device_soundscape(layers=[])` beendet nur Atmosphäre mit Ausblendung.

PCM: 16 kHz, mono, signed 16-bit little-endian. Aufgenommene Hintergründe werden
in kurze Varianten vorbereitet, Ereignisse in kurze Ausschnitte. Ferne Gespräche
erhalten gedämpfte Höhen und einen geringeren Pegel; sie sind keine zugesicherte
Entfernungssimulation und nicht garantiert vollständig unverständlich.

Originaldateien und gespeicherte Lizenzseiten bleiben auf dem PC. Quelle, Autor,
CC0-Lizenz und Prüfsumme stehen im Katalog. Nur nach vollständiger geprüfter
Übertragung darf `catalog.json` als `installed.json` veröffentlicht werden.
Diese Daten und PCM-Dateien sind lokale, ignorierte Assets. Code wird wie üblich
über Git ausgerollt; Firmwarebibliotheken und Assets werden auf das Board übertragen.

Aktuelle Import-/Installationshelfer liegen im lokalen Nova-Arbeitsordner:
`build-sound-library.py` und `install-sound-library.py`. Der begrenzte
LAN-Übertragungsdienst verwendet ein einmaliges Token, eine feste Dateiauswahl,
Größenlimits und SHA-256-Prüfung; der Listener schließt nach dem Transfer oder Timeout.

Für Szenen wählt Nova passende Epochen und Umgebungen: keine Autos im
mittelalterlichen Gasthaus; entfernte Gespräche statt klarer Vordergrunddialoge.
Regen draußen mit Feuer drinnen ist möglich. Das Werkzeug prüft Dateinamen und
Parameter, aber nicht die semantische Stimmigkeit einer ganzen Szene.
