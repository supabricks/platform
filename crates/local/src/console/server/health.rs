//! A transport deadline is not evidence that the console lost ownership.
use crate::store::{Error, Result};
use std::{
    io::ErrorKind,
    time::{Duration, Instant},
};

// Match the longest bounded console package operation, without extending any
// session, notebook heartbeat lease, or authorization lifetime.
const BUSY_GRACE: Duration = Duration::from_secs(120);

pub(super) struct Health {
    last_success: Instant,
}

pub(super) fn reason(result: &Result<()>) -> &'static str {
    match result {
        Err(Error::Io(e)) if matches!(e.kind(), ErrorKind::TimedOut | ErrorKind::WouldBlock) => {
            "control timeout grace exhausted"
        }
        _ => "daemon unavailable or ownership rejected",
    }
}

impl Health {
    pub(super) fn new(now: Instant) -> Self {
        Self { last_success: now }
    }

    pub(super) fn observe(&mut self, result: &Result<()>, now: Instant) -> bool {
        match result {
            Ok(()) => {
                self.last_success = now;
                true
            }
            Err(Error::Io(e))
                if matches!(e.kind(), ErrorKind::TimedOut | ErrorKind::WouldBlock) =>
            {
                now.duration_since(self.last_success) < BUSY_GRACE
            }
            // Explicit binding/generation/instance rejection, a missing daemon,
            // and malformed replies fail closed without the timeout grace.
            Err(_) => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::error::conflict;
    use supabricks_core::error::OperationError;

    #[test]
    fn busy_daemon_survives_multiple_deadlines_and_recovers() {
        let start = Instant::now();
        let mut health = Health::new(start);
        for seconds in [6, 12, 18, 90] {
            let timeout = Err(std::io::Error::from(ErrorKind::WouldBlock).into());
            assert!(health.observe(&timeout, start + Duration::from_secs(seconds)));
        }
        assert!(health.observe(&Ok(()), start + Duration::from_secs(100)));
        let timeout = Err(std::io::Error::from(ErrorKind::TimedOut).into());
        assert!(health.observe(&timeout, start + Duration::from_secs(130)));
        assert!(!health.observe(&timeout, start + Duration::from_secs(220)));
    }

    #[test]
    fn timeout_grace_never_masks_lost_ownership_or_daemon() {
        let start = Instant::now();
        for error in [
            conflict("notebook console generation changed"),
            conflict("console process does not own this project binding"),
            OperationError::Unavailable("daemon unavailable".into()).into(),
            std::io::Error::from(ErrorKind::ConnectionReset).into(),
        ] {
            assert!(!Health::new(start).observe(&Err(error), start));
        }
    }
}
