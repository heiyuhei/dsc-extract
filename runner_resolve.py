#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runner-side: locate the XR shared cache, dump image bases, and try to resolve target symbols."""
import os, sys, json, struct, glob, mmap

def find_dsc():
    print("cwd =", os.getcwd())
    best = None
    for base in ("out", "work", ".", os.getcwd()):
        if not os.path.isdir(base):
            continue
        for p in glob.glob(os.path.join(base, "**", "dyld_shared_cache_arm64e"), recursive=True):
            if "DriverKit" in p:
                continue
            d = os.path.dirname(p)
            if os.path.exists(os.path.join(d, "dyld_shared_cache_arm64e.symbols")):
                sz = os.path.getsize(p)
                if best is None or sz < best[0]:
                    best = (sz, d)
    return best[1] if best else None

D = find_dsc()
if not D:
    print("DSC NOT FOUND"); sys.exit(1)
MAIN = os.path.join(D, "dyld_shared_cache_arm64e")
SYMB = os.path.join(D, "dyld_shared_cache_arm64e.symbols")
print("dsc dir:", D)
print("main:", os.path.getsize(MAIN), "symbols:", os.path.getsize(SYMB))

TARGETS = json.load(open("targets.json"))

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
    imgs.append({"load": lo, "size": ts, "path": mb[po:end].decode("utf-8", "replace")})
print("images:", len(imgs))
json.dump(imgs, open("xr_images.json", "w"))

# ---------- local symbols ----------
with open(SYMB, "rb") as f:
    f.seek(0x4000)
    nlistOff, nlistCount, strOff, strSize, entriesOff, entriesCount = struct.unpack("<IIIIII", f.read(24))
NBASE = 0x4000 + nlistOff
SBASE = 0x4000 + strOff
print("nlist:", nlistCount, "entries:", entriesCount, "strOff:", hex(strOff), "strSize:", strSize)

with open(SYMB, "rb") as f:
    f.seek(0x4000 + entriesOff)
    eb = f.read(entriesCount*16)
entries = []
for i in range(entriesCount):
    dy, st, cn = struct.unpack_from("<QII", eb, i*16)
    entries.append((dy, st, cn))

fm = open(SYMB, "rb")
m = mmap.mmap(fm.fileno(), 0, access=mmap.ACCESS_READ)

def cstr(off):
    e = m.find(b"\x00", SBASE + off)
    if e < 0: return ""
    return m[SBASE+off:e].decode("latin1")

# sanity: dump the first symbols of the whole table
print("first 5 symbol names in table:")
for k in range(5):
    o = NBASE + k*16
    strx = struct.unpack_from("<I", m, o)[0]
    val = struct.unpack_from("<Q", m, o+8)[0]
    print("    ", repr(cstr(strx)[:50]), hex(val))

# map module name -> image indices
def imgs_for(mod):
    out = []
    for i, im in enumerate(imgs):
        b = os.path.basename(im["path"])
        if b == mod or b.replace(".dylib", "") == mod or mod in im["path"]:
            out.append(i)
    return out

by_module = {}
for key, t in TARGETS.items():
    if t.get("name"):
        by_module.setdefault(t["module"], []).append((key, t["name"]))

res = {}
for mod, items in sorted(by_module.items()):
    idxs = imgs_for(mod)
    names = dict((nm, key) for key, nm in items)
    found = {}
    dbg = []
    for ii in idxs:
        dy, st, cn = entries[ii]
        base = NBASE + st*16
        for k in range(cn):
            o = base + k*16
            strx = struct.unpack_from("<I", m, o)[0]
            val = struct.unpack_from("<Q", m, o+8)[0]
            if strx >= strSize: continue
            s = cstr(strx)
            if mod == sorted(by_module.keys())[0] and k < 8:
                dbg.append((s[:45], hex(val)))
            if s in names:
                found.setdefault(names[s], val)
    print(f"[{mod}] imgs={len(idxs)} wanted={len(names)} found={len(found)}")
    if dbg:
        print("    sample syms:", dbg[:6])
    for key, val in found.items():
        res[key] = {"value": val, "how": "local", "module": mod}

m.close(); fm.close()
json.dump(res, open("xr_symbols.json", "w"), indent=1)
print("RESOLVED", len(res), "of", len([k for k in TARGETS if TARGETS[k].get("name")]))
