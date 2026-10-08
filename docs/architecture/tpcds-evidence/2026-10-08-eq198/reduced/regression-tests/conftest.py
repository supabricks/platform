import os
import pytest
from pyspark.sql import SparkSession

@pytest.fixture(scope='session')
def spark():
    server=None
    if os.environ.get('EQ200_ENGINE')=='spark':
        session=(SparkSession.builder.master('local[2]').config('spark.ui.enabled','false')
                 .config('spark.driver.memory','1g').config('spark.sql.warehouse.dir','/reports/warehouse').getOrCreate())
    else:
        from pysail.spark import SparkConnectServer
        server=SparkConnectServer('127.0.0.1',0);server.start(background=True)
        session=SparkSession.builder.remote('sc://127.0.0.1:'+str(server.listening_address[1])).getOrCreate()
    try:yield session
    finally:
        session.stop()
        if server:server.stop()

def pytest_addoption(parser):
    parser.addoption('--correlated-source', choices=['delta', 'dataframe'], default='delta')
