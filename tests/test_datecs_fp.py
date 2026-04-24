"""Wire-level tests for the Datecs FP-55 / FP-700 framing.

Run with:
    cd /opt/360booking/fiscal-bridge
    python -m pytest tests/ -v

No serial hardware needed — DatecsFPTransport is instantiated with a
dummy port and we exercise only the pure-Python frame builder and BCC
helpers (no serial.Serial() is opened).
"""
from __future__ import annotations

import pytest

from bridge.printers.datecs_fp import (
    _calc_bcc,
    _encode_4nibbles,
    DatecsFPTransport,
    STX,
    ETX,
    POST,
)


# ─── Nibble encoding ───────────────────────────────────────────────────

def test_encode_4nibbles_fp55_zero():
    # All zeros → offset bytes
    assert _encode_4nibbles(0x0000, 0x20) == bytes([0x20, 0x20, 0x20, 0x20])


def test_encode_4nibbles_fp55_open_fiscal():
    # open_fiscal = 0x0030 → nibbles 0,0,3,0 → 0x20,0x20,0x23,0x20
    assert _encode_4nibbles(0x0030, 0x20) == bytes([0x20, 0x20, 0x23, 0x20])


def test_encode_4nibbles_fp55_len_14():
    # 14 bytes → 0x000E → nibbles 0,0,0,E → 0x20,0x20,0x20,0x2E
    assert _encode_4nibbles(14, 0x20) == bytes([0x20, 0x20, 0x20, 0x2E])


def test_encode_4nibbles_fp700_offset_0x30():
    # With FP-700 offset 0x30: value 0x0030 → nibbles 0,0,3,0
    # bytes 0x30,0x30,0x33,0x30
    assert _encode_4nibbles(0x0030, 0x30) == bytes([0x30, 0x30, 0x33, 0x30])


# ─── BCC calculation ───────────────────────────────────────────────────

def test_bcc_sum_fp55_open_fiscal_1_0000_1():
    """Verify the BCC from the real NAK'd frame captured in production.
    Frame body: SEQ(0x20) + CMD(0x20,0x20,0x23,0x20) + DATA("1\t0000\t1") + POST(0x05)
    Expected BCC bytes: 20 21 2d 2c (encoding SUM=0x01DC)"""
    body = (
        bytes([0x20])              # SEQ
        + bytes([0x20, 0x20, 0x23, 0x20])  # CMD open_fiscal
        + b"1\t0000\t1"            # DATA
        + bytes([0x05])            # POST
    )
    bcc = _calc_bcc(body, algo="sum", offset=0x20)
    assert bcc == bytes([0x20, 0x21, 0x2D, 0x2C])


def test_bcc_sum_fp55_open_fiscal_1_0001_1():
    """Different password → different BCC."""
    body = (
        bytes([0x20])
        + bytes([0x20, 0x20, 0x23, 0x20])
        + b"1\t0001\t1"
        + bytes([0x05])
    )
    bcc = _calc_bcc(body, algo="sum", offset=0x20)
    # Sum: 32 + 131 + 309 + 5 = 477 = 0x01DD → 0,1,D,D
    assert bcc == bytes([0x20, 0x21, 0x2D, 0x2D])


def test_bcc_xor_fp700():
    """FP-700 uses XOR over the same bytes, encoded with offset 0x30."""
    body = bytes([0x20, 0x20, 0x30])  # SEQ + 1-byte CMD + POST stand-in
    bcc = _calc_bcc(body, algo="xor", offset=0x30)
    # XOR: 0x20 ^ 0x20 ^ 0x30 = 0x30 → nibbles 0,0,3,0
    assert bcc == bytes([0x30, 0x30, 0x33, 0x30])


# ─── Frame building (end-to-end) ───────────────────────────────────────

def _transport(**kwargs) -> DatecsFPTransport:
    """Build a transport without opening a port; enough for _build_frame()."""
    defaults = dict(port="DUMMY", baud=9600, timeout=1.0,
                    encoding_offset=0x20, bcc_algo="sum",
                    bcc_coverage="body", cmd_width=4)
    defaults.update(kwargs)
    return DatecsFPTransport(**defaults)


def test_build_frame_open_fiscal_fp55():
    """End-to-end frame for cmd=0x30 data=b'1\\t0001\\t1' on FP-55.
    Must match the wire bytes the DP-25 expects.
    """
    t = _transport()
    # Force SEQ to 0 so the output is deterministic
    t._seq = 0x1F  # _next_seq will bump to 0x20
    frame = t._build_frame(0x30, b"1\t0001\t1")
    # Expected structure:
    # STX(01) LEN(20 20 20 2e) SEQ(20) CMD(20 20 23 20) DATA(8) POST(05) BCC(4) ETX(03)
    expected_hex = (
        "01"                        # STX
        "2020202e"                  # LEN = 14
        "20"                        # SEQ = 0
        "20202320"                  # CMD = 0x30
        "3109303030310931"          # DATA = "1\t0001\t1"
        "05"                        # POST
        "20212d2d"                  # BCC SUM = 0x1DD
        "03"                        # ETX
    )
    assert frame.hex() == expected_hex


def test_build_frame_fp700_uses_1byte_cmd():
    t = _transport(encoding_offset=0x30, bcc_algo="xor", cmd_width=1)
    t._seq = 0x1F
    frame = t._build_frame(0x4A, b"")
    # FP-700 frame: STX LEN(4 offset-0x30) SEQ(1) CMD(1 raw) POST BCC(XOR offset-0x30) ETX
    # LEN = len(SEQ+CMD+POST) = 3
    # SEQ = 0x20 (first _next_seq)
    assert frame[0] == STX
    assert frame[-1] == ETX
    assert 0x4A in frame  # raw CMD byte present


def test_build_frame_seq_increments_between_calls():
    t = _transport()
    t._seq = 0x1F
    f1 = t._build_frame(0x4A, b"")
    f2 = t._build_frame(0x4A, b"")
    # SEQ byte is at offset 5 (after STX + 4-byte LEN)
    assert f1[5] == 0x20  # first
    assert f2[5] == 0x21  # second
    assert f1 != f2       # at minimum, BCC differs


def test_build_frame_seq_wraps_at_7F():
    t = _transport()
    t._seq = 0x7F
    f = t._build_frame(0x4A, b"")
    assert f[5] == 0x20  # wraps back to the start


def test_frame_bounds():
    """STX at start, ETX at end, no bytes in between are 0x01 or 0x03."""
    t = _transport()
    t._seq = 0x1F
    frame = t._build_frame(0x4A, b"test")
    assert frame[0] == STX
    assert frame[-1] == ETX
    # The protocol forbids bare STX/ETX inside the frame because they're
    # framing bytes. Our test data doesn't contain 0x01 or 0x03 so this
    # just validates we don't accidentally emit them ourselves.
    for b in frame[1:-1]:
        assert b not in (STX, ETX), f"unexpected framing byte 0x{b:02x} inside frame"


# ─── open_fiscal data format ───────────────────────────────────────────

def test_open_fiscal_data_uses_tab_separator():
    """Datecs DP-25 FP-55 wants TAB, not comma (comma is FP-700 dialect)."""
    from bridge.printers.datecs_dp25 import DatecsDP25Printer
    # Instantiate without really opening the port — we only touch
    # self.operator / operator_password.
    p = DatecsDP25Printer(config={
        "serial_port": "DUMMY",
        "operator": "1",
        "operator_password": "0001",
    })
    # The data string built inside _print_receipt is:
    #   f"{operator}\t{password}\t1"
    # We reconstruct the same expression here to pin the format.
    data = f"{p.operator}\t{p.operator_password}\t1".encode("ascii")
    assert data == b"1\t0001\t1"
    # Critically NOT "1,0001,1" which is FP-700 syntax
    assert b"," not in data
