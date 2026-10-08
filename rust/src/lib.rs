//! Mimic capabilities beside genuine automation framework objects.
//!
//! Importing this crate never installs a runtime or opens a listener. Select
//! the optional `chromiumoxide` feature for that framework's native adapter.
mod cache;
#[cfg(feature = "chromiumoxide")]
pub mod chromiumoxide;
pub mod generated;
pub mod runtime;
pub mod transport;

pub use runtime::{RuntimeManager, RuntimeOptions, RuntimeProcess};
pub use transport::{Client, Error, Result, Transport};
