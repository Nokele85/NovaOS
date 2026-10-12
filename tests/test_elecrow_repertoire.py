"""Stored notation is not a guessed tune (SPEC §26.7)."""
import json
from pathlib import Path

import pytest

from tools import elecrow_repertoire as music


def xml(notes, *, attributes='', direction='', ending=''):
    return (f'<score-partwise><work><work-title>Practice</work-title></work>'
            f'<part-list><score-part id="P1"><part-name>Melody</part-name></score-part></part-list>'
            f'<part id="P1"><measure number="1"><attributes><divisions>2</divisions>'
            f'{attributes}</attributes>{direction}{notes}{ending}</measure></part></score-partwise>').encode()


def note(name='C', duration=2, *, alter=0, extra='', voice='1'):
    return (f'<note><pitch><step>{name}</step><alter>{alter}</alter><octave>4</octave></pitch>'
            f'<duration>{duration}</duration><voice>{voice}</voice>{extra}</note>')


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv('ELECROW_REPERTOIRE', str(tmp_path))
    return tmp_path


def test_xml_preserves_accidentals_durations_and_rests():
    parsed = music.parse_musicxml(xml(note('F', 3, alter=1) +
        '<note><rest/><duration>1</duration></note>' + note('G', 2)))
    assert parsed['melody'] == 'F#4:1.5 -:0.5 G4:1'
    assert parsed['beats'] == 3
    assert parsed['confirmed'] is False


def test_tied_notes_are_held_not_rearticulated():
    parsed = music.parse_musicxml(xml(note(extra='<tie type="start"/>') +
        note(duration=4, extra='<tie type="stop"/>')))
    assert parsed['melody'] == 'C4:3'


def test_basic_repeat_is_expanded():
    parsed = music.parse_musicxml(xml(note(), ending='<barline><repeat direction="backward"/></barline>'))
    assert parsed['melody'] == 'C4:1 C4:1'


def test_voice_selection_does_not_mix_piano_hands():
    raw = xml(note('C', voice='1') + '<backup><duration>2</duration></backup>' + note('E', voice='2'))
    assert music.parse_musicxml(raw, voice='1')['melody'] == 'C4:1'
    assert music.parse_musicxml(raw, voice='2')['melody'] == 'E4:1'


@pytest.mark.parametrize('raw', [
    xml(note(extra='<grace/>')),
    xml(note(extra='<tie type="stop"/>')),
    xml(note() + note('E', extra='<chord/>')),
    xml(note(), attributes='<transpose><chromatic>2</chromatic></transpose>'),
    xml(note(), ending='<barline><ending number="1"/></barline>'),
    xml(note(), direction='<direction><sound tempo="60"/></direction><direction><sound tempo="80"/></direction>'),
])
def test_unsupported_notation_is_not_guessed(raw):
    with pytest.raises(ValueError):
        music.parse_musicxml(raw)


@pytest.mark.parametrize('melody', ['C4+E4:2', 'C2:1', 'C7:1', 'C4:0', 'C4:17', '', 'C4:NaN'])
def test_invalid_or_folded_melody_is_refused(melody):
    with pytest.raises(ValueError):
        music.validate_melody(melody)


def test_save_reload_correct_and_keep_history(store):
    first = music.learn('practice', 'Practice', 'C4 D4:2 -:0.5', 81.5)
    assert first['revision'] == 1
    assert music.library(song_id='practice')['melody'] == 'C4:1 D4:2 -:0.5'
    music.learn('practice', 'Practice', 'C4 E4:2 -:0.5', 81.5, revision=1,
                correction='second note corrected')
    corrected = music.library(song_id='practice')
    assert corrected['revision'] == 2 and corrected['confirmed'] is False
    old = json.loads((store / 'history/practice-1.json').read_text())
    assert old['melody'] == first['melody']
    with pytest.raises(ValueError, match='Revision mismatch'):
        music.learn('practice', 'Practice', 'G4', revision=1)
    assert music.library(song_id='practice')['revision'] == 2


def test_confirmed_version_does_not_confirm_a_correction(store):
    music.learn('practice', 'Practice', 'C4', confirmed=True)
    music.learn('practice', 'Practice', 'D4', revision=1)
    assert music.library(song_id='practice')['confirmed'] is False


def test_source_and_part_provenance_are_kept(store):
    parsed = music.parse_musicxml(xml(note()))
    (store / 'sources').mkdir()
    source = {**parsed, 'source_id': 'abc123', 'url': 'https://example.org/practice.xml', 'sha256': 'fullhash'}
    (store / 'sources/abc123.json').write_text(json.dumps(source))
    learned = music.learn('practice', 'Practice', parsed['melody'], parsed['tempo'], source_id='abc123')
    assert learned['method'] == 'musicxml-exact'
    assert learned['source']['sha256'] == 'fullhash'


def test_saved_song_is_played_without_model_or_recomposition(store, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
    import elecrow_mcp_server as server
    music.learn('practice', 'Practice', 'C4:1 D4:2', tempo=81.5)
    calls = []
    def board(port, snippets, timeout):
        calls.extend(snippets)
        return {'ok': True, 'stdout': '{"playing": true}', 'port': 'wifi:test'}
    monkeypatch.setattr(server, '_device_call', board)
    result = server.build_server()._tool_manager.get_tool('device_play_learned').fn(song_id='practice')
    assert result['ok'] is True
    assert "melody='C4:1 D4:2'" in calls[0]
    assert 'tempo=81.5' in calls[0]
    assert "lead='piano'" in calls[0]
    assert result['revision'] == 1 and result['confirmed'] is False
