use crate::lsn::Lsn;

#[derive(Debug, PartialEq, Eq)]
pub enum HeadWait {
    Ready,
    KeepWaiting,
    HoldAndRequeue,
}

/// Only a known flush boundary and an ingested position at or past it are safe.
pub fn ingestion_verdict(
    ingested: Option<Lsn>,
    flush: Option<Lsn>,
    deadline_passed: bool,
) -> HeadWait {
    match (ingested, flush) {
        (Some(i), Some(f)) if i >= f => HeadWait::Ready,
        _ if deadline_passed => HeadWait::HoldAndRequeue,
        _ => HeadWait::KeepWaiting,
    }
}

/// Boundary adapter for existing wire responses. Invalid values fail closed.
pub fn head_wait_verdict(ingested: Option<&str>, flush: &str, deadline_passed: bool) -> HeadWait {
    ingestion_verdict(
        ingested.and_then(|s| s.parse().ok()),
        flush.parse().ok(),
        deadline_passed,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn invalid_positions_never_authorize_branching() {
        for (ingested, flush) in [
            (Some("bogus"), "0/0"),
            (Some("0/1000"), "bogus"),
            (Some("1/100000000"), "0/1"),
        ] {
            assert_eq!(
                head_wait_verdict(ingested, flush, false),
                HeadWait::KeepWaiting
            );
            assert_eq!(
                head_wait_verdict(ingested, flush, true),
                HeadWait::HoldAndRequeue
            );
        }
    }
    /// Review 002 P1's negative case: under sustained ingestion lag the
    /// verdict is never Ready — past the deadline the branch is HELD (the
    /// caller requeues before any timeline creation), not cut stale.
    #[test]
    fn head_wait_holds_on_sustained_lag() {
        assert_eq!(
            head_wait_verdict(Some("0/1000"), "0/2000", false),
            HeadWait::KeepWaiting
        );
        assert_eq!(
            head_wait_verdict(Some("0/1000"), "0/2000", true),
            HeadWait::HoldAndRequeue
        );
        assert_eq!(
            head_wait_verdict(None, "0/2000", true),
            HeadWait::HoldAndRequeue
        );
    }

    #[test]
    fn head_wait_ready_only_at_or_past_flush() {
        assert_eq!(
            head_wait_verdict(Some("0/2000"), "0/2000", false),
            HeadWait::Ready
        );
        assert_eq!(
            head_wait_verdict(Some("1/0"), "0/FFFFFFFF", true),
            HeadWait::Ready
        );
        assert_eq!(
            head_wait_verdict(Some("0/1FFF"), "0/2000", false),
            HeadWait::KeepWaiting
        );
    }
}
