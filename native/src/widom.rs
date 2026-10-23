//! Widom test-particle insertion (the Henry's-law limit of Materials Studio Sorption): a Lennard-Jones
//! probe inserted at random points of each frame, Boltzmann factors exp(-U/RT) averaged per frame.

use crate::cells::{minimum_image, norm2, CellList};
use crate::rng::Rng;
use rayon::prelude::*;

/// Gas constant in kcal mol^-1 K^-1.
pub const R_KCAL: f64 = 0.0019872043;

/// Analytic LJ tail energy (kcal/mol) of one probe beyond `cutoff`, for the atoms of one frame in volume `v`.
pub fn tail_energy(sigma: &[f64], epsilon: &[f64], probe_sigma: f64, probe_epsilon: f64, cutoff: f64, v: f64) -> f64 {
    let mut total = 0.0;
    for (s, e) in sigma.iter().zip(epsilon) {
        let sij = 0.5 * (s + probe_sigma);
        let eij = (e * probe_epsilon).sqrt();
        let x3 = (sij / cutoff).powi(3);
        total += eij * sij.powi(3) * (x3 * x3 * x3 / 3.0 - x3);
    }
    8.0 * std::f64::consts::PI / 3.0 * total / v
}

/// Probe energy (kcal/mol) at point p: plain LJ within cutoff, Lorentz-Berthelot mixing.
pub fn insertion_energy(cells: &CellList, sigma: &[f64], epsilon: &[f64], probe_sigma: f64, probe_epsilon: f64, cutoff: f64, p: &[f64; 3]) -> f64 {
    let c2 = cutoff * cutoff;
    let mut u = 0.0;
    cells.for_each_near(p, |i| {
        let r2 = norm2(&minimum_image(p, &cells.points[i], &cells.length));
        if r2 < c2 {
            let s = 0.5 * (sigma[i] + probe_sigma);
            let e = (epsilon[i] * probe_epsilon).sqrt();
            let s6 = (s * s / r2).powi(3);
            u += 4.0 * e * (s6 * s6 - s6);
        }
    });
    u
}

/// Mean Boltzmann factor <exp(-U/RT)> per frame from `insertions` random points each.
pub fn widom(frames: &[Vec<[f64; 3]>], boxes: &[[f64; 3]], sigma: &[f64], epsilon: &[f64], probe_sigma: f64, probe_epsilon: f64,
             temperature: f64, insertions: usize, cutoff: f64, seed: u64, tail: bool) -> Vec<f64> {
    let beta = 1.0 / (R_KCAL * temperature);
    frames
        .iter()
        .zip(boxes)
        .enumerate()
        .map(|(f, (positions, length))| {
            let cells = CellList::new(positions, *length, cutoff);
            let volume = length[0] * length[1] * length[2];
            let shift = if tail { tail_energy(sigma, epsilon, probe_sigma, probe_epsilon, cutoff, volume) } else { 0.0 };
            let total: f64 = (0..insertions)
                .into_par_iter()
                .map(|i| {
                    let mut rng = Rng::new(seed, (f as u64) << 32 | i as u64);
                    let p = [rng.uniform() * length[0], rng.uniform() * length[1], rng.uniform() * length[2]];
                    let u = insertion_energy(&cells, sigma, epsilon, probe_sigma, probe_epsilon, cutoff, &p) + shift;
                    (-beta * u).exp()
                })
                .sum();
            total / insertions as f64
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_box_gives_unit_boltzmann_factor_and_far_atoms_none() {
        let b = widom(&[vec![[1.0, 1.0, 1.0]]], &[[30.0, 30.0, 30.0]], &[0.0], &[0.0], 3.4, 0.2, 300.0, 1000, 10.0, 1, false);
        assert!((b[0] - 1.0).abs() < 1e-12);
    }

    #[test]
    fn energy_matches_the_lj_formula_and_tail_has_the_closed_form() {
        let length = [20.0, 20.0, 20.0];
        let cells = CellList::new(&[[10.0, 10.0, 10.0]], length, 9.0);
        let r = 4.0f64;
        let u = insertion_energy(&cells, &[3.0], &[0.1], 4.0, 0.4, 9.0, &[10.0 + r, 10.0, 10.0]);
        let (s, e) = (3.5f64, (0.04f64).sqrt());
        assert!((u - 4.0 * e * ((s / r).powi(12) - (s / r).powi(6))).abs() < 1e-12);
        // Minimum image across the boundary.
        let u2 = insertion_energy(&cells, &[3.0], &[0.1], 4.0, 0.4, 9.0, &[10.0 - r + 20.0, 10.0, 10.0]);
        assert!((u - u2).abs() < 1e-12);
        let t = tail_energy(&[3.0], &[0.1], 4.0, 0.4, 9.0, 8000.0);
        let x3 = (s / 9.0).powi(3);
        assert!((t - 8.0 * std::f64::consts::PI / 3.0 * e * s.powi(3) * (x3.powi(3) / 3.0 - x3) / 8000.0).abs() < 1e-15);
    }
}
