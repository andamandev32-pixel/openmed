"""
Profile-driven reader for RS-232 medical devices.

The other half of device_simulator.py: same profiles/*.json, used in reverse.
Point it at a port, pick the device, and it shows every frame as HEX, as
readable text, and as parsed values.

    python device_reader.py                                  window
    python device_reader.py --headless --port COM8 --device terumo_br500

Two details here were learned the hard way in Smart_OPD_Kiosk_V2 and are worth
keeping:

  * The port is polled on a timer rather than read as a stream.  On Windows
    `bytesAvailable` returns -1 once the port errors, and a stream reader takes
    that as a buffer length and crashes.  Guarding `available <= 0` avoids it.

  * Control bytes are stripped before splitting on commas.  Leave them in and
    the trailing checksum byte sticks to the last field, so the record-id check
    fails with no visible reason.
"""

import argparse
import queue
import sys
import threading
import time

from device_profile import load_all, to_hex, to_readable, use_utf8_console

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

POLL_SECONDS = 0.02  # 20 ms, same cadence the kiosk settled on


def open_port(profile, port_name, baud=None):
    if serial is None:
        raise RuntimeError("pyserial is not installed (pip install pyserial)")
    parity = {"none": serial.PARITY_NONE,
              "even": serial.PARITY_EVEN,
              "odd": serial.PARITY_ODD}.get(profile.parity, serial.PARITY_NONE)
    return serial.Serial(
        port=port_name,
        baudrate=int(baud or profile.baud),
        bytesize=profile.data_bits,
        parity=parity,
        stopbits=profile.stop_bits,
        timeout=0.2,
    )


class SerialWorker(threading.Thread):
    """Reads bytes on a background thread, hands whole frames to the caller."""

    def __init__(self, port, profile, out_queue):
        super().__init__(daemon=True)
        self.port = port
        self.profile = profile
        self.out = out_queue
        self.stop_flag = threading.Event()
        self.buffer = b""

    def run(self):
        while not self.stop_flag.is_set():
            try:
                available = self.port.in_waiting
                if available is None or available <= 0:
                    time.sleep(POLL_SECONDS)
                    continue
                chunk = self.port.read(available)
                if not chunk:
                    continue
                self.out.put(("raw", chunk))
                self.buffer += chunk
                frames, self.buffer = self.profile.extract_frames(self.buffer)
                for frame in frames:
                    self.out.put(("frame", frame))
            except Exception as exc:
                self.out.put(("error", str(exc)))
                time.sleep(0.5)

    def stop(self):
        self.stop_flag.set()


def describe(profile, frame):
    """One frame -> lines for the log."""
    lines = [f"HEX   {to_hex(frame)}", f"TEXT  {to_readable(frame)}"]
    parsed = profile.parse_frame(frame)
    if not parsed["ok"]:
        lines.append(f"      ไม่ผ่าน: {parsed['error']}")
        return lines, parsed

    bits = []
    for key in ("sys", "map", "dia", "pulse"):
        if key in parsed:
            bits.append(f"{key.upper()}={parsed[key]}")
    lines.append("      " + "  ".join(bits) if bits else "      (ไม่พบฟิลด์ค่าวัด)")

    if parsed["checksum"] is True:
        lines.append("      checksum ok")
    elif parsed["checksum"] is False:
        lines.append("      checksum BAD — สายรบกวนหรือ baud ผิด")

    expected = parsed.get("map_expected")
    if expected is not None and "map" in parsed:
        mark = "ตรง" if abs(int(parsed["map"]) - expected) <= 1 else "ไม่ตรง"
        lines.append(f"      ตรวจ MAP: (SYS+2×DIA)/3 = {expected} → {mark}")
    return lines, parsed


# --------------------------------------------------------------------------
def run_headless(args, profiles):
    profile = profiles[args.device]
    if not profile.verified:
        print(f"!! {profile.name}: profile ยังไม่ยืนยันกับเครื่องจริง")

    port = open_port(profile, args.port, args.baud)
    print(f"listening on {port.port} @ {port.baudrate} as {profile.name}")
    print("กด Ctrl+C เพื่อหยุด\n")

    q = queue.Queue()
    worker = SerialWorker(port, profile, q)
    worker.start()
    try:
        while True:
            kind, payload = q.get()
            stamp = time.strftime("%H:%M:%S")
            if kind == "frame":
                lines, _ = describe(profile, payload)
                print(f"[{stamp}] " + f"\n{' ' * 11}".join(lines) + "\n")
            elif kind == "error":
                print(f"[{stamp}] error: {payload}")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        worker.stop()
        port.close()
    return 0


def run_gui(profiles, preselect=None):
    import tkinter as tk
    from tkinter import ttk, messagebox

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Medical Device Reader — profile driven")
            self.geometry("820x700")

            self.profiles = profiles
            self.ser = None
            self.worker = None
            self.queue = queue.Queue()
            self.frame_count = 0

            self._build_ui()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self._on_device_change()
            self.after(100, self._drain)

        def _build_ui(self):
            top = ttk.Frame(self, padding=10)
            top.pack(fill="x")

            ttk.Label(top, text="Device:").grid(row=0, column=0, sticky="w")
            self.device_var = tk.StringVar(
                value=preselect if preselect in self.profiles else next(iter(self.profiles)))
            box = ttk.Combobox(top, textvariable=self.device_var, width=20,
                               state="readonly", values=list(self.profiles))
            box.grid(row=0, column=1, padx=4)
            box.bind("<<ComboboxSelected>>", lambda _e: self._on_device_change())

            ttk.Label(top, text="Port:").grid(row=0, column=2, sticky="w", padx=(10, 0))
            self.port_var = tk.StringVar(value="COM8")
            ports = []
            if serial is not None:
                ports = [p.device for p in serial.tools.list_ports.comports()]
            ttk.Combobox(top, textvariable=self.port_var, width=10,
                         values=ports).grid(row=0, column=3, padx=4)

            ttk.Label(top, text="Baud:").grid(row=0, column=4, sticky="w", padx=(10, 0))
            self.baud_var = tk.StringVar()
            self.baud_box = ttk.Combobox(top, textvariable=self.baud_var, width=9)
            self.baud_box.grid(row=0, column=5, padx=4)

            self.connect_btn = ttk.Button(top, text="Open", command=self.toggle_connect)
            self.connect_btn.grid(row=0, column=6, padx=8)

            self.verified_var = tk.StringVar()
            self.verified_lbl = tk.Label(self, textvariable=self.verified_var,
                                         font=("Segoe UI", 9, "bold"), anchor="w",
                                         wraplength=780, justify="left", padx=10, pady=6)
            self.verified_lbl.pack(fill="x", padx=10)

            vals = ttk.LabelFrame(self, text="ค่าล่าสุด", padding=14)
            vals.pack(fill="x", padx=10, pady=8)
            self.tiles = {}
            for i, (key, label, unit) in enumerate([
                ("sys", "SYSTOLIC", "mmHg"), ("map", "MAP", "mmHg"),
                ("dia", "DIASTOLIC", "mmHg"), ("pulse", "PULSE", "bpm"),
            ]):
                cell = ttk.Frame(vals)
                cell.grid(row=0, column=i, padx=18)
                ttk.Label(cell, text=label, foreground="#666666").pack()
                var = tk.StringVar(value="—")
                ttk.Label(cell, textvariable=var,
                          font=("Segoe UI", 30, "bold")).pack()
                ttk.Label(cell, text=unit, foreground="#888888").pack()
                self.tiles[key] = var

            logf = ttk.LabelFrame(self, text="เฟรมที่รับได้", padding=5)
            logf.pack(fill="both", expand=True, padx=10, pady=(0, 10))
            self.log = tk.Text(logf, font=("Consolas", 9))
            self.log.pack(fill="both", expand=True)

            self.status = tk.StringVar(value="closed")
            ttk.Label(self, textvariable=self.status, anchor="w",
                      relief="sunken").pack(fill="x", side="bottom")

        @property
        def profile(self):
            return self.profiles[self.device_var.get()]

        def _on_device_change(self):
            prof = self.profile
            bauds = [str(prof.baud)] + [str(b) for b in prof.alternate_bauds]
            self.baud_box.config(values=bauds)
            self.baud_var.set(str(prof.baud))
            if prof.verified:
                self.verified_var.set(f"✓ {prof.name} — profile ยืนยันแล้ว  [{prof.describe_port()}]")
                self.verified_lbl.config(fg="#058739", bg="#EFFBF4")
            else:
                self.verified_var.set(
                    f"⚠ {prof.name} — profile ยังไม่ยืนยันกับเครื่องจริง "
                    f"[{prof.describe_port()}]  ถ้าอ่านไม่ออก ให้ไล่ baud ตามบทที่ 6 ของ SOP")
                self.verified_lbl.config(fg="#B45309", bg="#FFF7ED")

        def _log(self, msg):
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            if float(self.log.index("end-1c").split(".")[0]) > 500:
                self.log.delete("1.0", "100.0")

        def toggle_connect(self):
            if self.ser and self.ser.is_open:
                if self.worker:
                    self.worker.stop()
                    self.worker = None
                self.ser.close()
                self.ser = None
                self.status.set("closed")
                self.connect_btn.config(text="Open")
                self._log("port closed")
                return
            try:
                self.ser = open_port(self.profile, self.port_var.get(), self.baud_var.get())
            except Exception as exc:
                messagebox.showerror("เปิดพอร์ตไม่สำเร็จ", str(exc))
                return
            self.worker = SerialWorker(self.ser, self.profile, self.queue)
            self.worker.start()
            self.status.set(f"listening {self.ser.port} @ {self.ser.baudrate}")
            self.connect_btn.config(text="Close")
            self._log(f"opened {self.ser.port} @ {self.ser.baudrate} as {self.profile.name}")

        def _drain(self):
            while True:
                try:
                    kind, payload = self.queue.get_nowait()
                except queue.Empty:
                    break
                stamp = time.strftime("%H:%M:%S")
                if kind == "frame":
                    self.frame_count += 1
                    lines, parsed = describe(self.profile, payload)
                    self._log(f"[{stamp}] " + f"\n{' ' * 11}".join(lines) + "")
                    for key, var in self.tiles.items():
                        if key in parsed:
                            var.set(str(parsed[key]))
                    self.status.set(f"รับแล้ว {self.frame_count} เฟรม")
                elif kind == "error":
                    self._log(f"[{stamp}] error: {payload}")
            self.after(100, self._drain)

        def _on_close(self):
            if self.worker:
                self.worker.stop()
            if self.ser and self.ser.is_open:
                self.ser.close()
            self.destroy()

    App().mainloop()
    return 0


def main():
    use_utf8_console()
    profiles = load_all()
    if not profiles:
        print("no profiles found in profiles/")
        return 1

    ap = argparse.ArgumentParser(description="Profile-driven medical device reader")
    ap.add_argument("--device", choices=sorted(profiles))
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    if args.headless:
        if not args.device or not args.port:
            ap.error("--device and --port are required with --headless")
        return run_headless(args, profiles)
    return run_gui(profiles, preselect=args.device)


if __name__ == "__main__":
    sys.exit(main())
