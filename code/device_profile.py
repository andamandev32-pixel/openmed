"""
Device profiles for RS-232 medical devices.

One code path, many devices: a profile is a JSON file that describes how a
device frames its data and where the measurement values sit.  The same
profile drives both directions --

    build_frame()    profile -> bytes   (used by device_simulator.py)
    extract_frames() bytes   -> frames  (used by device_reader.py)
    parse_frame()    frame   -> values

Adding a new device means adding a profile file, not editing code.

Frame families supported (see docs/ chapter 7):
    stx-etx      0x02 <body> 0x03 <checksum>      no line terminator
                 Terumo BR-500 works this way.
    line         <body> CRLF                      optionally wrapped in STX/ETX
                 Omron / AND / BAM scales work this way.

The frame splitter handles BOTH at once on purpose.  A splitter that only
waits for CRLF leaves Terumo frames stuck in the buffer forever -- that bug
was hit for real in Smart_OPD_Kiosk_V2 and is worth never repeating.
"""

import json
import os
import time

PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")

STX = 0x02
ETX = 0x03
CR = 0x0D
LF = 0x0A


# --------------------------------------------------------------------------
# checksums
# --------------------------------------------------------------------------
def checksum_sum8(data: bytes) -> int:
    """Terumo BCC: add every byte, keep the low 8 bits."""
    return sum(data) & 0xFF


def checksum_xor8(data: bytes) -> int:
    acc = 0
    for b in data:
        acc ^= b
    return acc & 0xFF


CHECKSUMS = {
    "sum8": checksum_sum8,
    "xor8": checksum_xor8,
    "none": None,
}


class ProfileError(Exception):
    pass


class DeviceProfile:
    def __init__(self, data: dict, path: str = ""):
        self.path = path
        self.raw = data

        self.id = data.get("id") or "unnamed"
        self.name = data.get("name") or self.id
        self.verified = bool(data.get("verified", False))
        self.source = data.get("source", "")
        self.notes = data.get("notes", [])

        port = data.get("port", {})
        self.baud = int(port.get("baud", 9600))
        self.data_bits = int(port.get("dataBits", 8))
        self.parity = port.get("parity", "none")
        self.stop_bits = int(port.get("stopBits", 1))
        self.alternate_bauds = [int(b) for b in port.get("alternateBauds", [])]

        fr = data.get("framing", {})
        self.stx = _as_byte(fr.get("stx"))
        self.etx = _as_byte(fr.get("etx"))
        self.checksum_name = fr.get("checksum", "none")
        if self.checksum_name not in CHECKSUMS:
            raise ProfileError(
                f"{self.id}: unknown checksum {self.checksum_name!r}; "
                f"expected one of {sorted(CHECKSUMS)}"
            )
        self.checksum_fn = CHECKSUMS[self.checksum_name]
        # Which bytes the checksum covers.  Terumo includes STX and ETX.
        self.checksum_range = fr.get("checksumRange", "stx-to-etx")
        self.line_terminator = fr.get("lineTerminator")  # e.g. "\r\n" or None

        body = data.get("body", {})
        self.delimiter = body.get("delimiter", ",")
        self.record_id = body.get("recordId")
        self.min_fields = int(body.get("minFields", 0))
        self.template = body.get("template", [])

        self.fields = {k: int(v) for k, v in data.get("fields", {}).items()}
        self.widths = {k: int(v) for k, v in data.get("widths", {}).items()}
        self.formats = data.get("formats", {})
        self.defaults = data.get("defaults", {})

        if not self.template:
            raise ProfileError(f"{self.id}: body.template is required")
        if self.stx is None and self.etx is None and not self.line_terminator:
            raise ProfileError(
                f"{self.id}: profile has no frame boundary "
                f"(needs stx/etx or lineTerminator)"
            )

    # ----------------------------------------------------------------- build
    def build_body(self, values: dict) -> str:
        """Fill the template with values and join with the delimiter."""
        merged = dict(self.defaults)
        merged.update(values or {})
        merged.setdefault("date", time.strftime(self.formats.get("date", "%y%m%d")))
        merged.setdefault("time", time.strftime(self.formats.get("time", "%H%M%S")))

        # MAP is derived when the caller did not supply it.  Same formula the
        # kiosk uses, and the same one that proves a field map is correct.
        if "map" in self.fields and merged.get("map") in (None, "", 0, "0"):
            try:
                merged["map"] = round((int(merged["sys"]) + 2 * int(merged["dia"])) / 3)
            except (KeyError, TypeError, ValueError):
                pass

        out = []
        for token in self.template:
            out.append(self._render_token(token, merged))
        return self.delimiter.join(out)

    def _render_token(self, token: str, values: dict) -> str:
        if not (isinstance(token, str) and token.startswith("{") and token.endswith("}")):
            return str(token)  # literal
        key = token[1:-1]
        if key not in values:
            raise ProfileError(f"{self.id}: template needs value {key!r}")
        text = str(values[key])
        width = self.widths.get(key)
        if width:
            text = text.zfill(width)
        return text

    def build_frame(self, values: dict) -> bytes:
        """Whole frame, ready to write to the serial port."""
        frame = bytearray()
        if self.stx is not None:
            frame.append(self.stx)
        frame.extend(self.build_body(values).encode("ascii", errors="replace"))
        if self.etx is not None:
            frame.append(self.etx)
        if self.checksum_fn is not None:
            frame.append(self.checksum_fn(self._checksum_source(bytes(frame))))
        if self.line_terminator:
            frame.extend(self.line_terminator.encode("ascii"))
        return bytes(frame)

    def _checksum_source(self, frame_so_far: bytes) -> bytes:
        """frame_so_far is STX + body + ETX (no checksum yet)."""
        if self.checksum_range == "body-only":
            start = 1 if self.stx is not None else 0
            end = len(frame_so_far) - (1 if self.etx is not None else 0)
            return frame_so_far[start:end]
        return frame_so_far  # "stx-to-etx" (Terumo)

    # --------------------------------------------------------------- extract
    def extract_frames(self, buf: bytes):
        """Pull complete frames out of a receive buffer.

        Returns (frames, remainder).  Handles ETX-terminated frames that carry
        a trailing checksum byte AND CRLF-terminated lines, whichever shows up
        first, so one reader copes with both device families.
        """
        frames = []
        i = 0
        start = 0
        n = len(buf)
        while i < n:
            b = buf[i]

            if self.etx is not None and b == self.etx:
                end = i + 1
                if self.checksum_fn is not None:
                    end += 1
                    if end > n:
                        break  # checksum byte has not arrived yet
                frames.append(buf[start:end])
                # A device may pad with CRLF after the frame; skip it.
                while end < n and buf[end] in (CR, LF):
                    end += 1
                i = start = end
                continue

            if b in (CR, LF):
                chunk = buf[start:i]
                if chunk.strip():
                    frames.append(chunk)
                i += 1
                start = i
                continue

            i += 1

        return frames, buf[start:]

    # ----------------------------------------------------------------- parse
    def parse_frame(self, frame: bytes) -> dict:
        """Turn one frame into {'sys':…, 'dia':…, 'map':…, 'pulse':…, …}.

        Strips control bytes first.  Skipping that step lets the trailing BCC
        stick to the last field (or to parts[0] on the next frame) and the
        record-id check then fails for no visible reason.
        """
        text = "".join(chr(b) for b in frame if 0x20 <= b <= 0x7E).strip()
        parts = [p.strip() for p in text.split(self.delimiter)]

        result = {
            "ok": False,
            "raw": text,
            "parts": parts,
            "checksum": None,
            "error": None,
        }

        if len(parts) < self.min_fields:
            result["error"] = f"packet too short ({len(parts)} fields, need {self.min_fields})"
            return result

        if self.record_id and not parts[0].startswith(self.record_id[0]):
            result["error"] = f"not a result record (got {parts[0]!r})"
            return result

        if self.checksum_fn is not None and self.etx is not None:
            result["checksum"] = self.verify_checksum(frame)

        for name, idx in self.fields.items():
            if idx < len(parts):
                value = parts[idx]
                result[name] = int(value) if value.lstrip("-").isdigit() else value

        if "map" in self.fields and "sys" in result and "dia" in result:
            try:
                result["map_expected"] = round((int(result["sys"]) + 2 * int(result["dia"])) / 3)
            except (TypeError, ValueError):
                pass

        result["ok"] = True
        return result

    def verify_checksum(self, frame: bytes):
        """True / False, or None when the frame carries no checksum."""
        if self.checksum_fn is None or self.etx is None:
            return None
        idx = frame.rfind(self.etx)
        if idx < 0 or idx + 1 >= len(frame):
            return None
        want = frame[idx + 1]
        got = self.checksum_fn(self._checksum_source(frame[: idx + 1]))
        return got == want

    def describe_port(self) -> str:
        p = {"none": "N", "even": "E", "odd": "O"}.get(self.parity, "N")
        return f"{self.baud}-{self.data_bits}-{p}-{self.stop_bits}"

    def __repr__(self):
        flag = "verified" if self.verified else "UNVERIFIED"
        return f"<DeviceProfile {self.id} {self.describe_port()} {flag}>"


def _as_byte(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return int(str(value), 0)  # accepts "0x02"


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
def load_profile(path: str) -> DeviceProfile:
    with open(path, "r", encoding="utf-8") as fh:
        return DeviceProfile(json.load(fh), path)


def load_all(directory: str = PROFILE_DIR) -> dict:
    """{profile_id: DeviceProfile} for every .json in the profiles folder."""
    profiles = {}
    if not os.path.isdir(directory):
        return profiles
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        try:
            prof = load_profile(os.path.join(directory, name))
        except (ProfileError, ValueError) as exc:
            print(f"skipping {name}: {exc}")
            continue
        profiles[prof.id] = prof
    return profiles


def use_utf8_console():
    """Thai text comes out as mojibake on the default Windows code page."""
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            if (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def to_hex(data: bytes) -> str:
    """Hercules-style HEX view: '02 52 31 2C ...'"""
    return " ".join(f"{b:02X}" for b in data)


def to_readable(data: bytes) -> str:
    """ASCII with control bytes spelled out, for on-screen logs."""
    names = {0x02: "<STX>", 0x03: "<ETX>", 0x06: "<ACK>", 0x15: "<NAK>",
             0x05: "<ENQ>", 0x0D: "<CR>", 0x0A: "<LF>"}
    out = []
    for b in data:
        if b in names:
            out.append(names[b])
        elif 0x20 <= b <= 0x7E:
            out.append(chr(b))
        else:
            out.append(f"<{b:02X}>")
    return "".join(out)


if __name__ == "__main__":
    for pid, prof in load_all().items():
        print(f"{pid:16} {prof.describe_port():14} "
              f"{'verified' if prof.verified else 'UNVERIFIED':10} {prof.name}")
