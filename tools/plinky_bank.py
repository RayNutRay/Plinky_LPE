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

Preset layouts, picked from the preset version byte in the bank itself:
    v2  "OG"      params[96][8] + 16 tail bytes                      = 1552 bytes
    v16 "LPE 0.4" same                                               = 1552 bytes
    v17 "LPE 0.4.2c" + multi_params[33][7] + 2 padding               = 2016 bytes
Bytes this tool does not decode are carried through untouched, so an unknown
layout still round-trips: `build` patches the fields it knows inside the page
payload it was given instead of re-encoding a preset from scratch.

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
NUM_FLASH_ITEMS  = FLOAT_PATTERN_ID + 4               # 141

PARAMS_BYTES = 96 * 8 * 2       # params[96][8] = 1536
TAIL_OFFSET = PARAMS_BYTES      # pad, seq_start, seq_len, paddy[3], version, category, name[8]
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
# names these slots had before v17, accepted when reading a json written earlier
PARAM_ID.update({"P_ENV1_UNUSED": PARAM_ID["P_ROOT"],
                 "P_MIX_UNUSED4": PARAM_ID["P_SETTINGS1"],
                 "P_MIX_UNUSED3": PARAM_ID["P_SETTINGS2"]})

# per-string values, strings 2-8 (string 1 uses params[...][base]). Order is the MultiParam enum.
MULTI_PARAM_NAMES = [
    "MP_SHAPE", "MP_DISTORTION", "MP_PITCH", "MP_OCT", "MP_GLIDE", "MP_INTERVAL",
    "MP_NOISE", "MP_RESO", "MP_DEGREE", "MP_SCALE", "MP_MICROTONE", "MP_COLUMN",
    "MP_ENV_LVL1", "MP_ATTACK1", "MP_DECAY1", "MP_SUSTAIN1", "MP_RELEASE1", "MP_ROOT",
    "MP_ENV_LVL2", "MP_ATTACK2", "MP_DECAY2", "MP_SUSTAIN2", "MP_RELEASE2",
    "MP_ARP_TGL", "MP_LATCH_TGL", "MP_GATE_LENGTH",
    "MP_SCRUB", "MP_GR_SIZE", "MP_PLAY_SPD", "MP_SMP_STRETCH",
    "MP_SCRUB_JIT", "MP_GR_SIZE_JIT", "MP_PLAY_SPD_JIT",
]
MULTI_PARAM_ID = {n: i for i, n in enumerate(MULTI_PARAM_NAMES)}
NUM_MULTI_PARAMS, NUM_MULTI_STRINGS = len(MULTI_PARAM_NAMES), 7
MULTI_OFFSET = 96 * 8 * 2 + 16          # straight after the tail
MULTI_BYTES = NUM_MULTI_PARAMS * NUM_MULTI_STRINGS * 2

# preset version byte -> layout
LAYOUTS = {
    2:  {"name": "OG",                "size": 1552, "multi": False},
    16: {"name": "LPE 0.4.0",         "size": 1552, "multi": False},
    17: {"name": "LPE 0.4.2c",        "size": MULTI_OFFSET + MULTI_BYTES + 2, "multi": True},
}
UNKNOWN_LAYOUT = {"name": "unknown", "size": 1552, "multi": False}

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
    if idx < NUM_FLASH_ITEMS:           return "float_pattern_qtr"
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

def detect_layout(latest):
    """Pick the layout from the preset version byte the bank itself carries."""
    versions = {}
    for idx, pg in latest.items():
        if idx < NUM_PRESETS:          # the 32 slots only: the floating preset may be stale
            v = pg.payload[TAIL_OFFSET + 6]
            versions[v] = versions.get(v, 0) + 1
    if not versions:
        return UNKNOWN_LAYOUT, None, versions
    version = max(versions, key=versions.get)        # the one most presets agree on
    return LAYOUTS.get(version, UNKNOWN_LAYOUT), version, versions

def decode_preset(payload: bytes, layout: dict) -> dict:
    """payload is the whole page payload; everything not decoded here is kept verbatim."""
    vals = struct.unpack_from("<768h", payload, 0)
    params = {}
    for pid, pname in enumerate(PARAM_NAMES):
        row = vals[pid * 8:(pid + 1) * 8]
        if any(row):
            params[pname] = {MOD_SRC[s]: v for s, v in enumerate(row) if v}
    tail = payload[TAIL_OFFSET:TAIL_OFFSET + 16]
    name = bytes(tail[8:16]).split(b"\x00")[0].decode("latin-1").rstrip()
    cat = tail[7]
    out = {
        "name": name,
        "category": CATEGORIES[cat] if cat < len(CATEGORIES) else f"?{cat}",
        "category_id": cat,
        "preset_version": tail[6],
        "seq_start": tail[1], "seq_len": tail[2],
        "_tail_hex": tail.hex(),          # keeps pad/paddy and anything else in the tail
        "params": params,
    }
    rest_at = TAIL_OFFSET + 16
    if layout["multi"]:
        multi = {}
        rows = struct.unpack_from(f"<{NUM_MULTI_PARAMS * NUM_MULTI_STRINGS}h", payload, MULTI_OFFSET)
        for mid, mname in enumerate(MULTI_PARAM_NAMES):
            row = list(rows[mid * NUM_MULTI_STRINGS:(mid + 1) * NUM_MULTI_STRINGS])
            if any(row):
                multi[mname] = row        # strings 2-8; string 1 lives in params[...]["base"]
        out["multi_params"] = multi
        rest_at = MULTI_OFFSET + MULTI_BYTES
    out["_rest_hex"] = payload[rest_at:].hex()
    return out

def encode_preset(d: dict, layout: dict) -> bytes:
    """Rebuild the whole page payload: decoded fields patched, unknown bytes restored."""
    payload = bytearray(PAGE_USABLE)
    vals = [0] * 768
    for pname, mods in d.get("params", {}).items():
        if pname not in PARAM_ID:
            sys.exit(f"unknown parameter name: {pname}")
        base = PARAM_ID[pname] * 8
        for src, v in mods.items():
            if src not in MOD_SRC:
                sys.exit(f"unknown mod source: {src} (in {pname})")
            v = int(v)
            if not -32768 <= v <= 32767:
                sys.exit(f"{pname}.{src} out of s16 range: {v}")
            vals[base + MOD_SRC.index(src)] = v
    struct.pack_into("<768h", payload, 0, *vals)

    tail = bytearray(bytes.fromhex(d["_tail_hex"])) if d.get("_tail_hex") else bytearray(16)
    if "seq_start" in d:      tail[1] = int(d["seq_start"]) & 0xFF
    if "seq_len" in d:        tail[2] = int(d["seq_len"]) & 0xFF
    if "preset_version" in d: tail[6] = int(d["preset_version"]) & 0xFF
    cat = d.get("category_id")
    if cat is None:
        cat_name = d.get("category", "")
        cat = CATEGORIES.index(cat_name) if cat_name in CATEGORIES else 0
    tail[7] = int(cat) & 0xFF
    nm = d.get("name", "").encode("latin-1")[:8]
    tail[8:16] = nm + b"\x00" * (8 - len(nm))
    payload[TAIL_OFFSET:TAIL_OFFSET + 16] = tail

    rest_at = TAIL_OFFSET + 16
    if layout["multi"]:
        rows = [0] * (NUM_MULTI_PARAMS * NUM_MULTI_STRINGS)
        for mname, row in (d.get("multi_params") or {}).items():
            if mname not in MULTI_PARAM_ID:
                sys.exit(f"unknown multi parameter name: {mname}")
            if len(row) != NUM_MULTI_STRINGS:
                sys.exit(f"{mname}: expected {NUM_MULTI_STRINGS} values, got {len(row)}")
            at = MULTI_PARAM_ID[mname] * NUM_MULTI_STRINGS
            for i, v in enumerate(row):
                v = int(v)
                if not -32768 <= v <= 32767:
                    sys.exit(f"{mname}[{i}] out of s16 range: {v}")
                rows[at + i] = v
        struct.pack_into(f"<{len(rows)}h", payload, MULTI_OFFSET, *rows)
        rest_at = MULTI_OFFSET + MULTI_BYTES

    rest = bytes.fromhex(d["_rest_hex"]) if d.get("_rest_hex") else b""
    if rest:
        if rest_at + len(rest) > PAGE_USABLE:
            sys.exit("_rest_hex is longer than the page can hold")
        payload[rest_at:rest_at + len(rest)] = rest
    return bytes(payload)

# ------------------------------------------------------------- page writing --

def make_page(idx: int, payload: bytes, sys_params: bytes, seq: int) -> bytes:
    body = bytearray(b"\x00" * PAGE_USABLE)
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
    layout, version, versions = detect_layout(latest)
    presets = []
    for slot in range(NUM_PRESETS):
        pg = latest.get(slot)
        if pg is None:
            presets.append({"slot": slot + 1, "empty": True})
            continue
        d = decode_preset(pg.payload, layout)
        d = {"slot": slot + 1, **d, "_flash_seq": pg.seq}
        presets.append(d)
    sp = newest.sys_params if newest else bytes(SYSPARAMS_SIZE)
    doc = {
        "_format": "plinky-bank/2",
        "_source": str(args.image),
        "layout": {
            "firmware": layout["name"],
            "preset_version": version,
            "preset_size": layout["size"],
            "multi_params": layout["multi"],
        },
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
        },
        "presets": presets,
    }
    out = Path(args.out)
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    s = doc["stats"]
    print(f"{args.image}: {s['presets_found']}/32 presets, {s['patterns_found']} pattern quarters, "
          f"{s['samples_found']} sample infos, {s['pages_used']} pages used"
          + (f", {len(bad)} BAD CRC pages: {bad}" if bad else ""))
    print(f"layout: {layout['name']} (preset version {version}, {layout['size']} bytes"
          + (", with per-string values)" if layout["multi"] else ")"))
    if len(versions) > 1:
        print(f"   note: presets disagree on the version byte: {versions}")
    if layout is UNKNOWN_LAYOUT:
        print("   note: unrecognised preset version - params and name are still decoded, and everything")
        print("         past them is carried through verbatim, so a rebuild stays lossless")
    print(f"accel sens: {doc['sys_params']['accel_sens_pct']}%  ->  {out}")

def cmd_build(args):
    doc = json.loads(Path(args.bank).read_text())
    region = read_image(Path(args.base))
    pages = parse_pages(region)
    latest, newest = latest_items(pages)
    img_layout, img_version, _ = detect_layout(latest)
    json_layout = doc.get("layout") or {}
    layout = LAYOUTS.get(json_layout.get("preset_version"), img_layout)
    if json_layout and json_layout.get("preset_version") != img_version and not args.force_layout:
        sys.exit(f"the json was dumped from a {json_layout.get('firmware')} bank (preset version "
                 f"{json_layout.get('preset_version')}) but --base is {img_layout['name']} (version "
                 f"{img_version}). Dump the base image again, or pass --force-layout to go ahead anyway.")
    by_slot = {}
    for p in doc["presets"]:
        if p.get("empty"):
            continue
        by_slot[int(p["slot"]) - 1] = encode_preset(p, layout)
    new_region = rebuild_region(pages, latest, newest, by_slot, args.keep_aligned)
    out = Path(args.out)
    if out.suffix.lower() == ".bin":
        out.write_bytes(new_region)
    else:
        write_uf2(out, new_region)
    print(f"wrote {out} ({len(by_slot)} presets, {len(latest)} live flash items, {layout['name']} layout)")
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
    layout, version, _ = detect_layout(latest)
    if want:
        layout = LAYOUTS.get((want.get("layout") or {}).get("preset_version"), layout)
        for p in want["presets"]:
            slot = int(p["slot"]) - 1
            if p.get("empty"):
                continue
            pg = latest.get(slot)
            if pg is None:
                errs.append(f"slot {slot+1}: missing in image")
                continue
            if pg.payload != encode_preset(p, layout):
                errs.append(f"slot {slot+1}: content differs from json")
    print(f"{args.image}: {len(latest)} live items, "
          f"{sum(1 for i in latest if item_kind(i)=='preset')} presets, {layout['name']} layout")
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
    b.add_argument("--force-layout", action="store_true",
                   help="build even though the json and the base image disagree on the preset layout")
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
