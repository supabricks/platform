"""Installed regression for complete-transaction row-limited publication."""
from composite import CompositeChecks


class BulkChecks(CompositeChecks):
    def run(self,python,export):
        from cell import wait
        self.setup_source(python,export,'CREATE TABLE bulk_left(id integer PRIMARY KEY,value integer); '
                          'CREATE TABLE bulk_right(id integer PRIMARY KEY,value integer)')
        p=self.cli('sync','create','--branch','main','--mode','triggered','--strategy','incremental','--key','bulk')
        _,first=self.finished(self.trigger(p,'empty'))
        reader=self.opened(epoch=first['epoch_id'],ttl_ms=600000)
        # Capture continues, but no apply runs until the explicit second trigger.
        # Every 2,048-row transaction is valid; together they exceed 16,384 rows.
        with self.source() as db:
            for batch in range(12):
                lo=1+batch*1024;hi=lo+1023
                with db.transaction():
                    db.execute('INSERT INTO bulk_left SELECT i,i FROM generate_series(%s::integer,%s::integer) i',(lo,hi))
                    db.execute('INSERT INTO bulk_right SELECT i,2*i FROM generate_series(%s::integer,%s::integer) i',(lo,hi))
        run,current=self.finished(self.trigger(p,'drain'))
        assert run['batches']>=2,run
        self.equal(current,['bulk_left','bulk_right'])
        assert self.query(reader,'SELECT count(*) FROM public.bulk_left')['rows']==[['0']]
        latest=self.opened(epoch=current['epoch_id'],ttl_ms=600000)
        assert self.query(latest,'SELECT count(*),sum(l.value+r.value) FROM public.bulk_left l JOIN public.bulk_right r ON l.id=r.id')['rows']==[
            ['12288',str(3*12288*12289//2)]]
        self.close(latest);self.close(reader)
        self.metrics['complete_transaction_prefix']=dict(source_transactions=12,rows_per_transaction=2048,
                                                         changed_rows=24576,apply_batches=run['batches'])
        self.check('complete_transaction_row_prefix_drains_with_exact_equality_and_pinned_reader')
        self.sql(self.parent,'INSERT INTO bulk_left SELECT i,i FROM generate_series(1000000,1019999)i')
        rejected=self.trigger(p,'oversized')
        result=wait(lambda:(r if (r:=self.sync('run',id=rejected['id']))['state'] in ('failed','succeeded') else False),timeout=120)
        assert result['state']=='failed',result
        assert self.status(dict(id=run['capture_id']))['error']=='apply_row_budget'
        assert self.current()['epoch_id']==current['epoch_id']
        self.check('oversized_single_transaction_preserves_last_complete_publication')
        self.stop()
