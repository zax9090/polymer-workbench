//! Distribution tails for regression inference, evaluated using the regularized beta function.
//! Lanczos log-gamma and the modified Lentz continued fraction avoid tail subtraction.

fn log_gamma(z: f64) -> f64 {
    let coefficients = [676.5203681218851, -1259.1392167224028, 771.3234287776531,
        -176.6150291621406, 12.507343278686905, -0.13857109526572012,
        9.984369578019572e-6, 1.5056327351493116e-7];
    let mut sum = 0.9999999999998099;
    for (i, value) in coefficients.iter().enumerate() { sum += value / (z + i as f64); }
    let t = z + 6.5;
    0.9189385332046727 + (z - 0.5) * t.ln() - t + sum.ln()
}

fn beta_fraction(a: f64, b: f64, x: f64) -> f64 {
    let floor = |v: f64| if v.abs() < 1e-300 { 1e-300_f64.copysign(v) } else { v };
    let mut c = 1.0;
    let mut d = 1.0 / floor(1.0 - (a + b) * x / (a + 1.0));
    let mut result = d;
    for m in 1..=10000 {
        let m = m as f64;
        let aa = m * (b - m) * x / ((a + 2.0 * m - 1.0) * (a + 2.0 * m));
        d = 1.0 / floor(1.0 + aa * d);
        c = floor(1.0 + aa / c);
        result *= d * c;
        let aa = -(a + m) * (a + b + m) * x / ((a + 2.0 * m) * (a + 2.0 * m + 1.0));
        d = 1.0 / floor(1.0 + aa * d);
        c = floor(1.0 + aa / c);
        let delta = d * c;
        result *= delta;
        if (delta - 1.0).abs() < 4.0 * f64::EPSILON { break; }
    }
    result
}

fn beta_regularized(x: f64, a: f64, b: f64) -> f64 {
    if x <= 0.0 { return 0.0; }
    if x >= 1.0 { return 1.0; }
    let scale = (log_gamma(a + b) - log_gamma(a) - log_gamma(b)
        + a * x.ln() + b * (-x).ln_1p()).exp();
    let result = if x < (a + 1.0) / (a + b + 2.0) {
        scale * beta_fraction(a, b, x) / a
    } else {
        1.0 - scale * beta_fraction(b, a, 1.0 - x) / b
    };
    result.clamp(0.0, 1.0)
}

pub fn student_two_sided(t: f64, df: usize) -> f64 {
    let degrees = df as f64;
    beta_regularized(degrees / (degrees + t * t), degrees / 2.0, 0.5)
}

pub fn student_critical_95(df: usize) -> f64 {
    let (mut low, mut high) = (0.0, 16.0);
    for _ in 0..64 {
        let midpoint = (low + high) * 0.5;
        if student_two_sided(midpoint, df) > 0.05 { low = midpoint; } else { high = midpoint; }
    }
    (low + high) * 0.5
}

pub fn f_survival(f: f64, numerator: usize, denominator: usize) -> f64 {
    let d1 = numerator as f64;
    let d2 = denominator as f64;
    beta_regularized(d2 / (d2 + d1 * f), d2 / 2.0, d1 / 2.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tails_and_quantiles_match_tabulated_values() {
        assert!((student_two_sided(1.0, 1) - 0.5).abs() < 1e-14);
        assert!((student_critical_95(1) - 1.0 / (std::f64::consts::PI * 0.025).tan()).abs() < 1e-10);
        assert!((student_critical_95(37) - 2.0261924630291093).abs() < 1e-10);
        assert!((student_critical_95(19990) - 1.960082664443392).abs() < 1e-8);
        assert!((f_survival(4.0, 2, 20) - (5.0_f64 / 7.0).powi(10)).abs() < 1e-14);
        assert!((f_survival(1.0, 1, 1) - 0.5).abs() < 1e-14);
    }
}
