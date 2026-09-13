"""
Round-trip tests for device profiles.

Build a frame from a profile, feed it back through the frame splitter and the
parser, and demand the original values come out again.  If that holds for a
profile, the simulator and the reader agree about that device.

Run:  python test_profiles.py
"""

import sys

from device_profile import (
    DeviceProfile,
    load_all,
    load_profile,
    to_hex,
    to_readable,
    checksum_sum8,
)

PASS, FAIL = [], []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(f"{name}: {detail}")
        print(f"  FAIL  {name}   {detail}")


# --------------------------------------------------------------------------
def test_known_bcc():
    """The one BCC value in the repo that can be checked by hand.

    web_serial_example.html:143 documents  02 44 31 2C 03  ->  A6
    (STX 'D' '1' ',' ETX).  If this fails the checksum formula is wrong.
    """
    print("\nBCC formula against the documented example")
    frame = bytes([0x02, 0x44, 0x31, 0x2C, 0x03])
    check("02 44 31 2C 03 -> A6",
          checksum_sum8(frame) == 0xA6,
          f"got {checksum_sum8(frame):02X}")


def test_terumo_sample():
    """Parse a real captured frame, not one we generated ourselves."""
    print("\nTerumo: real captured frame")
    prof = load_profile("profiles/terumo_br500.json")
    body = "R1,000000000,260429,103800,119,090,076,083,0000,0000,00000,000"
    inner = bytes([0x02]) + body.encode() + bytes([0x03])
    frame = inner + bytes([checksum_sum8(inner)])

    frames, rest = prof.extract_frames(frame)
    check("one frame extracted", len(frames) == 1, f"got {len(frames)}")
    check("nothing left over", rest == b"", f"got {rest!r}")

    got = prof.parse_frame(frames[0])
    check("parsed ok", got["ok"], got.get("error"))
    check("SYS = 119", got.get("sys") == 119, f"got {got.get('sys')}")
    check("MAP = 90", got.get("map") == 90, f"got {got.get('map')}")
    check("DIA = 76", got.get("dia") == 76, f"got {got.get('dia')}")
    check("PULSE = 83", got.get("pulse") == 83, f"got {got.get('pulse')}")
    check("checksum verifies", got["checksum"] is True, f"got {got['checksum']}")
    # MAP = (119 + 2*76)/3 = 90.3 -> 90.  The physiological cross-check that
    # proves fields 4/5/6 are mapped the right way round.
    check("MAP matches (SYS+2*DIA)/3",
          got.get("map") == got.get("map_expected"),
          f"{got.get('map')} vs {got.get('map_expected')}")


def test_round_trip(prof: DeviceProfile):
    print(f"\n{prof.name}: round trip  [{prof.describe_port()}] "
          f"{'verified' if prof.verified else 'UNVERIFIED'}")
    values = {"sys": 138, "dia": 87, "pulse": 66}
    frame = prof.build_frame(values)
    print(f"       HEX  {to_hex(frame)}")
    print(f"       TXT  {to_readable(frame)}")

    frames, rest = prof.extract_frames(frame)
    check(f"[{prof.id}] one frame extracted", len(frames) == 1, f"got {len(frames)}")
    check(f"[{prof.id}] nothing left over", rest == b"", f"got {rest!r}")
    if not frames:
        return

    got = prof.parse_frame(frames[0])
    check(f"[{prof.id}] parsed ok", got["ok"], got.get("error"))
    for key, want in values.items():
        check(f"[{prof.id}] {key} = {want}", got.get(key) == want, f"got {got.get(key)}")

    if prof.checksum_fn is not None:
        check(f"[{prof.id}] checksum verifies", got["checksum"] is True,
              f"got {got['checksum']}")

    if "map" in prof.fields:
        check(f"[{prof.id}] MAP derived = (138+2*87)/3 = 104",
              got.get("map") == 104, f"got {got.get('map')}")


def test_streaming(prof: DeviceProfile):
    """Frames arriving split across reads, the way a serial port delivers them."""
    print(f"\n{prof.name}: split across reads")
    a = prof.build_frame({"sys": 110, "dia": 70, "pulse": 60})
    b = prof.build_frame({"sys": 150, "dia": 95, "pulse": 88})
    stream = a + b

    collected, buf = [], b""
    for i in range(0, len(stream), 7):  # awkward chunk size on purpose
        buf += stream[i:i + 7]
        frames, buf = prof.extract_frames(buf)
        collected.extend(frames)

    check(f"[{prof.id}] both frames recovered", len(collected) == 2, f"got {len(collected)}")
    check(f"[{prof.id}] buffer drained", buf == b"", f"left {buf!r}")
    if len(collected) == 2:
        first = prof.parse_frame(collected[0])
        second = prof.parse_frame(collected[1])
        check(f"[{prof.id}] first SYS = 110", first.get("sys") == 110, f"got {first.get('sys')}")
        check(f"[{prof.id}] second SYS = 150", second.get("sys") == 150, f"got {second.get('sys')}")


def test_mixed_families():
    """The bug worth never repeating.

    A splitter that only waits for CRLF leaves a Terumo frame (ETX + BCC, no
    CRLF) stuck in the buffer forever.  Prove each profile's splitter releases
    its own frames even with the other family's bytes in the stream.
    """
    print("\nMixed traffic: ETX-terminated and CRLF-terminated together")
    terumo = load_profile("profiles/terumo_br500.json")
    omron = load_profile("profiles/omron_hbp9030.json")

    t_frame = terumo.build_frame({"sys": 121, "dia": 79, "pulse": 70})
    check("Terumo frame carries no CR/LF",
          b"\r" not in t_frame and b"\n" not in t_frame,
          f"{to_hex(t_frame)}")

    o_frame = omron.build_frame({"sys": 131, "dia": 69, "pulse": 92})
    check("Omron frame ends with CRLF", o_frame.endswith(b"\r\n"), to_hex(o_frame[-4:]))

    frames, rest = terumo.extract_frames(t_frame + o_frame)
    check("splitter released both frames", len(frames) == 2, f"got {len(frames)}")
    check("no leftovers", rest == b"", f"left {rest!r}")


def test_corrupt_checksum():
    print("\nTerumo: corrupted checksum is reported, not silently accepted")
    prof = load_profile("profiles/terumo_br500.json")
    frame = bytearray(prof.build_frame({"sys": 120, "dia": 80, "pulse": 70}))
    frame[-1] ^= 0xFF
    got = prof.parse_frame(bytes(frame))
    check("checksum reported as bad", got["checksum"] is False, f"got {got['checksum']}")
    check("values still readable", got.get("sys") == 120, f"got {got.get('sys')}")


def test_short_packet():
    print("\nShort packet is rejected")
    prof = load_profile("profiles/terumo_br500.json")
    got = prof.parse_frame(b"\x02R1,000000000,260429\x03")
    check("rejected", not got["ok"], "accepted a truncated frame")


# --------------------------------------------------------------------------
def main():
    test_known_bcc()
    test_terumo_sample()

    profiles = load_all()
    if not profiles:
        print("no profiles found in profiles/")
        return 1

    for prof in profiles.values():
        test_round_trip(prof)
        test_streaming(prof)

    test_mixed_families()
    test_corrupt_checksum()
    test_short_packet()

    print("\n" + "=" * 60)
    print(f"passed {len(PASS)}   failed {len(FAIL)}")
    for f in FAIL:
        print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
