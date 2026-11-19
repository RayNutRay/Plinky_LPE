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
}

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
    offset = param_offset(args.param, args.src)
    raw = max(-pb.RAW_FULL, min(pb.RAW_FULL, round(args.percent * pb.RAW_FULL / 100)))
    slots = parse_slots(args.slots)
    plinky = Plinky()

    print(f"{args.param}  mod {args.src}  ->  {args.percent:+g}%  (raw {raw})  "
          f"at offset {offset}, {len(slots)} slots")
    changes = []
    for slot in slots:
        cur = struct.unpack("<h", plinky.read_range(slot, offset, 2))[0]
        if cur != raw:
            changes.append((slot, cur))
        print(f"  slot {slot + 1:2}: {cur * 100 / pb.RAW_FULL:+7.1f}%"
              + ("  ->  unchanged" if cur == raw else f"  ->  {raw * 100 / pb.RAW_FULL:+.1f}%"))
    if not changes:
        print("nothing to change")
        return
    if not args.write:
        print(f"\n{len(changes)} slots would change. Re-run with --write to apply.")
        return

    payload = struct.pack("<h", raw)
    failed = []
    for slot, _ in changes:
        plinky.write_range(slot, offset, payload)
        # the device saves the slot a tick after the last byte, so read it back before moving on
        for _ in range(20):
            time.sleep(0.05)
            if struct.unpack("<h", plinky.read_range(slot, offset, 2))[0] == raw:
                break
        else:
            failed.append(slot + 1)
        print(f"\r  written {slot + 1}/{NUM_PRESETS}", end="", file=sys.stderr, flush=True)
    print("\r" + " " * 32 + "\r", end="", file=sys.stderr)
    if failed:
        sys.exit(f"did not read back as written in slots: {failed}")
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
    """returns (offset, raw, [(slot, current_raw)]) without writing anything"""
    offset = param_offset(param, src)
    raw = max(-pb.RAW_FULL, min(pb.RAW_FULL, round(percent * pb.RAW_FULL / 100)))
    changes = []
    for slot in slots:
        cur = struct.unpack("<h", plinky.read_range(slot, offset, 2))[0]
        if cur != raw:
            changes.append((slot, cur))
    return offset, raw, changes


def write_change(plinky, offset: int, raw: int, changes):
    payload = struct.pack("<h", raw)
    failed = []
    for slot, _ in changes:
        plinky.write_range(slot, offset, payload)
        for _ in range(20):
            time.sleep(0.05)
            if struct.unpack("<h", plinky.read_range(slot, offset, 2))[0] == raw:
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
        offset, raw, changes = plan_change(plinky, param, src, percent, slots)
        label = step.get("div") or (f"index {step['index']}" if "index" in step else f"{percent:+g}%")
        print(f"{param:<16} {src:<4} -> {label:<10} "
              f"{len(changes)} of {len(slots)} slots change")
        for slot, cur in changes:
            print(f"      slot {slot + 1:2}: {cur * 100 / pb.RAW_FULL:+7.1f}%  ->  {raw * 100 / pb.RAW_FULL:+.1f}%")
        planned.append((offset, raw, changes))
    total = sum(len(c) for _, _, c in planned)
    print()
    if not total:
        print("nothing to change")
        return
    if not args.write:
        print(f"{total} writes in total. Re-run with --write to apply.")
        return
    failed = []
    for offset, raw, changes in planned:
        failed += write_change(plinky, offset, raw, changes)
    if failed:
        sys.exit(f"did not read back as written in slots: {sorted(set(failed))}")
    print(f"{total} writes applied and verified")

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
