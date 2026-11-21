#!/bin/bash
# Build the ILP32Wrt package repository (unsigned) from the bin/ directories of release builds, in the layout
# the images expect (CONFIG_VERSION_REPO / CONFIG_VERSION_REPO_PACKAGES, include/feeds.mk):
#   <out>/releases/packages-<branch>/<arch>/<feed>/                      shared feeds (+ repacked LP64 packages
#                                                                        in the "packages" feed)
#   <out>/releases/<version>/targets/<target>/<subtarget>/packages/      target packages, no base-files, no kmods
#   <out>/releases/<version>/targets/<target>/<subtarget>/kmods/<kver>-<krel>-<vermagic>/   kmods
# Every directory gets an unsigned packages.adb; sign them with sign-repo.sh (the private key is not here).
#
# Usage: build-repo.sh <out> <version> <openwrt tree> <bin dir>...
#   openwrt tree: for staging_dir/host/bin/{apk,fakeroot}
#   bin dir:      the bin/ of one release build (several targets may share an arch; identical files are
#                 merged, a different file with the same name stops the script); targets:<bin dir> takes only
#                 its bin/targets (kernel, kmods, target packages): for the targets whose shared feed packages
#                 come from another build of the same package architecture (one aarch64_generic_ilp32 set)
# Environment: FILL_PACKAGES (default: fill-packages.txt next to this script, "none" = no fill step),
#              LP64_PACKAGES (default: lp64-packages.txt next to this script; empty file or "none" = no LP64
#              packages), LP64_WORK (work directory for lp64-repack.py, default <out>/../lp64-work)
set -eu
OUT=$1; VERSION=$2; TREE=$3; shift 3
HERE=$(cd "$(dirname "$0")" && pwd)
APK=$TREE/staging_dir/host/bin/apk; FAKEROOT=$TREE/staging_dir/host/bin/fakeroot
BRANCH=$(echo "$VERSION" | cut -d. -f1,2)
LP64_PACKAGES=${LP64_PACKAGES:-$HERE/lp64-packages.txt}
LP64_WORK=${LP64_WORK:-$(dirname "$OUT")/lp64-work}
SHARED=$OUT/releases/packages-$BRANCH
REL=$OUT/releases/$VERSION
mkdir -p "$SHARED" "$REL"

die() { echo "build-repo: $*" >&2; exit 1; }
# copy one apk, merging identical files from several builds
put() {
	local src=$1 dir=$2 dst=$2/$(basename "$1")
	mkdir -p "$dir"
	if [ -e "$dst" ]; then
		cmp -s "$src" "$dst" || die "different files with the same name: $src and $dst"
	else
		cp -p "$src" "$dst"
	fi
}

for bin in "$@"; do
	only_targets=
	case $bin in targets:*) bin=${bin#targets:}; only_targets=1 ;; esac
	[ -d "$bin/targets" ] && { [ -n "$only_targets" ] || [ -d "$bin/packages" ]; } || die "$bin is not a bin/ directory"
	# shared feeds: bin/packages/<arch>/<feed>/*.apk
	[ -n "$only_targets" ] || for d in "$bin"/packages/*/*/; do
		arch=$(basename "$(dirname "$d")"); feed=$(basename "$d")
		for f in "$d"*.apk; do [ -e "$f" ] && put "$f" "$SHARED/$arch/$feed"; done
	done
	# targets: bin/targets/<target>/<subtarget>-ilp32/packages/*.apk
	for d in "$bin"/targets/*/*/packages/; do
		[ -d "$d" ] || continue
		sub=$(basename "$(dirname "$d")"); target=$(basename "$(dirname "$(dirname "$d")")")
		sub=${sub%-ilp32}
		kernel=$(ls "$d" | grep -E '^kernel-[0-9]' | sort -V | tail -1 || true)
		[ -n "$kernel" ] || die "no kernel package in $d"
		# kernel-6.12.114~<vermagic>-r1.apk -> 6.12.114-1-<vermagic> (LINUX_VERSION-LINUX_RELEASE-LINUX_VERMAGIC)
		kv=$(echo "$kernel" | sed -E 's/^kernel-([0-9.]+)~([0-9a-f]+)-r([0-9]+)\.apk$/\1-\3-\2/')
		[ "$kv" != "$kernel" ] || die "cannot parse the kernel package name $kernel"
		for f in "$d"*.apk; do
			b=$(basename "$f")
			case $b in
			base-files-*) continue ;;                       # not in the feed: images only
			kmod-*) put "$f" "$REL/targets/$target/$sub/kmods/$kv" ;;
			*) put "$f" "$REL/targets/$target/$sub/packages" ;;
			esac
		done
	done
done

# fill: target packages that the targets built before the seeds selected them (FILL_PACKAGES, one name per line,
# optional class): copied into every target that lacks them, from a target that built them
#   (no class)  every target that built it has the same file as armsr; copied from armsr
#   any         the builds differ only in the build path (BTF, placeholder strings); copied from armsr
#   nand        the build depends on NAND_SUPPORT (FEATURES nand of the target): copied from armsr into the targets
#               without nand, from the nand targets (which must all have the same file) into the nand targets
FILL_PACKAGES=${FILL_PACKAGES:-$HERE/fill-packages.txt}
has_nand() { # <target> <subtarget>
	local mk=$TREE/target/linux/$1/Makefile smk=$TREE/target/linux/$1/$2/target.mk
	[ -e "$mk" ] || die "fill: no $mk"
	# a FEATURES:= in the subtarget replaces the target's list
	if [ -e "$smk" ] && grep -qE '^[[:space:]]*FEATURES[[:space:]]*:=' "$smk"; then mk=$smk; else mk="$mk $smk"; fi
	grep -hE '^[[:space:]]*FEATURES[[:space:]]*[:+]?=' $mk 2>/dev/null | grep -qw nand
}
pkg_in() { ls "$1/$2"-[0-9]*.apk 2>/dev/null | grep -E "/$2-[0-9][^-]*(-r[0-9]+)?\.apk\$" || true; }
if [ -s "$FILL_PACKAGES" ] && [ "$(cat "$FILL_PACKAGES")" != none ]; then
	ref=$REL/targets/armsr/armv8/packages
	[ -d "$ref" ] || die "fill: no armsr/armv8 target packages"
	tdirs=$(ls -d "$REL"/targets/*/*/packages)
	declare -A nand
	for d in $tdirs; do
		s=$(basename "$(dirname "$d")"); t=$(basename "$(dirname "$(dirname "$d")")")
		if has_nand "$t" "$s"; then nand[$d]=1; else nand[$d]=0; fi
		echo "build-repo: fill: $t/$s nand=${nand[$d]}"
	done
	nfill=0
	while read -r n class; do
		case $n in ''|'#'*) continue ;; esac
		a=$(pkg_in "$ref" "$n"); [ -n "$a" ] || die "fill: $n is not in armsr"
		[ "$(echo "$a" | wc -l)" = 1 ] || die "fill: several $n in armsr"
		missing= built_nand=
		for d in $tdirs; do
			f=$(pkg_in "$d" "$n")
			if [ -z "$f" ]; then missing="$missing $d"; continue; fi
			[ "$(echo "$f" | wc -l)" = 1 ] || die "fill: several $n in $d"
			case $class in
			any) ;;
			nand) if [ "${nand[$d]}" = 1 ]; then
					[ -z "$built_nand" ] && built_nand=$f
					cmp -s "$f" "$built_nand" || die "fill: $n differs between the nand targets ($f, $built_nand)"
				else
					cmp -s "$f" "$a" || die "fill: $n of $d differs from armsr"
				fi ;;
			'') cmp -s "$f" "$a" || die "fill: $n of $d differs from armsr" ;;
			*) die "fill: unknown class $class for $n" ;;
			esac
		done
		for d in $missing; do
			src=$a
			if [ "$class" = nand ] && [ "${nand[$d]}" = 1 ]; then
				[ -n "$built_nand" ] || die "fill: $n: no nand target built it"
				src=$built_nand
			fi
			put "$src" "$d"; nfill=$((nfill + 1))
		done
	done < "$FILL_PACKAGES"
	echo "build-repo: fill: $nfill target packages copied"
fi

# LP64 packages from the official release of the same upstream version, one run per package arch
UPSTREAM=${VERSION%%-ilp32*}
lp64_map() { # <ilp32 arch> -> "<official arch> <official target>"
	case $1 in
	aarch64_generic_ilp32) echo "aarch64_generic armsr/armv8" ;;
	aarch64_cortex-a53_ilp32) echo "aarch64_cortex-a53 mediatek/filogic" ;;
	aarch64_cortex-a72_ilp32) echo "aarch64_cortex-a72 bcm27xx/bcm2711" ;;
	aarch64_cortex-a76_ilp32) echo "aarch64_cortex-a76 bcm27xx/bcm2712" ;;
	*) echo "" ;;
	esac
}
# package name from an apk file name <name>-<version>.apk: a version has no "-" except an optional -r<n>,
# a name may have digits after a "-" (libpcre2-16), and many versions have no -r<n> (luci-*, ucode-mod-html)
roots=$( [ -s "$LP64_PACKAGES" ] && grep -vE '^\s*(#|$)' "$LP64_PACKAGES" | tr '\n' ' ' || true)
if [ -n "$roots" ] && [ "$roots" != "none " ]; then
	for ad in "$SHARED"/*/; do
		arch=$(basename "$ad"); m=$(lp64_map "$arch")
		[ -n "$m" ] || { echo "build-repo: no LP64 mapping for $arch, skipped"; continue; }
		set -- $m
		names=$LP64_WORK/$arch.names; mkdir -p "$LP64_WORK"
		find "$ad" "$REL"/targets -name '*.apk' -printf '%f\n' | sed -E 's/^(.*)-[0-9][^-]*(-r[0-9]+)?\.apk$/\1/; s/^/x\t/' > "$names"
		ILP32_NAMES=$names UPSTREAM_VERSION=$UPSTREAM "$FAKEROOT" -- python3 -I "$HERE/lp64-repack.py" "$APK" \
			"$LP64_WORK/out-$arch" "$LP64_WORK" "$arch" "$1" "$2" -- $roots > "$LP64_WORK/$arch.log" 2>&1 ||
			die "lp64-repack.py failed for $arch (see $LP64_WORK/$arch.log)"
		grep -E 'rc=[^0]' "$LP64_WORK/$arch.log" && die "mkpkg failed for $arch (see $LP64_WORK/$arch.log)"
		for f in "$LP64_WORK/out-$arch"/*.apk; do
			[ -e "$f" ] || continue
			n=$(basename "$f" | sed -E 's/^(.*)-[0-9][^-]*(-r[0-9]+)?\.apk$/\1/')
			ls "$SHARED/$arch"/*/"$n"-[0-9]*.apk >/dev/null 2>&1 && die "LP64 package $n is also an ILP32 package ($arch)"
			put "$f" "$SHARED/$arch/packages"
		done
		cp "$LP64_WORK/out-$arch/repack-report.tsv" "$LP64_WORK/repack-report-$arch.tsv"
		echo "build-repo: $arch: $(ls "$LP64_WORK/out-$arch"/*.apk | wc -l) LP64 packages"
	done
fi

# unsigned indexes
n=0
for d in $(find "$OUT/releases" -name '*.apk' -printf '%h\n' | sort -u); do
	"$APK" mkndx --allow-untrusted --output "$d/packages.adb" "$d"/*.apk >/dev/null || die "mkndx failed in $d"
	n=$((n + 1))
done
echo "build-repo: $n indexes (unsigned): $(find "$OUT/releases" -name '*.apk' | wc -l) apks"
find "$OUT/releases" -name packages.adb | sed "s|^$OUT/||" | sort > "$OUT/indexes.txt"
