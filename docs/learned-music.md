# Learning music from notation

Nova's repertoire stores notes, rests, quarter-note durations, tempo, provenance,
revision and confirmation. It never stores a MIDI playback or downloaded recording.
`device_play_learned` sends those exact saved notes to the board's recorded
instrument player. Existing atmosphere stays independent.

1. Search the web for sheet music with the existing search tools.
2. `device_score_source(url)` follows a direct MusicXML/MXL, PDF or image URL;
   HTML returns notation links. Multipart MusicXML requires selecting a part,
   and `voice` selects its melody voice. The parser preserves pitch, rests,
   duration, ties and basic repeats. It refuses unsupported notation visibly.
3. PDF/images are rendered on the workstation (install
   `tools/requirements-elecrow.txt`) and transcribed by the proxy's vision model.
   Read each page separately. Visual transcription is a fallible candidate, not
   proof of correct notes. No model configured means no transcription.
4. Save with `device_learn_music`, keeping `source_id`. A melody is mandatory;
   a list of chords alone cannot be learned as a tune. Confirm only after the
   owner approves this particular version.
5. Play with `device_play_learned`, initially piano melody alone. To correct,
   load with `device_music_library(song_id=...)`, edit the required notes, send
   the current revision and a correction note. Previous revisions are retained.
   Confirmation resets on every edit unless this version has been approved.

The ignored PC folder `elecrow-firmware/music-library` contains `sources/`,
`songs/` and `history/`. `ELECROW_REPERTOIRE` can select another directory. Files
remain across process and board restarts; PC availability is still required.
The owner enables the four new hands and a `device_score_source=90` timeout
override. Reading notation is not a board call. The source timeout includes PDF
rendering and the proxy's transcription request; failed fetches are not melodies.

Current limits are one melody voice, constant tempo 20..160, up to 400
notes/rests and the sample pitch range C3..G6 (accidentals within 130..1600 Hz).
Longer pieces need explicitly named sections. MusicXML alternate endings,
transposing instruments, grace notes and overlapping melody voices require an
unfolded or simplified edition; the parser does not silently guess them.
Instrument choice is separate from the learned notes. The existing companion
chords are optional and stay subject to the board's chord limits.

Examples: “Lerne dieses Stück aus dieser Noten-URL”, “Spiel die gespeicherte
Amazing-Grace-Melodie auf Klavier”, “Korrigiere die zweite Note in Takt drei und
speichere die neue Fassung”, “Spiel es wieder, diesmal auf der Harfe”.
