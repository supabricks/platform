import sys,json,resource,time
sys.path.insert(0,'e2e/native/performance')
from packed_observer import PackedSamples,PackedMarkers
packed=sys.argv[1]=='packed'
samples=PackedSamples() if packed else []
ends=PackedMarkers('Q') if packed else {};captured=PackedMarkers('d') if packed else {}
start=time.perf_counter();before=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
for i in range(600000):
 xid=i+1000
 samples.append(dict(xid=xid,ack_ms=1791250000000.+i*1.6,latency_ms=11.+i%9,late_ms=.1*i))
 ends[xid]=31481408+i*123;captured[xid]=1791250000000.+i*1.6+100
print(json.dumps(dict(mode=sys.argv[1],transactions=len(samples),rss_before_kib=before,rss_after_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,seconds=time.perf_counter()-start)))
