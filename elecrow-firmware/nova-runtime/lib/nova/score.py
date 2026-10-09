# A melody as she writes it, turned into (frequency, beats).
#
#   "E4 G4 A4:2 - C5:0.5 B4:0.5 | A4:4"
#
# One token per note: NAME then optionally :BEATS (1 when left out). Names are
# C D E F G A B (H is the German B), with # or b, and an octave 2..7; "-" is a
# rest; "|" is a bar line for the eye and means nothing. Pure Python on
# purpose: the host checks a score with this same file before anything is sent
# to the board, so what is refused there is exactly what would fail here.
_SEMI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11, "H": 11}
_REST = ("-", "_", "R", "r", "P", "p")
MAX_NOTES = 400
HOW = "write notes like C4 F#3 Bb4, lengths like C4:2 or E4:0.5, and - for a rest"


def note_freq(name):
    """'A4' -> 440.0."""
    s = name.strip()
    letter = s[:1].upper()
    if letter not in _SEMI:
        raise ValueError("%r is not a note - %s" % (name, HOW))
    semi = _SEMI[letter]
    i = 1
    while i < len(s) and s[i] in "#b":
        semi += 1 if s[i] == "#" else -1
        i += 1
    octave = s[i:] or "4"
    if not octave.isdigit() or not 2 <= int(octave) <= 7:
        raise ValueError("%r: the octave is a number from 2 to 7 - %s" % (name, HOW))
    midi = (int(octave) + 1) * 12 + semi
    return 440.0 * 2 ** ((midi - 69) / 12)


def parse(score):
    """The score as a list of (frequency in Hz or 0.0 for a rest, beats)."""
    notes = []
    for token in str(score).replace("|", " ").replace(",", " ").split():
        name, _, beats = token.partition(":")
        try:
            length = float(beats) if beats else 1.0
        except ValueError:
            raise ValueError("%r: the length is a number of beats - %s" % (token, HOW))
        if not 0 < length <= 16:
            raise ValueError("%r: a note lasts between 0 and 16 beats" % token)
        notes.append((0.0 if name in _REST else note_freq(name), length))
    if not notes:
        raise ValueError("the score is empty - " + HOW)
    if len(notes) > MAX_NOTES:
        raise ValueError("%d notes; a piece holds %d at most" % (len(notes), MAX_NOTES))
    return notes


def seconds(notes, tempo):
    """How long one pass through the piece takes at `tempo` beats per minute."""
    return sum(beats for _, beats in notes) * 60.0 / tempo


# A carpet of sound: chords instead of notes, voices joined by "+".
#
#   "A2+E3+C4:8 F2+C3+A3:8 -:2"
MAX_VOICES = 4
MAX_CHORDS = 64
HOW_PAD = "write chords like A2+E3+C4:8 (voices joined by +, then :BEATS) and - for a rest"


def parse_chords(score):
    """The score as a list of (tuple of frequencies, beats); () is a rest.
    A chord with no length lasts 4 beats."""
    chords = []
    for token in str(score).replace("|", " ").replace(",", " ").split():
        name, _, beats = token.partition(":")
        try:
            length = float(beats) if beats else 4.0
        except ValueError:
            raise ValueError("%r: the length is a number of beats - %s" % (token, HOW_PAD))
        if not 0 < length <= 64:
            raise ValueError("%r: a chord lasts between 0 and 64 beats" % token)
        if name in _REST:
            voices = ()
        else:
            parts = [p for p in name.split("+") if p]
            if not parts or len(parts) > MAX_VOICES:
                raise ValueError("%r: a chord has 1 to %d voices - %s"
                                 % (token, MAX_VOICES, HOW_PAD))
            voices = tuple(note_freq(p) for p in parts)
        chords.append((voices, length))
    if not chords:
        raise ValueError("the score is empty - " + HOW_PAD)
    if len(chords) > MAX_CHORDS:
        raise ValueError("%d chords; a piece holds %d at most" % (len(chords), MAX_CHORDS))
    return chords
