# Nova: Musik und vereinbarte Meldungen

`device_play_keys` bietet warmpad, halo, choir, strings, harp, vibes,
musicbox und piano. Bis zu drei Instrumente lassen sich kombinieren.
Melodie und Begleitung werden automatisch auf dieselbe Durchlauflänge gebracht:
Begleitung wiederholen, letzten Akkord bei Bedarf kürzen. Alle Spuren starten
am gemeinsamen Audio-Slice neu. Dies garantiert Rhythmus, nicht passende Harmonie.

Beispiele für Telegram:

- „Komponier zehn Minuten ruhige Musik mit Klavier und warmen Flächen auf dem Elecrow.“
- „Spiel eine eigene Spieluhr-Melodie mit Streichern als Begleitung.“
- „Mach nur Waldatmosphäre mit Wind, Blättern und Vögeln für 30 Minuten.“
- „Misch leisen Regen und Lagerfeuer unter die Musik.“
- „Beende nur die Atmosphäre; die Musik soll weiterlaufen.“
- „Stoppe nur die Musik; lass die Atmosphäre weiterlaufen.“
- „Stoppe Musik und Atmosphäre.“

Atmosphäre wird prozedural erzeugt; Instrumente verwenden aufgenommene Samples.
Lautstärke bleibt bei der Einstellung am Boardknopf, sofern nicht ausdrücklich angefragt.

Die externen systemd-Timer in `tools/systemd/` erinnern Nova täglich um 09:00
und 20:00 Europe/Berlin über den offiziellen Dokument-Posteingang (§34.6).
Wiederholungen alle zehn Minuten innerhalb der Stunde dienen ausschließlich dem
Retry: `.yurios/daily-inbox/` verhindert erneutes Einreichen nach Erfolg.
Kein Nachholen nach Ende der Stunde und keine Änderung an wiederkehrenden Zielen.
Nova entscheidet über ihre üblichen Ruhezeit-, Budget- und Kontaktregeln;
eine eingereichte Erinnerung garantiert deshalb keine Telegram-Nachricht.
Deaktivieren: `sudo systemctl disable --now nova-daily-inbox-{morning,evening}.timer`.
Installation aus dem ausgerollten Checkout: Service und beide Timer nach
`/etc/systemd/system/` installieren, daemon-reload, dann beide Timer enable --now.
