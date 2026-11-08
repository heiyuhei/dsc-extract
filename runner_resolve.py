#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run on the GitHub runner: resolve the 53 target symbols inside the XR dyld cache.
Pure stdlib (mmap + struct), no numpy needed. Output is a tiny JSON.
"""
import os, sys, json, struct, glob

def find_dsc():
    """Only the real shared cache lives under System/Library/Caches/com.apple.dyld/.
    (DriverKit also ships a tiny dyld_shared_cache_arm64e - must be skipped.)"""
    cands = []
    for base in ("out", "work", "."):
        if not os.path.isdir(base):
            continue
        cands += glob.glob(os.path.join(base, "**", "System", "Library", "Caches", "com.apple.dyld", "dyld_shared_cache_arm64e"), recursive=True)
    for p in cands:
        d = os.path.dirname(p)
        if os.path.exists(os.path.join(d, "dyld_shared_cache_arm64e.symbols")):
            return d
    return None

D = find_dsc()
if not D:
    print("DSC NOT FOUND", file=sys.stderr); sys.exit(1)
MAIN = os.path.join(D, "dyld_shared_cache_arm64e")
SYMB = os.path.join(D, "dyld_shared_cache_arm64e.symbols")
print("dsc dir:", D, "main size:", os.path.getsize(MAIN))

TARGETS = json.load(open("targets.json"))   # {key: {"module":..., "name":...}}

# ---------- images ----------
with open(MAIN, "rb") as f:
    mb = f.read()
ito = struct.unpack_from("<Q", mb, 0x88)[0]
itc = struct.unpack_from("<Q", mb, 0x90)[0]
imgs = []
for i in range(itc):
    o = ito + i*32
    lo, ts, po = struct.unpack_from("<QII", mb, o+16)
    end = mb.index(b"\x00", po)
    imgs.append(dict(load=lo, size=ts, path=mb[po:end].decode("utf-8", "replace")))
print("images:", len(imgs))

with open(SYMB, "rb") as f:
    f.seek(0x4000)
    nlistOff, nlistCount, strOff, strSize, entriesOff, entriesCount = struct.unpack("<IIIIII", f.read(24))
NBASE = 0x4000 + nlistOff
SBASE = 0x4000 + strOff
print("nlist:", nlistCount, "entries:", entriesCount)

with open(SYMB, "rb") as f:
    f.seek(0x4000 + entriesOff)
    eb = f.read(entriesCount*16)
entries = []
for i in range(entriesCount):
    dy, st, cn = struct.unpack_from("<QII", eb, i*16)
    entries.append((st, cn))

# module basename -> image index
def img_index_for(modname):
    hits = [i for i, im in enumerate(imgs) if os.path.basename(im["path"]).replace(".dylib","") == modname
            or modname in im["path"]]
    return hits

want = {}   # (imgsym name) -> key
by_module = {}
for key, t in TARGETS.items():
    if not t.get("name"):
        continue
    by_module.setdefault(t["module"], []).append((key, t["name"]))

res = {}
mm = open(SYMB, "rb")
try:
    import mmap
    m = mmap.mmap(mm.fileno(), 0, access=mmap.ACCESS_READ)
except Exception:
    m = None

def cstr(off):
    e = m.find(b"\x00", SBASE + off)
    return m[SBASE+off:e].decode("latin1")

for mod, items in by_module.items():
    idxs = img_index_for(mod)
    if not idxs:
        print(f"  [{mod}] image not found"); continue
    names = dict(items)     # symbol name -> key
    found = {}
    for ii in idxs:
        st, cn = entries[ii]
        base = NBASE + st*16
        for k in range(cn):
            o = base + k*16
            strx = struct.unpack_from("<I", m, o)[0]
            val  = struct.unpack_from("<Q", m, o+8)[0]
            if strx >= strSize: continue
            s = cstr(strx)
            if s in names:
                found.setdefault(names[s], val)
    for nm, key in names.items():
        if key in found:
            res[key] = dict(value=found[key], how="local", module=mod, name=nm)
    print(f"  [{mod}] wanted={len(names)} found={len([k for k in found])}")

if m: m.close()

json.dump(res, open("xr_symbols.json", "w"), indent=1)
print("\nresolved:", len(res), "/", len(TARGETS))
missing = [k for k in TARGETS if k not in res and TARGETS[k].get("name")]
print("missing:", len(missing))
for k in missing[:20]:
    print("   ", k, TARGETS[k]["name"])
