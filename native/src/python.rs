//! PyO3 bindings: module workbench_native.

use crate::contacts::{self, closest_scaled_contact, pair_energy, Molecule, COULOMB};
use crate::{doe, mixture, reliability, topology, trajectory, volume, widom};
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3};
use pyo3::types::{PyDict, PyList};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::HashSet;

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
    if a.shape()[2] != 3 || a.shape()[0] == 0 || a.shape()[1] == 0 {
        return Err(PyValueError::new_err("frames must have nonempty shape (frames, atoms, 3)"));
    }
    if a.shape()[0] > 2000 || a.len() / 3 > 20_000_000 {
        return Err(PyValueError::new_err("at most 2000 frames and 20 million frame-atoms are supported"));
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
    let result: Vec<[f64; 3]> = b.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect();
    if result.iter().any(|l| !(l.iter().product::<f64>().is_normal())) {
        return Err(PyValueError::new_err("box volume must be finite and positive"));
    }
    Ok(result)
}

fn indices(values: &PyReadonlyArray1<i64>, atoms: usize) -> PyResult<Vec<usize>> {
    let values = values.as_array();
    if values.is_empty() || values.len() > atoms {
        return Err(PyValueError::new_err("select a nonempty set of unique atoms"));
    }
    let mut seen = HashSet::with_capacity(values.len());
    values.iter().map(|&v| {
        if v < 0 || (v as usize) >= atoms {
            Err(PyValueError::new_err("atom index out of range"))
        } else if !seen.insert(v) {
            Err(PyValueError::new_err("atom indices must be unique"))
        } else {
            Ok(v as usize)
        }
    }).collect()
}

fn cell_list_budget(boxes: &[[f64; 3]], cutoff: f64) -> PyResult<()> {
    // A tiny positive cutoff otherwise allocates an unbounded dense array of empty cells.
    if boxes.iter().any(|l| {
        let count = l.iter().map(|v| (v / cutoff).floor().max(1.0)).product::<f64>();
        !count.is_finite() || count > 1_000_000.0
    }) {
        return Err(PyValueError::new_err("cutoff creates more than one million neighbour cells; increase it"));
    }
    Ok(())
}

/// rdf_counts(frames[F,N,3], boxes[F,3], a[int], b[int], r_max, bins, molecule=None) -> ndarray of ordered pair counts
#[pyfunction]
#[pyo3(signature = (frames, boxes, a, b, r_max, bins, molecule=None))]
fn rdf_counts<'py>(py: Python<'py>, frames: PyReadonlyArray3<'py, f64>, boxes: PyReadonlyArray2<'py, f64>, a: PyReadonlyArray1<'py, i64>,
                   b: PyReadonlyArray1<'py, i64>, r_max: f64, bins: usize, molecule: Option<PyReadonlyArray1<'py, i64>>) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let f = frames3(&frames)?;
    let atoms = f.first().map(|x| x.len()).unwrap_or(0);
    let bx = boxes2(&boxes, f.len())?;
    if !(r_max.is_finite() && r_max > 0.0) || bins == 0 || bins > 10_000 || bx.iter().any(|l| r_max > 0.5 * l.iter().cloned().fold(f64::INFINITY, f64::min)) {
        return Err(PyValueError::new_err("r_max must be finite, positive and at most half the smallest box edge; bins must be 1..10000"));
    }
    cell_list_budget(&bx, r_max)?;
    let (ia, ib) = (indices(&a, atoms)?, indices(&b, atoms)?);
    if molecule.as_ref().is_some_and(|m| m.as_array().len() != atoms) {
        return Err(PyValueError::new_err("molecule must have one entry per atom"));
    }
    let mol: Option<Vec<i64>> = molecule.map(|m| m.as_array().to_vec());
    let counts = py.detach(|| trajectory::rdf_counts(&f, &bx, &ia, &ib, mol.as_deref(), r_max, bins));
    Ok(counts.into_pyarray(py))
}

/// msd(unwrapped_frames[F,N,3], atoms[int]) -> ndarray MSD for lags 0..F-1 (windowed average)
#[pyfunction]
fn msd<'py>(py: Python<'py>, frames: PyReadonlyArray3<'py, f64>, atoms: PyReadonlyArray1<'py, i64>) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let f = frames3(&frames)?;
    let n = f.first().map(|x| x.len()).unwrap_or(0);
    let idx = indices(&atoms, n)?;
    if f.len() as u128 * f.len() as u128 * idx.len() as u128 > 1_000_000_000 {
        return Err(PyValueError::new_err("MSD calculation exceeds one billion atom-time pairs; select fewer atoms or frames"));
    }
    let out = py.detach(|| trajectory::msd(&f, &idx));
    if out.iter().any(|v| !v.is_finite()) {
        return Err(PyValueError::new_err("MSD overflow: coordinate differences are too large"));
    }
    Ok(out.into_pyarray(py))
}

/// free_volume(positions[N,3], radii[N], box[3], probe, spacing, bin=0.1, bins=100) -> dict
#[pyfunction]
#[pyo3(signature = (positions, radii, box_length, probe, spacing, bin=0.1, bins=100))]
fn free_volume<'py>(py: Python<'py>, positions: PyReadonlyArray2<'py, f64>, radii: PyReadonlyArray1<'py, f64>, box_length: [f64; 3],
                    probe: f64, spacing: f64, bin: f64, bins: usize) -> PyResult<Bound<'py, PyDict>> {
    let positions = positions.as_array();
    if positions.ncols() != 3 || positions.nrows() == 0 || positions.nrows() > 1_000_000 {
        return Err(PyValueError::new_err("positions must have shape (1..1000000, 3)"));
    }
    if positions.iter().any(|v| !v.is_finite()) {
        return Err(PyValueError::new_err("positions must be finite"));
    }
    if radii.as_array().len() != positions.nrows() {
        return Err(PyValueError::new_err("positions and radii must match"));
    }
    let p: Vec<[f64; 3]> = positions.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect();
    let r = radii.as_array().to_vec();
    if p.len() != r.len() || p.is_empty() || r.iter().any(|v| !(v.is_finite() && *v >= 0.0)) {
        return Err(PyValueError::new_err("positions and radii must match and radii be non-negative"));
    }
    if !(probe.is_finite() && probe >= 0.0 && spacing.is_finite() && spacing > 0.0 && bin.is_finite() && bin > 0.0)
        || bins == 0 || bins > 10_000 || box_length.iter().any(|l| !(l.is_finite() && *l > 0.0))
        || !box_length.iter().product::<f64>().is_normal() {
        return Err(PyValueError::new_err("finite probe >= 0, spacing > 0, bin > 0, bins 1..10000 and positive box edges are required"));
    }
    let dimensions = box_length.map(|l| (l / spacing).ceil().max(1.0));
    let points = dimensions.iter().product::<f64>();
    if !points.is_finite() || points > 20_000_000.0 {
        return Err(PyValueError::new_err("grid too fine: more than 20 million points"));
    }
    let stencil = (0..3).map(|k| 2.0 * (probe / (box_length[k] / dimensions[k])).floor() + 1.0).product::<f64>();
    if !stencil.is_finite() || stencil > 1_000_000.0 || points * stencil > 50_000_000_000.0 {
        return Err(PyValueError::new_err("probe/grid combination is too costly; use a smaller probe or coarser spacing"));
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
    if sigma.as_array().len() != n || epsilon.as_array().len() != n {
        return Err(PyValueError::new_err("sigma and epsilon must have one entry per atom"));
    }
    let (s, e) = (sigma.as_array().to_vec(), epsilon.as_array().to_vec());
    if s.len() != n || e.len() != n || s.iter().chain(&e).any(|v| !(v.is_finite() && *v >= 0.0))
        || !(probe_sigma.is_finite() && probe_sigma > 0.0 && probe_epsilon.is_finite() && probe_epsilon >= 0.0)
        || !(temperature.is_finite() && temperature > 0.0) || insertions == 0 || !(cutoff.is_finite() && cutoff > 0.0) {
        return Err(PyValueError::new_err("finite non-negative sigma/epsilon per atom, probe_sigma > 0, probe_epsilon >= 0, temperature > 0, insertions >= 1 and cutoff > 0 are required"));
    }
    if bx.iter().any(|l| cutoff > 0.5 * l.iter().cloned().fold(f64::INFINITY, f64::min)) {
        return Err(PyValueError::new_err("cutoff must be at most half the smallest box edge"));
    }
    if f.len() as u128 * insertions as u128 > 20_000_000 || f.len() as u128 * insertions as u128 * n as u128 > 20_000_000_000 {
        return Err(PyValueError::new_err("Widom sampling budget exceeded; use fewer frames, atoms or insertions"));
    }
    cell_list_budget(&bx, cutoff)?;
    let out = py.detach(|| widom::widom(&f, &bx, &s, &e, probe_sigma, probe_epsilon, temperature, insertions, cutoff, seed, tail));
    if out.iter().any(|v| !v.is_finite() || *v < 0.0) {
        return Err(PyValueError::new_err("Widom Boltzmann factors overflowed; check temperature and force-field parameters"));
    }
    Ok(out.into_pyarray(py))
}

#[pyfunction]
fn doe_design<'py>(py: Python<'py>, factors: usize, kind: &str, center_points: usize, replicates: usize, seed: u64) -> PyResult<Bound<'py, PyDict>> {
    let result = doe::design(factors, kind, center_points, replicates, seed).map_err(PyValueError::new_err)?;
    let out = PyDict::new(py);
    out.set_item("points", result.points)?;
    out.set_item("types", result.types)?;
    Ok(out)
}

#[pyfunction]
fn doe_fit<'py>(py: Python<'py>, coded: PyReadonlyArray2<'py, f64>, response: PyReadonlyArray1<'py, f64>, model: &str) -> PyResult<Bound<'py, PyDict>> {
    let input = coded.as_array();
    if input.nrows() > 20000 || !(2..=6).contains(&input.ncols()) {
        return Err(PyValueError::new_err("DOE input requires at most 20000 rows and 2..6 factors"));
    }
    if response.as_array().len() != input.nrows() {
        return Err(PyValueError::new_err("DOE response length must match the coded input rows"));
    }
    let points: Vec<Vec<f64>> = input.rows().into_iter().map(|r| r.to_vec()).collect();
    let y = response.as_array().to_vec();
    let result = py.detach(|| doe::fit(&points, &y, model)).map_err(PyValueError::new_err)?;
    let out = PyDict::new(py);
    out.set_item("coefficients", result.coefficients)?;
    out.set_item("standard_errors", result.standard_errors)?;
    out.set_item("fitted", result.fitted)?;
    out.set_item("residuals", result.residuals)?;
    out.set_item("sse", result.sse)?;
    out.set_item("sst", result.sst)?;
    out.set_item("condition_indicator", result.condition_indicator)?;
    out.set_item("degrees", result.degrees)?;
    out.set_item("rmse", result.rmse)?;
    out.set_item("r_squared", result.r_squared)?;
    out.set_item("adjusted_r_squared", result.adjusted_r_squared)?;
    out.set_item("exact_fit", result.exact_fit)?;
    out.set_item("constant_response", result.constant_response)?;
    let inference = PyList::empty(py);
    for coefficient in result.inference {
        let value = PyDict::new(py);
        value.set_item("t_statistic", coefficient.t_statistic)?;
        value.set_item("p_value", coefficient.p_value)?;
        value.set_item("confidence_interval", coefficient.confidence_interval)?;
        inference.append(value)?;
    }
    out.set_item("coefficients_inference", inference)?;
    let anova = PyDict::new(py);
    for (name, row) in [("regression", result.anova.regression), ("residual", result.anova.residual),
        ("total", result.anova.total), ("lack_of_fit", result.anova.lack_of_fit), ("pure_error", result.anova.pure_error)] {
        let value = PyDict::new(py);
        value.set_item("df", row.df)?;
        value.set_item("ss", row.ss)?;
        value.set_item("ms", row.ms)?;
        value.set_item("f_statistic", row.f_statistic)?;
        value.set_item("p_value", row.p_value)?;
        anova.set_item(name, value)?;
    }
    anova.set_item("replicate_groups", result.anova.replicate_groups)?;
    out.set_item("anova", anova)?;
    let diagnostics = PyDict::new(py);
    diagnostics.set_item("leverage", result.diagnostics.leverage)?;
    diagnostics.set_item("standardized_residuals", result.diagnostics.standardized_residuals)?;
    diagnostics.set_item("cooks_distance", result.diagnostics.cooks_distance)?;
    diagnostics.set_item("press", result.diagnostics.press)?;
    diagnostics.set_item("predicted_r_squared", result.diagnostics.predicted_r_squared)?;
    let unusual_rows = PyList::empty(py);
    for row in result.diagnostics.unusual_rows {
        let value = PyDict::new(py);
        value.set_item("row", row.row)?;
        value.set_item("high_leverage", row.high_leverage)?;
        value.set_item("large_residual", row.large_residual)?;
        value.set_item("influential", row.influential)?;
        unusual_rows.append(value)?;
    }
    diagnostics.set_item("unusual_rows", unusual_rows)?;
    out.set_item("diagnostics", diagnostics)?;
    Ok(out)
}

#[pyfunction]
fn doe_predict<'py>(py: Python<'py>, coded: PyReadonlyArray2<'py, f64>, response: PyReadonlyArray1<'py, f64>,
                   model: &str, prediction_points: PyReadonlyArray2<'py, f64>) -> PyResult<Bound<'py, PyList>> {
    let input = coded.as_array();
    let predictions = prediction_points.as_array();
    if input.nrows() > 20000 || !(2..=6).contains(&input.ncols()) {
        return Err(PyValueError::new_err("DOE input requires at most 20000 rows and 2..6 factors"));
    }
    if response.as_array().len() != input.nrows() {
        return Err(PyValueError::new_err("DOE response length must match the coded input rows"));
    }
    if predictions.nrows() == 0 || predictions.nrows() > 20000 || predictions.ncols() != input.ncols() {
        return Err(PyValueError::new_err("DOE prediction input requires 1..20000 rows and matching factor columns"));
    }
    let points: Vec<Vec<f64>> = input.rows().into_iter().map(|r| r.to_vec()).collect();
    let y = response.as_array().to_vec();
    let prediction_points: Vec<Vec<f64>> = predictions.rows().into_iter().map(|r| r.to_vec()).collect();
    let result = py.detach(|| doe::predict_intervals(&points, &y, model, &prediction_points)).map_err(PyValueError::new_err)?;
    let out = PyList::empty(py);
    for prediction in result {
        let value = PyDict::new(py);
        value.set_item("predicted_response", prediction.predicted_response)?;
        value.set_item("standard_error_mean", prediction.standard_error_mean)?;
        value.set_item("confidence_interval", prediction.confidence_interval)?;
        value.set_item("prediction_interval", prediction.prediction_interval)?;
        value.set_item("leverage", prediction.leverage)?;
        out.append(value)?;
    }
    Ok(out)
}

#[pyfunction]
#[pyo3(signature = (coefficients, factors, model, objective, target=None))]
fn doe_optimize<'py>(py: Python<'py>, coefficients: PyReadonlyArray1<'py, f64>, factors: usize, model: &str, objective: &str, target: Option<f64>) -> PyResult<Bound<'py, PyDict>> {
    let expected = doe::terms(factors, model).map_err(PyValueError::new_err)?;
    if coefficients.as_array().len() != expected {
        return Err(PyValueError::new_err("DOE coefficient length must match the model terms"));
    }
    let coefficients = coefficients.as_array().to_vec();
    let result = py.detach(|| doe::optimize(&coefficients, factors, model, objective, target)).map_err(PyValueError::new_err)?;
    let out = PyDict::new(py);
    out.set_item("coded", result.point)?;
    out.set_item("predicted_response", result.response)?;
    out.set_item("starts", result.starts)?;
    Ok(out)
}

fn region(lower: &[f64], upper: &[f64]) -> PyResult<mixture::Region> {
    mixture::Region::new(lower, upper).map_err(PyValueError::new_err)
}

/// mixture_region(lower, upper) -> (lower, upper, simplex): bounds tightened by the sum-to-one constraint.
#[pyfunction]
fn mixture_region(lower: Vec<f64>, upper: Vec<f64>) -> PyResult<(Vec<f64>, Vec<f64>, bool)> {
    let r = region(&lower, &upper)?;
    let simplex = r.is_simplex();
    Ok((r.lower, r.upper, simplex))
}

/// mixture_design(lower, upper, kind, degree=2, axial=False, centroid=True, replicates=1, seed=1)
/// -> dict(points, types, lower, upper, simplex); points are real proportions summing to one.
#[pyfunction]
#[pyo3(signature = (lower, upper, kind, degree=2, axial=false, centroid=true, replicates=1, seed=1))]
fn mixture_design<'py>(py: Python<'py>, lower: Vec<f64>, upper: Vec<f64>, kind: &str, degree: usize, axial: bool, centroid: bool,
                       replicates: usize, seed: u64) -> PyResult<Bound<'py, PyDict>> {
    let r = region(&lower, &upper)?;
    let result = mixture::design(&r, kind, degree, axial, centroid, replicates, seed).map_err(PyValueError::new_err)?;
    let out = PyDict::new(py);
    out.set_item("points", result.points)?;
    out.set_item("types", result.types)?;
    out.set_item("simplex", r.is_simplex())?;
    out.set_item("lower", r.lower)?;
    out.set_item("upper", r.upper)?;
    Ok(out)
}

/// mixture_optimize(lower, upper, coefficients, model, objective, target=None) -> dict(point, pseudo, predicted_response, starts)
/// Coefficients are a Scheffé model in L-pseudocomponents of the region.
#[pyfunction]
#[pyo3(signature = (lower, upper, coefficients, model, objective, target=None))]
fn mixture_optimize<'py>(py: Python<'py>, lower: Vec<f64>, upper: Vec<f64>, coefficients: Vec<f64>, model: String, objective: &str,
                         target: Option<f64>) -> PyResult<Bound<'py, PyDict>> {
    let r = region(&lower, &upper)?;
    let response = mixture::Response { coefficients, model };
    let domain = mixture::Domain::Mixture(r.clone());
    mixture::check_response(&domain, &response).map_err(PyValueError::new_err)?;
    let aim = match (objective, target) {
        ("maximize" | "minimize", None) => 0.0,
        ("target", Some(t)) if t.is_finite() => t,
        _ => return Err(PyValueError::new_err("objective is maximize, minimize, or target with a finite target")),
    };
    let score = |x: &[f64]| {
        let y = domain.predict(&response, x);
        match objective { "maximize" => y, "minimize" => -y, _ => -(y - aim).abs() }
    };
    let best = py.detach(|| mixture::optimize_region(&r, &score));
    let out = PyDict::new(py);
    out.set_item("predicted_response", domain.predict(&response, &best.point))?;
    out.set_item("pseudo", r.to_pseudo(&best.point))?;
    out.set_item("point", best.point)?;
    out.set_item("starts", best.starts)?;
    Ok(out)
}

type GoalSpec = (String, f64, f64, f64, f64, f64, f64);

fn goals(specs: &[GoalSpec]) -> PyResult<Vec<mixture::Goal>> {
    specs.iter().map(|(kind, lower, target, upper, s, t, importance)|
        mixture::Goal::new(kind, *lower, *target, *upper, *s, *t, *importance).map_err(PyValueError::new_err)).collect()
}

/// desirability(goals, values) -> (individual, composite); goal = (kind, lower, target, upper, lower_shape, upper_shape, importance).
#[pyfunction]
fn desirability(goal_specs: Vec<GoalSpec>, values: Vec<f64>) -> PyResult<(Vec<f64>, f64)> {
    let g = goals(&goal_specs)?;
    if g.len() != values.len() {
        return Err(PyValueError::new_err("one value per goal is required"));
    }
    Ok(mixture::composite(&g, &values))
}

/// desirability_optimize(domain, models, goals, factors=0, lower=[], upper=[]) -> dict(point, values, individual, composite, starts)
/// domain "cube" searches coded [-1, 1]^factors; "mixture" searches the bounded mixture region.
#[pyfunction]
#[pyo3(signature = (domain, models, goal_specs, factors=0, lower=Vec::new(), upper=Vec::new()))]
fn desirability_optimize<'py>(py: Python<'py>, domain: &str, models: Vec<(Vec<f64>, String)>, goal_specs: Vec<GoalSpec>, factors: usize,
                              lower: Vec<f64>, upper: Vec<f64>) -> PyResult<Bound<'py, PyDict>> {
    let domain = match domain {
        "cube" => mixture::Domain::Cube(factors),
        "mixture" => mixture::Domain::Mixture(region(&lower, &upper)?),
        _ => return Err(PyValueError::new_err("domain is cube or mixture")),
    };
    let responses: Vec<mixture::Response> = models.into_iter().map(|(coefficients, model)| mixture::Response { coefficients, model }).collect();
    let g = goals(&goal_specs)?;
    let best = py.detach(|| mixture::optimize_desirability(&domain, &responses, &g)).map_err(PyValueError::new_err)?;
    let out = PyDict::new(py);
    out.set_item("point", best.point)?;
    out.set_item("values", best.values)?;
    out.set_item("individual", best.individual)?;
    out.set_item("composite", best.composite)?;
    out.set_item("starts", best.starts)?;
    Ok(out)
}

/// connectivity_indices(atomic_numbers, hydrogens, bonds) -> dict(chi0, chi1, chi0v, chi1v, delta, delta_v)
#[pyfunction]
fn connectivity_indices<'py>(py: Python<'py>, atomic_numbers: Vec<u32>, hydrogens: Vec<u32>, bonds: Vec<(usize, usize)>) -> PyResult<Bound<'py, PyDict>> {
    let result = topology::indices(&atomic_numbers, &hydrogens, &bonds).map_err(PyValueError::new_err)?;
    let out = PyDict::new(py);
    out.set_item("chi0", result.chi0)?;
    out.set_item("chi1", result.chi1)?;
    out.set_item("chi0v", result.chi0v)?;
    out.set_item("chi1v", result.chi1v)?;
    out.set_item("delta", result.delta)?;
    out.set_item("delta_v", result.delta_v)?;
    Ok(out)
}

/// block_atoms(n, bonds, closure) -> list[bool]: atoms of the biconnected component holding bond `closure`.
#[pyfunction]
fn block_atoms(n: usize, bonds: Vec<(usize, usize)>, closure: usize) -> PyResult<Vec<bool>> {
    topology::block_of(n, &bonds, closure).map_err(PyValueError::new_err)
}

/// Descriptive summary of independent replicate densities; no validation decision is made here.
#[pyfunction]
#[pyo3(signature = (values, reference=None, reference_standard_uncertainty=None))]
fn replicate_summary<'py>(py: Python<'py>, values: PyReadonlyArray1<'py, f64>, reference: Option<Bound<'py, PyAny>>,
                          reference_standard_uncertainty: Option<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyDict>> {
    if values.as_array().is_empty() || values.as_array().len() > 1000 {
        return Err(PyValueError::new_err("Expected 1..1000 replicate densities"));
    }
    let number = |value: Option<Bound<'py, PyAny>>| -> PyResult<Option<f64>> {
        value.map(|v| {
            if v.is_instance_of::<pyo3::types::PyBool>() {
                return Err(PyValueError::new_err("Boolean reference values are not numbers"));
            }
            v.extract::<f64>()
        }).transpose()
    };
    let reference = number(reference)?;
    let uncertainty = number(reference_standard_uncertainty)?;
    let input = values.as_array().to_vec();
    let summary = py.detach(|| reliability::summarize(&input, reference, uncertainty)).map_err(PyValueError::new_err)?;
    let out = PyDict::new(py);
    out.set_item("schema_version", 1)?;
    out.set_item("n", summary.n)?;
    out.set_item("mean", summary.mean)?;
    out.set_item("min", summary.min)?;
    out.set_item("max", summary.max)?;
    out.set_item("sample_sd", summary.sample_sd)?;
    out.set_item("standard_error", summary.standard_error)?;
    out.set_item("confidence_interval", summary.confidence_interval)?;
    out.set_item("relative_sd", summary.relative_sd)?;
    out.set_item("confidence", 0.95)?;
    out.set_item("unit", "g/cm^3")?;
    out.set_item("method", "Welford sample variance and Student-t interval for independent replicate means")?;
    out.set_item("warnings", summary.warnings)?;
    match summary.reference_comparison {
        Some(comparison) => {
            let value = PyDict::new(py);
            value.set_item("reference", comparison.reference)?;
            value.set_item("value_difference", comparison.value_difference)?;
            value.set_item("relative_difference", comparison.relative_difference)?;
            value.set_item("combined_standard_uncertainty", comparison.combined_standard_uncertainty)?;
            value.set_item("standardized_difference", comparison.standardized_difference)?;
            out.set_item("reference_comparison", value)?;
        },
        None => out.set_item("reference_comparison", py.None())?,
    }
    Ok(out)
}

#[pymodule]
fn workbench_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<ExecutionQueue>()?;
    m.add_function(wrap_pyfunction!(replicate_summary, m)?)?;
    m.add_function(wrap_pyfunction!(contact_energies, m)?)?;
    m.add_function(wrap_pyfunction!(coordination_numbers, m)?)?;
    m.add_function(wrap_pyfunction!(sample_contact, m)?)?;
    m.add_function(wrap_pyfunction!(rdf_counts, m)?)?;
    m.add_function(wrap_pyfunction!(msd, m)?)?;
    m.add_function(wrap_pyfunction!(free_volume, m)?)?;
    m.add_function(wrap_pyfunction!(widom_insertion, m)?)?;
    m.add_function(wrap_pyfunction!(doe_design, m)?)?;
    m.add_function(wrap_pyfunction!(doe_fit, m)?)?;
    m.add_function(wrap_pyfunction!(doe_predict, m)?)?;
    m.add_function(wrap_pyfunction!(doe_optimize, m)?)?;
    m.add_function(wrap_pyfunction!(connectivity_indices, m)?)?;
    m.add_function(wrap_pyfunction!(mixture_design, m)?)?;
    m.add_function(wrap_pyfunction!(mixture_region, m)?)?;
    m.add_function(wrap_pyfunction!(mixture_optimize, m)?)?;
    m.add_function(wrap_pyfunction!(desirability, m)?)?;
    m.add_function(wrap_pyfunction!(desirability_optimize, m)?)?;
    m.add_function(wrap_pyfunction!(block_atoms, m)?)?;
    m.add("R_KCAL", widom::R_KCAL)?;
    m.add("COULOMB", COULOMB)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}

#[pyclass]
struct ExecutionQueue { inner: crate::execution::ExecutionQueue }

#[pymethods]
impl ExecutionQueue {
    #[new]
    fn new() -> Self { Self { inner: crate::execution::ExecutionQueue::new() } }
    fn enqueue(&mut self, id: String) -> PyResult<()> {
        self.inner.enqueue(id).map_err(PyValueError::new_err)
    }
    fn acquire(&mut self, id: &str) -> bool { self.inner.acquire(id) }
    fn release(&mut self, id: &str) { self.inner.release(id); }
}
