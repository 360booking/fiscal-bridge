"""Datecs FP-55 / DP-25 ASCII protocol.

Frame layout:
  STX    0x01
  LEN    4 bytes — each byte = (nibble & 0xF) + 0x20
                   Represents body length = len(SEQ + CMD + DATA + POST)
  SEQ    1 byte, 0x20..0x7F, incremented per request
  CMD    4 bytes — each byte = (nibble & 0xF) + 0x20
                   Represents the command code as a 16-bit value
  DATA   payload bytes (tab-separated where relevant, CP1250 for RO diacritics)
  POST   0x05
  BCC    4 bytes — SUM of SEQ..POST, encoded like LEN
  ETX    0x03

This is the FP-55 variant used by DP-25, DP-150, FP-550, etc.
The older FP-700 spec used nibble+0x30 and XOR for BCC — different
protocol, same family. If we ever hit a device that needs FP-700,
add a `protocol_variant` flag and branch on it.

Reference: Datecs DP-25 integrator manual + the datecs-rs Rust crate
(github.com/ovidiu-pristav/datecs-rs) which was written by someone who
reverse-engineered the real wire bytes against a physical DP-25.
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


def _encode_4nibbles(value: int, offset: int = 0x20) -> bytes:
    """Datecs nibble encoding — 4 bytes, each = (nibble & 0xF) + offset.
    FP-55 uses offset=0x20, FP-700 uses offset=0x30. The `offset` is
    pushable from the backend via protocol_config.encoding_offset so
    we can flip variants without a new build.
    """
    return bytes([
        ((value >> 12) & 0xF) + offset,
        ((value >> 8) & 0xF) + offset,
        ((value >> 4) & 0xF) + offset,
        (value & 0xF) + offset,
    ])


def _calc_bcc(payload: bytes, algo: str = "sum", offset: int = 0x20) -> bytes:
    """Compute the 4-nibble BCC for `payload`.

    algo: "sum" (FP-55) or "xor" (FP-700).
    offset: nibble offset for encoding (matches frame LEN/CMD encoding).
    """
    if algo == "xor":
        total = 0
        for b in payload:
            total ^= b
    else:
        total = 0
        for b in payload:
            total += b
    total &= 0xFFFF
    return _encode_4nibbles(total, offset)


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

    def __init__(
        self,
        port: str,
        baud: int = 9600,
        timeout: float = 3.0,
        *,
        encoding_offset: int = 0x20,
        bcc_algo: str = "sum",
        bcc_coverage: str = "body",
        cmd_width: int = 4,
    ):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.encoding_offset = encoding_offset
        self.bcc_algo = bcc_algo
        self.bcc_coverage = bcc_coverage
        self.cmd_width = cmd_width
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
        # CMD encoding: FP-55 uses 4 nibble-encoded bytes, FP-700 uses
        # a single raw byte. Switchable at runtime via cmd_width.
        if self.cmd_width == 4:
            cmd_enc = _encode_4nibbles(cmd, self.encoding_offset)
        else:
            cmd_enc = bytes([cmd])
        body = bytes([seq]) + cmd_enc + data + bytes([POST])
        length_enc = _encode_4nibbles(len(body), self.encoding_offset)
        bcc_target = body if self.bcc_coverage == "body" else length_enc + body
        bcc = _calc_bcc(bcc_target, algo=self.bcc_algo, offset=self.encoding_offset)
        return bytes([STX]) + length_enc + body + bcc + bytes([ETX])

    def _read_frame(self) -> bytes:
        """Read one complete frame ending at ETX.

        NAK = frame rejected (raise). SYN = "I'm busy, wait" (extend
        deadline). Everything else accumulates into the buffer.
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
        logger.info(
            "→ cmd=0x%02X data=%r  frame=%s  (variant=%s offset=0x%02X bcc=%s cmd_width=%d)",
            cmd, data, frame.hex(),
            "fp55" if self.encoding_offset == 0x20 else ("fp700" if self.encoding_offset == 0x30 else "custom"),
            self.encoding_offset, self.bcc_algo, self.cmd_width,
        )
        self._ser.write(frame)
        self._ser.flush()
        try:
            raw = self._read_frame()
        except DatecsFPError as exc:
            pending = self._ser.read(64) if self._ser.in_waiting else b""
            logger.info("← (error) %s  pending=%s", exc, pending.hex())
            raise
        logger.info("← raw=%s", raw.hex())
        # raw = LEN(4) + SEQ(1) + CMD(4) + DATA + POST + STATUS(6) + BCC(4)
        if len(raw) < 4 + 1 + 4 + 1 + 6 + 4:
            raise DatecsFPError(f"Short frame: {raw!r}")
        # CMD echoed as 4 nibble-encoded bytes starting at index 5
        cmd_echo = (
            ((raw[5] - 0x20) & 0xF) << 12
            | ((raw[6] - 0x20) & 0xF) << 8
            | ((raw[7] - 0x20) & 0xF) << 4
            | ((raw[8] - 0x20) & 0xF)
        )
        try:
            post_idx = raw.index(POST, 9)
        except ValueError:
            raise DatecsFPError("Malformed reply — no POST marker")
        data_bytes = raw[9:post_idx]
        status = raw[post_idx + 1 : post_idx + 7]
        return FrameResponse(cmd=cmd_echo, data=data_bytes, status=status, raw=raw)
