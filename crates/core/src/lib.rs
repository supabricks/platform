//! Portable configuration and domain contracts shared by platform adapters.
//! This crate performs no cluster discovery, process supervision or state I/O.
pub mod branch;
pub mod error;
pub mod keys;
pub mod lsn;
pub mod resource;
pub mod spec;
pub mod validation;
