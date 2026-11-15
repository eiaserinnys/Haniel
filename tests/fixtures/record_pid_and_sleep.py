"""Record this process id to a file, then sleep until killed.

Used by lifecycle lock tests to prove how many children were really spawned.
"""

import os
import sys
import time

with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(f"{os.getpid()}\n")
    handle.flush()

time.sleep(60)
