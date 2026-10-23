//! Blends-style contact sampling (Fan, Olafson, Blanco and Hsu, Macromolecules 25, 3667 (1992)):
//! two rigid molecules are brought into van der Waals contact from random relative orientations and
//! their intermolecular energy is recorded, and molecules are packed around a central one to count
//! the coordination number. The Python driver (workbench/blends.py) turns these into chi(T).

use crate::rng::Rng;
use rayon::prelude::*;

/// Coulomb constant in kcal mol^-1 angstrom e^-2 (the value LAMMPS uses for real units).
pub const COULOMB: f64 = 332.06371;

/// A rigid molecule centred on its geometric centre, with per-atom LJ sigma (angstrom), epsilon
/// (kcal/mol), partial charge (e) and contact radius (angstrom).
#[derive(Clone, Debug)]
pub struct Molecule {
    pub pos: Vec<[f64; 3]>,
    pub sigma: Vec<f64>,
    pub epsilon: Vec<f64>,
    pub charge: Vec<f64>,
    pub radius: Vec<f64>,
}

impl Molecule {
    pub fn new(pos: Vec<[f64; 3]>, sigma: Vec<f64>, epsilon: Vec<f64>, charge: Vec<f64>, radius: Vec<f64>) -> Result<Self, String> {
        let n = pos.len();
        if n == 0 || sigma.len() != n || epsilon.len() != n || charge.len() != n || radius.len() != n {
            return Err("molecule arrays must be non-empty and of equal length".into());
        }
        if pos.iter().flatten().chain(sigma.iter()).chain(epsilon.iter()).chain(charge.iter()).chain(radius.iter()).any(|v| !v.is_finite()) {
            return Err("molecule arrays must be finite".into());
        }
        let mut c = [0.0; 3];
        for p in &pos {
            for k in 0..3 {
                c[k] += p[k] / n as f64;
            }
        }
        let pos = pos.iter().map(|p| [p[0] - c[0], p[1] - c[1], p[2] - c[2]]).collect();
        Ok(Molecule { pos, sigma, epsilon, charge, radius })
    }

    fn len(&self) -> usize {
        self.pos.len()
    }
}

fn rotate(m: &[[f64; 3]; 3], p: &[f64; 3]) -> [f64; 3] {
    [
        m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2],
        m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2],
        m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2],
    ]
}

/// Distance t along `u` at which `moving` (already rotated, centred at the origin) first touches
/// `fixed` when it comes in from far away along +u: the largest t with |a - (b + t u)| = scale (r_a + r_b)
/// over all atom pairs. For larger t no pair overlaps. NEG_INFINITY means the line never brings any
/// pair into contact (possible only through an open ring), and the caller draws a new direction.
fn contact_distance(fixed: &Molecule, moving: &[[f64; 3]], moving_radius: &[f64], u: &[f64; 3], scale: f64) -> f64 {
    let mut best = f64::NEG_INFINITY;
    for (a, ra) in fixed.pos.iter().zip(&fixed.radius) {
        for (b, rb) in moving.iter().zip(moving_radius) {
            let w = [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
            let wu = w[0] * u[0] + w[1] * u[1] + w[2] * u[2];
            let ww = w[0] * w[0] + w[1] * w[1] + w[2] * w[2];
            let c = scale * (ra + rb);
            let disc = wu * wu - ww + c * c;
            if disc >= 0.0 {
                let t = wu + disc.sqrt();
                if t > best {
                    best = t;
                }
            }
        }
    }
    best
}

/// Intermolecular LJ (Lorentz-Berthelot) + Coulomb energy in kcal/mol, no cutoff.
pub fn pair_energy(a: &Molecule, b_pos: &[[f64; 3]], b: &Molecule, dielectric: f64) -> f64 {
    let mut total = 0.0;
    for i in 0..a.len() {
        let (pa, sa, ea, qa) = (a.pos[i], a.sigma[i], a.epsilon[i], a.charge[i]);
        for j in 0..b.len() {
            let pb = b_pos[j];
            let d = [pa[0] - pb[0], pa[1] - pb[1], pa[2] - pb[2]];
            let r2 = d[0] * d[0] + d[1] * d[1] + d[2] * d[2];
            let sigma = 0.5 * (sa + b.sigma[j]);
            let eps = (ea * b.epsilon[j]).sqrt();
            let s6 = (sigma * sigma / r2).powi(3);
            total += 4.0 * eps * (s6 * s6 - s6) + COULOMB * qa * b.charge[j] / (dielectric * r2.sqrt());
        }
    }
    total
}

/// Place `moving` in contact with `fixed` from a random orientation and direction.
fn random_contact(fixed: &Molecule, moving: &Molecule, rng: &mut Rng, scale: f64) -> Vec<[f64; 3]> {
    loop {
        let rot = rng.rotation();
        let u = rng.unit_vector();
        let rotated: Vec<[f64; 3]> = moving.pos.iter().map(|p| rotate(&rot, p)).collect();
        let t = contact_distance(fixed, &rotated, &moving.radius, &u, scale);
        if t.is_finite() {
            return rotated.iter().map(|p| [p[0] + t * u[0], p[1] + t * u[1], p[2] + t * u[2]]).collect();
        }
    }
}

/// Energies (kcal/mol) of `samples` random contact configurations of `moving` around `fixed`.
pub fn contact_energies(fixed: &Molecule, moving: &Molecule, samples: usize, seed: u64, scale: f64, dielectric: f64) -> Vec<f64> {
    (0..samples)
        .into_par_iter()
        .map(|s| {
            let mut rng = Rng::new(seed, s as u64);
            let placed = random_contact(fixed, moving, &mut rng, scale);
            pair_energy(fixed, &placed, moving, dielectric)
        })
        .collect()
}

fn overlaps(a: &[[f64; 3]], ra: &[f64], b: &[[f64; 3]], rb: &[f64], scale: f64) -> bool {
    for (p, r1) in a.iter().zip(ra) {
        for (q, r2) in b.iter().zip(rb) {
            let d = [p[0] - q[0], p[1] - q[1], p[2] - q[2]];
            let c = scale * (r1 + r2);
            if d[0] * d[0] + d[1] * d[1] + d[2] * d[2] < c * c * (1.0 - 1e-9) {
                return true;
            }
        }
    }
    false
}

/// Coordination number per cluster: copies of `moving` are put in contact with `fixed` one by one and
/// kept when they overlap no copy already kept; a cluster ends after `max_failures` rejections in a row.
pub fn coordination_numbers(fixed: &Molecule, moving: &Molecule, clusters: usize, seed: u64, scale: f64, max_failures: usize) -> Vec<f64> {
    (0..clusters)
        .into_par_iter()
        .map(|c| {
            let mut rng = Rng::new(seed ^ 0xC0_0D1A_7E, c as u64);
            let mut kept: Vec<Vec<[f64; 3]>> = Vec::new();
            let mut failures = 0;
            while failures < max_failures {
                let placed = random_contact(fixed, moving, &mut rng, scale);
                if kept.iter().any(|other| overlaps(&placed, &moving.radius, other, &moving.radius, scale)) {
                    failures += 1;
                } else {
                    kept.push(placed);
                    failures = 0;
                }
            }
            kept.len() as f64
        })
        .collect()
}

/// Closest scaled contact (min over pairs of distance / (scale (r_a + r_b))) between two placed molecules.
pub fn closest_scaled_contact(a: &Molecule, b_pos: &[[f64; 3]], b_radius: &[f64], scale: f64) -> f64 {
    let mut best = f64::INFINITY;
    for (p, r1) in a.pos.iter().zip(&a.radius) {
        for (q, r2) in b_pos.iter().zip(b_radius) {
            let d = ((p[0] - q[0]).powi(2) + (p[1] - q[1]).powi(2) + (p[2] - q[2]).powi(2)).sqrt();
            best = best.min(d / (scale * (r1 + r2)));
        }
    }
    best
}

/// One sampled contact (for tests and for checking the Python reference implementation).
pub fn sample_contact(fixed: &Molecule, moving: &Molecule, seed: u64, index: u64, scale: f64) -> Vec<[f64; 3]> {
    let mut rng = Rng::new(seed, index);
    random_contact(fixed, moving, &mut rng, scale)
}


#[cfg(test)]
mod tests {
    use super::*;

    fn atom(x: f64, y: f64, z: f64, q: f64) -> ([f64; 3], f64) {
        ([x, y, z], q)
    }

    fn mol(atoms: &[([f64; 3], f64)], radius: f64) -> Molecule {
        let n = atoms.len();
        Molecule::new(atoms.iter().map(|a| a.0).collect(), vec![3.4; n], vec![0.1; n], atoms.iter().map(|a| a.1).collect(), vec![radius; n]).unwrap()
    }

    #[test]
    fn contacts_touch_without_overlap() {
        let a = mol(&[atom(0.0, 0.0, 0.0, 0.0), atom(1.5, 0.0, 0.0, 0.0), atom(3.0, 0.5, 0.0, 0.0)], 1.7);
        let b = mol(&[atom(0.0, 0.0, 0.0, 0.0), atom(0.0, 1.4, 0.0, 0.0)], 1.5);
        for i in 0..500 {
            let placed = sample_contact(&a, &b, 7, i, 1.0);
            let closest = closest_scaled_contact(&a, &placed, &b.radius, 1.0);
            assert!((closest - 1.0).abs() < 1e-9, "sample {i}: {closest}");
        }
    }

    #[test]
    fn lj_minimum_and_coulomb_match_formulas() {
        let a = mol(&[atom(0.0, 0.0, 0.0, 0.5)], 1.7);
        let b = mol(&[atom(0.0, 0.0, 0.0, -0.5)], 1.7);
        let rmin = 2f64.powf(1.0 / 6.0) * 3.4;
        let e = pair_energy(&a, &[[rmin, 0.0, 0.0]], &b, 1.0);
        assert!((e - (-0.1 - COULOMB * 0.25 / rmin)).abs() < 1e-9);
    }

    #[test]
    fn equal_spheres_around_a_sphere_stay_below_the_kissing_number() {
        let a = mol(&[atom(0.0, 0.0, 0.0, 0.0)], 1.0);
        let z = coordination_numbers(&a, &a, 64, 3, 1.0, 400);
        let mean = z.iter().sum::<f64>() / z.len() as f64;
        assert!(z.iter().all(|&v| v <= 12.0));
        // Random sequential packing of touching equal spheres jams near 7-10 neighbours.
        assert!((6.5..=10.5).contains(&mean), "mean {mean}");
    }

    #[test]
    fn results_do_not_depend_on_threads() {
        let a = mol(&[atom(0.0, 0.0, 0.0, 0.3), atom(1.5, 0.0, 0.0, -0.3)], 1.6);
        let first = contact_energies(&a, &a, 2000, 11, 1.0, 1.0);
        let pool = rayon::ThreadPoolBuilder::new().num_threads(1).build().unwrap();
        let second = pool.install(|| contact_energies(&a, &a, 2000, 11, 1.0, 1.0));
        assert_eq!(first, second);
    }
}
