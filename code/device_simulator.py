"""
Profile-driven medical device simulator.

Pretends to be a blood-pressure monitor on a serial port so an application can
be developed and tested without the real hardware.  Which device it pretends
to be comes from profiles/*.json -- adding a device means adding a file.

Typical setup on Windows:
    1. Install com0com (free virtual null-modem driver) or HHD Virtual Serial
       Port Tools, and create a pair, e.g. COM8 <-> COM9.
    2. Run this simulator on COM9.
    3. Run device_reader.py, terumo_reader.py, or Hercules on COM8.
    4. Press "Send once" -- the other side sees the frame.

Headless (no window), useful in scripts:
    python device_simulator.py --headless --port COM9 --device terumo_br500
    python device_simulator.py --headless --port COM9 --device omron_hbp9030 \
           --sys 138 --dia 87 --pulse 66 --interval 5 --count 10
    python device_simulator.py --dry-run --device omron_hbp9030   # no port needed

The existing terumo_simulator.py is left alone; this one sits beside it.
"""

import argparse
import random
import sys
import threading
import time

from device_profile import load_all, to_hex, to_readable, use_utf8_console

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # dry-run still works without pyserial
    serial = None


# Presets follow the usual clinical bands.  Handy for demos and screenshots.
PRESETS = {
    "ปกติ":            (118, 76, 72),
    "ค่อนข้างสูง":      (128, 78, 76),
    "สูงระดับ 1":       (142, 92, 82),
    "สูงระดับ 2":       (158, 98, 88),
    "ต่ำ":              (92, 58, 62),
    "วิกฤต":            (182, 118, 104),
}


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
        timeout=0.5,
    )


# --------------------------------------------------------------------------
# headless / dry-run
# --------------------------------------------------------------------------
def run_headless(args, profiles):
    profile = profiles[args.device]
    values = {"sys": args.sys, "dia": args.dia, "pulse": args.pulse}

    if not profile.verified:
        print(f"!! {profile.name}: profile ยังไม่ยืนยันกับเครื่องจริง "
              f"(source: {profile.source})")

    if args.dry_run:
        frame = profile.build_frame(values)
        print(f"device : {profile.name}  [{profile.describe_port()}]")
        print(f"HEX    : {to_hex(frame)}")
        print(f"TEXT   : {to_readable(frame)}")
        print(f"bytes  : {len(frame)}")
        return 0

    port = open_port(profile, args.port, args.baud)
    print(f"opened {port.port} @ {port.baudrate} as {profile.name}")
    sent = 0
    try:
        while args.count == 0 or sent < args.count:
            if args.random:
                values = randomized()
            frame = profile.build_frame(values)
            port.write(frame)
            port.flush()
            sent += 1
            print(f"[{time.strftime('%H:%M:%S')}] -> {to_readable(frame)}")
            if args.count and sent >= args.count:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        port.close()
    return 0


def randomized():
    sys_ = random.randint(105, 150)
    dia = random.randint(65, 95)
    return {"sys": sys_, "dia": dia, "pulse": random.randint(60, 100)}


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
def run_gui(profiles, preselect=None):
    import tkinter as tk
    from tkinter import ttk, messagebox

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Medical Device Simulator — profile driven")
            self.geometry("760x720")

            self.profiles = profiles
            self.ser = None
            self.auto_stop = threading.Event()
            self.auto_thread = None
            self.sent_count = 0

            self._build_ui()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self._on_device_change()

        # ---------------- UI ----------------
        def _build_ui(self):
            dev = ttk.LabelFrame(self, text="เครื่องที่จำลอง", padding=10)
            dev.pack(fill="x", padx=10, pady=(10, 4))

            ttk.Label(dev, text="Device:").grid(row=0, column=0, sticky="w")
            self.device_var = tk.StringVar(
                value=preselect if preselect in self.profiles else next(iter(self.profiles)))
            box = ttk.Combobox(dev, textvariable=self.device_var, width=22,
                               state="readonly", values=list(self.profiles))
            box.grid(row=0, column=1, padx=6)
            box.bind("<<ComboboxSelected>>", lambda _e: self._on_device_change())

            self.device_name = tk.StringVar()
            ttk.Label(dev, textvariable=self.device_name,
                      font=("Segoe UI", 10, "bold")).grid(row=0, column=2, padx=10, sticky="w")

            self.verified_var = tk.StringVar()
            self.verified_lbl = tk.Label(dev, textvariable=self.verified_var,
                                         font=("Segoe UI", 9, "bold"), anchor="w",
                                         wraplength=700, justify="left")
            self.verified_lbl.grid(row=1, column=0, columnspan=4, sticky="we", pady=(8, 0))
            dev.columnconfigure(3, weight=1)

            # ---- port ----
            top = ttk.Frame(self, padding=10)
            top.pack(fill="x")

            ttk.Label(top, text="Port:").grid(row=0, column=0, sticky="w")
            self.port_var = tk.StringVar(value="COM9")
            ports = []
            if serial is not None:
                ports = [p.device for p in serial.tools.list_ports.comports()]
            ttk.Combobox(top, textvariable=self.port_var, width=10,
                         values=ports).grid(row=0, column=1, padx=4)

            ttk.Label(top, text="Baud:").grid(row=0, column=2, sticky="w", padx=(10, 0))
            self.baud_var = tk.StringVar()
            self.baud_box = ttk.Combobox(top, textvariable=self.baud_var, width=9)
            self.baud_box.grid(row=0, column=3, padx=4)

            self.connect_btn = ttk.Button(top, text="Open", command=self.toggle_connect)
            self.connect_btn.grid(row=0, column=4, padx=8)

            self.params_var = tk.StringVar()
            ttk.Label(top, textvariable=self.params_var,
                      foreground="#666666").grid(row=0, column=5, padx=6, sticky="w")

            # ---- values ----
            vals = ttk.LabelFrame(self, text="ค่าที่จะส่ง", padding=12)
            vals.pack(fill="x", padx=10, pady=6)

            self.sys_var = tk.StringVar(value="120")
            self.dia_var = tk.StringVar(value="80")
            self.pulse_var = tk.StringVar(value="75")
            for i, (label, var, unit) in enumerate([
                ("SYSTOLIC", self.sys_var, "mmHg"),
                ("DIASTOLIC", self.dia_var, "mmHg"),
                ("PULSE", self.pulse_var, "bpm"),
            ]):
                ttk.Label(vals, text=label, width=11).grid(row=i, column=0, sticky="w", pady=2)
                e = ttk.Entry(vals, textvariable=var, width=8, font=("Segoe UI", 14))
                e.grid(row=i, column=1, padx=8)
                e.bind("<KeyRelease>", lambda _e: self._refresh_preview())
                ttk.Label(vals, text=unit).grid(row=i, column=2, sticky="w")

            self.map_var = tk.StringVar()
            ttk.Label(vals, text="MAP (คำนวณ)", width=13).grid(row=0, column=3, sticky="w", padx=(24, 0))
            ttk.Label(vals, textvariable=self.map_var, font=("Segoe UI", 14),
                      foreground="#058739").grid(row=0, column=4, sticky="w")
            ttk.Label(vals, text="= (SYS + 2×DIA) / 3", foreground="#666666"
                      ).grid(row=1, column=3, columnspan=2, sticky="w", padx=(24, 0))

            pre = ttk.Frame(vals)
            pre.grid(row=4, column=0, columnspan=6, sticky="w", pady=(10, 0))
            ttk.Label(pre, text="Preset:").pack(side="left")
            for name, vals_tuple in PRESETS.items():
                ttk.Button(pre, text=name, width=11,
                           command=lambda v=vals_tuple: self.apply_preset(v)
                           ).pack(side="left", padx=2)
            ttk.Button(pre, text="สุ่ม", width=6, command=self.randomize).pack(side="left", padx=(10, 2))

            # ---- send ----
            ctrls = ttk.Frame(self, padding=10)
            ctrls.pack(fill="x")

            self.send_btn = ttk.Button(ctrls, text="Send once",
                                       command=self.send_once, state="disabled")
            self.send_btn.pack(side="left", padx=4)

            self.auto_var = tk.BooleanVar(value=False)
            self.auto_chk = ttk.Checkbutton(ctrls, text="ส่งอัตโนมัติทุก",
                                            variable=self.auto_var,
                                            command=self.toggle_auto, state="disabled")
            self.auto_chk.pack(side="left", padx=4)
            self.interval_var = tk.StringVar(value="5")
            ttk.Entry(ctrls, textvariable=self.interval_var, width=4).pack(side="left")
            ttk.Label(ctrls, text="วินาที").pack(side="left", padx=(2, 10))

            self.auto_rand_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(ctrls, text="สุ่มค่าใหม่ทุกครั้ง",
                            variable=self.auto_rand_var).pack(side="left")

            # ---- preview ----
            prev = ttk.LabelFrame(self, text="เฟรมที่จะส่ง (เทียบกับที่เห็นใน Hercules ได้เลย)",
                                  padding=8)
            prev.pack(fill="x", padx=10, pady=4)
            self.hex_text = tk.Text(prev, height=4, font=("Consolas", 9), wrap="word")
            self.hex_text.pack(fill="x")
            ttk.Button(prev, text="คัดลอกเป็น HEX",
                       command=self.copy_hex).pack(anchor="e", pady=(6, 0))

            # ---- log ----
            logf = ttk.LabelFrame(self, text="เฟรมที่ส่งไปแล้ว", padding=5)
            logf.pack(fill="both", expand=True, padx=10, pady=(4, 10))
            self.log = tk.Text(logf, height=8, font=("Consolas", 9))
            self.log.pack(fill="both", expand=True)

            self.status = tk.StringVar(value="closed")
            ttk.Label(self, textvariable=self.status, anchor="w",
                      relief="sunken").pack(fill="x", side="bottom")

        # ---------------- helpers ----------------
        @property
        def profile(self):
            return self.profiles[self.device_var.get()]

        def _on_device_change(self):
            prof = self.profile
            self.device_name.set(prof.name)
            self.params_var.set(prof.describe_port())
            bauds = [str(prof.baud)] + [str(b) for b in prof.alternate_bauds]
            self.baud_box.config(values=bauds)
            self.baud_var.set(str(prof.baud))

            if prof.verified:
                self.verified_var.set("✓ profile นี้ยืนยันกับเครื่องจริงแล้ว")
                self.verified_lbl.config(fg="#058739", bg="#EFFBF4")
            else:
                self.verified_var.set(
                    "⚠ profile นี้ยังไม่ยืนยันกับเครื่องจริง — "
                    "เฟรมที่ส่งออกไปเป็นค่าอนุมาน ห้ามนำไปอ้างเป็นสเปกของผู้ผลิต\n"
                    f"ที่มา: {prof.source}")
                self.verified_lbl.config(fg="#B45309", bg="#FFF7ED")

            self._refresh_preview()

        def _values(self):
            def num(var, fallback):
                try:
                    return int(var.get())
                except (TypeError, ValueError):
                    return fallback
            return {"sys": num(self.sys_var, 0),
                    "dia": num(self.dia_var, 0),
                    "pulse": num(self.pulse_var, 0)}

        def _refresh_preview(self):
            v = self._values()
            try:
                self.map_var.set(str(round((v["sys"] + 2 * v["dia"]) / 3)))
            except Exception:
                self.map_var.set("—")
            try:
                frame = self.profile.build_frame(v)
            except Exception as exc:
                text = f"(สร้างเฟรมไม่ได้: {exc})"
            else:
                text = f"HEX   {to_hex(frame)}\n\nTEXT  {to_readable(frame)}"
            self.hex_text.delete("1.0", "end")
            self.hex_text.insert("1.0", text)

        def _log(self, msg):
            self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            self.log.see("end")

        # ---------------- actions ----------------
        def apply_preset(self, values):
            self.sys_var.set(str(values[0]))
            self.dia_var.set(str(values[1]))
            self.pulse_var.set(str(values[2]))
            self._refresh_preview()

        def randomize(self):
            v = randomized()
            self.sys_var.set(str(v["sys"]))
            self.dia_var.set(str(v["dia"]))
            self.pulse_var.set(str(v["pulse"]))
            self._refresh_preview()

        def copy_hex(self):
            try:
                frame = self.profile.build_frame(self._values())
            except Exception as exc:
                messagebox.showerror("สร้างเฟรมไม่ได้", str(exc))
                return
            self.clipboard_clear()
            self.clipboard_append(to_hex(frame))
            self.status.set("คัดลอก HEX ไปยังคลิปบอร์ดแล้ว")

        def toggle_connect(self):
            if self.ser and self.ser.is_open:
                if self.auto_var.get():
                    self.auto_var.set(False)
                    self.toggle_auto()
                self.ser.close()
                self.ser = None
                self.status.set("closed")
                self.connect_btn.config(text="Open")
                self.send_btn.config(state="disabled")
                self.auto_chk.config(state="disabled")
                self._log("port closed")
                return
            try:
                self.ser = open_port(self.profile, self.port_var.get(), self.baud_var.get())
            except Exception as exc:
                messagebox.showerror("เปิดพอร์ตไม่สำเร็จ", str(exc))
                return
            self.status.set(f"open {self.ser.port} @ {self.ser.baudrate} — {self.profile.name}")
            self.connect_btn.config(text="Close")
            self.send_btn.config(state="normal")
            self.auto_chk.config(state="normal")
            self._log(f"opened {self.ser.port} @ {self.ser.baudrate}")

        def send_once(self):
            if not (self.ser and self.ser.is_open):
                return
            if self.auto_rand_var.get():
                self.randomize()
            try:
                frame = self.profile.build_frame(self._values())
                self.ser.write(frame)
                self.ser.flush()
                self.sent_count += 1
                self._log(f"-> {to_readable(frame)}")
                self.status.set(f"ส่งแล้ว {self.sent_count} เฟรม")
            except Exception as exc:
                self._log(f"send error: {exc}")

        def toggle_auto(self):
            if self.auto_var.get():
                try:
                    interval = max(0.5, float(self.interval_var.get()))
                except ValueError:
                    self.auto_var.set(False)
                    messagebox.showerror("ค่าไม่ถูกต้อง", "ช่วงเวลาต้องเป็นตัวเลข")
                    return
                self.auto_stop.clear()
                self.auto_thread = threading.Thread(
                    target=self._auto_loop, args=(interval,), daemon=True)
                self.auto_thread.start()
                self._log(f"auto-send started ({interval:g}s)")
            else:
                self.auto_stop.set()
                if self.auto_thread:
                    self.auto_thread.join(timeout=1.0)
                self._log("auto-send stopped")

        def _auto_loop(self, interval):
            while not self.auto_stop.is_set():
                self.after(0, self.send_once)
                self.auto_stop.wait(interval)

        def _on_close(self):
            self.auto_stop.set()
            if self.ser and self.ser.is_open:
                self.ser.close()
            self.destroy()

    App().mainloop()
    return 0


# --------------------------------------------------------------------------
def main():
    use_utf8_console()
    profiles = load_all()
    if not profiles:
        print("no profiles found in profiles/")
        return 1

    ap = argparse.ArgumentParser(description="Profile-driven medical device simulator")
    ap.add_argument("--device", choices=sorted(profiles), help="profile id")
    ap.add_argument("--port", help="serial port, e.g. COM9")
    ap.add_argument("--baud", type=int, help="override the profile baud rate")
    ap.add_argument("--sys", type=int, default=120)
    ap.add_argument("--dia", type=int, default=80)
    ap.add_argument("--pulse", type=int, default=75)
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between frames")
    ap.add_argument("--count", type=int, default=1, help="frames to send, 0 = forever")
    ap.add_argument("--random", action="store_true", help="new random values each frame")
    ap.add_argument("--headless", action="store_true", help="no window")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the frame and exit, no serial port needed")
    ap.add_argument("--list", action="store_true", help="list profiles and exit")
    args = ap.parse_args()

    if args.list:
        for pid, prof in profiles.items():
            flag = "verified" if prof.verified else "UNVERIFIED"
            print(f"{pid:16} {prof.describe_port():14} {flag:10} {prof.name}")
        return 0

    if args.headless or args.dry_run:
        if not args.device:
            ap.error("--device is required with --headless/--dry-run")
        if args.headless and not args.dry_run and not args.port:
            ap.error("--port is required with --headless")
        return run_headless(args, profiles)

    return run_gui(profiles, preselect=args.device)


if __name__ == "__main__":
    sys.exit(main())
