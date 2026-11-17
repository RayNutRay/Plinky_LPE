#!/usr/bin/env python3
"""
Self-test for plinky_bank.py - needs no hardware and no real bank.

Builds synthetic PRESETS.UF2 images the way the firmware writes them (journal
pages, hash over the first 2040 bytes, footers, erased 0xff fill past each
item), then checks that dump -> edit -> build -> verify keeps everything it was
not asked to change. Covers the 0.4.2c preset layout, where multi_params are
appended past the 1552 bytes this tool models, and the 0.4.0 one, where they
are not.

    python3 tools/test_plinky_bank.py
"""
import base64, json, os, random, struct, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp(prefix="plinky_bank_test_"))
import plinky_bank as pb

rnd = random.Random(11)
MULTI_BYTES = 33 * 7 * 2 + 2          # multi_params[33][7] + 2 padding, as of 0.4.2c


class A:
    pass


def preset_payload(slot, version, appended):
    """A page payload as the firmware leaves it: the preset, then erased flash."""
    p = bytearray(b"\xff" * pb.PAGE_USABLE)
    vals = [0] * 768
    for _ in range(30):
        vals[rnd.randrange(96) * 8 + rnd.randrange(8)] = rnd.randrange(-1024, 1025)
    struct.pack_into("<768h", p, 0, *vals)
    tail = bytearray(16)
    tail[0] = rnd.randrange(256)                               # pad
    tail[1], tail[2] = rnd.randrange(64), rnd.randrange(64)    # seq start / len
    tail[3:6] = bytes(rnd.randrange(256) for _ in range(3))    # paddy
    tail[6] = version
    tail[7] = rnd.randrange(1, 18)
    nm = f"OLD{slot + 1}".encode()
    tail[8:8 + len(nm)] = nm
    p[1536:1552] = tail
    if appended:
        rows = [0] * (33 * 7)
        for _ in range(12):
            rows[rnd.randrange(len(rows))] = rnd.randrange(-1024, 1025)
        struct.pack_into(f"<{len(rows)}h", p, 1552, *rows)
        p[1552 + 33 * 7 * 2:1552 + MULTI_BYTES] = b"\xab\xcd"
    return bytes(p)


def build_bank(path, version, appended):
    region = bytearray(b"\xff" * pb.REGION_SIZE)
    sysp = bytes([3, 0x00, 150, 200, 0b00000101] + [0] * 10 + [17])
    presets = {s: preset_payload(s, version, appended) for s in range(32)}
    others = {}
    for idx in list(range(32, 128, 9)) + list(range(128, 136)) + [136, 137, 138, 139, 140]:
        others[idx] = bytes(rnd.randrange(256) for _ in range(pb.PAGE_USABLE))
    page, seq = 0, 1
    # a stale copy of slot 0 and a page with a broken hash: both must be ignored
    region[0:2048] = pb.make_page(0, preset_payload(99, version, appended), sysp, seq)
    bad = bytearray(pb.make_page(5, preset_payload(5, version, appended), sysp, seq + 1))
    bad[2042] ^= 0xFF
    region[2048:4096] = bytes(bad)
    page, seq = 2, 3
    for idx, payload in list(presets.items()) + list(others.items()):
        region[page * 2048:(page + 1) * 2048] = pb.make_page(idx, payload, sysp, seq)
        page += 1
        seq += 1
    pb.write_uf2(pb.Path(path), bytes(region))
    return presets, others, sysp


def dump(img, out):
    a = A()
    a.image, a.out = img, out
    pb.cmd_dump(a)
    return json.loads(open(out).read())


def build(bank, base, out):
    b = A()
    b.bank, b.base, b.out, b.keep_aligned = bank, base, out, False
    pb.cmd_build(b)


def verify(img, against):
    v = A()
    v.image, v.against = img, against
    pb.cmd_verify(v)


def payload_of(img, idx):
    latest, _ = pb.latest_items(pb.parse_pages(pb.read_image(pb.Path(img))))
    return latest[idx].payload


# ===================== a 0.4.2c bank: appended bytes present =====================
print("=== 0.4.2c bank (multi_params appended) ===")
presets, others, sysp = build_bank("B17.UF2", 17, True)
doc = dump("B17.UF2", "b17.json")
assert doc["stats"]["bad_crc_pages"] == [1], doc["stats"]
assert doc["stats"]["presets_found"] == 32
assert all(p.get("_rest_b64") for p in doc["presets"]), "appended bytes were not captured"
assert all(len(base64.b64decode(p["_rest_b64"])) == pb.PAGE_USABLE - pb.PRESET_SIZE
           for p in doc["presets"])
for p in doc["presets"]:
    assert pb.encode_preset(p) == presets[p["slot"] - 1], f"slot {p['slot']} is not lossless"
print("  decode/encode lossless, appended bytes captured: OK")

order = list(range(31, -1, -1))          # reverse the bank while editing it
newp = []
for new_slot, old in enumerate(order):
    d = dict(doc["presets"][old])
    d["slot"] = new_slot + 1
    d["name"] = f"NEW{new_slot + 1}"
    d["category_id"] = 3
    params = {k: dict(v) for k, v in d["params"].items()}
    for q in ("P_X_DEPTH", "P_Y_DEPTH", "P_X_SCALE", "P_Y_SCALE", "P_X_OFFSET", "P_Y_OFFSET"):
        params.pop(q, None)
    params.setdefault("P_SHAPE", {})["X"] = 154
    params.setdefault("P_RESO", {})["Y"] = 154
    d["params"] = params
    newp.append(d)
del newp[0]["_rest_b64"]                 # the documented way to clear appended data
doc["presets"] = newp
open("b17_edit.json", "w").write(json.dumps(doc, indent=1))

build("b17_edit.json", "B17.UF2", "OUT17.UF2")
verify("OUT17.UF2", "b17_edit.json")
doc2 = dump("OUT17.UF2", "b17_after.json")

for p in doc2["presets"]:
    assert p["name"] == f"NEW{p['slot']}", p["name"]
    assert p["params"]["P_SHAPE"]["X"] == 154 and p["params"]["P_RESO"]["Y"] == 154
    assert "P_X_DEPTH" not in p["params"] and p["category"] == "Pads"
assert "_rest_b64" not in doc2["presets"][0], "deleting _rest_b64 did not clear the appended bytes"
assert payload_of("OUT17.UF2", 0)[pb.PRESET_SIZE:] == b"\xff" * (pb.PAGE_USABLE - pb.PRESET_SIZE)
for new_slot, old in enumerate(order):
    if new_slot == 0:
        continue
    assert (base64.b64decode(doc2["presets"][new_slot]["_rest_b64"])
            == presets[old][pb.PRESET_SIZE:]), f"slot {new_slot + 1} lost its appended bytes"
print("  edits applied, appended bytes followed their preset, one slot cleared: OK")

pages2 = pb.parse_pages(pb.read_image(pb.Path("OUT17.UF2")))
latest2, newest2 = pb.latest_items(pages2)
for idx, payload in others.items():
    assert idx in latest2 and latest2[idx].payload == payload, f"item {idx} lost or corrupted"
assert newest2.sys_params[:4] == sysp[:4] and newest2.sys_params[5:] == sysp[5:]
assert newest2.sys_params[4] == sysp[4] & ~(1 << 6), "preset_aligned was not cleared"
assert all(p.crc_ok for p in pages2 if p.idx != 0xFF), "a rebuilt page has a bad hash"
print("  pattern quarters, sample infos, floating preset, system settings kept: OK")

# ===================== a 0.4.0 bank: nothing appended =====================
print("=== 0.4.0 bank (no appended bytes) ===")
presets16, _, _ = build_bank("B16.UF2", 16, False)
doc16 = dump("B16.UF2", "b16.json")
assert not any("_rest_b64" in p for p in doc16["presets"]), "erased fill was mistaken for data"
for p in doc16["presets"]:
    assert pb.encode_preset(p) == presets16[p["slot"] - 1][:pb.PRESET_SIZE]
build("b16.json", "B16.UF2", "OUT16.UF2")
verify("OUT16.UF2", "b16.json")
for slot in range(32):
    assert payload_of("OUT16.UF2", slot) == presets16[slot], f"slot {slot + 1} is not byte-identical"
print("  round-trips byte-identical to what the device wrote: OK")

print("\nALL CHECKS PASSED")
