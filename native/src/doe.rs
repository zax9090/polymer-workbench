//! Two-level experimental designs, pivoted-QR response surfaces and bounded candidate search.
//! Designs are face-centred (alpha = 1), not rotatable central composite designs.
use crate::rng::Rng;
use crate::statistics::{f_survival, student_critical_95, student_two_sided};
use std::collections::BTreeMap;

pub fn terms(k: usize, model: &str) -> Result<usize, String> {
    if !(2..=6).contains(&k) {
        return Err("DOE requires 2 to 6 factors".into());
    }
    let pairs = k * (k - 1) / 2;
    match model {
        "linear" => Ok(1 + k),
        "interaction" => Ok(1 + k + pairs),
        "quadratic" if k <= 4 => Ok(1 + 2 * k + pairs),
        // Scheffé mixture polynomials have no intercept: the components sum to one.
        "scheffe_linear" => Ok(k),
        "scheffe_quadratic" => Ok(k + pairs),
        "scheffe_special_cubic" if k >= 3 => Ok(k + pairs + k * (k - 1) * (k - 2) / 6),
        _ => Err("Unknown model, more than 4 factors for a quadratic model, or fewer than 3 components for a special cubic".into()),
    }
}

pub fn features(point: &[f64], model: &str) -> Vec<f64> {
    if let Some(order) = model.strip_prefix("scheffe_") {
        let mut out = point.to_vec();
        if order != "linear" {
            for i in 0..point.len() {
                for j in i + 1..point.len() {
                    out.push(point[i] * point[j]);
                }
            }
        }
        if order == "special_cubic" {
            for i in 0..point.len() {
                for j in i + 1..point.len() {
                    for l in j + 1..point.len() {
                        out.push(point[i] * point[j] * point[l]);
                    }
                }
            }
        }
        return out;
    }
    let mut out = vec![1.0];
    out.extend_from_slice(point);
    if model != "linear" {
        for i in 0..point.len() {
            for j in i + 1..point.len() {
                out.push(point[i] * point[j]);
            }
        }
    }
    if model == "quadratic" {
        out.extend(point.iter().map(|x| x * x));
    }
    out
}

pub struct Design {
    pub points: Vec<Vec<f64>>,
    pub types: Vec<&'static str>,
}

pub fn design(k: usize, kind: &str, centres: usize, replicates: usize, seed: u64) -> Result<Design, String> {
    terms(k, "linear")?;
    if !(1..=10).contains(&replicates) || centres > 20 {
        return Err("Replicates must be 1..10 and centre points 0..20".into());
    }
    if kind != "factorial" && kind != "central_composite" {
        return Err("Unknown design kind".into());
    }
    if kind == "central_composite" && (k > 4 || centres == 0) {
        return Err("Face-centred CCD requires 2..4 factors and at least one centre point".into());
    }
    let base = (1usize << k) + if kind == "central_composite" { 2 * k } else { 0 };
    if (base + centres) * replicates > 1000 {
        return Err("Design exceeds 1000 runs".into());
    }
    let mut points = Vec::new();
    let mut types = Vec::new();
    for _ in 0..replicates {
        for mask in 0..1usize << k {
            points.push((0..k).map(|j| if mask & (1 << j) == 0 { -1.0 } else { 1.0 }).collect());
            types.push("factorial");
        }
        if kind == "central_composite" {
            for j in 0..k {
                for level in [-1.0, 1.0] {
                    let mut point = vec![0.0; k];
                    point[j] = level;
                    points.push(point);
                    types.push("axial");
                }
            }
        }
        for _ in 0..centres {
            points.push(vec![0.0; k]);
            types.push("centre");
        }
    }
    let mut rng = Rng::new(seed, 0x444f45);
    for i in (1..points.len()).rev() {
        let j = (rng.uniform() * (i + 1) as f64).floor() as usize;
        points.swap(i, j);
        types.swap(i, j);
    }
    Ok(Design { points, types })
}

pub struct Fit {
    pub coefficients: Vec<f64>,
    pub standard_errors: Vec<f64>,
    pub fitted: Vec<f64>,
    pub residuals: Vec<f64>,
    pub sse: f64,
    pub sst: f64,
    pub condition_indicator: f64,
    pub degrees: usize,
    pub rmse: f64,
    pub r_squared: Option<f64>,
    pub adjusted_r_squared: Option<f64>,
    pub exact_fit: bool,
    pub constant_response: bool,
    pub inference: Vec<CoefficientInference>,
    pub anova: Anova,
    pub diagnostics: Diagnostics,
    covariance_factor: Vec<Vec<f64>>,
}

pub struct CoefficientInference {
    pub t_statistic: Option<f64>,
    pub p_value: Option<f64>,
    pub confidence_interval: [f64; 2],
}

pub struct AnovaRow {
    pub df: usize,
    pub ss: f64,
    pub ms: Option<f64>,
    pub f_statistic: Option<f64>,
    pub p_value: Option<f64>,
}

pub struct Anova {
    pub regression: AnovaRow,
    pub residual: AnovaRow,
    pub total: AnovaRow,
    pub lack_of_fit: AnovaRow,
    pub pure_error: AnovaRow,
    pub replicate_groups: usize,
}

pub struct UnusualRow {
    pub row: usize,
    pub high_leverage: bool,
    pub large_residual: bool,
    pub influential: bool,
}

pub struct Diagnostics {
    pub leverage: Vec<f64>,
    pub standardized_residuals: Vec<Option<f64>>,
    pub cooks_distance: Vec<Option<f64>>,
    pub press: Option<f64>,
    pub predicted_r_squared: Option<f64>,
    pub unusual_rows: Vec<UnusualRow>,
}

fn finite(value: f64) -> Option<f64> { value.is_finite().then_some(value) }

fn row_variance(features: &[f64], factor: &[Vec<f64>]) -> f64 {
    (0..features.len()).map(|column| features.iter().zip(factor)
        .map(|(x, row)| x * row[column]).sum::<f64>().powi(2)).sum()
}

fn anova_row(df: usize, ss: f64, denominator: Option<(f64, usize)>) -> AnovaRow {
    let ms = (df > 0).then(|| ss / df as f64);
    let statistic = ms.zip(denominator).and_then(|(ms, (error, degrees))|
        if error > 0.0 && degrees > 0 { finite(ms / error) } else { None });
    let p_value = statistic.zip(denominator).map(|(f, (_, degrees))| f_survival(f, df, degrees));
    AnovaRow { df, ss, ms, f_statistic: statistic, p_value }
}

pub fn fit(points: &[Vec<f64>], y: &[f64], model: &str) -> Result<Fit, String> {
    let n = points.len();
    let k = points.first().map_or(0, Vec::len);
    let p = terms(k, model)?;
    if n <= p || n > 20000 || y.len() != n || y.iter().any(|v| !v.is_finite() || v.abs() > 1e100)
        || points.iter().any(|row| row.len() != k || row.iter().any(|v| !v.is_finite() || v.abs() > 1.0 + 1e-10)) {
        return Err("Finite coded factors in [-1,1], matching responses and positive residual degrees of freedom are required".into());
    }
    let original: Vec<Vec<f64>> = points.iter().map(|row| features(row, model)).collect();
    let mut a = original.clone();
    let scales: Vec<f64> = (0..p).map(|j| a.iter().map(|row| row[j] * row[j]).sum::<f64>().sqrt()).collect();
    if scales.iter().any(|s| *s == 0.0) {
        return Err("Design is rank deficient: a model term is zero for every run".into());
    }
    for row in &mut a {
        for j in 0..p { row[j] /= scales[j]; }
    }
    let mut rhs = y.to_vec();
    let mut permutation: Vec<usize> = (0..p).collect();
    let tolerance = 64.0 * f64::EPSILON * n.max(p) as f64;
    let mut diagonals = Vec::new();
    // Householder QR with column pivoting; never form X'X to solve the fit.
    for j in 0..p {
        let pivot = (j..p).max_by(|&u, &v| {
            let su: f64 = a[j..].iter().map(|row| row[u] * row[u]).sum();
            let sv: f64 = a[j..].iter().map(|row| row[v] * row[v]).sum();
            su.total_cmp(&sv)
        }).unwrap();
        for row in &mut a { row.swap(j, pivot); }
        permutation.swap(j, pivot);
        let norm = a[j..].iter().map(|row| row[j] * row[j]).sum::<f64>().sqrt();
        if norm <= tolerance {
            return Err("Design is rank deficient or numerically indistinguishable; add independent runs or use a simpler model".into());
        }
        let diagonal = -norm.copysign(a[j][j]);
        let mut householder: Vec<f64> = a[j..].iter().map(|row| row[j]).collect();
        householder[0] -= diagonal;
        let divisor: f64 = householder.iter().map(|v| v * v).sum();
        for col in j..p {
            let dot: f64 = householder.iter().zip(&a[j..]).map(|(v, row)| v * row[col]).sum();
            for (i, v) in householder.iter().enumerate() { a[j + i][col] -= 2.0 * dot / divisor * v; }
        }
        let dot: f64 = householder.iter().zip(&rhs[j..]).map(|(v, b)| v * b).sum();
        for (i, v) in householder.iter().enumerate() { rhs[j + i] -= 2.0 * dot / divisor * v; }
        a[j][j] = diagonal;
        for row in &mut a[j + 1..] { row[j] = 0.0; }
        diagonals.push(diagonal.abs());
    }
    let mut solved = vec![0.0; p];
    for i in (0..p).rev() {
        let other: f64 = (i + 1..p).map(|j| a[i][j] * solved[j]).sum();
        solved[i] = (rhs[i] - other) / a[i][i];
    }
    let mut coefficients = vec![0.0; p];
    for i in 0..p { coefficients[permutation[i]] = solved[i] / scales[permutation[i]]; }
    let fitted: Vec<f64> = original.iter().map(|row| row.iter().zip(&coefficients).map(|(x, b)| x * b).sum()).collect();
    let residuals: Vec<f64> = y.iter().zip(&fitted).map(|(a, b)| a - b).collect();
    let sse: f64 = residuals.iter().map(|r| r * r).sum();
    let mean = y.iter().sum::<f64>() / n as f64;
    let sst: f64 = y.iter().map(|v| (v - mean).powi(2)).sum();
    let variance = sse / (n - p) as f64;
    let mut inverse = vec![vec![0.0; p]; p];
    for col in 0..p {
        for i in (0..p).rev() {
            let other: f64 = (i + 1..p).map(|j| a[i][j] * inverse[j][col]).sum();
            inverse[i][col] = ((if i == col { 1.0 } else { 0.0 }) - other) / a[i][i];
        }
    }
    let mut covariance_factor = vec![vec![0.0; p]; p];
    let mut standard_errors = vec![0.0; p];
    for i in 0..p {
        let v: f64 = inverse[i].iter().map(|x| x * x).sum();
        standard_errors[permutation[i]] = (variance * v).sqrt() / scales[permutation[i]];
        for j in 0..p { covariance_factor[permutation[i]][j] = inverse[i][j] / scales[permutation[i]]; }
    }
    let condition_indicator = diagonals.iter().copied().fold(0.0, f64::max)
        / diagonals.iter().copied().fold(f64::INFINITY, f64::min);
    if coefficients.iter().chain(&standard_errors).chain(&fitted).chain(&residuals).any(|v| !v.is_finite()) || !sse.is_finite() || !sst.is_finite() {
        return Err("Fit overflowed; rescale the response values".into());
    }
    let degrees = n - p;
    let constant_response = y.iter().all(|v| *v == y[0]);
    let response_scale = y.iter().map(|v| v.abs()).fold(0.0, f64::max);
    let exact_fit = sse <= (64.0 * f64::EPSILON * response_scale).powi(2) * n as f64;
    let r_squared = (!constant_response).then(|| 1.0 - sse / sst).and_then(finite);
    let adjusted_r_squared = r_squared.map(|r| 1.0 - (1.0 - r) * (n - 1) as f64 / degrees as f64);
    let critical = student_critical_95(degrees);
    let inference = coefficients.iter().zip(&standard_errors).map(|(&value, &se)| {
        let statistic = if !exact_fit && !constant_response && se > 0.0 { finite(value / se) } else { None };
        CoefficientInference { t_statistic: statistic,
            p_value: statistic.map(|t| student_two_sided(t, degrees)),
            confidence_interval: [value - critical * se, value + critical * se] }
    }).collect();

    // Exact repeated coded coordinates define replicate groups; no tolerance-based merging.
    let mut groups: BTreeMap<Vec<u64>, (usize, f64, f64)> = BTreeMap::new();
    for (point, &response) in points.iter().zip(y) {
        let key = point.iter().map(|v| if *v == 0.0 { 0 } else { v.to_bits() }).collect();
        let entry = groups.entry(key).or_insert((0, 0.0, 0.0));
        entry.0 += 1;
        let delta = response - entry.1;
        entry.1 += delta / entry.0 as f64;
        entry.2 += delta * (response - entry.1);
    }
    let pure_ss = groups.values().map(|g| g.2.max(0.0)).sum::<f64>();
    let pure_df = n - groups.len();
    let lack_df = groups.len() - p;
    let lack_ss = if lack_df == 0 { 0.0 } else { (sse - pure_ss).max(0.0) };
    let error_denominator = (!exact_fit && !constant_response).then_some((variance, degrees));
    let pure_denominator = (pure_df > 0 && !exact_fit).then(|| (pure_ss / pure_df as f64, pure_df));
    let anova = Anova {
        regression: anova_row(p - 1, (sst - sse).max(0.0), error_denominator),
        residual: anova_row(degrees, sse, None),
        total: anova_row(n - 1, sst, None),
        lack_of_fit: anova_row(lack_df, lack_ss, pure_denominator),
        pure_error: anova_row(pure_df, pure_ss, None),
        replicate_groups: groups.values().filter(|g| g.0 > 1).count(),
    };

    let leverage: Vec<f64> = original.iter().map(|row| row_variance(row, &covariance_factor).clamp(0.0, 1.0)).collect();
    let standardized_residuals: Vec<Option<f64>> = residuals.iter().zip(&leverage).map(|(&r, &h)| {
        if exact_fit || constant_response || 1.0 - h <= 1e-10 { None }
        else { finite(r / (variance * (1.0 - h)).sqrt()) }
    }).collect();
    let cooks_distance: Vec<Option<f64>> = standardized_residuals.iter().zip(&leverage)
        .map(|(r, &h)| r.and_then(|r| finite(r * r * h / (p as f64 * (1.0 - h))))).collect();
    let press = if leverage.iter().any(|h| 1.0 - h <= 1e-10) { None }
        else { finite(residuals.iter().zip(&leverage).map(|(r, h)| (r / (1.0 - h)).powi(2)).sum()) };
    let predicted_r_squared = if constant_response { None } else { press.and_then(|v| finite(1.0 - v / sst)) };
    let unusual_rows = (0..n).filter_map(|i| {
        let high_leverage = leverage[i] > 2.0 * p as f64 / n as f64;
        let large_residual = standardized_residuals[i].is_some_and(|r| r.abs() > 2.0);
        let influential = cooks_distance[i].is_some_and(|d| d > 4.0 / n as f64);
        (high_leverage || large_residual || influential).then_some(UnusualRow { row: i + 1, high_leverage, large_residual, influential })
    }).collect();
    let diagnostics = Diagnostics { leverage, standardized_residuals, cooks_distance, press, predicted_r_squared, unusual_rows };
    Ok(Fit { coefficients, standard_errors, fitted, residuals, sse, sst, condition_indicator,
        degrees, rmse: variance.sqrt(), r_squared, adjusted_r_squared, exact_fit, constant_response,
        inference, anova, diagnostics, covariance_factor })
}

pub struct Prediction {
    pub predicted_response: f64,
    pub standard_error_mean: f64,
    pub confidence_interval: [f64; 2],
    pub prediction_interval: [f64; 2],
    pub leverage: f64,
}

pub fn predict_intervals(points: &[Vec<f64>], y: &[f64], model: &str, prediction_points: &[Vec<f64>]) -> Result<Vec<Prediction>, String> {
    let k = points.first().map_or(0, Vec::len);
    if prediction_points.is_empty() || prediction_points.len() > 20000 || prediction_points.iter().any(|row|
        row.len() != k || row.iter().any(|v| !v.is_finite() || v.abs() > 1.0 + 1e-10)) {
        return Err("Prediction requires 1..20000 finite coded rows inside [-1,1], matching the fitted factors".into());
    }
    let result = fit(points, y, model)?;
    let critical = student_critical_95(result.degrees);
    prediction_points.iter().map(|point| {
        let features = features(point, model);
        let mean: f64 = features.iter().zip(&result.coefficients).map(|(a, b)| a * b).sum();
        let leverage = row_variance(&features, &result.covariance_factor);
        let standard_error_mean = result.rmse * leverage.sqrt();
        let mean_margin = critical * standard_error_mean;
        let observation_margin = critical * result.rmse * (1.0 + leverage).sqrt();
        if [mean, leverage, standard_error_mean, mean_margin, observation_margin].iter().any(|v| !v.is_finite()) {
            return Err("Prediction overflowed; rescale responses or choose a simpler model".into());
        }
        Ok(Prediction { predicted_response: mean, standard_error_mean,
            confidence_interval: [mean - mean_margin, mean + mean_margin],
            prediction_interval: [mean - observation_margin, mean + observation_margin], leverage })
    }).collect()
}

fn predict(point: &[f64], coefficients: &[f64], model: &str) -> f64 {
    features(point, model).iter().zip(coefficients).map(|(a, b)| a * b).sum()
}

pub struct Candidate { pub point: Vec<f64>, pub response: f64, pub starts: usize }

pub fn optimize(coefficients: &[f64], k: usize, model: &str, objective: &str, target: Option<f64>) -> Result<Candidate, String> {
    if model.starts_with("scheffe_") {
        return Err("Scheffé mixture models are optimized over the mixture region (mixture::optimize_region)".into());
    }
    let p = terms(k, model)?;
    if coefficients.len() != p || coefficients.iter().any(|v| !v.is_finite() || v.abs() > 1e120)
        || !["maximize", "minimize", "target"].contains(&objective)
        || (objective == "target" && !target.is_some_and(|v| v.is_finite() && v.abs() <= 1e100)) {
        return Err("Invalid response surface or optimization objective".into());
    }
    let target = target.unwrap_or(0.0);
    let score = |y: f64| match objective { "maximize" => y, "minimize" => -y, _ => -(y - target).abs() };
    let mut starts = vec![vec![0.0; k]];
    for mask in 0..1usize << k {
        starts.push((0..k).map(|i| if mask & (1 << i) == 0 { -1.0 } else { 1.0 }).collect());
    }
    let mut rng = Rng::new(1729, 0x4f5054);
    for _ in 0..32 { starts.push((0..k).map(|_| 2.0 * rng.uniform() - 1.0).collect()); }
    let mut best = starts[0].clone();
    let mut best_value = predict(&best, coefficients, model);
    for initial in &starts {
        let mut x = initial.clone();
        for _ in 0..100 {
            let before = x.clone();
            for i in 0..k {
                x[i] = 0.0;
                let centre = predict(&x, coefficients, model);
                x[i] = 1.0;
                let plus = predict(&x, coefficients, model);
                x[i] = -1.0;
                let minus = predict(&x, coefficients, model);
                let linear = 0.5 * (plus - minus);
                let quadratic = 0.5 * (plus + minus) - centre;
                let mut choices = vec![-1.0, 1.0, before[i]];
                if quadratic != 0.0 {
                    choices.push(-linear / (2.0 * quadratic));
                    if objective == "target" {
                        let discriminant = linear * linear - 4.0 * quadratic * (centre - target);
                        if discriminant >= 0.0 {
                            let root = discriminant.sqrt();
                            let q = -0.5 * (linear + root.copysign(linear));
                            if q != 0.0 { choices.extend([q / quadratic, (centre - target) / q]); }
                            else { choices.push(-linear / (2.0 * quadratic)); }
                        }
                    }
                } else if objective == "target" && linear != 0.0 {
                    choices.push((target - centre) / linear);
                }
                let mut chosen = before[i];
                x[i] = chosen;
                let mut value = score(predict(&x, coefficients, model));
                for t in choices.into_iter().filter(|v| v.is_finite() && *v >= -1.0 && *v <= 1.0) {
                    x[i] = t;
                    let candidate_score = score(predict(&x, coefficients, model));
                    if candidate_score > value { chosen = t; value = candidate_score; }
                }
                x[i] = chosen;
            }
            if x.iter().zip(&before).all(|(a, b)| (a - b).abs() < 1e-10) { break; }
        }
        let value = predict(&x, coefficients, model);
        if score(value) > score(best_value) { best = x; best_value = value; }
    }
    if !best_value.is_finite() { return Err("Optimization overflowed".into()); }
    Ok(Candidate { point: best, response: best_value, starts: starts.len() })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ccd_fits_and_optimizes_known_quadratic() {
        let d = design(2, "central_composite", 3, 1, 8).unwrap();
        let y: Vec<f64> = d.points.iter().map(|p| 10.0 - (p[0] - 0.25).powi(2) - 2.0 * (p[1] + 0.5).powi(2)).collect();
        let f = fit(&d.points, &y, "quadratic").unwrap();
        let target = [9.4375, 0.5, -2.0, 0.0, -1.0, -2.0];
        for (a, b) in f.coefficients.iter().zip(target) { assert!((a - b).abs() < 1e-12); }
        let optimum = optimize(&f.coefficients, 2, "quadratic", "maximize", None).unwrap();
        assert!((optimum.point[0] - 0.25).abs() < 1e-8);
        assert!((optimum.point[1] + 0.5).abs() < 1e-8);
        assert!((optimum.response - 10.0).abs() < 1e-12);
    }

    #[test]
    fn factorial_quadratic_is_rank_deficient() {
        let d = design(2, "factorial", 3, 1, 2).unwrap();
        assert!(fit(&d.points, &vec![1.0; d.points.len()], "quadratic").is_err());
    }

    #[test]
    fn replicated_factorial_partitions_pure_error_and_predicts_intervals() {
        let d = design(2, "factorial", 2, 2, 5).unwrap();
        let mut groups: BTreeMap<Vec<u64>, usize> = BTreeMap::new();
        let y: Vec<f64> = d.points.iter().map(|point| {
            let key = point.iter().map(|v| v.to_bits()).collect();
            let count = groups.entry(key).or_insert(0);
            *count += 1;
            3.0 + 2.0 * point[0] - point[1] + if *count % 2 == 0 { 0.2 } else { -0.2 }
        }).collect();
        let fit = fit(&d.points, &y, "linear").unwrap();
        assert!((fit.anova.pure_error.ss - 0.48).abs() < 1e-12);
        assert_eq!(fit.anova.pure_error.df, 7);
        assert_eq!(fit.anova.lack_of_fit.df, 2);
        assert_eq!(fit.anova.replicate_groups, 5);
        assert!(fit.anova.lack_of_fit.ss < 1e-12);
        assert!((fit.diagnostics.leverage.iter().sum::<f64>() - 3.0).abs() < 1e-12);
        assert!(fit.diagnostics.press.unwrap() > fit.sse);
        let prediction = predict_intervals(&d.points, &y, "linear", &[vec![0.0, 0.0]]).unwrap();
        let prediction = &prediction[0];
        assert!((prediction.predicted_response - 3.0).abs() < 1e-12);
        assert!((prediction.leverage - 1.0 / 12.0).abs() < 1e-12);
        assert!(prediction.prediction_interval[1] > prediction.confidence_interval[1]);
    }

    #[test]
    fn leverage_one_does_not_claim_leave_one_out_validation() {
        let points = vec![vec![-1.0, -1.0], vec![1.0, -1.0], vec![0.0, 1.0], vec![0.0, 1.0]];
        let fit = fit(&points, &[1.0, 2.0, 3.0, 4.0], "linear").unwrap();
        assert!(fit.diagnostics.press.is_none());
        assert!(fit.diagnostics.predicted_r_squared.is_none());
        assert!(fit.diagnostics.standardized_residuals[0].is_none());
    }
}
