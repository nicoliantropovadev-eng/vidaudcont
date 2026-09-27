import multiprocessing
import sys

from vidaudcont.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # the analysis processes start as this same program
    sys.exit(main())
