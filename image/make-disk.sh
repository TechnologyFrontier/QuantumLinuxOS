#!/bin/bash
# Assemble a bootable raw disk image (or write a dedicated disk) from the
# output of build-rootfs.sh.
#
#   sudo image/make-disk.sh OUTDIR TARGET
#
# TARGET is a file (created with DISK_SIZE_MIB) or a whole block device.
# EVERYTHING ON TARGET IS DESTROYED.  Layout (QOS-BD 3章):
#   1 ESP         vfat  systemd-boot, /quantumos/{a,b}/{vmlinuz,initrd.img}
#   2 qos-root-a  read-only root slot A (initial, confirmed)
#   3 qos-root-b  read-only root slot B (empty until the first update)
#   4 qos-state   ext4  /var: homes, /etc overlay, update state
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/config.env"
OUT="${1:?OUTDIR}"; TARGET="${2:?TARGET}"
[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
for t in sfdisk losetup mkfs.vfat mkfs.ext4 bootctl; do
    command -v "$t" >/dev/null || { echo "missing tool: $t" >&2; exit 1; }
done

if [ ! -b "$TARGET" ]; then
    rm -f "$TARGET"; truncate -s "${DISK_SIZE_MIB}M" "$TARGET"
    DEV="$(losetup --find --show --partscan "$TARGET")"
    trap 'losetup -d "$DEV"' EXIT
    P="${DEV}p"
else
    DEV="$TARGET"; case "$DEV" in *[0-9]) P="${DEV}p" ;; *) P="$DEV" ;; esac
fi

sfdisk --wipe always "$DEV" <<SF
label: gpt
size=${ESP_SIZE_MIB}MiB,       type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name=ESP
size=${ROOT_SLOT_SIZE_MIB}MiB, type=4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709, name=qos-root-a
size=${ROOT_SLOT_SIZE_MIB}MiB, type=4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709, name=qos-root-b
                               type=4D21B016-B534-45C2-A9FB-5C16E091FD2D, name=qos-state
SF
partprobe "$DEV" 2>/dev/null || true; udevadm settle 2>/dev/null || true

mkfs.vfat -F 32 -n ESP "${P}1"
dd if="$OUT/root.img" of="${P}2" bs=4M conv=fsync status=progress
mkfs.ext4 -q -L qos-state "${P}4"

MNT="$(mktemp -d)"
mount "${P}4" "$MNT"
cp -a "$OUT/var-seed/." "$MNT/"
install -d -m0700 "$MNT/lib/quantumos/update"
umount "$MNT"

mount "${P}1" "$MNT"
bootctl --esp-path="$MNT" --no-variables install
install -D -m0644 "$OUT/vmlinuz"    "$MNT/quantumos/a/vmlinuz"
install -D -m0644 "$OUT/initrd.img" "$MNT/quantumos/a/initrd.img"
mkdir -p "$MNT/loader/entries"
cat > "$MNT/loader/entries/quantumos-a.conf" <<ENT
title QuantumOS $QOS_VERSION (slot a)
version $QOS_VERSION
linux /quantumos/a/vmlinuz
initrd /quantumos/a/initrd.img
options root=PARTLABEL=qos-root-a ro quantumos.slot=a $KERNEL_CMDLINE_EXTRA
ENT
cat > "$MNT/loader/loader.conf" <<LDR
default quantumos-a.conf
timeout 3
editor no
LDR
umount "$MNT"; rmdir "$MNT"
sync
echo ">> disk ready: $TARGET"
