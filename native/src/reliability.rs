//! Descriptive uncertainty of independent replicate densities, without scientific pass/fail claims.
//!
//! Welford (1962), Technometrics 4(3), 419–420, corrected sums of squares;
//! Student (1908), Biometrika 6(1), 1–25, t interval for a normal-population mean.
//! The reference comparison combines independent standard uncertainties in quadrature.
//! It does not establish experimental agreement, equilibrium or replicate independence.
use crate::statistics::student_critical_95;

const MIN_DENSITY: f64 = 1e-12;
const MAX_DENSITY: f64 = 1e6;

pub struct ReferenceComparison {
    pub reference: f64,
    pub value_difference: f64,
    pub relative_difference: f64,
    pub combined_standard_uncertainty: Option<f64>,
    pub standardized_difference: Option<f64>,
}

pub struct Summary {
    pub n: usize,
    pub mean: f64,
    pub min: f64,
    pub max: f64,
    pub sample_sd: Option<f64>,
    pub standard_error: Option<f64>,
    pub confidence_interval: Option<[f64; 2]>,
    pub relative_sd: Option<f64>,
    pub reference_comparison: Option<ReferenceComparison>,
    pub warnings: Vec<&'static str>,
}

fn valid_density(value: f64) -> bool {
    value.is_finite() && (MIN_DENSITY..=MAX_DENSITY).contains(&value)
}

pub fn summarize(values: &[f64], reference: Option<f64>, reference_standard_uncertainty: Option<f64>) -> Result<Summary, String> {
    if values.is_empty() || values.len() > 1000 || values.iter().any(|&v| !valid_density(v)) {
        return Err("Expected 1..1000 finite positive densities between 1e-12 and 1e6 g/cm^3".into());
    }
    if reference.is_some_and(|v| !valid_density(v)) {
        return Err("Reference density must be finite and between 1e-12 and 1e6 g/cm^3".into());
    }
    if reference_standard_uncertainty.is_some_and(|v| !v.is_finite() || !(0.0..=MAX_DENSITY).contains(&v)) {
        return Err("Reference standard uncertainty must be finite and between 0 and 1e6 g/cm^3".into());
    }
    if reference.is_none() && reference_standard_uncertainty.is_some() {
        return Err("Reference standard uncertainty requires a reference density".into());
    }
    let (mut mean, mut m2, mut min, mut max) = (0.0, 0.0, f64::INFINITY, f64::NEG_INFINITY);
    for (i, &value) in values.iter().enumerate() {
        let delta = value - mean;
        mean += delta / (i + 1) as f64;
        m2 += delta * (value - mean);
        min = min.min(value);
        max = max.max(value);
    }
    let n = values.len();
    let sample_sd = (n > 1).then(|| (m2.max(0.0) / (n - 1) as f64).sqrt());
    let standard_error = sample_sd.map(|v| v / (n as f64).sqrt());
    let confidence_interval = standard_error.map(|v| {
        let margin = student_critical_95(n - 1) * v;
        [mean - margin, mean + margin]
    });
    let relative_sd = sample_sd.map(|v| v / mean);
    let mut warnings = vec!["Replicate summaries assume independent, identically prepared runs; distinct seeds alone do not prove independence.",
        "The 95% t interval describes uncertainty of the replicate mean under independent normal errors, not physical accuracy or equilibrium."];
    if n < 2 {
        warnings.push("A single run cannot estimate between-run variability, standard error or a confidence interval.");
    } else if min == max {
        warnings.push("All replicate densities are identical: zero observed spread does not establish precision or independent replication.");
    }
    let reference_comparison = reference.map(|value| {
        let combined = standard_error.zip(reference_standard_uncertainty).map(|(se, u)| se.hypot(u));
        let difference = mean - value;
        let standardized = combined.filter(|u| *u > 0.0).map(|u| difference / u).filter(|v| v.is_finite());
        if reference_standard_uncertainty.is_none() {
            warnings.push("The reference standard uncertainty is not recorded; combined uncertainty and standardized difference are unavailable.");
        } else if combined == Some(0.0) {
            warnings.push("Combined uncertainty is zero; a standardized difference is not reported.");
        }
        ReferenceComparison { reference: value, value_difference: difference, relative_difference: difference / value,
            combined_standard_uncertainty: combined, standardized_difference: standardized }
    });
    Ok(Summary { n, mean, min, max, sample_sd, standard_error, confidence_interval, relative_sd, reference_comparison, warnings })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn analytical_triple_and_reference_uncertainty() {
        let summary = summarize(&[0.9, 1.0, 1.1], Some(0.98), Some(0.02)).unwrap();
        assert_eq!(summary.n, 3);
        assert!((summary.mean - 1.0).abs() < 1e-14);
        assert!((summary.sample_sd.unwrap() - 0.1).abs() < 1e-14);
        assert!((summary.standard_error.unwrap() - 0.1 / 3.0_f64.sqrt()).abs() < 1e-14);
        let interval = summary.confidence_interval.unwrap();
        assert!((interval[0] - (1.0 - 4.302652729749464 * 0.1 / 3.0_f64.sqrt())).abs() < 1e-11);
        let reference = summary.reference_comparison.unwrap();
        let expected_u = (0.01 / 3.0 + 0.0004_f64).sqrt();
        assert!((reference.value_difference - 0.02).abs() < 1e-14);
        assert!((reference.combined_standard_uncertainty.unwrap() - expected_u).abs() < 1e-14);
        assert!((reference.standardized_difference.unwrap() - 0.02 / expected_u).abs() < 1e-13);
    }

    #[test]
    fn single_and_constant_runs_preserve_unknowns() {
        let single = summarize(&[1.0], Some(0.9), Some(0.01)).unwrap();
        assert!(single.sample_sd.is_none() && single.standard_error.is_none() && single.confidence_interval.is_none());
        assert!(single.reference_comparison.unwrap().combined_standard_uncertainty.is_none());
        let constant = summarize(&[1.0, 1.0], Some(1.0), Some(0.0)).unwrap();
        assert_eq!(constant.sample_sd, Some(0.0));
        assert_eq!(constant.confidence_interval, Some([1.0, 1.0]));
        assert!(constant.reference_comparison.unwrap().standardized_difference.is_none());
        assert!(constant.warnings.iter().any(|w| w.starts_with("All replicate")));
        let missing = summarize(&[0.9, 1.1], Some(1.0), None).unwrap();
        assert!(missing.reference_comparison.unwrap().combined_standard_uncertainty.is_none());
    }

    #[test]
    fn invalid_density_and_reference_inputs_are_rejected() {
        for values in [vec![], vec![0.0], vec![-0.1], vec![f64::NAN], vec![f64::INFINITY], vec![1e7], vec![1e-13], vec![1.0; 1001]] {
            assert!(summarize(&values, None, None).is_err());
        }
        assert!(summarize(&[1.0], None, Some(0.1)).is_err());
        assert!(summarize(&[1.0], Some(0.0), None).is_err());
        assert!(summarize(&[1.0], Some(1.0), Some(-0.1)).is_err());
        assert!(summarize(&[1.0], Some(1.0), Some(f64::NAN)).is_err());
    }
}
