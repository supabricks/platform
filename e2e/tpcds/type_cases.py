"""Native PostgreSQL input and dialect-shared queries for EQ01 type qualification."""
DATE_DDL="CREATE TABLE typed(id integer PRIMARY KEY,d date,amount decimal(18,2),note text)"
CHAR_DDL="CREATE TABLE typed(id integer PRIMARY KEY,c char(4),d char(8),s text,amount decimal(18,2))"
DATE_INSERT="""INSERT INTO typed VALUES(1,DATE '0001-01-01',1234567890123456.78,'minimum'),
 (2,DATE '9999-12-31',NULL,'maximum'),(3,NULL,0,'null'),(4,DATE '2000-02-29',1.25,'leap')"""
# chr() avoids different PostgreSQL/Spark SQL string escape rules.
CHAR_INSERT="""INSERT INTO typed VALUES(1,'x','x','x',1234567890123456.78),(2,'x ','x ','x ',NULL),
 (3,'','','',0),(4,NULL,NULL,NULL,1.25),(5,'🧱é','🧱é','🧱é',2.50),
 (6,concat('a',chr(9)),concat('a',chr(9)),concat('a',chr(9)),3.75)"""
DATE_UPDATE="UPDATE typed SET id=10,d=DATE '2024-02-29',amount=1234567890123456.79 WHERE id=1; UPDATE typed SET d=NULL WHERE id=4; DELETE FROM typed WHERE id=2; INSERT INTO typed VALUES(5,DATE '1900-03-01',2.50,'new')"
CHAR_UPDATE="UPDATE typed SET id=10,c='y',d='y',amount=1234567890123456.79 WHERE id=1; UPDATE typed SET c=NULL WHERE id=5; DELETE FROM typed WHERE id=2; INSERT INTO typed VALUES(7,'xy  ','xy','xy  ',2.50)"
# Independent JVM Spark reconstructs the post-transaction state via INSERT,
# because the reference's Parquet datasource does not implement UPDATE/DELETE.
DATE_UPDATED="""INSERT INTO typed VALUES(10,DATE '2024-02-29',1234567890123456.79,'minimum'),
 (3,NULL,0,'null'),(4,NULL,1.25,'leap'),(5,DATE '1900-03-01',2.50,'new')"""
CHAR_UPDATED="""INSERT INTO typed VALUES(10,'y','y','x',1234567890123456.79),(3,'','','',0),
 (4,NULL,NULL,NULL,1.25),(5,NULL,'🧱é','🧱é',2.50),
 (6,concat('a',chr(9)),concat('a',chr(9)),concat('a',chr(9)),3.75),(7,'xy  ','xy','xy  ',2.50)"""
DATE_QUERIES=["SELECT id,d,amount FROM public.typed ORDER BY id",
 "SELECT id,year(d),month(d),day(d) FROM public.typed ORDER BY id",
 "SELECT id FROM public.typed WHERE d BETWEEN DATE '1900-01-01' AND DATE '2024-12-31' ORDER BY id",
 "SELECT count(d),min(d),max(d) FROM public.typed"]
CHAR_QUERIES=["SELECT id,c,d,s,amount FROM public.typed ORDER BY id",
 "SELECT id,c='x',c='x ',c=d,c=s,c LIKE 'x',length(c),CAST(c AS STRING)='x' FROM public.typed ORDER BY id",
 "SELECT id,c IN ('x','y',NULL),c NOT IN ('x','y',NULL),c BETWEEN 'x' AND 'y',c < 'x\\n' FROM public.typed ORDER BY id",
 "SELECT a.id,b.id FROM public.typed a JOIN public.typed b ON a.c=b.d ORDER BY a.id,b.id",
 "SELECT id FROM public.typed WHERE c IN (SELECT d FROM public.typed) ORDER BY id",
 "SELECT c,count(*) FROM public.typed GROUP BY c ORDER BY c",
 "SELECT id,c=concat('x',''),concat(c,'')='x',c IN (concat('x',''),NULL) FROM public.typed ORDER BY id",
 "SELECT id,x='x' FROM (SELECT id,c AS x FROM public.typed) ORDER BY id",
 "SELECT id,x='x' FROM (SELECT id,CAST(c AS STRING) AS x FROM public.typed) ORDER BY id",
 "SELECT id,c='🧱é',c='x     ',c <=> d,c IS NULL,c IN ('x','y'),c IN (concat('x',''),'y') FROM public.typed ORDER BY id"]

def cases(kind):
    prefix=kind.upper();return {k:globals()[prefix+'_'+k.upper()] for k in ('ddl','insert','update','updated','queries')}
