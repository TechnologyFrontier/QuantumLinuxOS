#!/bin/bash
# Build the QuantumOS root filesystem and the per-slot boot files.
#
#   sudo image/build-rootfs.sh [OUTDIR]
#
# Produces in OUTDIR (default: image/out):
#   rootfs/      the root tree (kept for inspection)
#   root.img     ext4 image of the read-only root (one slot)
#   vmlinuz      kernel
#   initrd.img   initramfs with the quantumos-state hook
#
# Requires: root, debootstrap, e2fsprogs (mkfs.ext4 -d), network access to
# the Debian mirror.  NOTE: build and boot are not yet evaluated (QOS-BD 4.2).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# shellcheck source=config.env
. "$HERE/config.env"
OUT="${1:-$HERE/out}"
ROOT="$OUT/rootfs"

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
for t in debootstrap mkfs.ext4 chroot; do
    command -v "$t" >/dev/null || { echo "missing tool: $t" >&2; exit 1; }
done

mkdir -p "$OUT"
rm -rf "$ROOT"

PKG_FILE="${QOS_PACKAGES_FILE:-$HERE/packages.txt}"
PKGS="$(grep -v '^\s*#' "$PKG_FILE" | grep -v '^\s*$' | paste -sd, -)"
echo ">> debootstrap $DEBIAN_SUITE"
debootstrap --variant=minbase ${DEBOOTSTRAP_OPTS:-} --include="$PKGS" \
    "$DEBIAN_SUITE" "$ROOT" "$DEBIAN_MIRROR"

echo ">> installing QuantumOS components"
PYDIR="$ROOT/usr/lib/python3/dist-packages"
rm -rf "$PYDIR/quantumos"
cp -a "$REPO/src/quantumos" "$PYDIR/quantumos"
find "$PYDIR/quantumos" -name '__pycache__' -prune -exec rm -rf {} +
rm -f "$PYDIR/quantumos/admin/bundletool.py"   # distributor-only tool
for pair in qosctl:quantumos.cli qosd:quantumos.service qos-admin:quantumos.admin.cli; do
    name="${pair%%:*}"; mod="${pair#*:}"
    cat > "$ROOT/usr/bin/$name" <<PY
#!/usr/bin/python3 -I
import sys
from $mod import main
sys.exit(main())
PY
    chmod 0755 "$ROOT/usr/bin/$name"
done
# administrator-only tool
chmod 0750 "$ROOT/usr/bin/qos-admin"

install -D -m0644 "$REPO/system/systemd/qosd.socket"            "$ROOT/usr/lib/systemd/system/qosd.socket"
install -D -m0644 "$REPO/system/systemd/qosd.service"           "$ROOT/usr/lib/systemd/system/qosd.service"
install -D -m0644 "$REPO/system/systemd/qos-boot-assess.service" "$ROOT/usr/lib/systemd/system/qos-boot-assess.service"
install -D -m0644 "$REPO/system/sysusers.d/quantumos.conf"      "$ROOT/usr/lib/sysusers.d/quantumos.conf"
install -D -m0644 "$REPO/system/tmpfiles.d/quantumos.conf"      "$ROOT/usr/lib/tmpfiles.d/quantumos.conf"
install -D -m0644 "$REPO/system/config/qosd.conf"               "$ROOT/etc/quantumos/qosd.conf"
install -D -m0644 "$REPO/system/config/update.conf"             "$ROOT/usr/lib/quantumos/update.conf"
install -D -m0644 "$REPO/README.md"                             "$ROOT/usr/share/doc/quantumos/README.md"

if [ -n "${QOS_UPDATE_PUBKEY:-}" ]; then
    install -D -m0644 "$QOS_UPDATE_PUBKEY" "$ROOT/usr/share/quantumos/keys/update-ed25519.pub"
else
    echo "WARNING: QOS_UPDATE_PUBKEY not set; image will reject every update bundle" >&2
fi

cp -a "$HERE/rootfs-overlay/." "$ROOT/"
install -D -m0755 "$HERE/initramfs/quantumos-state" "$ROOT/etc/initramfs-tools/scripts/local-bottom/quantumos-state"
install -D -m0755 "$HERE/initramfs/quantumos-hook"  "$ROOT/etc/initramfs-tools/hooks/quantumos"

cat >> "$ROOT/usr/lib/os-release" <<OSR
QUANTUMOS_VERSION="$QOS_VERSION"
OSR
echo "quantumos" > "$ROOT/etc/hostname"
: > "$ROOT/etc/machine-id"   # generated on first boot, kept in the /etc overlay

# Writable locations live on the state partition (/var)
rm -rf "$ROOT/home" "$ROOT/root"
ln -s var/home "$ROOT/home"
ln -s var/roothome "$ROOT/root"
mkdir -p "$ROOT/efi"

echo ">> configuring services"
mount --bind /dev "$ROOT/dev"; mount -t proc proc "$ROOT/proc"; mount -t sysfs sys "$ROOT/sys"
trap 'umount -l "$ROOT/dev" "$ROOT/proc" "$ROOT/sys" 2>/dev/null || true' EXIT
chroot "$ROOT" systemd-sysusers
chroot "$ROOT" systemctl enable qosd.socket qos-boot-assess.service getty@tty1.service serial-getty@ttyS0.service
chroot "$ROOT" systemctl disable apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
# Local administrator: root password hash from the build environment
# (e.g. QOS_ROOT_PASSWORD_HASH="$(openssl passwd -6)").  Without it root is
# locked and the image cannot be administered from the console.
if [ -n "${QOS_ROOT_PASSWORD_HASH:-}" ]; then
    chroot "$ROOT" usermod -p "$QOS_ROOT_PASSWORD_HASH" root
else
    echo "WARNING: QOS_ROOT_PASSWORD_HASH not set; root account is locked" >&2
    chroot "$ROOT" passwd -l root
fi
# the image carries no network services; quantum service never listens on IP
chroot "$ROOT" update-initramfs -u -k all
umount -l "$ROOT/dev" "$ROOT/proc" "$ROOT/sys"; trap - EXIT

KVER="$(ls "$ROOT/boot" | sed -n 's/^vmlinuz-//p' | sort -V | tail -1)"
cp "$ROOT/boot/vmlinuz-$KVER" "$OUT/vmlinuz"
cp "$ROOT/boot/initrd.img-$KVER" "$OUT/initrd.img"
rm -rf "$ROOT/var/cache/apt/archives/"*.deb "$ROOT/var/lib/apt/lists/"*

# /var content of the image is the seed for a fresh state partition
rm -rf "$OUT/var-seed"; mv "$ROOT/var" "$OUT/var-seed"; mkdir "$ROOT/var"

echo ">> creating root.img (${ROOT_IMAGE_SIZE_MIB} MiB ext4)"
rm -f "$OUT/root.img"
mkfs.ext4 -q -L "qos-root" -O ^has_journal -d "$ROOT" "$OUT/root.img" "${ROOT_IMAGE_SIZE_MIB}M"
sha256sum "$OUT/root.img" "$OUT/vmlinuz" "$OUT/initrd.img"
echo ">> done: $OUT"
