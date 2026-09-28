"""VidAudCont: finds and losslessly cuts non-conversation parts of recordings."""
import os

__version__ = "1.3.3"

# torch and CTranslate2 each bundle Intel's OpenMP runtime (Windows, Intel Macs); loading the
# second copy aborts the process unless this is set before either library is imported
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
