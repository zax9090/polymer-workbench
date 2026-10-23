//! PyO3 bindings: module workbench_native.

use crate::contacts::{self, closest_scaled_contact, pair_energy, Molecule, COULOMB};
use crate::{trajectory, volume, widom};
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3};
use pyo3::types::PyDict;
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
    let energies = py.detach(|| contacts::contact_energies(&a, &b, samples, seed, scale, dielectric));
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
    let counts = py.detach(|| contacts::coordination_numbers(&a, &b, clusters, seed, scale, max_failures));
    Ok(counts.into_pyarray(py))
}

/// sample_contact(fixed, moving, seed, index, scale=1.0) -> (positions[n,3] of moving, energy, closest scaled contact)
#[pyfunction]
#[pyo3(signature = (fixed, moving, seed, index, scale=1.0, dielectric=1.0))]
fn sample_contact<'py>(py: Python<'py>, fixed: Mol<'py>, moving: Mol<'py>, seed: u64, index: u64, scale: f64, dielectric: f64) -> PyResult<(Bound<'py, PyArray2<f64>>, f64, f64)> {
    let (a, b) = (unpack(fixed)?, unpack(moving)?);
    let placed = contacts::sample_contact(&a, &b, seed, index, scale);
    let energy = pair_energy(&a, &placed, &b, dielectric);
    let closest = closest_scaled_contact(&a, &placed, &b.radius, scale);
    let flat: Vec<f64> = placed.iter().flatten().copied().collect();
    let array = numpy::ndarray::Array2::from_shape_vec((placed.len(), 3), flat).expect("shape").into_pyarray(py);
    Ok((array, energy, closest))
}


fn frames3(frames: &PyReadonlyArray3<f64>) -> PyResult<Vec<Vec<[f64; 3]>>> {
    let a = frames.as_array();
    if a.shape()[2] != 3 {
        return Err(PyValueError::new_err("frames must have shape (frames, atoms, 3)"));
    }
    if a.iter().any(|v| !v.is_finite()) {
        return Err(PyValueError::new_err("frames must be finite"));
    }
    Ok(a.outer_iter().map(|f| f.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect()).collect())
}

fn boxes2(boxes: &PyReadonlyArray2<f64>, frames: usize) -> PyResult<Vec<[f64; 3]>> {
    let b = boxes.as_array();
    if b.nrows() != frames || b.ncols() != 3 || b.iter().any(|v| !(v.is_finite() && *v > 0.0)) {
        return Err(PyValueError::new_err("boxes must have shape (frames, 3) with positive edge lengths"));
    }
    Ok(b.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect())
}

fn indices(values: &PyReadonlyArray1<i64>, atoms: usize) -> PyResult<Vec<usize>> {
    values.as_array().iter().map(|&v| if v >= 0 && (v as usize) < atoms { Ok(v as usize) } else { Err(PyValueError::new_err("atom index out of range")) }).collect()
}

/// rdf_counts(frames[F,N,3], boxes[F,3], a[int], b[int], r_max, bins, molecule=None) -> ndarray of ordered pair counts
#[pyfunction]
#[pyo3(signature = (frames, boxes, a, b, r_max, bins, molecule=None))]
fn rdf_counts<'py>(py: Python<'py>, frames: PyReadonlyArray3<'py, f64>, boxes: PyReadonlyArray2<'py, f64>, a: PyReadonlyArray1<'py, i64>,
                   b: PyReadonlyArray1<'py, i64>, r_max: f64, bins: usize, molecule: Option<PyReadonlyArray1<'py, i64>>) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let f = frames3(&frames)?;
    let atoms = f.first().map(|x| x.len()).unwrap_or(0);
    let bx = boxes2(&boxes, f.len())?;
    if !(r_max > 0.0) || bins == 0 || bx.iter().any(|l| r_max > 0.5 * l.iter().cloned().fold(f64::INFINITY, f64::min)) {
        return Err(PyValueError::new_err("r_max must be positive and at most half the smallest box edge; bins >= 1"));
    }
    let (ia, ib) = (indices(&a, atoms)?, indices(&b, atoms)?);
    let mol: Option<Vec<i64>> = molecule.map(|m| m.as_array().to_vec());
    if mol.as_ref().is_some_and(|m| m.len() != atoms) {
        return Err(PyValueError::new_err("molecule must have one entry per atom"));
    }
    let counts = py.detach(|| trajectory::rdf_counts(&f, &bx, &ia, &ib, mol.as_deref(), r_max, bins));
    Ok(counts.into_pyarray(py))
}

/// msd(unwrapped_frames[F,N,3], atoms[int]) -> ndarray MSD for lags 0..F-1 (windowed average)
#[pyfunction]
fn msd<'py>(py: Python<'py>, frames: PyReadonlyArray3<'py, f64>, atoms: PyReadonlyArray1<'py, i64>) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let f = frames3(&frames)?;
    let n = f.first().map(|x| x.len()).unwrap_or(0);
    let idx = indices(&atoms, n)?;
    if idx.is_empty() {
        return Err(PyValueError::new_err("select at least one atom"));
    }
    let out = py.detach(|| trajectory::msd(&f, &idx));
    Ok(out.into_pyarray(py))
}

/// free_volume(positions[N,3], radii[N], box[3], probe, spacing, bin=0.1, bins=100) -> dict
#[pyfunction]
#[pyo3(signature = (positions, radii, box_length, probe, spacing, bin=0.1, bins=100))]
fn free_volume<'py>(py: Python<'py>, positions: PyReadonlyArray2<'py, f64>, radii: PyReadonlyArray1<'py, f64>, box_length: [f64; 3],
                    probe: f64, spacing: f64, bin: f64, bins: usize) -> PyResult<Bound<'py, PyDict>> {
    let p: Vec<[f64; 3]> = positions.as_array().rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect();
    let r = radii.as_array().to_vec();
    if p.len() != r.len() || p.is_empty() || r.iter().any(|v| !(v.is_finite() && *v >= 0.0)) {
        return Err(PyValueError::new_err("positions and radii must match and radii be non-negative"));
    }
    if !(probe >= 0.0 && spacing > 0.0 && bin > 0.0) || box_length.iter().any(|l| !(l.is_finite() && *l > 0.0)) {
        return Err(PyValueError::new_err("probe >= 0, spacing > 0, bin > 0 and positive box edges are required"));
    }
    let points = box_length.iter().map(|l| (l / spacing).ceil()).product::<f64>();
    if points > 4.0e8 {
        return Err(PyValueError::new_err("grid too fine: more than 400 million points"));
    }
    let res = py.detach(|| volume::free_volume(&p, &r, box_length, probe, spacing, bin, bins));
    let d = PyDict::new(py);
    d.set_item("grid", res.grid.to_vec())?;
    d.set_item("occupied_fraction", res.occupied_fraction)?;
    d.set_item("accessible_fraction", res.accessible_fraction)?;
    d.set_item("occupiable_fraction", res.occupiable_fraction)?;
    d.set_item("surface_distance_histogram", res.surface_distance_histogram.into_pyarray(py))?;
    Ok(d)
}

/// widom(frames[F,N,3], boxes[F,3], sigma[N], epsilon[N], probe_sigma, probe_epsilon, temperature, insertions, cutoff, seed, tail=True)
/// -> ndarray of <exp(-U/RT)> per frame (energies in kcal/mol)
#[pyfunction]
#[pyo3(signature = (frames, boxes, sigma, epsilon, probe_sigma, probe_epsilon, temperature, insertions, cutoff, seed, tail=true))]
fn widom_insertion<'py>(py: Python<'py>, frames: PyReadonlyArray3<'py, f64>, boxes: PyReadonlyArray2<'py, f64>, sigma: PyReadonlyArray1<'py, f64>,
                        epsilon: PyReadonlyArray1<'py, f64>, probe_sigma: f64, probe_epsilon: f64, temperature: f64, insertions: usize,
                        cutoff: f64, seed: u64, tail: bool) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let f = frames3(&frames)?;
    let n = f.first().map(|x| x.len()).unwrap_or(0);
    let bx = boxes2(&boxes, f.len())?;
    let (s, e) = (sigma.as_array().to_vec(), epsilon.as_array().to_vec());
    if s.len() != n || e.len() != n || !(temperature > 0.0) || insertions == 0 || !(cutoff > 0.0) {
        return Err(PyValueError::new_err("sigma/epsilon per atom, temperature > 0, insertions >= 1 and cutoff > 0 are required"));
    }
    if bx.iter().any(|l| cutoff > 0.5 * l.iter().cloned().fold(f64::INFINITY, f64::min)) {
        return Err(PyValueError::new_err("cutoff must be at most half the smallest box edge"));
    }
    let out = py.detach(|| widom::widom(&f, &bx, &s, &e, probe_sigma, probe_epsilon, temperature, insertions, cutoff, seed, tail));
    Ok(out.into_pyarray(py))
}

#[pymodule]
fn workbench_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(contact_energies, m)?)?;
    m.add_function(wrap_pyfunction!(coordination_numbers, m)?)?;
    m.add_function(wrap_pyfunction!(sample_contact, m)?)?;
    m.add_function(wrap_pyfunction!(rdf_counts, m)?)?;
    m.add_function(wrap_pyfunction!(msd, m)?)?;
    m.add_function(wrap_pyfunction!(free_volume, m)?)?;
    m.add_function(wrap_pyfunction!(widom_insertion, m)?)?;
    m.add("R_KCAL", widom::R_KCAL)?;
    m.add("COULOMB", COULOMB)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
