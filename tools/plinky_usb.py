#!/usr/bin/env python3
"""
plinky_usb.py - read and patch Plinky preset slots over WebUSB, on a running device.

No bootloader, no PRESETS.UF2: the firmware's web editor (sw/Core/Src/plinky/usb/web_editor.c)
serves any byte range of any of the 32 preset slots over the vendor interface.

    ./plinky_usb.py dump -o bank.json          # all 32 slots -> json (plinky_bank format)
    ./plinky_usb.py show                       # one line per slot
    ./plinky_usb.py set P_DISTORTION A 25 --slots 1-24      # dry run, prints what would change
    ./plinky_usb.py set P_DISTORTION A 25 --slots 1-24 --write

Protocol: 4 magic bytes, then cmd (0 = send to host, 1 = save to device), slot, offset, length,
as u16 with magic ...ca or u32 with magic ...cb. Length 0 on a read means "to the end of the
preset". A write loads that slot into the device's ram, overwrites the range and saves it, so
only the bytes sent are touched - which is what makes a per-parameter batch edit safe on a
firmware whose Preset is larger than this tool knows about.

Needs pyusb and a libusb backend (pip install pyusb libusb-package).
"""

import argparse, json, struct, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plinky_bank as pb

VID, PID = 0xCAFE, 0x4018
MAGIC16 = bytes([0xF3, 0x0F, 0xAB, 0xCA])
NUM_PRESETS = 32
TIMEOUT_MS = 3000


class Plinky:
    def __init__(self):
        import usb.core, usb.util
        try:
            import libusb_package, usb.backend.libusb1
            backend = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
        except ImportError:
            backend = None
        self.usb_util = usb.util
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if dev is None:
            sys.exit("no Plinky on the usb bus (powered on, cable in the data port?)")
        intf = next((i for i in dev.get_active_configuration() if i.bInterfaceClass == 0xFF), None)
        if intf is None:
            sys.exit("Plinky is there but exposes no vendor interface")
        self.dev, self.intf = dev, intf
        self.out = next(e for e in intf if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.inp = next(e for e in intf if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        try:
            usb.util.claim_interface(dev, intf.bInterfaceNumber)
        except Exception:
            pass  # windows hands it over without claiming

    def read_range(self, slot: int, offset: int, length: int) -> bytes:
        self.out.write(MAGIC16 + struct.pack("<BBHH", 0, slot, offset, length), TIMEOUT_MS)
        hdr = bytes(self.inp.read(10, TIMEOUT_MS))
        if hdr[:4] != MAGIC16:
            sys.exit(f"slot {slot + 1}: device answered with {hdr[:4].hex()}, not the editor magic")
        want = struct.unpack_from("<H", hdr, 8)[0]
        data = bytearray()
        while len(data) < want:
            data += bytes(self.inp.read(min(4096, want - len(data) + 64), TIMEOUT_MS))
        return bytes(data[:want])

    def write_range(self, slot: int, offset: int, data: bytes) -> None:
        """the device loads the slot, overwrites exactly these bytes and saves it back"""
        self.out.write(MAGIC16 + struct.pack("<BBHH", 1, slot, offset, len(data)), TIMEOUT_MS)
        self.out.write(data, TIMEOUT_MS)

    def read_slot(self, slot: int) -> bytes:
        """whole preset: length 0 asks the device for sizeof(Preset) - offset"""
        self.out.write(MAGIC16 + struct.pack("<BBHH", 0, slot, 0, 0), TIMEOUT_MS)
        hdr = bytes(self.inp.read(10, TIMEOUT_MS))
        if hdr[:4] != MAGIC16:
            sys.exit(f"slot {slot + 1}: device answered with {hdr[:4].hex()}, not the editor magic")
        length = struct.unpack_from("<H", hdr, 8)[0]
        data = bytearray()
        while len(data) < length:
            data += bytes(self.inp.read(min(4096, length - len(data) + 64), TIMEOUT_MS))
        return bytes(data[:length])


def read_bank(plinky):
    """returns {slot_index: raw preset bytes}"""
    out = {}
    for slot in range(NUM_PRESETS):
        out[slot] = plinky.read_slot(slot)
        print(f"\r  reading slot {slot + 1}/{NUM_PRESETS}", end="", file=sys.stderr, flush=True)
    print("\r" + " " * 32 + "\r", end="", file=sys.stderr)
    return out


def cmd_dump(args):
    raw = read_bank(Plinky())
    presets = []
    for slot, buf in sorted(raw.items()):
        # decode_preset reads a page payload, so pad the preset out to one
        d = pb.decode_preset(buf + b"\xff" * (pb.PAGE_USABLE - len(buf)))
        presets.append({"slot": slot + 1, **d})
    doc = {
        "_format": "plinky-bank/1",
        "_source": f"usb {VID:04x}:{PID:04x}",
        "_preset_size": len(next(iter(raw.values()))),
        "presets": presets,
    }
    Path(args.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    named = sum(1 for p in presets if p["name"])
    print(f"{NUM_PRESETS} slots read over usb ({doc['_preset_size']} bytes each), "
          f"{named} named  ->  {args.out}")



# ----------------------------------------------------------- index helpers --

SYNC_DIVS_32NDS = [1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 32, 40,
                   48, 64, 80, 96, 128, 160, 192, 256, 320, 384, 512, 640, 768, 1024]


def div_to_index(spec: str) -> int:
    """'1/8', '3/16', '1bar', '2bars' -> index into SYNC_DIVS_32NDS"""
    spec = spec.strip().lower().replace(" ", "")
    if spec.endswith("bars"):
        n32 = int(spec[:-4]) * 32
    elif spec.endswith("bar"):
        n32 = int(spec[:-3]) * 32
    elif "/" in spec:
        num, den = (int(x) for x in spec.split("/", 1))
        if 32 * num % den:
            sys.exit(f"{spec} is not a whole number of 32nds")
        n32 = 32 * num // den
    else:
        sys.exit(f"cannot read a division out of {spec!r} (try 1/8, 3/16, 1bar)")
    if n32 not in SYNC_DIVS_32NDS:
        sys.exit(f"{spec} = {n32}/32 is not one of the divisions plinky offers")
    return SYNC_DIVS_32NDS.index(n32)


def index_to_percent(index: int, rng: int) -> float:
    """INDEX_TO_RAW from utils.h: the lowest raw that lands fully inside the index"""
    raw = (abs(index) * 1024 + rng - 1) // rng
    return raw * 100 / pb.RAW_FULL


# the ranges this tool needs to turn an index into a value; see param_defs.h
PARAM_RANGE = {
    "P_SEQ_CLK_DIV": len(SYNC_DIVS_32NDS) + 1,   # + "Gate cv"
    "P_ARP_CLK_DIV": len(SYNC_DIVS_32NDS),
    "P_DLY_TIME": len(SYNC_DIVS_32NDS),
    "P_SCALE": 27,          # NUM_SCALES; 0 Major, 1 Minor, 2 Harmonic minor, ...
    "P_ROOT": 12,           # 0 = C
    "P_X_SHAPE": 11,        # NUM_LFO_SHAPES
    "P_Y_SHAPE": 11,
    "P_A_SHAPE": 11,
    "P_B_SHAPE": 11,
}


# ------------------------------------------------------- multi timbral base --

# MultiParam order from defs.h. For these 33 parameters the mod depths are shared, but the BASE is
# split: string 0 lives in params[param][SRC_BASE], strings 1-7 in multi_params[mp][0..6].
MULTI_PARAMS = [
    "P_SHAPE", "P_DISTORTION", "P_PITCH", "P_OCT", "P_GLIDE", "P_INTERVAL",
    "P_NOISE", "P_RESO", "P_DEGREE", "P_SCALE", "P_MICROTONE", "P_COLUMN",
    "P_ENV_LVL1", "P_ATTACK1", "P_DECAY1", "P_SUSTAIN1", "P_RELEASE1", "P_ROOT",
    "P_ENV_LVL2", "P_ATTACK2", "P_DECAY2", "P_SUSTAIN2", "P_RELEASE2",
    "P_ARP_TGL", "P_LATCH_TGL", "P_GATE_LENGTH",
    "P_SCRUB", "P_GR_SIZE", "P_PLAY_SPD", "P_SMP_STRETCH",
    "P_SCRUB_JIT", "P_GR_SIZE_JIT", "P_PLAY_SPD_JIT",
]
MULTI_ID = {n: i for i, n in enumerate(MULTI_PARAMS)}
MULTI_BASE = pb.PRESET_SIZE          # 1552: multi_params[33][7] starts right after the v0.4.0 part
STRINGS_IN_MULTI = 7


def multi_offset(param_name: str) -> int:
    """byte offset of multi_params[mp][0]; the seven strings are contiguous, 14 bytes"""
    return MULTI_BASE + MULTI_ID[param_name] * STRINGS_IN_MULTI * 2


def writes_for(param: str, src: str, raw: int):
    """every (offset, payload) one logical change needs"""
    out = [(param_offset(param, src), struct.pack("<h", raw))]
    if src == "base" and param in MULTI_ID:
        out.append((multi_offset(param), struct.pack("<h", raw) * STRINGS_IN_MULTI))
    return out

# ---------------------------------------------------------------- batch set --

NUM_MOD_SOURCES = 8
MOD_SRC = pb.MOD_SRC  # base, env2, pres, A, B, X, Y, rnd


def param_offset(param_name: str, src: str) -> int:
    """byte offset of one mod depth inside Preset.params[NUM_PARAMS][NUM_MOD_SOURCES]"""
    if param_name not in pb.PARAM_ID:
        sys.exit(f"unknown parameter: {param_name}")
    if src not in MOD_SRC:
        sys.exit(f"unknown mod source: {src} (one of {', '.join(MOD_SRC)})")
    return (pb.PARAM_ID[param_name] * NUM_MOD_SOURCES + MOD_SRC.index(src)) * 2


def parse_slots(spec: str):
    """'1-24', '1,5,9', '1-8,32' -> zero based slot indices"""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            out += list(range(lo, hi + 1))
        else:
            out.append(int(part))
    bad = [s for s in out if not 1 <= s <= NUM_PRESETS]
    if bad:
        sys.exit(f"slots out of range 1-{NUM_PRESETS}: {bad}")
    return sorted({s - 1 for s in out})


def cmd_set(args):
    args.percent = resolve_percent(args.param, args.percent, args.index, args.div)
    slots = parse_slots(args.slots)
    plinky = Plinky()
    writes, raw, changes = plan_change(plinky, args.param, args.src, args.percent, slots)
    tail = f"  (+{STRINGS_IN_MULTI} per-string)" if len(writes) > 1 else ""
    print(f"{args.param}  mod {args.src}  ->  {args.percent:+g}%  (raw {raw}){tail}, {len(slots)} slots")
    for slot, cur in changes:
        print(f"  slot {slot + 1:2}: {cur * 100 / pb.RAW_FULL:+7.1f}%  ->  {raw * 100 / pb.RAW_FULL:+.1f}%")
    if not changes:
        print("nothing to change")
        return
    if not args.write:
        print()
        print(f"{len(changes)} slots would change. Re-run with --write to apply.")
        return
    failed = write_change(plinky, writes, raw, changes)
    if failed:
        sys.exit(f"did not read back as written in slots: {sorted(set(failed))}")
    print(f"{len(changes)} slots written and verified")


def resolve_percent(param: str, percent=None, index=None, div=None) -> float:
    given = [x for x in (percent, index, div) if x is not None]
    if len(given) != 1:
        sys.exit("give exactly one of a percent, --index or --div")
    if percent is not None:
        return float(percent)
    if div is not None:
        index = div_to_index(div)
    if param not in PARAM_RANGE:
        sys.exit(f"no index range known for {param}; use a percent")
    return index_to_percent(int(index), PARAM_RANGE[param])


def plan_change(plinky, param: str, src: str, percent: float, slots):
    """returns (writes, raw, [(slot, current_raw)]) without writing anything"""
    raw = max(-pb.RAW_FULL, min(pb.RAW_FULL, round(percent * pb.RAW_FULL / 100)))
    wr = writes_for(param, src, raw)
    changes = []
    for slot in slots:
        if any(plinky.read_range(slot, off, len(pay)) != pay for off, pay in wr):
            cur = struct.unpack("<h", plinky.read_range(slot, wr[0][0], 2))[0]
            changes.append((slot, cur))
    return wr, raw, changes


def write_change(plinky, writes, raw, changes):
    """the device saves the slot a tick after the last byte, so read it back before moving on"""
    failed = []
    for slot, _ in changes:
        for offset, payload in writes:
            plinky.write_range(slot, offset, payload)
            for _ in range(20):
                time.sleep(0.05)
                if plinky.read_range(slot, offset, len(payload)) == payload:
                    break
            else:
                failed.append(slot + 1)
    return failed


def cmd_apply(args):
    """a recipe is a json list of {param, src, slots, and one of percent / index / div}"""
    recipe = json.loads(Path(args.recipe).read_text(encoding="utf-8"))
    if isinstance(recipe, dict):
        recipe = recipe.get("steps", [])
    plinky = Plinky()
    planned = []
    for step in recipe:
        param, src = step["param"], step.get("src", "base")
        percent = resolve_percent(param, step.get("percent"), step.get("index"), step.get("div"))
        slots = parse_slots(str(step.get("slots", "1-32")))
        writes, raw, changes = plan_change(plinky, param, src, percent, slots)
        label = step.get("div") or (f"index {step['index']}" if "index" in step else f"{percent:+g}%")
        tail = f"  (+{STRINGS_IN_MULTI} per-string)" if len(writes) > 1 else ""
        print(f"{param:<16} {src:<5} -> {label:<10} {len(changes):2} of {len(slots):2} slots change{tail}")
        for slot, cur in changes:
            print(f"      slot {slot + 1:2}: {cur * 100 / pb.RAW_FULL:+7.1f}%  ->  {raw * 100 / pb.RAW_FULL:+.1f}%")
        planned.append((writes, raw, changes))
    total = sum(len(c) for _, _, c in planned)
    print()
    if not total:
        print("nothing to change")
        return
    if not args.write:
        print(f"{total} slot writes in total. Re-run with --write to apply.")
        return
    failed = []
    for writes, raw, changes in planned:
        failed += write_change(plinky, writes, raw, changes)
    if failed:
        sys.exit(f"did not read back as written in slots: {sorted(set(failed))}")
    print(f"{total} slot writes applied and verified")


def cmd_show(args):
    for slot, buf in sorted(read_bank(Plinky()).items()):
        d = pb.decode_preset(buf + b"\xff" * (pb.PAGE_USABLE - len(buf)))
        multi = sum(1 for b in buf[pb.PRESET_SIZE:] if b)
        print(f"{slot + 1:3}  {d['name']:<9} {d['category']:<8} "
              f"{len(d['params']):3} params  multi {multi:4} bytes")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump", help="read all 32 slots into a json bank")
    d.add_argument("-o", "--out", default="bank.json")
    d.set_defaults(func=cmd_dump)
    st = sub.add_parser("set", help="set one mod depth across slots (dry run unless --write)")
    st.add_argument("param", help="parameter name, e.g. P_DISTORTION")
    st.add_argument("src", help=f"mod source: {', '.join(pb.MOD_SRC)}")
    st.add_argument("percent", type=float, nargs="?", help="depth in percent, -100..100")
    st.add_argument("--index", type=int, help="index value instead of a percent")
    st.add_argument("--div", help="clock division instead of a percent, e.g. 1/8 or 3/16")
    st.add_argument("--slots", default="1-32", help="e.g. 1-24 or 1,5,9 (default all)")
    st.add_argument("--write", action="store_true", help="actually write (otherwise dry run)")
    st.set_defaults(func=cmd_set)
    ap_ = sub.add_parser("apply", help="run a json recipe of set steps in one pass")
    ap_.add_argument("recipe")
    ap_.add_argument("--write", action="store_true", help="actually write (otherwise dry run)")
    ap_.set_defaults(func=cmd_apply)
    s = sub.add_parser("show", help="one line per slot")
    s.set_defaults(func=cmd_show)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
