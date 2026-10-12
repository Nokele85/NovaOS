"""Read notation and keep editable scores, not recordings (SPEC §26.7)."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from fractions import Fraction
from html.parser import HTMLParser
from pathlib import Path

ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
NOTE = re.compile(r"^([A-G](?:#|b)?[2-7]|-)(?::([0-9]+(?:\.[0-9]+)?))?$")
MAX_BYTES = 8 * 1024 * 1024
INSTRUMENTS = {'piano', 'harp', 'vibes', 'musicbox', 'strings', 'choir', 'halo', 'warmpad'}


def root():
    return Path(os.environ.get('ELECROW_REPERTOIRE', str(
        Path(__file__).resolve().parent.parent / 'elecrow-firmware' / 'music-library')))


def _id(value):
    if not ID.fullmatch(value):
        raise ValueError('id must be lowercase letters, digits, underscores or hyphens')
    return value


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def validate_melody(melody):
    tokens = str(melody).replace('|', ' ').split()
    if not 1 <= len(tokens) <= 400:
        raise ValueError('A melody needs 1..400 notes/rests; split longer pieces into named sections')
    beats = 0.0
    canonical = []
    for token in tokens:
        match = NOTE.fullmatch(token)
        if not match:
            raise ValueError('Write a melody, not chords: C4:1 D4:0.5 -:0.5')
        name, length = match.groups()
        duration = float(length or 1)
        if not math.isfinite(duration) or not 0 < duration <= 16:
            raise ValueError('Note lengths must be greater than zero and at most 16 quarter-note beats')
        if name != '-':
            semitone = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}[name[0]]
            semitone += 1 if '#' in name else -1 if 'b' in name else 0
            midi = (int(name[-1]) + 1) * 12 + semitone
            frequency = 440 * 2 ** ((midi - 69) / 12)
            if not 130 <= frequency <= 1600:
                raise ValueError(f'{name} is outside the sample player range; transpose the whole piece explicitly')
        beats += duration
        canonical.append(f'{name}:{duration:g}')
    return ' '.join(canonical), len(tokens), round(beats, 6)


def parse_musicxml(raw, part='', voice='', chord_mode='reject'):
    """Exact written pitch/duration, explicit refusal of unsupported notation."""
    if len(raw) > MAX_BYTES or b'<!ENTITY' in raw.upper():
        raise ValueError('Notation too large or contains entity declarations')
    tree = ET.fromstring(raw)
    for element in tree.iter():
        element.tag = element.tag.rsplit('}', 1)[-1]
    if tree.tag != 'score-partwise':
        raise ValueError('Expected score-partwise MusicXML')
    names = {p.attrib['id']: p.findtext('part-name', '') for p in tree.findall('part-list/score-part')}
    parts = tree.findall('part')
    choices = [{'id': p.attrib['id'], 'name': names.get(p.attrib['id'], '')} for p in parts]
    selected = next((p for p in parts if p.attrib.get('id') == part), None)
    if not part and len(parts) == 1:
        selected = parts[0]
    if selected is None:
        return {'status': 'choose_part', 'parts': choices}
    voices = sorted({n.findtext('voice', '1') for n in selected.findall('measure/note')})
    voice = voice or (voices[0] if voices else '1')
    if voice not in voices:
        raise ValueError('Unknown voice; available: ' + ', '.join(voices))
    if chord_mode not in ('reject', 'highest', 'lowest'):
        raise ValueError('chord_mode is reject, highest or lowest')
    if selected.find('.//transpose') is not None:
        raise ValueError('Transposing instruments require a concert-pitch export')
    if selected.find('.//ending') is not None or selected.find('.//sound[@dacapo]') is not None or selected.find('.//sound[@dalsegno]') is not None:
        raise ValueError('Alternate endings / da capo / dal segno need an unfolded notation export')
    tempos = {float(s.attrib['tempo']) for s in selected.findall('.//sound') if 'tempo' in s.attrib}
    if len(tempos) > 1:
        raise ValueError('Tempo changes require separate named sections')
    tempo = next(iter(tempos), 80)
    divisions = Fraction(1)
    measures = []
    sequence = []
    repeat_start = 0
    for measure in selected.findall('measure'):
        events = []
        cursor = Fraction(0)
        previous_start = Fraction(0)
        previous_voice = '1'
        span = Fraction(0)
        for child in measure:
            if child.tag == 'attributes' and child.find('divisions') is not None:
                divisions = Fraction(child.findtext('divisions'))
                if divisions <= 0:
                    raise ValueError('Invalid divisions')
            elif child.tag in ('backup', 'forward'):
                length = Fraction(child.findtext('duration', '0')) / divisions
                cursor += length if child.tag == 'forward' else -length
                if cursor < 0:
                    raise ValueError('MusicXML backup moved before the measure')
            elif child.tag == 'note':
                if child.find('grace') is not None:
                    raise ValueError('Grace notes need an explicitly simplified practice edition')
                duration = Fraction(child.findtext('duration', '0')) / divisions
                if duration <= 0:
                    raise ValueError('Missing note duration')
                chord = child.find('chord') is not None
                note_voice = child.findtext('voice') or (previous_voice if chord else '1')
                start = previous_start if chord else cursor
                if not chord:
                    previous_voice = note_voice
                    previous_start = cursor
                    cursor += duration
                span = max(span, start + duration)
                if note_voice != voice:
                    continue
                pitch = child.find('pitch')
                name = '-'
                if pitch is not None:
                    alter = int(pitch.findtext('alter', '0'))
                    if alter not in (-1, 0, 1):
                        raise ValueError('Microtones/double accidentals need an explicit simplified edition')
                    name = pitch.findtext('step') + {-1: 'b', 0: '', 1: '#'}[alter] + pitch.findtext('octave')
                elif child.find('rest') is None:
                    raise ValueError('Unpitched percussion cannot be read as a melody')
                tied = {t.attrib.get('type') for t in child.findall('tie')}
                if chord:
                    if chord_mode == 'reject':
                        raise ValueError('Voice contains chords: choose a melody voice or explicitly select chord_mode=highest/lowest')
                    if not events or events[-1][0] != start or events[-1][2] != duration:
                        raise ValueError(f'Chord tones have different durations in measure {measure.get("number")}; use a monophonic edition')
                    def pitch_number(n):
                        if n == '-':
                            raise ValueError('A rest cannot be a chord tone')
                        semi = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}[n[0]]
                        return (int(n[-1]) + 1) * 12 + semi + (1 if '#' in n else -1 if 'b' in n else 0)
                    previous = events[-1]
                    choose = pitch_number(name) > pitch_number(previous[1])
                    if choose == (chord_mode == 'highest'):
                        events[-1] = (start, name, duration, tied)
                    continue
                events.append((start, name, duration, tied))
        measures.append((events, span))
        index = len(measures) - 1
        if measure.find('.//repeat[@direction="forward"]') is not None:
            repeat_start = len(sequence)
        sequence.append(index)
        backward = measure.find('.//repeat[@direction="backward"]')
        if backward is not None:
            times = int(backward.attrib.get('times', '2'))
            if not 2 <= times <= 4:
                raise ValueError('Repeat count must be 2..4')
            repeated = sequence[repeat_start:]
            sequence.extend(repeated * (times - 1))
            repeat_start = len(sequence)
    tune = []
    offset = Fraction(0)
    pending_tie = False
    for index in sequence:
        events, span = measures[index]
        local = Fraction(0)
        for start, name, duration, tied in sorted(events):
            if start < local:
                raise ValueError('Overlapping notes in the chosen melody voice')
            if start > local:
                tune.append(['-', start - local])
            if 'stop' in tied:
                if not pending_tie or not tune or tune[-1][0] != name or start != local:
                    raise ValueError('Unmatched tie')
                tune[-1][1] += duration
            else:
                if pending_tie:
                    raise ValueError('A tie does not connect to the next note')
                tune.append([name, duration])
            pending_tie = 'start' in tied
            local = start + duration
        if local < span:
            if pending_tie:
                raise ValueError('Tie interrupted by a rest')
            tune.append(['-', span - local])
        offset += span
    if pending_tie:
        raise ValueError('Unfinished tie at end of piece')
    tokens = []
    for name, duration in tune:
        while duration > 16:
            # Splitting a tied pitch would re-articulate it: fail visibly.
            if name != '-':
                raise ValueError('A sustained note exceeds 16 beats; use a shorter practice section')
            tokens.append('-:16')
            duration -= 16
        if duration:
            tokens.append(f'{name}:{float(duration):.8g}')
    melody, count, beats = validate_melody(' '.join(tokens))
    return {'status': 'parsed', 'title': tree.findtext('work/work-title', tree.findtext('movement-title', '')),
            'parts': choices, 'part': selected.attrib['id'], 'voices': voices, 'voice': voice,
            'melody': melody, 'tempo': tempo, 'notes': count, 'beats': beats,
            'method': 'musicxml', 'chord_mode': chord_mode, 'confirmed': False,
            'warning': ('Selected ' + chord_mode + ' note of each chord; confirm it is the intended melody.')
                       if chord_mode != 'reject' else ''}


def _xml_bytes(raw):
    if raw.startswith(b'PK'):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if archive.getinfo('META-INF/container.xml').file_size > 64 * 1024:
                raise ValueError('MusicXML container metadata too large')
            container = archive.read('META-INF/container.xml')
            tree = ET.fromstring(container)
            entry = next(e for e in tree.iter() if e.tag.endswith('rootfile'))
            info = archive.getinfo(entry.attrib['full-path'])
            if info.file_size > MAX_BYTES:
                raise ValueError('Uncompressed MusicXML too large')
            return archive.read(info)
    return raw


class Links(HTMLParser):
    def __init__(self, url):
        super().__init__()
        self.url = url
        self.links = []

    def handle_starttag(self, tag, attrs):
        row = dict(attrs)
        link = row.get('href') if tag == 'a' else row.get('src') if tag == 'img' else None
        if link and len(self.links) < 40 and re.search(r'\.(pdf|mxl|xml|musicxml|png|jpg|jpeg)(?:[?#]|$)', link, re.I):
            self.links.append(urllib.parse.urljoin(self.url, link))


def read_source(url, part='', voice='', page=1, chord_mode='reject'):
    if len(url) > 2048:
        raise ValueError('Notation URL exceeds 2048 characters')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
        raise ValueError('Use a public HTTP(S) sheet-music URL without credentials')
    request = urllib.request.Request(url, headers={'User-Agent': 'NovaNotation/1.0'})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(MAX_BYTES + 1)
        resolved = response.url
        content_type = response.headers.get('Content-Type', '')
    if len(raw) > MAX_BYTES:
        raise ValueError('Notation source exceeds 8 MB')
    digest = hashlib.sha256(raw).hexdigest()
    # The same file's different parts/pages are distinct notation sources.
    sid = hashlib.sha256(raw + f'{part}:{voice}:{page}:{chord_mode}'.encode()).hexdigest()[:20]
    location = root() / 'sources'
    location.mkdir(parents=True, exist_ok=True)
    (location / (sid + '.bin')).write_bytes(raw)
    metadata = {'source_id': sid, 'url': resolved, 'sha256': digest, 'bytes': len(raw), 'fetched': int(time.time())}
    if raw.startswith(b'%PDF') or 'image/' in content_type or raw.startswith((b'\x89PNG', b'\xff\xd8')):
        import pymupdf as fitz
        document = fitz.open(stream=raw, filetype='pdf' if raw.startswith(b'%PDF') else None)
        if not 1 <= page <= len(document):
            raise ValueError('Page outside the document')
        if not raw.startswith(b'%PDF'):
            document = fitz.open('pdf', document.convert_to_pdf())
        selected = document[page - 1]
        scale = min(1.8, 1800 / max(selected.rect.width, selected.rect.height))
        pix = selected.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        result = {**metadata, 'status': 'visual_notation', 'page': page, 'pages': len(document),
                  'notation_png_b64': base64.b64encode(pix.tobytes('png')).decode(),
                  'notation_document_b64': base64.b64encode(raw).decode(),
                  'notation_filetype': 'pdf' if raw.startswith(b'%PDF') else 'image',
                  'part': part, 'voice': voice, 'chord_mode': chord_mode,
                  'confirmed': False, 'warning': 'Image transcription is a candidate, not verified notes. Read each page separately.'}
        _write(location / (sid + '.json'), metadata)
        return result
    if 'html' in content_type or raw.lstrip().lower().startswith(b'<!doctype html'):
        parser = Links(resolved)
        parser.feed(raw.decode('utf-8', 'replace'))
        _write(location / (sid + '.json'), metadata)
        return {**metadata, 'status': 'choose_notation_link', 'links': parser.links,
                'message': 'Follow a sheet-music link with this tool; lyrics/chord labels are not a melody.'}
    result = {**metadata, **parse_musicxml(_xml_bytes(raw), part, voice, chord_mode)}
    _write(location / (sid + '.json'), result)
    return result


def library(query='', song_id=''):
    if song_id:
        return json.loads((root() / 'songs' / (_id(song_id) + '.json')).read_text(encoding='utf-8'))
    songs = []
    for path in sorted((root() / 'songs').glob('*.json')):
        row = json.loads(path.read_text(encoding='utf-8'))
        if query.lower() not in (row['id'] + ' ' + row['title']).lower():
            continue
        songs.append({k: row[k] for k in ('id', 'title', 'revision', 'tempo', 'notes', 'confirmed', 'method')})
    return {'songs': songs[:40], 'matches': len(songs)}


def learn(song_id, title, melody, tempo=80, source_id='', chords='', revision=0,
          confirmed=False, correction=''):
    song_id = _id(song_id)
    melody, count, beats = validate_melody(melody)
    if not 20 <= float(tempo) <= 160 or not math.isfinite(float(tempo)):
        raise ValueError('Tempo must be 20..160 quarter notes per minute')
    if not title.strip() or len(title) > 120 or len(correction) > 1000:
        raise ValueError('Use a title up to 120 characters and a short correction note')
    previous = None
    path = root() / 'songs' / (song_id + '.json')
    if path.exists():
        previous = library(song_id=song_id)
    if revision != (previous['revision'] if previous else 0):
        raise ValueError('Revision mismatch: load the current song before changing it')
    source = json.loads((root() / 'sources' / (_id(source_id) + '.json')).read_text()) if source_id else {}
    exact = source.get('method') == 'musicxml' and source.get('melody') == melody and source.get('tempo') == tempo
    row = {'id': song_id, 'title': title.strip(), 'melody': melody, 'chords': chords,
           'tempo': float(tempo), 'notes': count, 'beats': beats,
           'seconds': round(beats * 60 / float(tempo), 3), 'revision': revision + 1,
           'confirmed': bool(confirmed), 'method': 'musicxml-exact' if exact else 'transcription-or-correction',
           'source': {k: source[k] for k in ('source_id', 'url', 'sha256') if k in source},
           'correction': correction, 'updated': int(time.time()),
           'warning': '' if confirmed else 'Not yet confirmed by Benni; play as a practice candidate.'}
    if previous:
        _write(root() / 'history' / f'{song_id}-{previous["revision"]}.json', previous)
    _write(path, row)
    return row
