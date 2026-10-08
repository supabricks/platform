"""EQ02 real-Parquet pruning and exact lookup with or without statistics."""
from pathlib import Path
import random
import tempfile
import unittest

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from incremental_worker import key_batches, key_filter


class KeyLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'rows.parquet'

    def dataset(self,rows,statistics=True):
        pq.write_table(pa.Table.from_pylist(rows),self.path,
                       row_group_size=1024,write_statistics=statistics)
        return ds.dataset(self.path,format='parquet')

    def selected(self,dataset,columns,keys):
        batches=list(key_batches(dataset,columns,keys))
        self.assertTrue(all(b.num_rows<=32 for b in batches))
        return [row for batch in batches for row in batch.to_pylist()]

    def test_large_nonmatching_range_prunes_real_row_groups(self):
        columns=[[1,'id',20,-1]]
        dataset=self.dataset([dict(id=i) for i in range(65536)])
        for keys in (set(range(65536,81920)),set(range(-16384,0))):
            with self.subTest(first=min(keys)):
                predicate=key_filter(columns,keys)
                fragments=list(dataset.get_fragments())
                self.assertEqual(sum(len(f.row_groups) for f in fragments),64)
                self.assertEqual(sum(len(f.split_by_row_group(filter=predicate)) for f in fragments),0)
                self.assertEqual(self.selected(dataset,columns,keys),[])

    def test_sparse_unsorted_keys_extremes_and_missing_statistics(self):
        columns=[[1,'id',20,-1],[0,'payload',25,-1]]
        values=[-2**63,*range(-2048,2049),2**63-1]
        random.Random(182).shuffle(values)
        rows=[dict(id=i,payload=f'value:{i}') for i in values]
        keys={-2**63,-2048,-17,0,19,2048,2**63-1,9000}
        for statistics in (False,True):
            with self.subTest(statistics=statistics):
                dataset=self.dataset(rows,statistics)
                self.assertEqual(self.selected(dataset,columns,keys),[r for r in rows if r['id'] in keys])
                self.assertEqual(self.selected(dataset,columns,set()),[])

    def test_composite_bounds_preserve_exact_identity_without_statistics(self):
        columns=[[1,'a',20,-1],[0,'payload',25,-1],[1,'b',21,-1]]
        rows=[dict(a=a,b=b,payload='x'*1024) for a in range(128) for b in range(128)]
        rows.extend([dict(a=-2**63,b=-32768,payload='low'),dict(a=2**63-1,b=32767,payload='high')])
        random.Random(182).shuffle(rows)
        keys={(i,i) for i in range(128)}|{(-2**63,-32768),(2**63-1,32767)}
        for statistics in (False,True):
            with self.subTest(statistics=statistics):
                dataset=self.dataset(rows,statistics)
                self.assertEqual(self.selected(dataset,columns,keys),[r for r in rows if (r['a'],r['b']) in keys])


if __name__=='__main__':unittest.main()
