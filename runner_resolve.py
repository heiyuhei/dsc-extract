#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resolve target symbols (local + export) inside the XR dyld shared cache."""
import os, sys, json, struct, glob, mmap

def find_dsc():
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
print("dsc dir:", D)
MAIN = os.path.join(D, "dyld_shared_cache_arm64e")
SYMB = os.path.join(D, "dyld_shared_cache_arm64e.symbols")
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

res = {}

# ================= local symbols =================
with open(SYMB, "rb") as f:
    f.seek(0x4000)
    nlistOff, nlistCount, strOff, strSize, entriesOff, entriesCount = struct.unpack("<IIIIII", f.read(24))
NBASE = 0x4000 + nlistOff
SBASE = 0x4000 + strOff
with open(SYMB, "rb") as f:
    f.seek(0x4000 + entriesOff)
    eb = f.read(entriesCount*16)
entries = []
for i in range(entriesCount):
    dy, st, cn = struct.unpack_from("<QII", eb, i*16)
    entries.append((dy, st, cn))

fm = open(SYMB, "rb"); m = mmap.mmap(fm.fileno(), 0, access=mmap.ACCESS_READ)
def cstr(off):
    e = m.find(b"\x00", SBASE + off)
    return "" if e < 0 else m[SBASE+off:e].decode("latin1")

want_by_name = {}   # symbol name -> key
mod_of_key = {}
for key, t in TARGETS.items():
    if t.get("name"):
        want_by_name.setdefault(t["name"], []).append(key)
        mod_of_key[key] = t["module"]

# scan the whole nlist once (12M) but stop early per key
remaining = set(want_by_name.keys())
print("local scan, target names:", len(remaining))
for i in range(nlistCount):
    o = NBASE + i*16
    strx = struct.unpack_from("<I", m, o)[0]
    if strx >= strSize:
        continue
    s = cstr(strx)
    if s in remaining:
        val = struct.unpack_from("<Q", m, o+8)[0]
        for key in want_by_name[s]:
            if key not in res:
                res[key] = {"value": val, "how": "local", "name": s}
        remaining.discard(s)
        if not remaining:
            break
print("after local:", len(res), "/", len([k for k in TARGETS if TARGETS[k].get("name")]))
m.close(); fm.close()

# ================= export tries =================
# read mappings from every sub-cache to resolve VAs
maps = []
for fn in os.listdir(D):
    if not fn.startswith("dyld_shared_cache_arm64e") or ".atlas" in fn:
        continue
    p = os.path.join(D, fn)
    with open(p, "rb") as f:
        hb = f.read(0x200)
        if not hb.startswith(b"dyld_v1"):
            continue
        mo, mc = struct.unpack_from("<II", hb, 0x10)
        f.seek(mo); mm2 = f.read(mc*32)
    for i in range(mc):
        a, sz, fo, mp, ip = struct.unpack_from("<QQQII", mm2, i*32)
        if sz:
            maps.append((a, a+sz, p, fo))
maps.sort()
print("mappings:", len(maps))

handles = {}
def rd(va, n):
    lo_ = 0; hi_ = len(maps)-1
    idx = -1
    while lo_ <= hi_:
        mid = (lo_+hi_)//2
        if maps[mid][0] <= va:
            idx = mid; lo_ = mid+1
        else:
            hi_ = mid-1
    if idx < 0: return None
    a, b, p, fo = maps[idx]
    if not (a <= va < b): return None
    if p not in handles: handles[p] = open(p, "rb")
    f = handles[p]; f.seek(fo + (va - a)); return f.read(min(n, b-va))

def uleb(buf, o):
    r = 0; sh = 0
    while True:
        b = buf[o]; o += 1
        r |= (b & 0x7f) << sh
        if not (b & 0x80): return r, o
        sh += 7

def parse_trie(buf, image_base):
    out = {}
    stack = [(0, "")]
    guard = set()
    while stack:
        no, pre = stack.pop()
        if no in guard or no >= len(buf): continue
        guard.add(no)
        o = no
        tsize, o = uleb(buf, o)
        if tsize:
            tstart = o
            flags, o = uleb(buf, o)
            if flags & 0x08:
                _ord, o = uleb(buf, o)
                e = buf.find(b"\x00", o); o = (e+1) if e >= 0 else len(buf)
                out[pre] = None
            else:
                addr, o = uleb(buf, o)
                out[pre] = image_base + addr
            o = tstart + tsize
        if o < len(buf):
            nch = buf[o]; o += 1
            for _ in range(nch):
                e = buf.find(b"\x00", o)
                if e < 0: break
                edge = buf[o:e].decode("latin1"); o = e + 1
                cn, o = uleb(buf, o)
                stack.append((cn, pre + edge))
    return out

# which modules still needed
still = {}
for key, t in TARGETS.items():
    if key in res or not t.get("name"): continue
    still.setdefault(t["module"], []).append((key, t["name"]))

for mod, items in sorted(still.items()):
    idxs = [i for i, im in enumerate(imgs) if os.path.basename(im["path"]) == mod or mod in im["path"]]
    names = dict((nm, key) for key, nm in items)
    hit = 0
    for ii in idxs:
        img = imgs[ii]
        hdr = rd(img["load"], 32)
        if not hdr or len(hdr) < 32: continue
        magic, cpu, sub, ft, ncmds, sz, flags, _r = struct.unpack("<IiiIIIII", hdr)
        if magic != 0xfeedfacf: continue
        lc = rd(img["load"]+32, sz)
        if not lc: continue
        o = 0; eoff = esize = None; le_vm = le_fo = None
        for _ in range(ncmds):
            cmd, csz = struct.unpack_from("<II", lc, o)
            if cmd == 0x80000033:
                eoff, esize = struct.unpack_from("<II", lc, o+8)
            elif cmd == 0x80000022:
                eoff, esize = struct.unpack_from("<II", lc, o+40)
            elif cmd == 0x19:
                nm = lc[o+8:o+24].split(b"\x00")[0].decode()
                if nm == "__LINKEDIT":
                    vm, vs, fo, fs = struct.unpack_from("<QQQQ", lc, o+24)
                    le_vm, le_fo = vm, fo
            o += csz
        if eoff is None or le_vm is None: continue
        bias = le_vm - le_fo
        buf = rd(bias + eoff, esize)
        if not buf or len(buf) < esize: continue
        syms = parse_trie(buf, img["load"])
        for nm, key in names.items():
            if key not in res and nm in syms and syms[nm] is not None:
                res[key] = {"value": syms[nm], "how": "export", "name": nm, "module": mod}
                hit += 1
    print(f"[export {mod}] wanted={len(names)} hit={hit}")

json.dump(res, open("xr_symbols.json", "w"), indent=1)
total = len([k for k in TARGETS if TARGETS[k].get("name")])
print("RESOLVED", len(res), "of", total)
miss = [k for k in TARGETS if TARGETS[k].get("name") and k not in res]
print("still missing:", len(miss))
for k in miss: print("   ", k, TARGETS[k]["name"])
