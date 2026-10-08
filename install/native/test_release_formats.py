from pathlib import Path
import re
import unittest
from release_formats import LOCAL_CATALOG


class ReleaseFormats(unittest.TestCase):
    def test_catalog_format_matches_compiled_migrations(self):
        root=Path(__file__).resolve().parents[2]
        source=(root/'crates/local/src/store/migrations.rs').read_text()
        migrations=re.findall(r'include_str!\("migrations/(\d+)_.*?\.sql"\)',source)
        self.assertEqual([int(n) for n in migrations],list(range(1,LOCAL_CATALOG+1)))


if __name__=='__main__':unittest.main()
