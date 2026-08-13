//! NodePort allocation for the M1 endpoint block (RFC 012 D3, deleted in M2).
//! Pure function over the set of ports already claimed by existing Services:
//! stable hash of the endpoint name into the block, linear probing on
//! collision. Deterministic → the same endpoint re-acquires its port when
//! nothing else claimed it, and the reconciler stays stateless.

use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

pub const RANGE_START: i32 = 30001;
pub const RANGE_LEN: i32 = 20;

pub fn pick(name: &str, used: &BTreeSet<i32>) -> Option<i32> {
    let h = Sha256::digest(name.as_bytes());
    let start = i32::from(h[0] % RANGE_LEN as u8);
    (0..RANGE_LEN)
        .map(|i| RANGE_START + (start + i) % RANGE_LEN)
        .find(|p| !used.contains(p))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fills_block_without_duplicates_then_exhausts() {
        let mut used = BTreeSet::new();
        for i in 0..RANGE_LEN {
            let p = pick(&format!("db{i}"), &used).expect("block not full yet");
            assert!((RANGE_START..RANGE_START + RANGE_LEN).contains(&p));
            assert!(used.insert(p), "duplicate allocation");
        }
        assert_eq!(
            pick("overflow", &used),
            None,
            "exhaustion is a None, not a panic"
        );
    }

    #[test]
    fn stable_reacquisition() {
        let used = BTreeSet::new();
        let a = pick("db1", &used).unwrap();
        let b = pick("db1", &used).unwrap();
        assert_eq!(a, b);
    }

    #[test]
    fn probes_past_collisions() {
        let mut used = BTreeSet::new();
        let first = pick("db1", &used).unwrap();
        used.insert(first);
        let second = pick("db1", &used).unwrap();
        assert_ne!(first, second);
    }
}
