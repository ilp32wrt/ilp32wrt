#!/usr/bin/python3
"""Repackage official OpenWrt LP64 apks so they install next to ILP32Wrt in one apk database.

ILP32Wrt user space is AArch64 ILP32; the kernel also runs LP64 programs. Packages that have no ILP32
build (LuaJIT and its users, the QEMU host side) are taken from the official OpenWrt release of the same
version and rewritten so that both ABIs share one apk database without file conflicts:

- roots (the packages named on the command line) keep their name; shared libraries directly in lib/ or
  usr/lib/ move to usr/lib/lp64/, everything else (executables, init scripts, configs) stays in place.
  Scripts and conffiles lists are kept.
- dependencies with shared libraries in lib/ or usr/lib/ become <name>-lp64 with only their lib content,
  moved to usr/lib/lp64/ (relative paths kept).
- Lua modules (usr/lib/lua/...) become <name>-lp64 at their original paths, for the LP64 LuaJIT; they
  conflict with the ILP32 module of the same name (one ABI per Lua module).
- other plugins (e.g. usr/lib/ucode/*.so) belong to ILP32 interpreters: not converted, the ILP32Wrt
  package is used.
- dependencies without shared objects use the ILP32Wrt package of the same name; if ILP32Wrt does not
  ship it (names from $ILP32_NAMES, a TSV with the package name in column 2), it is copied unchanged.
- libc-lp64 also carries the LP64 loader link /lib/ld-musl-aarch64.so.1 -> /usr/lib/lp64/libc.so and
  the search path file /etc/ld-musl-aarch64.path ("/usr/lib/lp64"). The ILP32 loader is
  /lib/ld-musl-aarch64_ilp32.so.1 and does not search usr/lib/lp64.
  Note: usr/lib64 cannot be used, base-files makes it a symlink to lib.

Run under OpenWrt's fakeroot so file owners are root:
  fakeroot -- lp64-repack.py <apk> <out-dir> <work-dir> <ilp32 arch> <official arch> <official target> -- <root>...
  e.g. ... aarch64_cortex-a53_ilp32 aarch64_cortex-a53 mediatek/filogic -- dnsdist knot-resolver snort3
Output: <out-dir>/*.apk (unsigned; the repository index is signed) and repack-report.tsv.
"""
import os, re, shutil, subprocess, sys, urllib.request, pathlib

APK = sys.argv[1]; OUT = pathlib.Path(sys.argv[2]); WORK = pathlib.Path(sys.argv[3]); ARCH = sys.argv[4]
OARCH = sys.argv[5]; OTARGET = sys.argv[6]
sep = sys.argv.index("--"); ROOTS = sys.argv[sep + 1:]
FEEDS = ["base", "packages", "luci", "routing", "telephony", "video"]
ILP32 = set()
if os.environ.get('ILP32_NAMES'):
    for line in open(os.environ['ILP32_NAMES']):
        f = line.split('\t')
        if len(f) > 1: ILP32.add(f[1])
BASE = "https://downloads.openwrt.org/releases/25.12.6"
os.umask(0o022)
OUT.mkdir(parents=True, exist_ok=True); WORK.mkdir(parents=True, exist_ok=True)

def dump(path):
    return subprocess.run([APK, "adbdump", str(path)], capture_output=True, text=True).stdout

def parse_index(text):
    """packages list of an index adbdump -> dict name -> fields (lists for depends/provides)."""
    pk, cur, key = {}, None, None
    for line in text.splitlines():
        if line.startswith("  - name: "):
            cur = {"name": line[10:].strip(), "depends": [], "provides": []}; pk[cur["name"]] = cur; key = None
        elif cur is None:
            continue
        elif re.match(r"^    [a-z-]+: ", line) or re.match(r"^    [a-z-]+:$", line) or re.match(r"^    [a-z-]+: #", line):
            k, _, v = line.strip().partition(":"); v = v.strip()
            key = k if k in ("depends", "provides") else None
            if key is None and not v.startswith("#") and v != "|":
                cur[k] = v
        elif key and line.startswith("      - "):
            cur[key].append(line[8:].strip())
    return pk

def fetch_index(feed):
    u = (f"{BASE}/targets/{OTARGET}/packages/packages.adb" if feed == "target"
         else f"{BASE}/packages/{OARCH}/{feed}/packages.adb")
    dst = WORK / "index" / OARCH / f"{feed}.adb"
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True); urllib.request.urlretrieve(u, dst)
    return dst
index, feed_of = {}, {}
for feed in ["target"] + FEEDS:
    ix = fetch_index(feed)
    for n, p in parse_index(dump(ix)).items():
        index.setdefault(n, p); feed_of.setdefault(n, feed)
providers = {}
for n, p in index.items():
    for pr in p["provides"]:
        providers.setdefault(re.split(r"[<>=~]", pr)[0], n)

def dep_name(d):
    return re.split(r"[<>=~]", d.lstrip("!"))[0]

def resolve(name):
    return name if name in index else providers.get(name)

def url(n):
    p = index[n]; f = feed_of[n]
    fn = f"{n}-{p['version']}.apk"
    if f == "target":
        return f"{BASE}/targets/{OTARGET}/packages/{fn}"
    return f"{BASE}/packages/{OARCH}/{f}/{fn}"

def fetch(n):
    dst = WORK / "dl" / OARCH / f"{n}-{index[n]['version']}.apk"
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url(n), dst)
    return dst

def extract(apkf, n):
    d = WORK / "x" / OARCH / n
    shutil.rmtree(d, ignore_errors=True); d.mkdir(parents=True)
    subprocess.run([APK, "extract", "--allow-untrusted", "--destination", str(d), str(apkf)], check=True,
                   capture_output=True)
    return d

def kind_of(d):
    """lib: shared objects directly in lib/ or usr/lib/; lua: only Lua modules; plugin: other .so in
    subdirectories (modules of ILP32 interpreters such as ucode); none: no shared objects."""
    direct = lua = other = False
    for f in d.rglob("*"):
        if not re.search(r"\.so(\.|$)", f.name):
            continue
        rel = f.relative_to(d).parts
        if rel[:3] == ("lib", "apk", "packages"):
            continue
        if (len(rel) == 2 and rel[0] == "lib") or (len(rel) == 3 and rel[:2] == ("usr", "lib")):
            direct = True
        elif rel[:3] == ("usr", "lib", "lua"):
            lua = True
        else:
            other = True
    return "lib" if direct else "lua" if lua else "plugin" if other else "none"

# closure from the roots
cls, todo = {}, list(ROOTS)
for r in ROOTS:
    cls[r] = "app"
extracted = {}
while todo:
    n = todo.pop()
    rn = resolve(n)
    if rn is None:
        print("unresolved:", n); continue
    if rn != n:
        cls.setdefault(n, "alias"); n = rn
    if n not in extracted:
        extracted[n] = extract(fetch(n), n)
    if n not in cls:
        k = "lib" if n == "libc" else kind_of(extracted[n])
        cls[n] = {"lib": "lib", "lua": "lua"}.get(k, "skip")
        if cls[n] == "skip" and ILP32 and n not in ILP32 and k == "none":
            cls[n] = "data"
    if cls[n] == "skip":
        continue
    for d in index[n]["depends"]:
        dn = dep_name(d)
        if d.startswith("!") or not dn:
            continue
        if dn not in cls and resolve(dn) not in cls:
            todo.append(dn)

def newname(n):
    n = resolve(n) or n
    return n + "-lp64" if cls.get(n) in ("lib", "lua") else n

def scripts_of(apkf):
    """post-install etc. from the package adbdump (block scalars)."""
    out, cur, buf = {}, None, []
    text = dump(apkf); insc = False
    for line in text.splitlines():
        if line.startswith("scripts:"):
            insc = True; continue
        if insc:
            m = re.match(r"^  ([a-z-]+): \|", line)
            if m:
                if cur: out[cur] = "\n".join(buf) + "\n"
                cur, buf = m.group(1), []
            elif line.startswith("    ") and cur:
                buf.append(line[4:])
            elif not line.startswith(" "):
                break
    if cur: out[cur] = "\n".join(buf) + "\n"
    return out

report = []
for n, c in sorted(cls.items()):
    if c not in ("app", "lib", "lua", "data"):
        continue
    src = extracted[n]; p = index[n]
    new = newname(n)
    root = WORK / "r" / OARCH / new
    shutil.rmtree(root, ignore_errors=True); root.mkdir(parents=True)
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src)
        if rel.parts[:3] == ("lib", "apk", "packages") or f.is_dir() and not f.is_symlink():
            continue
        top = rel.parts[0]
        in_lib = rel.parts[:1] == ("lib",) or rel.parts[:2] == ("usr", "lib")
        if c == "data":
            dst = root / rel
        elif c == "lua":
            if rel.parts[:3] != ("usr", "lib", "lua"):
                continue
            dst = root / rel
        elif c == "lib":
            if not in_lib:
                continue
            sub = rel.parts[1:] if rel.parts[0] == "lib" else rel.parts[2:]
            dst = root / "usr/lib/lp64" / pathlib.Path(*sub)
        else:
            direct = in_lib and len(rel.parts) == (2 if rel.parts[0] == "lib" else 3) and re.search(r"\.so(\.|$)", f.name)
            dst = root / ("usr/lib/lp64/" + f.name if direct else str(rel))
        dst.parent.mkdir(parents=True, exist_ok=True)
        if f.is_symlink():
            os.symlink(os.readlink(f), dst)
        else:
            shutil.copy2(f, dst)
    if n == "libc":
        (root / "lib").mkdir(exist_ok=True)
        os.symlink("/usr/lib/lp64/libc.so", root / "lib/ld-musl-aarch64.so.1")
        (root / "etc").mkdir(exist_ok=True)
        (root / "etc/ld-musl-aarch64.path").write_text("/usr/lib/lp64\n")
    files = sorted("/" + str(x.relative_to(root)) for x in root.rglob("*") if not x.is_dir() or x.is_symlink())
    lp = root / "lib/apk/packages"; lp.mkdir(parents=True, exist_ok=True)
    (lp / f"{new}.list").write_text("\n".join(files) + "\n")
    # keep the configuration file lists (upgrades must not overwrite edited configs); apps only,
    # library packages carry no /etc files
    if c in ("app", "data"):
        for cf in (src / "lib/apk/packages").glob(f"{n}.conffiles*"):
            (lp / (new + cf.name[len(n):])).write_text(cf.read_text())
    deps = []
    for d in p["depends"]:
        dn = dep_name(d)
        if d.startswith("!") or not dn:
            continue
        deps.append(newname(dn))
    if c == "app" and "libc-lp64" not in deps:
        deps.append("libc-lp64")
    args = [APK, "mkpkg", "--info", f"name:{new}", "--info", f"version:{p['version']}",
            "--info", f"arch:{ARCH}", "--info", f"description:LP64 build of {n} (official OpenWrt 25.12.6 {OARCH}, repacked for ILP32Wrt)",
            "--info", f"license:{p.get('license', '')}", "--info", f"origin:{p.get('origin', '')}",
            "--info", "depends:" + " ".join(sorted(set(deps))), "--files", str(root),
            "--output", str(OUT / f"{new}-{p['version']}.apk")]
    if c in ("app", "data"):
        prov = [x for x in p["provides"]]
        if prov:
            args[2:2] = ["--info", "provides:" + " ".join(prov)]
        for t, body in scripts_of(fetch(n)).items():
            sf = WORK / "s" / f"{new}.{t}"; sf.parent.mkdir(parents=True, exist_ok=True); sf.write_text(body)
            args[2:2] = ["--script", f"{t}:{sf}"]
    r = subprocess.run(args, capture_output=True, text=True)
    report.append((new, c, p["version"], len(files), " ".join(sorted(set(deps))), r.returncode, r.stderr.strip()[:200]))

skipped = sorted(n for n, c in cls.items() if c == "skip")
with open(OUT / "repack-report.tsv", "w") as f:
    f.write("package\tclass\tversion\tfiles\tdepends\tmkpkg_rc\terror\n")
    for row in report:
        f.write("\t".join(str(x) for x in row) + "\n")
    f.write("# not converted (ILP32Wrt package of the same name is used): " + " ".join(skipped) + "\n")
for row in report:
    print("%-28s %-4s %-14s files=%-4d rc=%d deps: %s %s" % (row[0], row[1], row[2], row[3], row[5], row[4][:110], row[6]))
print("not converted:", " ".join(skipped))
