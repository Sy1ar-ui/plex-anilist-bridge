"""Docker HEALTHCHECK probe.

Uses the interpreter that is already in the image rather than pulling in curl,
which keeps the image smaller and one fewer thing pinned.
"""
import os
import sys
import urllib.error
import urllib.request

url = f"http://127.0.0.1:{os.environ.get('PORT', '8787')}/health"

try:
    with urllib.request.urlopen(url, timeout=4) as response:  # noqa: S310 - fixed localhost URL
        sys.exit(0 if response.status == 200 else 1)
except (urllib.error.URLError, OSError) as exc:
    print(f"health check failed: {exc}", file=sys.stderr)
    sys.exit(1)
