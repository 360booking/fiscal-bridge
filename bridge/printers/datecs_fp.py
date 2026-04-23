"""Datecs FP-700 / FP-2000 family ASCII protocol (DP-25, DP-55, etc.).

Frame layout (bytes):
  STX    0x01
  LEN    4 bytes — each byte = (nibble & 0xF) + 0x30
  SEQ    1 byte, 0x20-0x7F, incremented per request
  CMD    1 byte command code, raw binary
  DATA   payload bytes (CP1250 for RO diacritics)
  POST   0x05
  BCC    4 bytes XOR checksum — each byte = (nibble & 0xF) + 0x30
  ETX    0x03

The key subtlety: LEN and BCC are NOT ASCII hex. Each nibble is
offset by 0x30 so nibble 10 encodes as byte 0x3A (':'), not 'A'.

The checksum runs XOR across SEQ..POST (inclusive).

Reference: Datecs FP-700 integrator manual §3. Field widths may differ
on exotic firmwares — the constants near the top of the file are
overridable via the `config` dict if we ever hit one.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import serial

logger = logging.getLogger("bridge.datecs_fp")


# ---- Frame constants ----------------------------------------------------

STX = 0x01
ETX = 0x03
POST = 0x05
SYN = 0x16
NAK = 0x15
ACK = 0x06


def _encode_4nibbles(value: int) -> bytes:
    """Datecs nibble encoding: 4 bytes, each = (nibble & 0xF) + 0x30.
    Example: 0x0123 → b'0123' (0x30 0x31 0x32 0x33)
             0x00AB → b'00:;' (0x30 0x30 0x3A 0x3B)
    """
    return bytes([
        ((value >> 12) & 0xF) + 0x30,
        ((value >> 8) & 0xF) + 0x30,
        ((value >> 4) & 0xF) + 0x30,
        (value & 0xF) + 0x30,
    ])


def _calc_bcc(payload: bytes) -> bytes:
    """XOR checksum across `payload`. 16-bit result, encoded per the
    Datecs 4-nibble convention. In practice the cumulative XOR of the
    bytes is 0-255 but we pad to 16 bits so the encoding stays 4 bytes."""
    x = 0
    for b in payload:
        x ^= b
    return _encode_4nibbles(x)


class DatecsFPError(Exception):
    pass


@dataclass
class FrameResponse:
    cmd: int
    data: bytes
    status: bytes          # 6 status bytes from the device
    raw: bytes


class DatecsFPTransport:
    """Opens the COM port, sends framed commands, reads framed replies."""

    def __init__(self, port: str, baud: int = 9600, timeout: float = 3.0):
        # FP-700 family defaults to 9600 baud on the serial line; we
        # honour whatever the caller configured in BridgeConfig.
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser: Optional[serial.Serial] = None
        self._seq = 0x1F  # first _next_seq() returns 0x20

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
        # Flush any stale device chatter from a previous aborted txn.
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()

    # -- framing --

    def _next_seq(self) -> int:
        self._seq = 0x20 if self._seq >= 0x7F else self._seq + 1
        return self._seq

    def _build_frame(self, cmd: int, data: bytes) -> bytes:
        seq = self._next_seq()
        # Body = SEQ + CMD + DATA + POST
        body = bytes([seq, cmd]) + data + bytes([POST])
        # LEN is the length of the body, encoded per nibble convention.
        length_enc = _encode_4nibbles(len(body))
        # BCC runs over LEN + body (i.e. everything after STX, before BCC itself).
        bcc_target = length_enc + body
        bcc = _calc_bcc(bcc_target)
        return bytes([STX]) + length_enc + body + bcc + bytes([ETX])

    def _read_frame(self) -> bytes:
        """Read one complete frame ending at ETX.

        Accepts NAK / SYN as in-band signals: NAK means the device
        rejected our last write (re-raise as DatecsFPError), SYN is a
        "processing, wait" ping and resets the deadline.
        """
        if not self._ser:
            raise DatecsFPError("Port not open")
        buf = bytearray()
        deadline = time.monotonic() + max(self.timeout, 3.0)
        while time.monotonic() < deadline:
            b = self._ser.read(1)
            if not b:
                continue
            byte = b[0]
            if byte == SYN:
                deadline = time.monotonic() + self.timeout
                continue
            if byte == NAK:
                logger.warning("Device returned NAK — frame rejected")
                raise DatecsFPError("Device NAK")
            if byte == STX:
                buf = bytearray()
                continue
            if byte == ETX:
                return bytes(buf)
            buf.append(byte)
        raise DatecsFPError(f"Frame timeout after {self.timeout}s")

    # -- send/receive --

    def execute(self, cmd: int, data: bytes = b"") -> FrameResponse:
        if not self._ser:
            raise DatecsFPError("Port not open")
        frame = self._build_frame(cmd, data)
        # INFO-level so the bytes are visible without --verbose.
        logger.info("→ cmd=0x%02X data=%r  frame=%s", cmd, data, frame.hex())
        self._ser.write(frame)
        self._ser.flush()
        try:
            raw = self._read_frame()
        except DatecsFPError as exc:
            # Dump anything that came back before the error
            pending = self._ser.read(64) if self._ser.in_waiting else b""
            logger.info("← (error) %s  pending=%s", exc, pending.hex())
            raise
        logger.info("← raw=%s", raw.hex())
        # raw = LEN(4) + SEQ(1) + CMD(1) + DATA + POST + STATUS(6) + BCC(4)
        if len(raw) < 4 + 1 + 1 + 1 + 6 + 4:
            raise DatecsFPError(f"Short frame: {raw!r}")
        cmd_echo = raw[5]
        try:
            post_idx = raw.index(POST, 6)
        except ValueError:
            raise DatecsFPError("Malformed reply — no POST marker")
        data_bytes = raw[6:post_idx]
        status = raw[post_idx + 1 : post_idx + 7]
        return FrameResponse(cmd=cmd_echo, data=data_bytes, status=status, raw=raw)
