"""Run the existing bounded/fenced IPC cases against the RocksDB owner."""
import unittest
from unittest.mock import patch
import test_journal_owner as original
from rocks_journal import RocksJournal


class RocksOwnerTests(unittest.TestCase):
    request=original.OwnerTests.request
    connect=original.OwnerTests.connect
    def setUp(self):
        factory=patch('capture.sqlite_journal.SQLiteJournal',RocksJournal);factory.start();self.addCleanup(factory.stop)
        original.OwnerTests.setUp(self)
    def tearDown(self):original.OwnerTests.tearDown(self)
    def assert_unpinned(self):
        self.assertFalse(self.spool.backend.pinned)
        self.assertTrue(self.spool.backend.reader.acquire(blocking=False))
        self.spool.backend.reader.release()


# Reuse transport tests verbatim. SQL format inspection and the legacy SQLite
# subprocess bootstrap are covered separately by RocksDB replay/crash tests.
for name in (
    'test_exact_request_and_project_generation_authority_fences',
    'test_atomic_replacement_retries_current_authority_and_fences_revocations',
    'test_replacement_churn_after_payload_defers_without_completion',
    'test_replacement_churn_before_snapshot_is_retryable_and_owner_recovers',
    'test_startup_retries_atomic_control_replacement',
    'test_current_control_revocation_and_retry_replacement_fence_before_response',
    'test_oversize_request_rejected_before_body_and_owner_remains_usable',
    'test_missing_owner_defers_without_direct_database_fallback',
    'test_disconnect_cancels_snapshot_and_releases_lease',
    'test_slow_reader_holds_no_snapshot_and_shutdown_is_bounded',
    'test_bounded_materialization_stops_at_record_and_payload_limit',
    'test_expired_queued_request_never_opens_snapshot',
    'test_queue_pressure_shares_deadline_and_expired_readers_open_no_snapshot',
    'test_symlink_grant_and_non_socket_endpoint_are_rejected',
    'test_revocation_after_payload_requires_fenced_completion_marker',
    'test_client_rejects_response_budget_corruption_and_truncation',
):setattr(RocksOwnerTests,name,getattr(original.OwnerTests,name))
