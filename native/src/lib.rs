//! Polymer Lab compute kernels, written from published methods:
//!
//! - contacts:   Blends-style pair contact sampling and coordination numbers (Fan et al. 1992)
//! - trajectory: radial distribution function pair counts and mean-squared displacement
//! - volume:     occupied, probe-accessible and probe-occupiable volume on a grid
//! - widom:      Widom test-particle insertion of a Lennard-Jones probe
//!
//! Every random sample draws from its own generator seeded by (seed, sample index), so results do
//! not depend on the number of threads.

pub mod cells;
pub mod contacts;
pub mod rng;
pub mod trajectory;
pub mod volume;
pub mod widom;
pub mod doe;
pub mod execution;
pub mod statistics;

#[cfg(feature = "python")]
mod python;
