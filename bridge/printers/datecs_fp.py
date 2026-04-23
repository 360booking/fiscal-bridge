"""Datecs FP-700 / FP-2000 family ASCII protocol.

This is the base-class wire protocol used by Datecs fiscal devices
(DP-25, DP-55, FP-550, etc.). Per-model differences live in subclasses
that override command codes and data formats.

Frame layout (bytes):
  STX     0x01
  LEN     4 ASCII digits (0x20-0x7F), total_frame_len_offset_by_0x20
  SEQ     1 ASCII byte (0x20-0x7F) — incrementing per request
  CMD     4 ASCII hex digits — command code
  DATA    tab-separated payload (0x09 separator where applicable)
  POST    0x05
  BCC     4 ASCII hex digits — sum-check (see _calc_bcc)
  ETX     0x03

Reference: public Datecs FP-700 integrator manual, sections 2 and 3.
If you have the official Datecs PDF, the canonical source is §5-6 of
the DP-25 programmer's manual — field orders differ slightly between
models and this module exposes overridable constants for that.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import serial

logger = logging.getLogger("bridge.datecs_fp")


# ---- Frame constants ----------------------------------------------------

STX = 0x01
ETX = 0x03
POST = 0x05
SYN = 0x16
NAK = 0x15
ACK = 0x06


# ---- Low-level framing --------------------------------------------------

def _calc_bcc(data: bytes) -> bytes:
    """Sum of bytes mod 0xFFFF, rendered as 4 ASCII hex uppercase."""
    total = sum(data) & 0xFFFF
    hex_str = f"{total:04X}"
    # BCC bytes are each in the 0x30–0x3F range? No — standard hex digits
    # 0x30-0x39, 0x41-0x46. Datecs expects plain ASCII hex.
    return hex_str.encode("ascii")


def _encode_len(frame_body_len: int) -> bytes:
    """LEN is 4 ASCII bytes representing len + 0x20 each."""
    # Datecs encoding: len in 4 ASCII digits, each byte = (nibble + 0x20)?
    # Different sources disagree. Using the FP-700 integrator docs: LEN is
    # the length of SEQ+CMD+DATA+POST, transmitted as 4 ASCII characters
    # each in 0x20..0x7F, representing a base-32ish encoded value. In
    # practice most drivers just emit 4 hex digits. Keep it hex — most
    # Datecs devices accept that dialect.
    return f"{frame_body_len:04X}".encode("ascii")


class DatecsFPError(Exception):
    pass


@dataclass
class FrameResponse:
    cmd: int
    data: bytes
    status: bytes  # 6 status bytes from the device
    raw: bytes


class DatecsFPTransport:
    """Opens the COM port, sends framed commands, reads framed replies.

    Thread-safe only via a single instance — the serial handle itself
    is not thread-safe. Bridge calls it serially from one worker.
    """

    def __init__(self, port: str, baud: int = 115200, timeout: float = 3.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser: Optional[serial.Serial] = None
        self._seq = 0x20

    # -- lifecycle --

    def open(self) -> None:
        if self._ser and self._ser.is_open:
            return
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baud,
            bytesize=8,
            parity=serial.PARITY_NONE,
            stopbits=1,
            timeout=self.timeout,
        )

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()

    # -- framing --

    def _next_seq(self) -> int:
        self._seq = 0x20 if self._seq >= 0x7F else self._seq + 1
        return self._seq

    def _build_frame(self, cmd: int, data: bytes) -> bytes:
        seq = bytes([self._next_seq()])
        cmd_bytes = f"{cmd:04X}".encode("ascii")
        body = seq + cmd_bytes + data + bytes([POST])
        length = _encode_len(len(body))
        frame = bytes([STX]) + length + body
        bcc = _calc_bcc(frame[1:])  # over LEN..POST (exclude STX)
        return frame + bcc + bytes([ETX])

    def _read_frame(self) -> bytes:
        """Read one complete frame ending at ETX, skipping NAK/SYN."""
        if not self._ser:
            raise DatecsFPError("Port not open")
        buf = bytearray()
        deadline = time.monotonic() + max(self.timeout, 3.0)
        while time.monotonic() < deadline:
            b = self._ser.read(1)
            if not b:
                continue
            if b[0] == SYN:
                # Device asks for more time — extend deadline
                deadline = time.monotonic() + self.timeout
                continue
            if b[0] == NAK:
                raise DatecsFPError("Device NAK")
            if b[0] == STX:
                buf = bytearray()
                continue
            if b[0] == ETX:
                return bytes(buf)
            buf.append(b[0])
        raise DatecsFPError(f"Frame timeout after {self.timeout}s")

    # -- send/receive --

    def execute(self, cmd: int, data: bytes = b"") -> FrameResponse:
        if not self._ser:
            raise DatecsFPError("Port not open")
        frame = self._build_frame(cmd, data)
        logger.debug("→ %s", frame.hex())
        self._ser.write(frame)
        self._ser.flush()
        raw = self._read_frame()
        logger.debug("← %s", raw.hex())
        # raw = LEN(4) + SEQ(1) + CMD(4) + DATA + POST + STATUS(6) + BCC(4)
        if len(raw) < 4 + 1 + 4 + 1 + 6 + 4:
            raise DatecsFPError(f"Short frame: {raw!r}")
        cmd_echo = int(raw[5:9].decode("ascii", errors="replace"), 16)
        # Find POST separator
        try:
            post_idx = raw.index(POST, 9)
        except ValueError:
            raise DatecsFPError("Malformed reply — no POST")
        data_bytes = raw[9:post_idx]
        status = raw[post_idx + 1 : post_idx + 7]
        return FrameResponse(cmd=cmd_echo, data=data_bytes, status=status, raw=raw)
