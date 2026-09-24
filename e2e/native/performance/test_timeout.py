"""Missing observation history must not masquerade as a runtime capacity failure."""
import unittest
from trial import drain_timeout_evidence


class TimeoutProof(unittest.TestCase):
    def test_incomplete_capture_is_proven_even_if_some_markers_are_unseen(self):
        result=drain_timeout_evidence(1000,800,250)
        self.assertEqual(result['uncaptured_commits_lower_bound'],200)

    def test_missing_history_without_capture_proof_is_invalid(self):
        with self.assertRaisesRegex(AssertionError,'no proof'):
            drain_timeout_evidence(1000,1200,1)

    def test_known_commit_markers_allow_publication_timeout(self):
        self.assertEqual(drain_timeout_evidence(1000,1200,0)['missing_observed_markers'],0)
        with self.assertRaisesRegex(AssertionError,'contradict'):
            drain_timeout_evidence(1000,800,0)


if __name__=='__main__':unittest.main()
