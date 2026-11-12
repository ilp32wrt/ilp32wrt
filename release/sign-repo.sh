#!/bin/sh
# Sign every packages.adb of a repository tree made by build-repo.sh with the ILP32Wrt apk key.
# Run where the private key is (only there); the public key in the images is release/keys/ilp32wrt-apk.pem.
# Usage: sign-repo.sh <out> <private key.pem> [<apk binary>]
set -eu
OUT=$1; KEY=$2; APK=${3:-apk}
[ -r "$KEY" ] || { echo "sign-repo: cannot read $KEY" >&2; exit 1; }
n=0
for f in $(find "$OUT/releases" -name packages.adb | sort); do
	"$APK" adbsign --allow-untrusted --sign-key "$KEY" "$f"
	n=$((n + 1))
done
echo "sign-repo: $n indexes signed"
