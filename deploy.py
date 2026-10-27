#!/usr/bin/env python3
"""Build, flash and monitor the barrel boards.

Boards are listed in boards.toml (MAC, chip, role, and optionally a port). A board
is found by its MAC: each USB serial port is asked for its MAC over serial (the
firmware answers 'mac?'), the configured port first, so a renumbered ttyUSB still
works. That asks reset nothing. esptool is used to identify new boards and to
flash; it refuses to write when the chip type does not match.

Each chip has its own sketch for each role: an ESP-01 (esp8266) builds vibrator/,
an ESP32-C3 or -S3 builds vibrator_esp32/ (VIBRATOR) or trigger_esp32/ (TRIGGER,
the button). The boards of one role can mix chips.

  python3 deploy.py list                        ports and the board on each
  python3 deploy.py identify /dev/ttyUSB0       chip + MAC of a new board (boards.toml entry)
  python3 deploy.py build   vibrator|all
  python3 deploy.py flash   vibrator            flash the last build
  python3 deploy.py deploy  all --monitor       build + flash, then serial output
  python3 deploy.py monitor vibrator            serial output, prefixed with the board name

The ESP-01 must be in download mode to be identified or flashed: GPIO0 low while
it resets, or --no-reset when you have already put it there yourself.

Needs Python 3.11+ (tomllib), pyserial, arduino-cli with the cores in CORES (see 'update').
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
BOARDS_FILE = os.path.join(HERE, "boards.toml")
DIST = os.path.join(HERE, "dist")
BUILD_CACHE = os.path.join(HERE, ".build")
ROLES = ("VIBRATOR", "TRIGGER")
FLASH_BAUD = 115200     # 921600 failed mid-upload on the ESP-01's USB-serial programmer

# The arduino cores, by the name arduino-cli uses for them. 'update' installs all of them.
CORES = {
    "esp8266:esp8266": {"version": "3.1.2",
                        "index": "http://arduino.esp8266.com/stable/package_esp8266com_index.json"},
    "esp32:esp32": {"version": "3.3.12",
                    "index": "https://espressif.github.io/arduino-esp32/package_esp32_index.json"},
}

# Per chip: the core, arduino-cli FQBN, esptool (a path under the arduino packages directory, and the
# --chip name), the compiler flags and board options, the sketch folder per role, the flash layout
# (address, file; {sketch} is the sketch name) and the core files to copy into the build.
# The ESP32 entries share their settings: native USB serial (no adapter), and the no_ota partition
# table: bootloader at 0x0, table at 0x8000, otadata at 0xe000 (boot_app0 selects app0), app at 0x10000.
ESP32 = {
    "core": "esp32:esp32",
    "esptool": "esp32/tools/esptool_py/*/esptool",
    "flags": "-DARDUINO_USB_MODE=1 -DARDUINO_USB_CDC_ON_BOOT=1",
    "board_options": "PartitionScheme=no_ota",
    "sketches": {"VIBRATOR": "vibrator_esp32", "TRIGGER": "trigger_esp32"},
    "layout": (
        ("0x0", "{sketch}.ino.bootloader.bin"),
        ("0x8000", "{sketch}.ino.partitions.bin"),
        ("0xe000", "boot_app0.bin"),
        ("0x10000", "{sketch}.ino.bin"),
    ),
    "core_files": ("esp32/hardware/esp32/*/tools/partitions/boot_app0.bin",),
}
CHIPS = {
    "esp8266": {
        "core": "esp8266:esp8266",
        "fqbn": "esp8266:esp8266:generic",
        "esptool": "esp8266/hardware/esp8266/*/tools/esptool/esptool.py",
        "esptool_chip": "esp8266",
        "flags": "",
        "board_options": "",
        "sketches": {"VIBRATOR": "vibrator"},
        # The core writes one image that starts at flash address 0, bootloader and partition table included.
        "layout": (("0x0", "{sketch}.ino.bin"),),
        "core_files": (),
    },
    "esp32c3": {**ESP32, "fqbn": "esp32:esp32:esp32c3", "esptool_chip": "esp32c3"},
    "esp32s3": {**ESP32, "fqbn": "esp32:esp32:esp32s3", "esptool_chip": "esp32s3"},
}


def die(msg):
    sys.exit(f"deploy: {msg}")


def norm_mac(mac):
    """'9C:13:9E:F0:50:60', '9c-13-...' or '9c13 9ef0 5060' -> '9c:13:9e:f0:50:60' (None if not a MAC)."""
    h = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(h) != 12:
        return None
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)).lower()


# ---- boards.toml ------------------------------------------------------------

def load_boards():
    with open(BOARDS_FILE, "rb") as f:
        cfg = tomllib.load(f)
    boards = {}
    for name, b in cfg.get("boards", {}).items():
        role = str(b.get("role", "")).upper()
        chip = str(b.get("chip", "")).lower()
        if role not in ROLES:
            die(f"boards.toml: [boards.{name}] role must be one of {', '.join(ROLES)}")
        if chip not in CHIPS:
            die(f"boards.toml: [boards.{name}] chip must be one of {', '.join(CHIPS)}")
        if role not in CHIPS[chip]["sketches"]:
            die(f"boards.toml: [boards.{name}] {chip} has no sketch for role {role}")
        mac = b.get("mac", "")
        if mac and not norm_mac(mac):
            die(f"boards.toml: [boards.{name}] mac '{mac}' is not a MAC address")
        boards[name] = {"name": name, "role": role, "chip": chip,
                        "mac": norm_mac(mac), "port": b.get("port", "")}
    ports = [b["port"] for b in boards.values() if b["port"]]
    if len(ports) != len(set(ports)):
        die("boards.toml: the same port is listed twice")
    for b in boards.values():
        # the ports of the other boards: a search for this board does not probe them
        b["claimed"] = {os.path.realpath(o["port"]) for o in boards.values() if o["port"] and o is not b}
    return boards


def resolve(boards, n):
    """One selector (board name, role in any case, or 'all') -> (boards, must be connected).
    A name that is both a board and a role (a board called 'vibrator') means the whole role as soon as the
    role has more than one board (a spare), so 'deploy vibrator' reaches the spare too."""
    if n.lower() == "all":
        return list(boards.values()), False
    role_hits = [b for b in boards.values() if b["role"] == n.upper()]
    if n in boards and not (len(role_hits) > 1 and n.upper() in ROLES):
        return [boards[n]], True
    return role_hits, len(role_hits) == 1


def select(boards, names):
    """Board names, roles (any case) or 'all' -> list of boards, in boards.toml order."""
    if not names:
        die("name a board, a role or 'all' (see 'deploy.py list')")
    picked = []
    for n in names:
        hits, _ = resolve(boards, n)
        if not hits:
            die(f"no board or role '{n}' in boards.toml (have: {', '.join(boards)})")
        picked += [b for b in hits if b not in picked]
    return picked


def select_req(boards, names):
    """Like select(), plus the names of the boards that must be connected: a board named exactly, or the
    only board of a role. A board picked as one of several (a role with a spare, or 'all') is optional."""
    picked = select(boards, names)
    required = set()
    for n in names:
        hits, req = resolve(boards, n)
        if req:
            required.update(b["name"] for b in hits)
    return picked, required


def present(picked, required, port):
    """The boards of a selection that can be used now, found by their MAC (see locate). Optional boards that
    are not plugged in are skipped with a note; a required one is an error."""
    if port:
        # --port names the board on that port: 'vibrator' may be two boards, the one configured there is it
        if len(picked) > 1:
            picked = [b for b in picked if b["port"] and os.path.realpath(b["port"]) == os.path.realpath(port)]
        if len(picked) != 1:
            die(f"--port {port}: no single selected board is configured on that port")
        return picked
    out = []
    for b in picked:
        if not b["port"] and not b["mac"]:
            why = "no port or mac in boards.toml"
        else:
            b["found"] = locate(b)
            if b["found"]:
                out.append(b)
                continue
            why = "not connected"
        if b["name"] not in required:
            print(f"(skipping {b['name']}: {why})")
        else:
            find_port(b)     # dies with the reason
    if not out:
        die("none of the selected boards is connected (see 'deploy.py list')")
    return out


# ---- ports ------------------------------------------------------------------

def list_ports():
    """[(device, vid, description)] for all USB serial ports."""
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        die("pyserial is missing: pip install pyserial (or apt install python3-serial)")
    return [(p.device, p.vid, p.description or "") for p in sorted(lp.comports(), key=lambda p: p.device)
            if p.vid is not None]


def stable_port(dev):
    """The /dev/serial/by-id/... name of a port, which survives replugging; the port itself otherwise."""
    for link in sorted(glob.glob("/dev/serial/by-id/*")):
        if os.path.realpath(link) == os.path.realpath(dev):
            return link
    return dev


def open_quiet(port):
    """Open a serial port with DTR and RTS low from the start. Opening a port normally raises DTR, which
    resets a board whose DTR is wired to RST (or GPIO0), so it is set before the port opens."""
    import serial
    s = serial.Serial()
    s.port = port
    s.baudrate = 115200
    s.timeout = 0.2
    s.dtr = False
    s.rts = False
    s.open()
    return s


def query_mac(port):
    """The MAC the firmware on port reports ('mac?' -> 'mac <MAC>'), or None. The port is opened with DTR and
    RTS low, so the board is not reset. A board that is still booting gets a few tries."""
    import serial
    try:
        s = open_quiet(port)
    except (serial.SerialException, OSError):
        return None
    try:
        buf = b""
        for _ in range(10):
            s.write(b"mac?\n")
            end = time.time() + 0.3
            while time.time() < end:
                buf = (buf + s.read(256))[-1024:]
                m = re.search(rb"mac ([0-9a-fA-F:]{17})", buf)
                if m:
                    return norm_mac(m.group(1).decode())
        return None
    finally:
        s.close()


def locate(board):
    """The port a board is on, by its MAC: its configured port first, then the USB serial ports that no other
    board claims. A board without a MAC can only use its configured port. None when no port answers."""
    if not board["mac"]:
        return board["port"] if board["port"] and os.path.exists(board["port"]) else None
    cands = [board["port"]] if board["port"] else []
    cands += [dev for dev, _, _ in list_ports() if os.path.realpath(dev) not in board["claimed"]]
    tried = set()
    for dev in cands:
        real = os.path.realpath(dev)
        if not os.path.exists(dev) or real in tried:
            continue
        tried.add(real)
        if query_mac(dev) == board["mac"]:
            return dev
    return None


def find_port(board, override=None):
    """The port to use for a board: --port, else the one locate() found (once per run)."""
    if override:
        return override
    if "found" not in board:
        if not board["port"] and not board["mac"]:
            die(f"{board['name']}: no port or mac in boards.toml yet; see 'deploy.py identify <port>'")
        board["found"] = locate(board)
    if not board["found"]:
        die(f"{board['name']} ({board['mac'] or board['port']}) is not connected (see 'deploy.py list')")
    return board["found"]


# ---- tools ------------------------------------------------------------------

def arduino_data_dir():
    """$ARDUINO_DIRECTORIES_DATA, else ./.arduino15 if it exists, else arduino-cli's own
    default (~/.arduino15)."""
    d = os.environ.get("ARDUINO_DIRECTORIES_DATA")
    if not d and os.path.isdir(os.path.join(HERE, ".arduino15")):
        d = os.path.join(HERE, ".arduino15")
    if d:
        os.environ["ARDUINO_DIRECTORIES_DATA"] = d
        return d
    try:
        r = subprocess.run(["arduino-cli", "config", "get", "directories.data"],
                           capture_output=True, text=True, check=True)
        return r.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return os.path.expanduser("~/.arduino15")


def version_key(path):
    return [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", path)]


def core_file(pattern):
    """Newest match of a glob under the arduino packages directory."""
    found = glob.glob(os.path.join(arduino_data_dir(), "packages", pattern))
    return max(found, key=version_key) if found else None


def esptool_cmd(chip):
    """The esptool of the chip's core, else one on PATH, else 'python3 -m esptool'."""
    found = core_file(CHIPS[chip]["esptool"])
    if found:
        return [sys.executable, found] if found.endswith(".py") else [found]
    for name in ("esptool", "esptool.py"):
        if shutil.which(name):
            return [name]
    return [sys.executable, "-m", "esptool"]


def esptool_v5(cmd):
    """esptool 5 spells commands write-flash/chip-id, older ones write_flash/chip_id."""
    try:
        r = subprocess.run(cmd + ["version"], capture_output=True, text=True, timeout=30)
        m = re.search(r"(\d+)\.\d+", r.stdout + r.stderr)
        return bool(m) and int(m.group(1)) >= 5
    except (OSError, subprocess.TimeoutExpired):
        return True


V4_NAMES = {"write-flash": "write_flash", "chip-id": "chip_id",
            "default-reset": "default_reset", "hard-reset": "hard_reset",
            "no-reset": "no_reset", "--flash-mode": "--flash_mode",
            "--flash-freq": "--flash_freq", "--flash-size": "--flash_size"}


def esptool_argv(chip, args):
    """The command for the chip's esptool with args, spelled for its version."""
    cmd = esptool_cmd(chip)
    if not esptool_v5(cmd):
        args = [V4_NAMES.get(a, a) for a in args]
    return cmd + args


def run_esptool(chip, args):
    """Run the chip's esptool, echo its output, return (exit code, output)."""
    cmd = esptool_argv(chip, args)
    print("+ " + " ".join(cmd), flush=True)
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as e:
        die(f"cannot run esptool: {e}")
    out = []
    for line in p.stdout:
        sys.stdout.write(line)
        out.append(line)
    return p.wait(), "".join(out)


CHIP_RE = r"(ESP32-[CS]\d|ESP32|ESP8266)"


def parse_esptool(out):
    """Chip ('esp8266', 'esp32s3', ...) and MAC from esptool output."""
    chip = re.search(r"Detecting chip type\.\.\. *" + CHIP_RE, out) or \
        re.search(r"Chip (?:is|type:) *" + CHIP_RE, out)
    mac = re.search(r"MAC: *([0-9a-fA-F:]{17})", out)
    return (chip.group(1).replace("-", "").lower() if chip else None,
            norm_mac(mac.group(1)) if mac else None)


# ---- commands ---------------------------------------------------------------

def sketch_name(board):
    return CHIPS[board["chip"]]["sketches"][board["role"]]


def out_dir(board):
    return os.path.join(DIST, board["chip"], sketch_name(board))


def cmd_list(args, boards):
    by_port = {os.path.realpath(b["port"]): b for b in boards.values()
               if b["port"] and os.path.exists(b["port"])}
    seen = set()
    print(f"{'PORT':30} {'BOARD':12} {'ROLE':9} {'CHIP':8} NOTE")
    for dev, _, desc in list_ports():
        b = by_port.get(os.path.realpath(dev))
        if b:
            seen.add(b["name"])
            print(f"{b['port']:30} {b['name']:12} {b['role']:9} {b['chip']:8}")
        else:
            print(f"{dev:30} {'-':12} {'-':9} {'-':8} unknown: deploy.py identify {dev}  ({desc})")
    for b in boards.values():
        if b["name"] not in seen:
            state = "not connected" if b["port"] else "no port in boards.toml"
            print(f"{b['port'] or '-':30} {b['name']:12} {b['role']:9} {b['chip']:8} ({state})")


def cmd_identify(args, boards):
    # The esp32 core's esptool (newer, knows the C3 and S3) first, then the esp8266 core's (knows the ESP-01).
    for tool in ("esp32s3", "esp8266"):
        rc, out = run_esptool(tool, ["--port", args.port, "chip-id"])
        chip, mac = parse_esptool(out)
        if not rc and mac:
            break
    if rc or not mac:
        die(f"esptool could not read {args.port} (is the board in download mode?)")
    known = [b for b in boards.values() if b["mac"] == mac]
    print()
    if known:
        b = known[0]
        print(f"{args.port}: {mac} is [boards.{b['name']}] ({b['role']}, {b['chip']})")
        if chip and chip != b["chip"]:
            print(f"WARNING: boards.toml says chip = \"{b['chip']}\", esptool found {chip}")
    else:
        print("New board, add to boards.toml (pick a name):\n")
        print(f"[boards.NAME]\nmac  = \"{mac}\"\nchip = \"{chip or '?'}\"\nrole = \"VIBRATOR\"\n"
              f"port = \"{stable_port(args.port)}\"")


def build_flags(board, args):
    flags = [CHIPS[board["chip"]]["flags"]]
    if os.environ.get("DEBUG") == "1":
        flags.append("-DDEBUG")
    flags += (os.environ.get("EXTRA_FLAGS", "") + " " + " ".join(args.flags or [])).split()
    return " ".join(f for f in flags if f)


def build(board, flags, args):
    chip = CHIPS[board["chip"]]
    sketch = sketch_name(board)
    dst = out_dir(board)
    os.makedirs(dst, exist_ok=True)
    print(f"== build {sketch} for {board['chip']} -> {os.path.relpath(dst, HERE)}")
    print(f"   flags: {flags or '(none)'}", flush=True)
    arduino_data_dir()
    cmd = ["arduino-cli", "compile", "-b", chip["fqbn"],
           "--output-dir", dst,
           "--build-path", os.path.join(BUILD_CACHE, f"{board['chip']}-{sketch}"),
           "--build-property", f"compiler.cpp.extra_flags={flags}",
           "--build-property", f"compiler.c.extra_flags={flags}",
           "--build-property", f"build.extra_flags={flags}"]
    if chip["board_options"]:
        cmd += ["--board-options", chip["board_options"]]
    cmd.append(os.path.join(HERE, sketch))
    if args.verbose:
        cmd[2:2] = ["-v"]
    if subprocess.run(cmd).returncode:
        die(f"build of {sketch} for {board['chip']} failed")
    for pattern in chip["core_files"]:
        f = core_file(pattern)
        if not f:
            die(f"{pattern} not found, run 'deploy.py update'")
        shutil.copy(f, dst)
    with open(os.path.join(dst, "build.json"), "w") as f:
        json.dump({"sketch": sketch, "role": board["role"], "chip": board["chip"], "flags": flags,
                   "built": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=1)


def build_boards(picked, args):
    done = set()     # one build per chip + role, also for a spare board
    for b in picked:
        key = (b["chip"], b["role"])
        if key not in done:
            build(b, build_flags(b, args), args)
            done.add(key)


def cmd_build(args, boards):
    build_boards(select(boards, args.boards), args)


def flash(board, args):
    chip = CHIPS[board["chip"]]
    src = out_dir(board)
    sketch = sketch_name(board)
    bins = [(addr, os.path.join(src, name.format(sketch=sketch))) for addr, name in chip["layout"]]
    missing = [p for _, p in bins if not os.path.exists(p)]
    if missing:
        die(f"{board['name']}: no build in {os.path.relpath(src, HERE)}, run 'deploy.py deploy {board['name']}'")
    try:
        with open(os.path.join(src, "build.json")) as f:
            info = json.load(f)
    except (OSError, ValueError):
        info = {}
    newest_src = max(os.path.getmtime(p) for p in glob.glob(os.path.join(HERE, sketch, "*.*")))
    if os.path.getmtime(bins[-1][1]) < newest_src:
        print(f"WARNING: {board['name']}: the build is older than the sketch sources")
    port = find_port(board, args.port)
    print(f"== flash {board['name']} ({board['role']}, {board['chip']}) on {port}"
          f"  built {info.get('built', '?')}  flags: {info.get('flags') or '(none)'}", flush=True)
    # --no-reset: the board is already in download mode (GPIO0 low while it resets); no DTR/RTS reset
    # is attempted first. The new firmware is started with a hard reset at the end.
    before = "no-reset" if args.no_reset else "default-reset"
    rc, out = run_esptool(
        board["chip"],
        ["--chip", chip["esptool_chip"], "--port", port, "--baud", str(FLASH_BAUD),
         "--before", before, "--after", "hard-reset",
         "write-flash", "-z", "--flash-mode", "keep", "--flash-freq", "keep", "--flash-size", "keep"]
        + [x for addr, path in bins for x in (addr, path)])
    chip_found, mac = parse_esptool(out)
    if rc:
        die(f"flashing {board['name']} on {port} failed (esptool exit {rc}); "
            f"is a monitor still open on {port}, or is the board in download mode?")
    if board["mac"] and mac and mac != board["mac"]:
        die(f"{port} turned out to be {mac}, not {board['name']} ({board['mac']})")
    print(f"== {board['name']} flashed OK ({chip_found or '?'}, {mac or '?'})")


def cmd_flash(args, boards):
    picked, required = select_req(boards, args.boards)
    use = present(picked, required, args.port)
    if args.port and len(use) > 1:
        die("--port works with one board only")
    for b in use:
        flash(b, args)


def cmd_deploy(args, boards):
    picked, required = select_req(boards, args.boards)
    use = present(picked, required, args.port)      # fails before building when a required board is not there
    if args.port and len(use) > 1:
        die("--port works with one board only")
    build_boards(use, args)
    for b in use:
        flash(b, args)
    if args.monitor:
        cmd_monitor(args, boards)


_say_lock = threading.Lock()


def say(line):
    """One whole line per write, so the lines of two boards never interleave."""
    with _say_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def monitor_one(board, port_override, prefix, stop):
    import serial
    tag = f"[{board['name']}] " if prefix else ""
    if not port_override and not board["port"] and not board["mac"]:
        say(f"{tag}no port or mac in boards.toml")
        return
    last_search = 0.0
    while not stop.is_set():
        port = port_override or (board["port"] if board["port"] and os.path.exists(board["port"]) else None)
        if not port and board["mac"] and time.time() - last_search > 10:
            # Not on its configured port: search by MAC, at most every 10 s.
            last_search = time.time()
            port = locate(board)
        if not port:
            time.sleep(0.5)          # not plugged in (yet): wait for it to come back
            continue
        try:
            s = open_quiet(port)     # DTR and RTS low before the open, so monitoring doesn't restart the board
        except (serial.SerialException, OSError):
            time.sleep(0.5)
            continue
        say(f"{tag}--- connected on {port} ---")
        buf = b""
        try:
            while not stop.is_set():
                buf += s.read(4096)
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    say(tag + line.decode("utf-8", "replace").rstrip("\r"))
        except (serial.SerialException, OSError):
            say(f"{tag}--- disconnected ---")
        finally:
            s.close()


def cmd_monitor(args, boards):
    picked, _ = select_req(boards, args.boards)     # monitor never probes: a board not plugged in is waited for
    if args.port and len(picked) > 1:
        die("--port works with one board only")
    stop = threading.Event()
    threads = [threading.Thread(target=monitor_one, args=(b, args.port, len(picked) > 1, stop),
                                daemon=True) for b in picked]
    for t in threads:
        t.start()
    print("--- monitor: Ctrl-C to quit ---", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()


def cmd_update(args, boards):
    """Install every core in CORES, so each chip in boards.toml can be built."""
    arduino_data_dir()
    urls = []
    for c in CORES.values():
        if c["index"] not in urls:
            urls.append(c["index"])
    base = [x for u in urls for x in ("--additional-urls", u)]
    steps = [base + ["update"]] + [base + ["core", "install", f"{name}@{c['version']}"]
                                   for name, c in CORES.items()]
    for step in steps:
        if subprocess.run(["arduino-cli"] + step).returncode:
            die("arduino-cli " + " ".join(step) + " failed")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 epilog=__doc__.split("\n", 2)[2],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="serial ports, and the board on each")
    p = sub.add_parser("identify", help="chip and MAC of the board on a port (board in download mode)")
    p.add_argument("port")
    cores = ", ".join(f"{name} {c['version']}" for name, c in CORES.items())
    sub.add_parser("update", help=f"install the arduino cores ({cores})")
    for name, hlp in (("build", "compile"), ("flash", "flash the last build"),
                      ("deploy", "build + flash"), ("monitor", "serial output")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("boards", nargs="+", help="board name, role or 'all'")
        p.add_argument("--port", help="use this port instead of the one in boards.toml (one board only)")
        if name in ("build", "deploy"):
            p.add_argument("--flags", action="append", help="extra compiler flags, written as --flags='-DX -UY' (also $EXTRA_FLAGS)")
            p.add_argument("-v", "--verbose", action="store_true", help="verbose arduino-cli compile")
        if name in ("flash", "deploy"):
            p.add_argument("--no-reset", action="store_true",
                           help="board is already in download mode (GPIO0 low while it resets): don't reset it first")
        if name == "deploy":
            p.add_argument("--monitor", action="store_true", help="monitor the boards afterwards")
    args = ap.parse_args()
    boards = load_boards()
    {"list": cmd_list, "identify": cmd_identify, "update": cmd_update, "build": cmd_build,
     "flash": cmd_flash, "deploy": cmd_deploy, "monitor": cmd_monitor}[args.cmd](args, boards)


if __name__ == "__main__":
    main()
