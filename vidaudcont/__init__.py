"""VidAudCont: finds and losslessly cuts non-conversation parts of recordings."""
import os
import sys

__version__ = "1.8.5"

# torch and CTranslate2 each bundle Intel's OpenMP runtime (Windows, Intel Macs); loading the
# second copy aborts the process unless this is set before either library is imported
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

# macOS keeps its root certificates in the Keychain, which the OpenSSL of the bundled Python does not read: without
# certifi's list every HTTPS request (the table, YouTube titles) fails with CERTIFICATE_VERIFY_FAILED
if sys.platform == "darwin" and not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass
