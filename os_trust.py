"""Verify TLS certificates with the operating system instead of Python's copy of the store.

On Windows, Python's ``ssl`` only reads the root certificates that are already in
the Windows store. Windows itself downloads a missing root the first time a
native program needs it, but Python never asks it to. When GitHub moved
api.github.com to a Sectigo ECC chain (Root E46, 2026-10-28) that this PC had
not fetched yet, every GitHub API call from ai-commit failed with
``CERTIFICATE_VERIFY_FAILED: unable to get issuer certificate`` while ``gh``
and browsers kept working. ``truststore`` routes verification through the OS,
which fetches the missing root itself.

Call ``enable()`` once at program start, before any HTTPS request.
"""

_enabled = False


def enable():
    """Inject truststore into ``ssl``. Returns True when active.

    A missing package is not fatal (the app still works wherever Python's own
    store is enough), so this never raises.
    """
    global _enabled
    if _enabled:
        return True
    try:
        import truststore
    except ImportError:
        return False
    truststore.inject_into_ssl()
    # urllib.request caches one global opener, and its HTTPSHandler builds an
    # SSL context once, when the opener is first created (Python 3.12+). Any
    # urlopen() before this call would keep using the old OpenSSL-only
    # context, so drop the cache and let the next urlopen() rebuild it.
    import urllib.request
    urllib.request._opener = None
    _enabled = True
    return True
