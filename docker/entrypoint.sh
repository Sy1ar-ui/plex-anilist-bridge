#!/bin/sh
# Drop to the unprivileged uid/gid Unraid expects (99:100 by default) after
# making /config writable by it.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-022}"
CONFIG_DIR="${CONFIG_DIR:-/config}"

umask "${UMASK}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "${CONFIG_DIR}"
    # Only touch ownership when it is actually wrong: on a large appdata
    # directory a blanket recursive chown on every start is slow for no gain.
    current_owner="$(stat -c '%u:%g' "${CONFIG_DIR}" 2>/dev/null || echo '')"
    if [ "${current_owner}" != "${PUID}:${PGID}" ]; then
        echo "entrypoint: taking ownership of ${CONFIG_DIR} as ${PUID}:${PGID}" >&2
        chown -R "${PUID}:${PGID}" "${CONFIG_DIR}" || \
            echo "entrypoint: could not chown ${CONFIG_DIR}; continuing" >&2
    fi

    # --clear-groups rather than --init-groups: PUID 99 has no passwd entry in
    # this image, which is fine because nothing here needs one.
    exec setpriv --reuid "${PUID}" --regid "${PGID}" --clear-groups "$@"
fi

# Already unprivileged (for example `docker run --user`); just run.
exec "$@"
