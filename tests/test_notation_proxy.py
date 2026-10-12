"""Recognized sheet music cannot masquerade as owner confirmation (SPEC §26.7)."""
import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from .test_elecrow_repertoire import note, xml


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
    spec = importlib.util.spec_from_file_location('notation_proxy_test',
        Path(__file__).resolve().parents[1] / 'tools/elecrow_mcp_stdio_proxy.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'HERE', tmp_path / 'tools')
    monkeypatch.setattr(module, 'SCREENS', tmp_path / 'data/screens')
    return module


async def test_source_images_and_documents_never_reach_the_language_model(proxy, monkeypatch):
    async def optical(document, data):
        assert document == 'document-bytes'
        return {'status': 'parsed', 'melody': 'C4:1', 'confirmed': False}
    monkeypatch.setattr(proxy, '_optical_notation', optical)
    result = await proxy._post_process({'notation_png_b64': 'image-bytes',
        'notation_document_b64': 'document-bytes', 'source_id': 'a' * 20})
    assert 'notation_png_b64' not in result and 'notation_document_b64' not in result
    assert result['transcription']['confirmed'] is False


async def test_cached_optical_notes_are_parsed_and_stay_unconfirmed(proxy):
    config = proxy.HERE.parent / 'secrets/elecrow_notation.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'command': ['/does/not/run/for/cached/output']}))
    directory = proxy.SCREENS.parent / 'elecrow-notation' / ('a' * 20 + '-1')
    directory.mkdir(parents=True)
    # A plain XML payload is supported by the MusicXML byte reader too.
    (directory / 'source.mxl').write_bytes(xml(note('D')))
    result = await proxy._optical_notation(base64.b64encode(b'%PDF-test').decode(),
        {'source_id': 'a' * 20, 'page': 1})
    assert result['melody'] == 'D4:1'
    assert result['method'] == 'audiveris-omr' and result['confirmed'] is False


async def test_no_optical_configuration_means_fallback_not_fake_notes(proxy):
    assert await proxy._optical_notation('', {}) is None
