#!/usr/bin/env python3
"""Boundary regressions for native analytical packaging."""
import unittest
from analytics import macho_dependencies


class MachOLoads(unittest.TestCase):
    def test_library_identity_and_universal_headers_are_not_dependencies(self):
        # psycopg wheels retain an absolute /DLC identity after their actual
        # load commands have been repaired to use adjacent bundled libraries.
        listing = '''/private/release/lib.so (architecture arm64):
Load command 0
          cmd LC_ID_DYLIB
      cmdsize 80
         name /DLC/psycopg_binary/.dylibs/libcom_err.3.0.dylib (offset 24)
Load command 1
          cmd LC_LOAD_DYLIB
         name @loader_path/libkrb5.dylib (offset 24)
Load command 2
          cmd LC_RPATH
         path @loader_path/../lib (offset 12)
/private/release/lib.so (architecture x86_64):
Load command 0
          cmd LC_LOAD_WEAK_DYLIB
         name /opt/homebrew/lib/unbundled.dylib (offset 24)
'''
        self.assertEqual(list(macho_dependencies(listing)), [
            '@loader_path/libkrb5.dylib', '@loader_path/../lib', '/opt/homebrew/lib/unbundled.dylib'])


if __name__ == '__main__':
    unittest.main()
