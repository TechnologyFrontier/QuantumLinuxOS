#!/bin/bash
# Create a signed offline update bundle from build-rootfs.sh output.
#   image/make-bundle.sh OUTDIR PRIVATE_KEY VERSION BUNDLE
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/config.env"
OUT="${1:?OUTDIR}"; KEY="${2:?PRIVATE_KEY}"; VER="${3:?VERSION}"; BUNDLE="${4:?BUNDLE}"
"$HERE/../bin/qos-bundle" create --key "$KEY" --version "$VER" \
    --product-id "$QOS_PRODUCT_ID" --architecture "$QOS_ARCH" \
    --root "$OUT/root.img" --kernel "$OUT/vmlinuz" --initrd "$OUT/initrd.img" "$BUNDLE"
