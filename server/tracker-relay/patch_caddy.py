#!/usr/bin/env python3
"""Add a /tracker-ingest reverse-proxy route to the sub2api Caddy site block.

Idempotent: re-running is a no-op once the handle block exists.
Writes a timestamped backup next to the Caddyfile before the first change.
"""

import os
import shutil
import sys
import time

PATH = os.environ.get("CADDYFILE_PATH", "/path/to/caddy/Caddyfile")

OLD = (
    "\treverse_proxy 127.0.0.1:8080 {\n"
    "\t\theader_up X-Real-IP {remote_host}\n"
    "\t\theader_up X-Forwarded-Proto {scheme}\n"
    "\t\tflush_interval -1\n"
    "\t}\n"
)

NEW = (
    "\thandle /tracker-ingest/* {\n"
    "\t\turi strip_prefix /tracker-ingest\n"
    "\t\treverse_proxy 127.0.0.1:8099\n"
    "\t}\n"
    "\n"
    "\thandle {\n"
    "\t\treverse_proxy 127.0.0.1:8080 {\n"
    "\t\t\theader_up X-Real-IP {remote_host}\n"
    "\t\t\theader_up X-Forwarded-Proto {scheme}\n"
    "\t\t\tflush_interval -1\n"
    "\t\t}\n"
    "\t}\n"
)


def main() -> int:
    with open(PATH, encoding="utf-8") as fh:
        text = fh.read()

    if "handle /tracker-ingest/*" in text:
        print("already patched; nothing to do")
        return 0

    count = text.count(OLD)
    if count != 1:
        print("expected exactly 1 upstream block, found %d; aborting" % count)
        return 1

    backup = "%s.bak.tracker-%s" % (PATH, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(PATH, backup)
    with open(PATH, "w", encoding="utf-8") as fh:
        fh.write(text.replace(OLD, NEW))

    print("patched %s (backup: %s)" % (PATH, backup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
