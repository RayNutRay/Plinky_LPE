#!/usr/bin/env python3
"""
Self-test for plinky_bank.py - needs no hardware and no real bank.

Builds synthetic PRESETS.UF2 images the way the firmware writes them (journal
pages, hash, footers), then checks that dump -> edit -> build -> verify keeps
everything it is not asked to change: per-string values, pattern quarters,
sample infos, the floating preset, system settings and the padding bytes inside
a preset. Covers the 0.4.2c layout (preset version 17, with multi_params), the
0.4.0 one (version 16), and an unknown version.

    python3 tools/test_plinky_bank.py
"""
import json, os, random, struct, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp(prefix="plinky_bank_test_"))

import plinky_bank as pb

class A: pass
rnd = random.Random(11)

def preset_payload(slot, version, with_multi, cat=None):
    """build a full page payload the way the firmware would have written it"""
    p = bytearray(pb.PAGE_USABLE)
    vals = [0] * 768
    for _ in range(30):
        vals[rnd.randrange(96) * 8 + rnd.randrange(8)] = rnd.randrange(-1024, 1025)
    struct.pack_into("<768h", p, 0, *vals)
    tail = bytearray(16)
    tail[0] = rnd.randrange(256)                 # pad
    tail[1], tail[2] = rnd.randrange(64), rnd.randrange(64)
    tail[3:6] = bytes(rnd.randrange(256) for _ in range(3))   # paddy
    tail[6] = version
    tail[7] = cat if cat is not None else rnd.randrange(1, 18)
    nm = f"OLD{slot+1}".encode()
    tail[8:8 + len(nm)] = nm
    p[pb.TAIL_OFFSET:pb.TAIL_OFFSET + 16] = tail
    if with_multi:
        rows = [0] * (pb.NUM_MULTI_PARAMS * pb.NUM_MULTI_STRINGS)
        for _ in range(12):
            rows[rnd.randrange(len(rows))] = rnd.randrange(-1024, 1025)
        struct.pack_into(f"<{len(rows)}h", p, pb.MULTI_OFFSET, *rows)
        p[pb.MULTI_OFFSET + pb.MULTI_BYTES:pb.MULTI_OFFSET + pb.MULTI_BYTES + 2] = b"\xab\xcd"
    return bytes(p)

def build_bank(path, version, with_multi):
    region = bytearray(b'\xff' * pb.REGION_SIZE)
    sysp = bytes([3, 0x00, 150, 200, 0b00000101] + [0] * 10 + [17])
    presets = {s: preset_payload(s, version, with_multi) for s in range(32)}
    others = {}
    for idx in list(range(32, 128, 9)) + list(range(128, 136)) + [136, 137, 138, 139, 140]:
        others[idx] = bytes(rnd.randrange(256) for _ in range(pb.PAGE_USABLE))
    page, seq = 0, 1
    for idx, payload in list(presets.items()) + list(others.items()):
        region[page * 2048:(page + 1) * 2048] = pb.make_page(idx, payload, sysp, seq)
        page += 1; seq += 1
    pb.write_uf2(pb.Path(path), bytes(region))
    return presets, others, sysp

def dump(img, out):
    a = A(); a.image = img; a.out = out
    pb.cmd_dump(a)
    return json.loads(open(out).read())

def build(bank, base, out):
    b = A(); b.bank = bank; b.base = base; b.out = out
    b.keep_aligned = False; b.force_layout = False
    pb.cmd_build(b)

def verify(img, against):
    v = A(); v.image = img; v.against = against
    pb.cmd_verify(v)

# ============ case 1: 0.4.2c bank (preset version 17, with multi_params) ============
print("=== 0.4.2c bank ===")
presets, others, sysp = build_bank('B17.UF2', 17, True)
doc = dump('B17.UF2', 'b17.json')
assert doc["layout"]["preset_version"] == 17 and doc["layout"]["multi_params"] is True, doc["layout"]
assert doc["layout"]["preset_size"] == 2016, doc["layout"]
layout = pb.LAYOUTS[17]
for p in doc["presets"]:
    assert pb.encode_preset(p, layout) == presets[p["slot"] - 1], f"slot {p['slot']} not lossless"
assert any(p.get("multi_params") for p in doc["presets"]), "no per-string values decoded at all"
print("  lossless decode/encode incl. multi_params: OK")

# batch edit: X/Y hygiene, tilt mods, rename, category, reverse the bank
order = list(range(31, -1, -1))
newp = []
for new_slot, old in enumerate(order):
    d = dict(doc["presets"][old])
    d["slot"] = new_slot + 1
    d["name"] = f"NEW{new_slot+1}"
    d["category_id"] = 3
    params = {k: dict(v) for k, v in d["params"].items()}
    for q in ("P_X_DEPTH", "P_Y_DEPTH", "P_X_SCALE", "P_Y_SCALE", "P_X_OFFSET", "P_Y_OFFSET"):
        params.pop(q, None)
    params.setdefault("P_SHAPE", {})["X"] = 154
    params.setdefault("P_RESO", {})["Y"] = 154
    d["params"] = params
    newp.append(d)
# and edit one per-string value, to prove that path works too
newp[0]["multi_params"] = dict(newp[0].get("multi_params") or {})
newp[0]["multi_params"]["MP_ROOT"] = [1, 2, 3, 4, 5, 6, 7]
doc["presets"] = newp
open('b17_edit.json', 'w').write(json.dumps(doc, indent=1))

build('b17_edit.json', 'B17.UF2', 'OUT17.UF2')
verify('OUT17.UF2', 'b17_edit.json')
doc2 = dump('OUT17.UF2', 'b17_after.json')

for p in doc2["presets"]:
    assert p["name"] == f"NEW{p['slot']}", p["name"]
    assert p["params"]["P_SHAPE"]["X"] == 154 and p["params"]["P_RESO"]["Y"] == 154
    assert "P_X_DEPTH" not in p["params"] and p["category"] == "Pads"
assert doc2["presets"][0]["multi_params"]["MP_ROOT"] == [1, 2, 3, 4, 5, 6, 7]
# per-string values of every other slot must be exactly what the source bank had
for new_slot, old in enumerate(order):
    if new_slot == 0:
        continue
    src = pb.decode_preset(presets[old], layout).get("multi_params")
    assert doc2["presets"][new_slot].get("multi_params") == src, f"slot {new_slot+1} multi_params changed"
print("  edits applied, per-string values of untouched presets intact: OK")

pages2 = pb.parse_pages(pb.read_image(pb.Path('OUT17.UF2')))
latest2, newest2 = pb.latest_items(pages2)
for idx, payload in others.items():
    assert idx in latest2 and latest2[idx].payload == payload, f"item {idx} lost or corrupted"
assert newest2.sys_params[:4] == sysp[:4] and newest2.sys_params[5:] == sysp[5:]
assert all(p.crc_ok for p in pages2 if p.idx != 0xFF)
print("  patterns/samples/float/sys_params preserved: OK")

# ============ case 2: 0.4.0 bank (version 16, no multi_params) ============
print("=== 0.4.0 bank ===")
presets16, _, _ = build_bank('B16.UF2', 16, False)
doc16 = dump('B16.UF2', 'b16.json')
assert doc16["layout"]["preset_version"] == 16 and doc16["layout"]["multi_params"] is False
l16 = pb.LAYOUTS[16]
for p in doc16["presets"]:
    assert "multi_params" not in p
    assert pb.encode_preset(p, l16) == presets16[p["slot"] - 1], f"v16 slot {p['slot']} not lossless"
build('b16.json', 'B16.UF2', 'OUT16.UF2')
verify('OUT16.UF2', 'b16.json')
print("  v16 bank round-trips untouched: OK")

# mixing layouts must be refused
try:
    b = A(); b.bank = 'b17_edit.json'; b.base = 'B16.UF2'; b.out = 'MIX.UF2'
    b.keep_aligned = False; b.force_layout = False
    pb.cmd_build(b)
    raise AssertionError("building a v17 json onto a v16 base was not refused")
except SystemExit as e:
    assert "force-layout" in str(e), e
print("  mismatched layouts refused: OK")

# ============ case 3: unknown preset version ============
print("=== unknown version ===")
presetsX, _, _ = build_bank('BX.UF2', 99, False)
docX = dump('BX.UF2', 'bx.json')
assert docX["layout"]["firmware"] == "unknown", docX["layout"]
for p in docX["presets"]:
    assert pb.encode_preset(p, pb.UNKNOWN_LAYOUT) == presetsX[p["slot"] - 1], "unknown layout not lossless"
print("  unknown version still lossless: OK")

print("\nALL CHECKS PASSED")
