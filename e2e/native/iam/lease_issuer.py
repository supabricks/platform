"""Trusted disposable admission-daemon stand-in; only it renews the external lease."""
from pathlib import Path
import time
root=Path('/work')
while True:
    temporary=root/'renewal.tmp'
    temporary.write_text(str(time.monotonic()+2))
    temporary.replace(root/'renewal')
    time.sleep(.1)
