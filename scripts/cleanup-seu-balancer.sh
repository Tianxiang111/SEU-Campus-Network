#!/bin/sh
# Remove only the policies reserved for this package; see docs/scripts.md.
set -eu
if [ "$(id -u)" != "0" ]; then
    echo "Run on the router as root." >&2
    exit 1
fi
if [ -x /etc/init.d/seu-balancer ]; then
    /etc/init.d/seu-balancer disable
    /etc/init.d/seu-balancer stop
fi
exec /usr/bin/python3 /usr/sbin/seu-balancer --cleanup
