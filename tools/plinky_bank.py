#!/usr/bin/env python3
"""
plinky_bank.py - batch edit tool for Plinky / Plinky LPE preset banks (PRESETS.UF2)

Workflow:
    1. boot Plinky into bootloader mode, copy PRESETS.UF2 off the PLINKY drive
    2. ./plinky_bank.py dump PRESETS.UF2 -o bank.json
    3. edit bank.json (by hand, or with `apply` recipes)
    4. ./plinky_bank.py build bank.json --base PRESETS.UF2 -o PRESETS_new.UF2
    5. ./plinky_bank.py verify PRESETS_new.UF2 --against bank.json
    6. rename to PRESETS.UF2 and drag onto the PLINKY drive

Firmware compatibility:
    The readable part of a preset is the v0.4.0 layout: params[96][8] + 16 tail bytes
    = 1552 bytes. Newer firmware appends to it - 0.4.2c has multi_params[33][7] + 2
    padding bytes, making Preset 2016 bytes - and the offsets before that are unchanged,
    so the json describes either. Everything past the 1552 bytes is carried through
    verbatim in `_rest_b64` and written back untouched, so `build` cannot silently drop
    the per-string values of a newer bank. Delete `_rest_b64` from a slot to clear them.

Format reverse-engineered from the LPE firmware sources:
    sw/Core/Src/plinky/hardware/memory.c   (flash journal, PageFooter, hash)
    sw/Core/Src/plinky/defs.h              (Preset, SysParams, Param enum)
    bootloader/Core/Src/ghostfat.c         (PRESETS.UF2 window @ 0x08080000)
"""

import argparse, base64, json, struct, sys
from pathlib import Path

# ---------------------------------------------------------------- constants --

REGION_BASE   = 0x08080000      # ghostfat.c: PRESETS UF2 content pointer
PAGE_SIZE     = 2048
NUM_PAGES     = 255             # page 255 = calibration, not part of the journal
REGION_SIZE   = NUM_PAGES * PAGE_SIZE           # 522240, == ghostfat writable window
SYSPARAMS_SIZE = 16
FOOTER_SIZE    = 8
PAGE_USABLE    = PAGE_SIZE - SYSPARAMS_SIZE - FOOTER_SIZE   # 2024
HASH_LEN       = PAGE_SIZE - FOOTER_SIZE                    # 2040, see compute_hash()
FOOTER_VERSION = 2
BLANK_IDX      = 0xFF

UF2_MAGIC0, UF2_MAGIC1, UF2_MAGIC_END = 0x0A324655, 0x9E5D5157, 0x0AB16F30
UF2_FLAG_FAMILYID = 0x2000
UF2_FAMILY = 0x00FF6919         # ghostfat.c, STM32L4xx

NUM_PRESETS, NUM_PATTERNS, NUM_SAMPLES = 32, 24, 8
PATTERNS_START   = NUM_PRESETS                      # 32
NUM_PTN_QUARTERS = NUM_PATTERNS * 4                 # 96
F_SAMPLES_START  = PATTERNS_START + NUM_PTN_QUARTERS  # 128
FLOAT_PRESET_ID  = F_SAMPLES_START + NUM_SAMPLES      # 136
FLOAT_PATTERN_ID = FLOAT_PRESET_ID + 1                # 137
GLOBAL_DATA_ID   = FLOAT_PATTERN_ID + 4               # 141
NUM_FLASH_ITEMS  = GLOBAL_DATA_ID + 1                 # 142

PRESET_SIZE = 96 * 8 * 2 + 16   # params[96][8] + 16 tail bytes = 1552
RAW_FULL = 1024                 # raw param scale: +-1024 == +-100.0%

MOD_SRC = ["base", "env2", "pres", "A", "B", "X", "Y", "rnd"]

PARAM_ROWS = [
    ("Sound 1",  ["P_SHAPE","P_DISTORTION","P_PITCH","P_OCT","P_GLIDE","P_INTERVAL"]),
    ("Sound 2",  ["P_NOISE","P_RESO","P_DEGREE","P_SCALE","P_MICROTONE","P_COLUMN"]),
    ("Env 1",    ["P_ENV_LVL1","P_ATTACK1","P_DECAY1","P_SUSTAIN1","P_RELEASE1","P_ROOT"]),
    ("Env 2",    ["P_ENV_LVL2","P_ATTACK2","P_DECAY2","P_SUSTAIN2","P_RELEASE2","P_ENV2_UNUSED"]),
    ("Delay",    ["P_DLY_SEND","P_DLY_TIME","P_PING_PONG","P_DLY_WOBBLE","P_DLY_FEEDBACK","P_TEMPO"]),
    ("Reverb",   ["P_RVB_SEND","P_RVB_TIME","P_SHIMMER","P_RVB_WOBBLE","P_RVB_UNUSED","P_SWING"]),
    ("Arp",      ["P_ARP_TGL","P_ARP_ORDER","P_ARP_CLK_DIV","P_ARP_CHANCE","P_ARP_EUC_LEN","P_ARP_OCTAVES"]),
    ("Sequencer",["P_LATCH_TGL","P_SEQ_ORDER","P_SEQ_CLK_DIV","P_SEQ_CHANCE","P_SEQ_EUC_LEN","P_GATE_LENGTH"]),
    ("Sampler 1",["P_SCRUB","P_GR_SIZE","P_PLAY_SPD","P_SMP_STRETCH","P_SAMPLE","P_PATTERN"]),
    ("Sampler 2",["P_SCRUB_JIT","P_GR_SIZE_JIT","P_PLAY_SPD_JIT","P_SMP_UNUSED1","P_SMP_UNUSED2","P_STEP_OFFSET"]),
    ("LFO A",    ["P_A_SCALE","P_A_OFFSET","P_A_DEPTH","P_A_RATE","P_A_SHAPE","P_A_SYM"]),
    ("LFO B",    ["P_B_SCALE","P_B_OFFSET","P_B_DEPTH","P_B_RATE","P_B_SHAPE","P_B_SYM"]),
    ("LFO X",    ["P_X_SCALE","P_X_OFFSET","P_X_DEPTH","P_X_RATE","P_X_SHAPE","P_X_SYM"]),
    ("LFO Y",    ["P_Y_SCALE","P_Y_OFFSET","P_Y_DEPTH","P_Y_RATE","P_Y_SHAPE","P_Y_SYM"]),
    ("Mixer 1",  ["P_SYN_LVL","P_SYN_WET_DRY","P_HPF","P_MIX_UNUSED1","P_SETTINGS1","P_VOLUME"]),
    ("Mixer 2",  ["P_IN_LVL","P_IN_WET_DRY","P_SYS_UNUSED1","P_MIX_UNUSED2","P_SETTINGS2","P_MIX_WIDTH"]),
]
PARAM_NAMES = [p for _, row in PARAM_ROWS for p in row]
assert len(PARAM_NAMES) == 96
PARAM_ID = {n: i for i, n in enumerate(PARAM_NAMES)}

CATEGORIES = ["", "Bass", "Leads", "Pads", "Arps", "Plinks", "Plonks", "Beeps", "Boops",
              "SFX", "Line-In", "Sampler", "Donk", "Jolly", "Sadness", "Wild", "Gnarly", "Weird"]

# -------------------------------------------------------------------- utils --

def compute_hash(data: bytes) -> int:
    """memory.c compute_hash(): u16 hash = 123; hash = hash * 23 + byte"""
    h = 123
    for b in data:
        h = (h * 23 + b) & 0xFFFF
    return h

def item_kind(idx: int) -> str:
    if idx < PATTERNS_START:            return "preset"
    if idx < F_SAMPLES_START:           return "pattern_qtr"
    if idx < FLOAT_PRESET_ID:           return "sample_info"
    if idx == FLOAT_PRESET_ID:          return "float_preset"
    if idx < GLOBAL_DATA_ID:            return "float_pattern_qtr"
    if idx == GLOBAL_DATA_ID:           return "global_data"
    return "unknown"

# ------------------------------------------------------------------ uf2 i/o --

def read_image(path: Path) -> bytearray:
    """Read PRESETS.UF2 (or a raw .bin dump) into the 522240-byte journal region."""
    blob = path.read_bytes()
    region = bytearray(b"\xff" * REGION_SIZE)
    if len(blob) >= 512 and struct.unpack_from("<I", blob, 0)[0] == UF2_MAGIC0:
        n_ok = 0
        for off in range(0, len(blob) - 511, 512):
            m0, m1, flags, addr, size, _blkno, _numblk, _fam = struct.unpack_from("<8I", blob, off)
            end, = struct.unpack_from("<I", blob, off + 508)
            if m0 != UF2_MAGIC0 or m1 != UF2_MAGIC1 or end != UF2_MAGIC_END:
                continue
            if size > 256:
                continue
            rel = addr - REGION_BASE
            if rel < 0 or rel + size > REGION_SIZE:
                continue        # outside the journal (e.g. calib page) - ignored
            region[rel:rel + size] = blob[off + 32: off + 32 + size]
            n_ok += 1
        if n_ok == 0:
            sys.exit(f"{path}: looks like UF2 but no block targets the preset region "
                     f"@ {REGION_BASE:#x}")
    else:
        if len(blob) < REGION_SIZE:
            sys.exit(f"{path}: raw image too small ({len(blob)} bytes, need {REGION_SIZE})")
        region[:] = blob[:REGION_SIZE]
    return region

def write_uf2(path: Path, region: bytes) -> None:
    blocks = REGION_SIZE // 256
    out = bytearray()
    for i in range(blocks):
        payload = region[i * 256:(i + 1) * 256]
        out += struct.pack("<8I", UF2_MAGIC0, UF2_MAGIC1, UF2_FLAG_FAMILYID,
                           REGION_BASE + i * 256, 256, i, blocks, UF2_FAMILY)
        out += payload + b"\x00" * (476 - 256)
        out += struct.pack("<I", UF2_MAGIC_END)
    path.write_bytes(bytes(out))

# ------------------------------------------------------------- journal scan --

class Page:
    __slots__ = ("page_id", "payload", "sys_params", "idx", "version", "crc", "seq", "crc_ok")

def parse_pages(region: bytes):
    pages = []
    for p in range(NUM_PAGES):
        raw = region[p * PAGE_SIZE:(p + 1) * PAGE_SIZE]
        idx, version, crc, seq = struct.unpack_from("<BBHI", raw, HASH_LEN)
        pg = Page()
        pg.page_id, pg.idx, pg.version, pg.crc, pg.seq = p, idx, version, crc, seq
        pg.payload = raw[:PAGE_USABLE]
        pg.sys_params = raw[PAGE_USABLE:HASH_LEN]
        pg.crc_ok = (compute_hash(raw[:HASH_LEN]) == crc)
        pages.append(pg)
    return pages

def latest_items(pages):
    """init_memory() rules: skip blank/old/bad-crc, highest seq per idx wins."""
    latest, newest = {}, None
    for pg in pages:
        if pg.idx >= NUM_FLASH_ITEMS or pg.version < FOOTER_VERSION or not pg.crc_ok:
            continue
        if newest is None or pg.seq > newest.seq:
            newest = pg
        cur = latest.get(pg.idx)
        if cur is None or pg.seq > cur.seq:
            latest[pg.idx] = pg
    return latest, newest

# ------------------------------------------------------------ preset codec --

def decode_preset(buf: bytes) -> dict:
    vals = struct.unpack_from("<768h", buf, 0)
    params = {}
    for pid, pname in enumerate(PARAM_NAMES):
        row = vals[pid * 8:(pid + 1) * 8]
        if any(row):
            params[pname] = {MOD_SRC[s]: v for s, v in enumerate(row) if v}
    rest = buf[PRESET_SIZE:]
    tail = buf[1536:1552]
    name = bytes(tail[8:16]).split(b"\x00")[0].decode("latin-1").rstrip()
    cat = tail[7]
    return {
        "name": name,
        "category": CATEGORIES[cat] if cat < len(CATEGORIES) else f"?{cat}",
        "category_id": cat,
        "preset_version": tail[6],
        "seq_start": tail[1], "seq_len": tail[2],
        "_tail_hex": tail.hex(),     # lossless: pad/paddy bytes preserved
        # whatever a newer firmware appended to Preset (0.4.2c: multi_params). not decoded, only carried through, so
        # that rebuilding a bank from this json cannot wipe it
        **({"_rest_b64": base64.b64encode(rest).decode()} if any(b != 0xFF for b in rest) else {}),
        "params": params,
    }

def encode_preset(d: dict) -> bytes:
    vals = [0] * 768
    for pname, mods in d.get("params", {}).items():
        if pname not in PARAM_ID:
            raise SystemExit(f"unknown parameter name: {pname}")
        base = PARAM_ID[pname] * 8
        for src, v in mods.items():
            if src not in MOD_SRC:
                raise SystemExit(f"unknown mod source: {src} (in {pname})")
            v = int(v)
            if not -32768 <= v <= 32767:
                raise SystemExit(f"{pname}.{src} out of s16 range: {v}")
            vals[base + MOD_SRC.index(src)] = v
    tail = bytearray(bytes.fromhex(d["_tail_hex"])) if d.get("_tail_hex") else bytearray(16)
    if "seq_start" in d:   tail[1] = int(d["seq_start"]) & 0xFF
    if "seq_len" in d:     tail[2] = int(d["seq_len"]) & 0xFF
    if "preset_version" in d: tail[6] = int(d["preset_version"]) & 0xFF
    cat = d.get("category_id")
    if cat is None:
        name_ = d.get("category", "")
        cat = CATEGORIES.index(name_) if name_ in CATEGORIES else 0
    tail[7] = int(cat) & 0xFF
    nm = d.get("name", "").encode("latin-1")[:8]
    tail[8:16] = nm + b"\x00" * (8 - len(nm))
    rest = base64.b64decode(d["_rest_b64"]) if d.get("_rest_b64") else b""
    if len(rest) > PAGE_USABLE - PRESET_SIZE:
        raise SystemExit(f"slot {d.get('slot')}: _rest_b64 holds {len(rest)} bytes, more than a page fits")
    return struct.pack("<768h", *vals) + bytes(tail) + rest

# ------------------------------------------------------------- page writing --

def make_page(idx: int, payload: bytes, sys_params: bytes, seq: int) -> bytes:
    # the firmware erases the page and then writes sizeof(item) bytes, so everything past the item
    # stays erased. filling the same way keeps a rebuilt page byte-identical to one the device wrote
    body = bytearray(b"\xff" * PAGE_USABLE)
    body[:len(payload)] = payload
    raw = bytearray(body + sys_params)
    assert len(raw) == HASH_LEN
    crc = compute_hash(bytes(raw))
    raw += struct.pack("<BBHI", idx, FOOTER_VERSION, crc, seq)
    return bytes(raw)

def rebuild_region(pages, latest, newest, presets_by_slot: dict, keep_aligned=False) -> bytes:
    """Compact the journal: every live item gets exactly one fresh page."""
    sys_params = newest.sys_params if newest else bytes(SYSPARAMS_SIZE)
    if not keep_aligned:
        # the floating (currently loaded) preset no longer matches its slot
        sp = bytearray(sys_params)
        sp[4] &= ~(1 << 6)          # SysParams.preset_aligned = false
        sys_params = bytes(sp)
    out = bytearray(b"\xff" * REGION_SIZE)
    seq, page_id = 1, 0
    for idx in sorted(latest):
        if idx < NUM_PRESETS and idx in presets_by_slot:
            payload = presets_by_slot[idx]
        else:
            payload = latest[idx].payload
        if page_id >= NUM_PAGES:
            sys.exit("out of flash pages while rebuilding (bank corrupt?)")
        out[page_id * PAGE_SIZE:(page_id + 1) * PAGE_SIZE] = make_page(idx, payload, sys_params, seq)
        page_id += 1
        seq += 1
    # presets that exist in the json but had no page in the base image
    for idx, payload in sorted(presets_by_slot.items()):
        if idx in latest:
            continue
        out[page_id * PAGE_SIZE:(page_id + 1) * PAGE_SIZE] = make_page(idx, payload, sys_params, seq)
        page_id += 1
        seq += 1
    return bytes(out)

# ---------------------------------------------------------------- commands --

def cmd_dump(args):
    region = read_image(Path(args.image))
    pages = parse_pages(region)
    latest, newest = latest_items(pages)
    bad = [p.page_id for p in pages if p.idx != BLANK_IDX and not p.crc_ok]
    presets = []
    for slot in range(NUM_PRESETS):
        pg = latest.get(slot)
        if pg is None:
            presets.append({"slot": slot + 1, "empty": True})
            continue
        d = decode_preset(pg.payload)
        d = {"slot": slot + 1, **d, "_flash_seq": pg.seq}
        presets.append(d)
    sp = newest.sys_params if newest else bytes(SYSPARAMS_SIZE)
    doc = {
        "_format": "plinky-bank/1",
        "_source": str(args.image),
        "sys_params": {
            "preset_id": sp[0], "accel_sens": sp[2],
            "accel_sens_pct": round((sp[2] - 100) * 2, 1),
            "version": sp[15], "_raw_b64": base64.b64encode(sp).decode(),
        },
        "stats": {
            "pages_used": len([p for p in pages if p.idx != BLANK_IDX]),
            "pages_live": len(latest),
            "presets_found": sum(1 for p in presets if not p.get("empty")),
            "patterns_found": sum(1 for i in latest if item_kind(i) == "pattern_qtr"),
            "samples_found": sum(1 for i in latest if item_kind(i) == "sample_info"),
            "bad_crc_pages": bad,
            "presets_with_appended_data": sum(1 for p in presets if p.get("_rest_b64")),
        },
        "presets": presets,
    }
    out = Path(args.out)
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    s = doc["stats"]
    print(f"{args.image}: {s['presets_found']}/32 presets, {s['patterns_found']} pattern quarters, "
          f"{s['samples_found']} sample infos, {s['pages_used']} pages used"
          + (f", {len(bad)} BAD CRC pages: {bad}" if bad else ""))
    print(f"accel sens: {doc['sys_params']['accel_sens_pct']}%  ->  {out}")

def cmd_build(args):
    doc = json.loads(Path(args.bank).read_text())
    region = read_image(Path(args.base))
    pages = parse_pages(region)
    latest, newest = latest_items(pages)
    by_slot = {}
    for p in doc["presets"]:
        if p.get("empty"):
            continue
        by_slot[int(p["slot"]) - 1] = encode_preset(p)
    new_region = rebuild_region(pages, latest, newest, by_slot, args.keep_aligned)
    out = Path(args.out)
    if out.suffix.lower() == ".bin":
        out.write_bytes(new_region)
    else:
        write_uf2(out, new_region)
    print(f"wrote {out} ({len(by_slot)} presets, {len(latest)} live flash items)")
    print("   check it with:  plinky_bank.py verify %s --against %s" % (out, args.bank))

def cmd_verify(args):
    region = read_image(Path(args.image))
    pages = parse_pages(region)
    latest, newest = latest_items(pages)
    errs = []
    for p in pages:
        if p.idx != BLANK_IDX and not p.crc_ok:
            errs.append(f"page {p.page_id}: bad crc")
    want = json.loads(Path(args.against).read_text()) if args.against else None
    if want:
        for p in want["presets"]:
            slot = int(p["slot"]) - 1
            if p.get("empty"):
                continue
            pg = latest.get(slot)
            if pg is None:
                errs.append(f"slot {slot+1}: missing in image")
                continue
            enc = encode_preset(p)
            if pg.payload[:len(enc)] != enc or any(b != 0xFF for b in pg.payload[len(enc):]):
                errs.append(f"slot {slot+1}: content differs from json")
    print(f"{args.image}: {len(latest)} live items, "
          f"{sum(1 for i in latest if item_kind(i)=='preset')} presets")
    if errs:
        print("PROBLEMS:")
        for e in errs:
            print("  -", e)
        sys.exit(1)
    print("OK" + (" - matches json" if want else ""))

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump", help="PRESETS.UF2 -> readable json")
    d.add_argument("image"); d.add_argument("-o", "--out", default="bank.json")
    d.set_defaults(func=cmd_dump)
    b = sub.add_parser("build", help="json -> PRESETS.UF2")
    b.add_argument("bank"); b.add_argument("--base", required=True,
                   help="original PRESETS.UF2 (patterns/samples/settings are carried over)")
    b.add_argument("-o", "--out", default="PRESETS_new.UF2")
    b.add_argument("--keep-aligned", action="store_true",
                   help="keep SysParams.preset_aligned (default: clear it, so the device "
                        "reloads the slot instead of trusting the floating preset)")
    b.set_defaults(func=cmd_build)
    v = sub.add_parser("verify", help="sanity-check an image, optionally against a json")
    v.add_argument("image"); v.add_argument("--against")
    v.set_defaults(func=cmd_verify)
    args = ap.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
