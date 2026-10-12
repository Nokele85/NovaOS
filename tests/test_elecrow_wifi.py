"""LAN transport stays behind the existing tools (SPEC §26.6)."""
import importlib.util
from pathlib import Path
import socket
import struct

import pytest

_spec = importlib.util.spec_from_file_location("elecrow_wifi", Path(__file__).resolve().parents[1] / "tools/elecrow_wifi.py")
assert _spec and _spec.loader
wifi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wifi)


def stream():
    left, right = socket.socketpair()
    client = wifi.WebReplStream.__new__(wifi.WebReplStream)
    client.sock, client.buffer, client.timeout = left, bytearray(), 0.01
    return client, right


def test_client_frames_are_masked_and_preserve_control_bytes():
    client, peer = stream()
    try:
        client.write(b'\r\x03\x01')
        frame = peer.recv(100)
        assert frame[:2] == b'\x81\x83'
        mask = frame[2:6]
        assert bytes(v ^ mask[i % 4] for i, v in enumerate(frame[6:])) == b'\r\x03\x01'
    finally:
        client.close()
        peer.close()


def test_read_preserves_partial_output_and_long_frames():
    client, peer = stream()
    try:
        payload = b'x' * 300 + b'\x04\x04>'
        peer.sendall(b'\x82\x7e' + struct.pack('>H', len(payload)) + payload)
        assert client.read(256) + client.read(256) == payload
    finally:
        client.close()
        peer.close()


def test_close_is_not_a_successful_empty_reply():
    client, peer = stream()
    try:
        peer.sendall(b'\x88\x00')
        with pytest.raises(OSError, match='closed'):
            client.read(256)
    finally:
        client.close()
        peer.close()


def test_missing_configuration_disables_wifi(monkeypatch, tmp_path):
    monkeypatch.setenv('ELECROW_NETWORK_CONFIG', str(tmp_path / 'missing.json'))
    assert wifi.configuration() == {}
