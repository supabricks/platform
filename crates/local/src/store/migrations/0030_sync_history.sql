-- Retention and durable retry lookups must not rescan every receipt per run.
CREATE INDEX incremental_receipt_run ON incremental_requests(json_extract(response,'$.id'));
CREATE INDEX incremental_receipt_owner ON incremental_requests(json_extract(request,'$.owner'));
CREATE INDEX sync_receipt_run ON sync_requests(json_extract(response,'$.id'));
CREATE INDEX sync_request_run ON sync_requests(json_extract(request,'$.id'));
