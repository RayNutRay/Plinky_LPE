#!/usr/bin/env python3
"""
plinky_usb.py - read and patch Plinky preset slots over WebUSB, on a running device.

No bootloader, no PRESETS.UF2: the firmware's web editor (sw/Core/Src/plinky/usb/web_editor.c)
serves any byte range of any of the 32 preset slots over the vendor interface.

    ./plinky_usb.py dump -o bank.json          # all 32 slots -> json (plinky_bank format)
    ./plinky_usb.py show                       # one line per slot

Protocol: 4 magic bytes, then cmd (0 = send to host, 1 = save to device), slot, offset, length,
as u16 with magic ...ca or u32 with magic ...cb. Length 0 on a read means "to the end of the
preset". A write loads that slot into the device's ram, overwrites the range and saves it, so
only the bytes sent are touched - which is what makes a per-parameter batch edit safe on a
firmware whose Preset is larger than this tool knows about.

Needs pyusb and a libusb backend (pip install pyusb libusb-package).
"""

import argparse, json, struct, sys
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
    s = sub.add_parser("show", help="one line per slot")
    s.set_defaults(func=cmd_show)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
