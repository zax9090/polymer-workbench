//! Polymer Lab compute kernels.
//!
//! Blends-style contact sampling (Fan, Olafson, Blanco and Hsu, Macromolecules 25, 3667 (1992)):
//! two rigid molecules are brought into van der Waals contact from random relative orientations and
//! their intermolecular energy is recorded, and molecules are packed around a central one to count
//! the coordination number. The Python driver (workbench/blends.py) turns these into chi(T).
//!
//! Every sample draws from its own generator seeded by (seed, sample index), so results do not depend
//! on the number of threads.

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

/// xoshiro256** seeded through SplitMix64.
struct Rng([u64; 4]);

impl Rng {
    fn new(seed: u64, stream: u64) -> Self {
        let mut x = seed ^ stream.wrapping_mul(0x9E37_79B9_7F4A_7C15).rotate_left(17);
        let mut next = || {
            x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
            let mut z = x;
            z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
            z ^ (z >> 31)
        };
        Rng([next(), next(), next(), next()])
    }

    fn next_u64(&mut self) -> u64 {
        let s = &mut self.0;
        let result = s[1].wrapping_mul(5).rotate_left(7).wrapping_mul(9);
        let t = s[1] << 17;
        s[2] ^= s[0];
        s[3] ^= s[1];
        s[1] ^= s[2];
        s[0] ^= s[3];
        s[2] ^= t;
        s[3] = s[3].rotate_left(45);
        result
    }

    /// Uniform in [0, 1).
    fn uniform(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / (1u64 << 53) as f64)
    }

    fn normal(&mut self) -> f64 {
        // Box-Muller; 1 - u keeps the logarithm finite.
        let (u, v) = (1.0 - self.uniform(), self.uniform());
        (-2.0 * u.ln()).sqrt() * (2.0 * std::f64::consts::PI * v).cos()
    }

    fn unit_vector(&mut self) -> [f64; 3] {
        loop {
            let v = [self.normal(), self.normal(), self.normal()];
            let n = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt();
            if n > 1e-12 {
                return [v[0] / n, v[1] / n, v[2] / n];
            }
        }
    }

    /// Uniformly distributed rotation matrix (from a random unit quaternion).
    fn rotation(&mut self) -> [[f64; 3]; 3] {
        let q = loop {
            let q = [self.normal(), self.normal(), self.normal(), self.normal()];
            let n = (q.iter().map(|x| x * x).sum::<f64>()).sqrt();
            if n > 1e-12 {
                break [q[0] / n, q[1] / n, q[2] / n, q[3] / n];
            }
        };
        let [a, b, c, d] = q;
        [
            [a * a + b * b - c * c - d * d, 2.0 * (b * c - a * d), 2.0 * (b * d + a * c)],
            [2.0 * (b * c + a * d), a * a - b * b + c * c - d * d, 2.0 * (c * d - a * b)],
            [2.0 * (b * d - a * c), 2.0 * (c * d + a * b), a * a - b * b - c * c + d * d],
        ]
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

#[cfg(feature = "python")]
mod python {
    use super::{closest_scaled_contact, pair_energy, Molecule, COULOMB};
    use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
    use pyo3::exceptions::PyValueError;
    use pyo3::prelude::*;

    fn molecule(pos: PyReadonlyArray2<f64>, sigma: PyReadonlyArray1<f64>, epsilon: PyReadonlyArray1<f64>,
                charge: PyReadonlyArray1<f64>, radius: PyReadonlyArray1<f64>) -> PyResult<Molecule> {
        let p = pos.as_array();
        if p.ncols() != 3 {
            return Err(PyValueError::new_err("positions must have shape (n, 3)"));
        }
        let rows = p.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect();
        let vec = |a: PyReadonlyArray1<f64>| a.as_array().to_vec();
        Molecule::new(rows, vec(sigma), vec(epsilon), vec(charge), vec(radius)).map_err(PyValueError::new_err)
    }

    type Mol<'py> = (PyReadonlyArray2<'py, f64>, PyReadonlyArray1<'py, f64>, PyReadonlyArray1<'py, f64>, PyReadonlyArray1<'py, f64>, PyReadonlyArray1<'py, f64>);

    fn unpack(m: Mol) -> PyResult<Molecule> {
        molecule(m.0, m.1, m.2, m.3, m.4)
    }

    /// contact_energies(fixed, moving, samples, seed, scale=1.0, dielectric=1.0) -> ndarray (kcal/mol)
    /// fixed/moving are (positions[n,3], sigma[n], epsilon[n], charge[n], radius[n]).
    #[pyfunction]
    #[pyo3(signature = (fixed, moving, samples, seed, scale=1.0, dielectric=1.0))]
    fn contact_energies<'py>(py: Python<'py>, fixed: Mol<'py>, moving: Mol<'py>, samples: usize, seed: u64, scale: f64, dielectric: f64) -> PyResult<Bound<'py, PyArray1<f64>>> {
        let (a, b) = (unpack(fixed)?, unpack(moving)?);
        if !(scale > 0.0 && dielectric > 0.0) {
            return Err(PyValueError::new_err("scale and dielectric must be positive"));
        }
        let energies = py.detach(|| super::contact_energies(&a, &b, samples, seed, scale, dielectric));
        Ok(energies.into_pyarray(py))
    }

    /// coordination_numbers(fixed, moving, clusters, seed, scale=1.0, max_failures=300) -> ndarray
    #[pyfunction]
    #[pyo3(signature = (fixed, moving, clusters, seed, scale=1.0, max_failures=300))]
    fn coordination_numbers<'py>(py: Python<'py>, fixed: Mol<'py>, moving: Mol<'py>, clusters: usize, seed: u64, scale: f64, max_failures: usize) -> PyResult<Bound<'py, PyArray1<f64>>> {
        let (a, b) = (unpack(fixed)?, unpack(moving)?);
        if !(scale > 0.0) || max_failures == 0 {
            return Err(PyValueError::new_err("scale must be positive and max_failures at least 1"));
        }
        let counts = py.detach(|| super::coordination_numbers(&a, &b, clusters, seed, scale, max_failures));
        Ok(counts.into_pyarray(py))
    }

    /// sample_contact(fixed, moving, seed, index, scale=1.0) -> (positions[n,3] of moving, energy, closest scaled contact)
    #[pyfunction]
    #[pyo3(signature = (fixed, moving, seed, index, scale=1.0, dielectric=1.0))]
    fn sample_contact<'py>(py: Python<'py>, fixed: Mol<'py>, moving: Mol<'py>, seed: u64, index: u64, scale: f64, dielectric: f64) -> PyResult<(Bound<'py, PyArray2<f64>>, f64, f64)> {
        let (a, b) = (unpack(fixed)?, unpack(moving)?);
        let placed = super::sample_contact(&a, &b, seed, index, scale);
        let energy = pair_energy(&a, &placed, &b, dielectric);
        let closest = closest_scaled_contact(&a, &placed, &b.radius, scale);
        let flat: Vec<f64> = placed.iter().flatten().copied().collect();
        let array = numpy::ndarray::Array2::from_shape_vec((placed.len(), 3), flat).expect("shape").into_pyarray(py);
        Ok((array, energy, closest))
    }

    #[pymodule]
    fn workbench_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(contact_energies, m)?)?;
        m.add_function(wrap_pyfunction!(coordination_numbers, m)?)?;
        m.add_function(wrap_pyfunction!(sample_contact, m)?)?;
        m.add("COULOMB", COULOMB)?;
        m.add("__version__", env!("CARGO_PKG_VERSION"))?;
        Ok(())
    }
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
