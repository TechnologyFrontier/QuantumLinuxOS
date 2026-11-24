#!/bin/bash
# Desktop edition steps of build-rootfs.sh (QOS-DT DT-01).  Sourced with
# ROOT, REPO, HERE and EDITION_DIR set; /dev, /proc and /sys are bind mounted.
set -euo pipefail

echo ">> desktop: QuantumOS desktop apps"
for pair in qos-studio:quantumos.desktop.studio_app \
            qos-update-manager:quantumos.desktop.update_app \
            qos-devenv-setup:quantumos.desktop.devenv_app; do
    name="${pair%%:*}"; mod="${pair#*:}"
    cat > "$ROOT/usr/bin/$name" <<PY
#!/usr/bin/python3 -I
import sys
from $mod import main
sys.exit(main())
PY
    chmod 0755 "$ROOT/usr/bin/$name"
done
install -d -m0755 "$ROOT/usr/libexec/quantumos"
cat > "$ROOT/usr/libexec/quantumos/qos-update-helper" <<'PY'
#!/usr/bin/python3 -I
# Runs as root through pkexec (polkit actions org.quantumos.update.*).
import sys
from quantumos.desktop.update_model import helper_main
sys.exit(helper_main(sys.argv[1:]))
PY
chmod 0755 "$ROOT/usr/libexec/quantumos/qos-update-helper"

for f in "$REPO"/system/desktop/applications/*.desktop; do
    install -D -m0644 "$f" "$ROOT/usr/share/applications/$(basename "$f")"
done
for f in "$REPO"/system/desktop/icons/*.svg; do
    install -D -m0644 "$f" "$ROOT/usr/share/icons/hicolor/scalable/apps/$(basename "$f")"
done
install -D -m0644 "$REPO/system/desktop/polkit/org.quantumos.update.policy" \
    "$ROOT/usr/share/polkit-1/actions/org.quantumos.update.policy"
install -D -m0644 "$REPO/system/desktop/polkit/50-quantumos-update.rules" \
    "$ROOT/usr/share/polkit-1/rules.d/50-quantumos-update.rules"
install -D -m0644 "$EDITION_DIR/update.conf" "$ROOT/usr/lib/quantumos/update.conf"
install -D -m0644 "$EDITION_DIR/90_quantumos.gschema.override" \
    "$ROOT/usr/share/glib-2.0/schemas/90_quantumos.gschema.override"
chroot "$ROOT" glib-compile-schemas /usr/share/glib-2.0/schemas
chroot "$ROOT" gtk-update-icon-cache -q -f /usr/share/icons/hicolor 2>/dev/null || true
chroot "$ROOT" update-desktop-database -q /usr/share/applications 2>/dev/null || true

# The IDE plugin is optional: QOS_IDE_PLUGIN_ZIP=build/distributions/...zip
if [ -n "${QOS_IDE_PLUGIN_ZIP:-}" ]; then
    install -D -m0644 "$QOS_IDE_PLUGIN_ZIP" "$ROOT/usr/share/quantumos/ide/quantum-os-ide-plugin.zip"
else
    echo "NOTE: QOS_IDE_PLUGIN_ZIP not set; the IDE setup installs the IDE without the plugin" >&2
fi

# Product name shown by GNOME (initial setup, Settings → About); ID stays
# "debian" so tools keep treating the base system as Debian.
sed -i -e "s/^PRETTY_NAME=.*/PRETTY_NAME=\"QuantumOS ${QOS_VERSION} Desktop (Debian 13 base)\"/" \
       -e 's/^NAME=.*/NAME="QuantumOS"/' "$ROOT/usr/lib/os-release"

echo ">> desktop: users, locale, services"
# Users created by GNOME (gnome-initial-setup / Settings → accountsservice →
# adduser) and by adduser join the quantum group (QOS-DT 2.3).
sed -i -e 's/^#\?EXTRA_GROUPS=.*/EXTRA_GROUPS="users quantum"/' \
       -e 's/^#\?ADD_EXTRA_GROUPS=.*/ADD_EXTRA_GROUPS=1/' "$ROOT/etc/adduser.conf"
grep -q '^EXTRA_GROUPS="users quantum"' "$ROOT/etc/adduser.conf"
grep -q '^ADD_EXTRA_GROUPS=1' "$ROOT/etc/adduser.conf"

sed -i -e 's/^# *\(ja_JP.UTF-8 UTF-8\)/\1/' -e 's/^# *\(en_US.UTF-8 UTF-8\)/\1/' "$ROOT/etc/locale.gen"
chroot "$ROOT" locale-gen
echo 'LANG=ja_JP.UTF-8' > "$ROOT/etc/default/locale"
echo 'LANG=ja_JP.UTF-8' > "$ROOT/etc/locale.conf"

# OS packages cannot change on the read-only root; GNOME Software manages
# Flatpak applications only.
chroot "$ROOT" systemctl mask packagekit.service packagekit-offline-update.service
chroot "$ROOT" systemctl enable gdm.service NetworkManager.service
chroot "$ROOT" systemctl set-default graphical.target
