//! Mixture experiments and multi-response desirability.
//!
//! - Simplex-lattice {q, m}: every proportion in {0, 1/m, ..., 1} with the proportions summing to one,
//!   and simplex-centroid: the centroid of every non-empty subset of components
//!   (Scheffé, J. R. Stat. Soc. B 20, 344, 1958 and 25, 235, 1963).
//! - Extreme vertices of L <= x <= U with Σx = 1 (McLean & Anderson, Technometrics 8, 447, 1966):
//!   fix all but one component at a bound and keep the point when the remaining one is within its own.
//! - Axial check blends halfway between the overall centroid and each vertex.
//! - Lower bounds on simplex designs use L-pseudocomponents x = L + (1 − ΣL) z.
//! - Desirability (Derringer & Suich, J. Qual. Technol. 12, 214, 1980): one-sided or two-sided
//!   individual d in [0, 1] with shape exponents, combined as the importance-weighted geometric mean D.
use crate::doe::{features, terms, Design};
use crate::rng::Rng;

const TOL: f64 = 1e-12;

#[derive(Debug, Clone)]
pub struct Region {
    pub lower: Vec<f64>,
    pub upper: Vec<f64>,
}

impl Region {
    /// Bounds tightened to the ones implied by Σx = 1; the region must have positive extent.
    pub fn new(lower: &[f64], upper: &[f64]) -> Result<Region, String> {
        let q = lower.len();
        if !(2..=6).contains(&q) || upper.len() != q {
            return Err("A mixture needs 2 to 6 components with one lower and one upper bound each".into());
        }
        if lower.iter().chain(upper).any(|v| !v.is_finite()) || lower.iter().zip(upper).any(|(l, u)| *l < 0.0 || u > &1.0 || l >= u) {
            return Err("Component bounds must satisfy 0 <= lower < upper <= 1".into());
        }
        let (sum_lower, sum_upper): (f64, f64) = (lower.iter().sum(), upper.iter().sum());
        if sum_lower >= 1.0 - 1e-9 || sum_upper <= 1.0 + 1e-9 {
            return Err("The bounds leave no mixture region: the lower bounds must sum below 1 and the upper bounds above 1".into());
        }
        let implied_upper: Vec<f64> = (0..q).map(|i| upper[i].min(1.0 - (sum_lower - lower[i]))).collect();
        let implied_lower: Vec<f64> = (0..q).map(|i| lower[i].max(1.0 - (sum_upper - upper[i]))).collect();
        Ok(Region { lower: implied_lower, upper: implied_upper })
    }

    pub fn q(&self) -> usize { self.lower.len() }

    fn scale(&self) -> f64 { 1.0 - self.lower.iter().sum::<f64>() }

    /// True when no upper bound cuts the L-simplex, so simplex designs in pseudocomponents apply.
    pub fn is_simplex(&self) -> bool {
        let s = self.scale();
        self.lower.iter().zip(&self.upper).all(|(l, u)| (u - (l + s)).abs() < 1e-9)
    }

    pub fn to_real(&self, z: &[f64]) -> Vec<f64> {
        let s = self.scale();
        z.iter().zip(&self.lower).map(|(z, l)| l + s * z).collect()
    }

    pub fn to_pseudo(&self, x: &[f64]) -> Vec<f64> {
        let s = self.scale();
        x.iter().zip(&self.lower).map(|(x, l)| (x - l) / s).collect()
    }

    pub fn feasible(&self, x: &[f64]) -> bool {
        (x.iter().sum::<f64>() - 1.0).abs() < 1e-9
            && x.iter().zip(&self.lower).zip(&self.upper).all(|((x, l), u)| *x >= l - 1e-9 && *x <= u + 1e-9)
    }

    fn active(&self, x: &[f64]) -> Vec<(usize, bool)> {
        let mut out = Vec::new();
        for i in 0..self.q() {
            if (x[i] - self.lower[i]).abs() < 1e-9 { out.push((i, false)); }
            if (x[i] - self.upper[i]).abs() < 1e-9 { out.push((i, true)); }
        }
        out
    }
}

/// All points of the {q, m} simplex lattice.
pub fn lattice(q: usize, m: usize) -> Vec<Vec<f64>> {
    fn fill(prefix: &mut Vec<usize>, left: usize, q: usize, m: usize, out: &mut Vec<Vec<f64>>) {
        if prefix.len() == q - 1 {
            prefix.push(left);
            out.push(prefix.iter().map(|&c| c as f64 / m as f64).collect());
            prefix.pop();
            return;
        }
        for c in (0..=left).rev() {
            prefix.push(c);
            fill(prefix, left - c, q, m, out);
            prefix.pop();
        }
    }
    let mut out = Vec::new();
    fill(&mut Vec::new(), m, q, m, &mut out);
    out
}

/// Centroids of the 2^q − 1 non-empty component subsets.
pub fn centroids(q: usize) -> Vec<Vec<f64>> {
    let mut subsets: Vec<usize> = (1..1usize << q).collect();
    subsets.sort_by_key(|mask| (mask.count_ones(), *mask));
    subsets.into_iter().map(|mask| {
        let size = mask.count_ones() as f64;
        (0..q).map(|i| if mask & (1 << i) != 0 { 1.0 / size } else { 0.0 }).collect()
    }).collect()
}

fn push_unique(points: &mut Vec<Vec<f64>>, point: Vec<f64>) -> bool {
    if points.iter().any(|p| p.iter().zip(&point).all(|(a, b)| (a - b).abs() < 1e-9)) {
        return false;
    }
    points.push(point);
    true
}

pub fn extreme_vertices(region: &Region) -> Vec<Vec<f64>> {
    let q = region.q();
    let mut vertices = Vec::new();
    for free in 0..q {
        for mask in 0..1usize << (q - 1) {
            let mut x = vec![0.0; q];
            let mut bit = 0;
            for i in (0..q).filter(|&i| i != free) {
                x[i] = if mask & (1 << bit) == 0 { region.lower[i] } else { region.upper[i] };
                bit += 1;
            }
            x[free] = 1.0 - x.iter().sum::<f64>();
            if x[free] >= region.lower[free] - TOL && x[free] <= region.upper[free] + TOL {
                x[free] = x[free].clamp(region.lower[free], region.upper[free]);
                push_unique(&mut vertices, x);
            }
        }
    }
    vertices
}

/// Midpoints of vertex pairs that share at least q − 2 active bounds, i.e. lie on one edge of the region.
pub fn edge_midpoints(region: &Region, vertices: &[Vec<f64>]) -> Vec<Vec<f64>> {
    let q = region.q();
    let mut out = Vec::new();
    for a in 0..vertices.len() {
        for b in a + 1..vertices.len() {
            let (sa, sb) = (region.active(&vertices[a]), region.active(&vertices[b]));
            let common = sa.iter().filter(|c| sb.contains(c)).count();
            if common + 2 >= q {
                let mid: Vec<f64> = vertices[a].iter().zip(&vertices[b]).map(|(x, y)| 0.5 * (x + y)).collect();
                // A pair can share q − 2 bounds yet span a face diagonal when a vertex is degenerate.
                let on_edge = region.active(&mid).len() + 2 >= q;
                if on_edge {
                    push_unique(&mut out, mid);
                }
            }
        }
    }
    out
}

fn centroid_of(points: &[Vec<f64>]) -> Vec<f64> {
    let q = points[0].len();
    (0..q).map(|i| points.iter().map(|p| p[i]).sum::<f64>() / points.len() as f64).collect()
}

fn kind_of(z: &[f64]) -> &'static str {
    match z.iter().filter(|v| **v > 1e-12).count() {
        1 => "vertex",
        2 => "edge",
        n if n == z.len() && z.iter().all(|v| (v - 1.0 / z.len() as f64).abs() < 1e-12) => "centroid",
        _ => "interior",
    }
}

/// Randomized mixture design in real proportions. Simplex designs require a region without active upper bounds.
pub fn design(region: &Region, kind: &str, degree: usize, axial: bool, centroid: bool, replicates: usize, seed: u64) -> Result<Design, String> {
    let q = region.q();
    if !(1..=10).contains(&replicates) {
        return Err("Replicates must be 1..10".into());
    }
    let mut points: Vec<Vec<f64>> = Vec::new();
    let mut types: Vec<&'static str> = Vec::new();
    match kind {
        "simplex_lattice" | "simplex_centroid" => {
            if !region.is_simplex() {
                return Err("Upper bounds cut the simplex; use extreme_vertices for this region".into());
            }
            let base = if kind == "simplex_lattice" {
                if !(1..=4).contains(&degree) { return Err("Lattice degree must be 1..4".into()); }
                lattice(q, degree)
            } else {
                centroids(q)
            };
            for z in base {
                types.push(kind_of(&z));
                points.push(region.to_real(&z));
            }
            if centroid {
                let z = vec![1.0 / q as f64; q];
                if push_unique(&mut points, region.to_real(&z)) { types.push("centroid"); }
            }
        }
        "extreme_vertices" => {
            if !(1..=2).contains(&degree) { return Err("Extreme-vertices degree is 1 (vertices) or 2 (vertices and edge midpoints)".into()); }
            let vertices = extreme_vertices(region);
            for v in &vertices { points.push(v.clone()); types.push("vertex"); }
            if degree == 2 {
                for m in edge_midpoints(region, &vertices) { points.push(m); types.push("edge"); }
            }
            if centroid && push_unique(&mut points, centroid_of(&vertices)) { types.push("centroid"); }
        }
        _ => return Err("Unknown mixture design kind".into()),
    }
    if axial {
        let vertices: Vec<Vec<f64>> = if kind == "extreme_vertices" { extreme_vertices(region) }
            else { (0..q).map(|i| region.to_real(&(0..q).map(|j| if i == j { 1.0 } else { 0.0 }).collect::<Vec<_>>())).collect() };
        let middle = centroid_of(&vertices);
        for v in &vertices {
            let point: Vec<f64> = v.iter().zip(&middle).map(|(a, b)| 0.5 * (a + b)).collect();
            if push_unique(&mut points, point) { types.push("axial"); }
        }
    }
    if points.len() * replicates > 1000 {
        return Err("Design exceeds 1000 runs".into());
    }
    let (base_points, base_types) = (points.clone(), types.clone());
    for _ in 1..replicates {
        points.extend(base_points.iter().cloned());
        types.extend(base_types.iter().copied());
    }
    let mut rng = Rng::new(seed, 0x4d4958);
    for i in (1..points.len()).rev() {
        let j = (rng.uniform() * (i + 1) as f64).floor() as usize;
        points.swap(i, j);
        types.swap(i, j);
    }
    Ok(Design { points, types })
}

/// Best value of f on [lo, hi] from 33 samples, then golden-section refinement around the best sample.
fn line_search(f: &dyn Fn(f64) -> f64, lo: f64, hi: f64) -> (f64, f64) {
    let mut best = (0.0f64.clamp(lo, hi), f(0.0f64.clamp(lo, hi)));
    if hi - lo < 1e-15 {
        return best;
    }
    let n = 32;
    let step = (hi - lo) / n as f64;
    let mut index = None;
    for k in 0..=n {
        let t = if k == n { hi } else { lo + step * k as f64 };
        let value = f(t);
        if value > best.1 { best = (t, value); index = Some(k); }
    }
    if let Some(k) = index {
        let (mut a, mut b) = ((lo + step * (k as f64 - 1.0)).max(lo), (lo + step * (k as f64 + 1.0)).min(hi));
        let ratio = 0.5 * (5f64.sqrt() - 1.0);
        let (mut c, mut d) = (b - ratio * (b - a), a + ratio * (b - a));
        let (mut fc, mut fd) = (f(c), f(d));
        for _ in 0..80 {
            if fc > fd { b = d; d = c; fd = fc; c = b - ratio * (b - a); fc = f(c); }
            else { a = c; c = d; fc = fd; d = a + ratio * (b - a); fd = f(d); }
        }
        for (t, value) in [(c, fc), (d, fd)] {
            if value > best.1 { best = (t, value); }
        }
    }
    best
}

pub struct Optimum { pub point: Vec<f64>, pub score: f64, pub starts: usize }

fn random_point(region: &Region, rng: &mut Rng) -> Option<Vec<f64>> {
    let q = region.q();
    for _ in 0..200 {
        let mut cuts: Vec<f64> = (0..q - 1).map(|_| rng.uniform()).collect();
        cuts.push(0.0);
        cuts.push(1.0);
        cuts.sort_by(f64::total_cmp);
        let z: Vec<f64> = cuts.windows(2).map(|w| w[1] - w[0]).collect();
        let x = region.to_real(&z);
        if region.feasible(&x) { return Some(x); }
    }
    None
}

/// Maximize score over the mixture region by exchanging proportion between pairs of components.
pub fn optimize_region(region: &Region, score: &dyn Fn(&[f64]) -> f64) -> Optimum {
    let q = region.q();
    let vertices = extreme_vertices(region);
    let middle = centroid_of(&vertices);
    let mut starts = vertices.clone();
    starts.push(middle.clone());
    starts.extend(vertices.iter().map(|v| v.iter().zip(&middle).map(|(a, b)| 0.5 * (a + b)).collect()));
    let mut rng = Rng::new(2024, 0x4f5054);
    for _ in 0..48 {
        if let Some(x) = random_point(region, &mut rng) { starts.push(x); }
    }
    let mut best = (middle.clone(), score(&middle));
    for start in &starts {
        let mut x = start.clone();
        let mut value = score(&x);
        for _ in 0..200 {
            let before = value;
            for i in 0..q {
                for j in 0..q {
                    if i == j { continue; }
                    let lo = (region.lower[i] - x[i]).max(x[j] - region.upper[j]);
                    let hi = (region.upper[i] - x[i]).min(x[j] - region.lower[j]);
                    if hi - lo <= 1e-15 { continue; }
                    let base = x.clone();
                    let along = |t: f64| {
                        let mut y = base.clone();
                        y[i] += t;
                        y[j] -= t;
                        score(&y)
                    };
                    let (t, v) = line_search(&along, lo.min(0.0), hi.max(0.0));
                    if v > value { x[i] += t; x[j] -= t; value = v; }
                }
            }
            if value - before <= 1e-14 * before.abs().max(1.0) { break; }
        }
        if value > best.1 { best = (x, value); }
    }
    let point = snap(region, best.0);
    let value = score(&point);
    Optimum { point, score: value, starts: starts.len() }
}

/// Puts proportions within 1e-10 of a bound on it and returns the rounding to the freest component.
fn snap(region: &Region, mut x: Vec<f64>) -> Vec<f64> {
    for i in 0..x.len() {
        if (x[i] - region.lower[i]).abs() < 1e-10 { x[i] = region.lower[i]; }
        if (region.upper[i] - x[i]).abs() < 1e-10 { x[i] = region.upper[i]; }
    }
    let free = (0..x.len()).max_by(|&a, &b| {
        let room = |i: usize| (x[i] - region.lower[i]).min(region.upper[i] - x[i]);
        room(a).total_cmp(&room(b))
    }).unwrap();
    x[free] += 1.0 - x.iter().sum::<f64>();
    x
}

/// Maximize score over the coded cube [−1, 1]^k by coordinate line searches.
pub fn optimize_cube(k: usize, score: &dyn Fn(&[f64]) -> f64) -> Optimum {
    let mut starts = vec![vec![0.0; k]];
    for mask in 0..1usize << k {
        starts.push((0..k).map(|i| if mask & (1 << i) == 0 { -1.0 } else { 1.0 }).collect());
    }
    let mut rng = Rng::new(2024, 0x435542);
    for _ in 0..32 { starts.push((0..k).map(|_| 2.0 * rng.uniform() - 1.0).collect()); }
    let mut best = (starts[0].clone(), score(&starts[0]));
    for start in &starts {
        let mut x = start.clone();
        let mut value = score(&x);
        for _ in 0..200 {
            let before = value;
            for i in 0..k {
                let base = x.clone();
                let along = |t: f64| { let mut y = base.clone(); y[i] += t; score(&y) };
                let (t, v) = line_search(&along, -1.0 - x[i], 1.0 - x[i]);
                if v > value { x[i] += t; value = v; }
            }
            if value - before <= 1e-14 * before.abs().max(1.0) { break; }
        }
        if value > best.1 { best = (x, value); }
    }
    Optimum { point: best.0, score: best.1, starts: starts.len() }
}

#[derive(Debug, Clone, Copy)]
pub struct Goal {
    pub kind: GoalKind,
    pub lower: f64,
    pub target: f64,
    pub upper: f64,
    pub lower_shape: f64,
    pub upper_shape: f64,
    pub importance: f64,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum GoalKind { Maximize, Minimize, Target }

impl Goal {
    pub fn new(kind: &str, lower: f64, target: f64, upper: f64, lower_shape: f64, upper_shape: f64, importance: f64) -> Result<Goal, String> {
        let kind = match kind { "maximize" => GoalKind::Maximize, "minimize" => GoalKind::Minimize, "target" => GoalKind::Target,
            _ => return Err("Goal must be maximize, minimize or target".into()) };
        let shapes_ok = [lower_shape, upper_shape].iter().all(|s| s.is_finite() && (0.1..=10.0).contains(s));
        if !shapes_ok || !(importance.is_finite() && (0.1..=10.0).contains(&importance)) {
            return Err("Shape exponents and importance must be between 0.1 and 10".into());
        }
        let ordered = match kind {
            GoalKind::Maximize => lower.is_finite() && target.is_finite() && lower < target,
            GoalKind::Minimize => target.is_finite() && upper.is_finite() && target < upper,
            GoalKind::Target => [lower, target, upper].iter().all(|v| v.is_finite()) && lower < target && target < upper,
        };
        if !ordered {
            return Err("Desirability bounds must be ordered: lower < target (maximize), target < upper (minimize), lower < target < upper (target)".into());
        }
        Ok(Goal { kind, lower, target, upper, lower_shape, upper_shape, importance })
    }

    pub fn desirability(&self, y: f64) -> f64 {
        let rising = |y: f64| ((y - self.lower) / (self.target - self.lower)).clamp(0.0, 1.0).powf(self.lower_shape);
        let falling = |y: f64| ((self.upper - y) / (self.upper - self.target)).clamp(0.0, 1.0).powf(self.upper_shape);
        if !y.is_finite() { return 0.0; }
        match self.kind {
            GoalKind::Maximize => rising(y),
            GoalKind::Minimize => falling(y),
            GoalKind::Target => if y < self.lower || y > self.upper { 0.0 } else if y <= self.target { rising(y) } else { falling(y) },
        }
    }
}

/// Importance-weighted geometric mean of individual desirabilities; zero when any one is zero.
pub fn composite(goals: &[Goal], values: &[f64]) -> (Vec<f64>, f64) {
    let d: Vec<f64> = goals.iter().zip(values).map(|(g, y)| g.desirability(*y)).collect();
    if d.iter().any(|v| *v <= 0.0) {
        return (d, 0.0);
    }
    let total: f64 = goals.iter().map(|g| g.importance).sum();
    let log: f64 = goals.iter().zip(&d).map(|(g, v)| g.importance * v.ln()).sum();
    (d, (log / total).exp())
}

pub enum Domain { Cube(usize), Mixture(Region) }

pub struct Response { pub coefficients: Vec<f64>, pub model: String }

impl Domain {
    pub fn dimension(&self) -> usize { match self { Domain::Cube(k) => *k, Domain::Mixture(r) => r.q() } }

    /// Model coordinates of a domain point: coded factors, or L-pseudocomponents of a mixture.
    pub fn model_point(&self, x: &[f64]) -> Vec<f64> {
        match self { Domain::Cube(_) => x.to_vec(), Domain::Mixture(r) => r.to_pseudo(x) }
    }

    pub fn predict(&self, response: &Response, x: &[f64]) -> f64 {
        features(&self.model_point(x), &response.model).iter().zip(&response.coefficients).map(|(a, b)| a * b).sum()
    }

    pub fn optimize(&self, score: &dyn Fn(&[f64]) -> f64) -> Optimum {
        match self { Domain::Cube(k) => optimize_cube(*k, score), Domain::Mixture(r) => optimize_region(r, score) }
    }
}

pub fn check_response(domain: &Domain, response: &Response) -> Result<(), String> {
    let mixture = matches!(domain, Domain::Mixture(_));
    if mixture != response.model.starts_with("scheffe_") {
        return Err("Mixture regions take Scheffé models and coded cubes take polynomial models".into());
    }
    let p = terms(domain.dimension(), &response.model)?;
    if response.coefficients.len() != p || response.coefficients.iter().any(|v| !v.is_finite()) {
        return Err("Each response needs finite coefficients matching its model".into());
    }
    Ok(())
}

pub struct DesirabilityOptimum { pub point: Vec<f64>, pub values: Vec<f64>, pub individual: Vec<f64>, pub composite: f64, pub starts: usize }

pub fn optimize_desirability(domain: &Domain, responses: &[Response], goals: &[Goal]) -> Result<DesirabilityOptimum, String> {
    if responses.is_empty() || responses.len() != goals.len() || responses.len() > 10 {
        return Err("Give 1 to 10 responses, each with one goal".into());
    }
    for response in responses { check_response(domain, response)?; }
    let score = |x: &[f64]| {
        let values: Vec<f64> = responses.iter().map(|r| domain.predict(r, x)).collect();
        composite(goals, &values).1
    };
    let optimum = domain.optimize(&score);
    let values: Vec<f64> = responses.iter().map(|r| domain.predict(r, &optimum.point)).collect();
    let (individual, d) = composite(goals, &values);
    Ok(DesirabilityOptimum { point: optimum.point, values, individual, composite: d, starts: optimum.starts })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::doe::fit;

    fn binomial(n: usize, k: usize) -> usize { (0..k).fold(1, |acc, i| acc * (n - i) / (i + 1)) }

    #[test]
    fn simplex_designs_have_the_textbook_point_counts() {
        for q in 2..=6 {
            for m in 1..=4 {
                let points = lattice(q, m);
                assert_eq!(points.len(), binomial(q + m - 1, m));
                assert!(points.iter().all(|p| (p.iter().sum::<f64>() - 1.0).abs() < 1e-12));
            }
            assert_eq!(centroids(q).len(), (1 << q) - 1);
        }
    }

    #[test]
    fn extreme_vertices_of_a_cut_simplex() {
        // x1 <= 0.6 cuts the vertex x1 = 1 of the triangle, leaving a quadrilateral.
        let region = Region::new(&[0.0, 0.0, 0.0], &[0.6, 1.0, 1.0]).unwrap();
        let vertices = extreme_vertices(&region);
        assert_eq!(vertices.len(), 4);
        assert!(vertices.iter().all(|v| region.feasible(v)));
        assert_eq!(edge_midpoints(&region, &vertices).len(), 4);
        // Lower bounds alone keep a simplex: three vertices of the L-simplex.
        let shifted = Region::new(&[0.2, 0.1, 0.0], &[1.0, 1.0, 1.0]).unwrap();
        assert!(shifted.is_simplex());
        let v = extreme_vertices(&shifted);
        assert_eq!(v.len(), 3);
        assert!(v.iter().any(|p| (p[0] - 0.9).abs() < 1e-12 && (p[1] - 0.1).abs() < 1e-12));
        assert!(Region::new(&[0.5, 0.6], &[1.0, 1.0]).is_err());
    }

    #[test]
    fn scheffe_quadratic_fit_recovers_coefficients_and_optimum() {
        let region = Region::new(&[0.0; 3], &[1.0; 3]).unwrap();
        let d = design(&region, "simplex_lattice", 2, true, true, 1, 3).unwrap();
        let truth = [10.0, 8.0, 6.0, 12.0, 4.0, -6.0];
        let y: Vec<f64> = d.points.iter().map(|x| features(x, "scheffe_quadratic").iter().zip(truth).map(|(a, b)| a * b).sum()).collect();
        let fitted = fit(&d.points, &y, "scheffe_quadratic").unwrap();
        for (a, b) in fitted.coefficients.iter().zip(truth) { assert!((a - b).abs() < 1e-10); }
        let domain = Domain::Mixture(region);
        let response = Response { coefficients: fitted.coefficients.clone(), model: "scheffe_quadratic".into() };
        let best = domain.optimize(&|x: &[f64]| domain.predict(&response, x));
        // On the x1-x2 edge f = 10 x1 + 8 x2 + 12 x1 x2 peaks at x1 = 7/12 (value 12.0833...), above every interior point.
        assert!((best.point[0] - 7.0 / 12.0).abs() < 1e-6 && best.point[2] == 0.0);
        assert!((best.point.iter().sum::<f64>() - 1.0).abs() < 1e-15);
        assert!((best.score - (10.0 * 7.0 / 12.0 + 8.0 * 5.0 / 12.0 + 12.0 * 35.0 / 144.0)).abs() < 1e-9);
    }

    #[test]
    fn desirability_follows_derringer_suich() {
        let up = Goal::new("maximize", 10.0, 20.0, 0.0, 1.0, 1.0, 1.0).unwrap();
        assert_eq!(up.desirability(5.0), 0.0);
        assert!((up.desirability(15.0) - 0.5).abs() < 1e-15);
        assert_eq!(up.desirability(25.0), 1.0);
        let curved = Goal::new("maximize", 10.0, 20.0, 0.0, 2.0, 1.0, 1.0).unwrap();
        assert!((curved.desirability(15.0) - 0.25).abs() < 1e-15);
        let aim = Goal::new("target", 0.0, 4.0, 6.0, 1.0, 1.0, 1.0).unwrap();
        assert!((aim.desirability(2.0) - 0.5).abs() < 1e-15 && (aim.desirability(5.0) - 0.5).abs() < 1e-15);
        assert_eq!(aim.desirability(7.0), 0.0);
        let heavy = Goal::new("minimize", 0.0, 1.0, 3.0, 1.0, 1.0, 3.0).unwrap();
        let (d, total) = composite(&[up, heavy], &[15.0, 2.0]);
        assert!((d[1] - 0.5).abs() < 1e-15);
        assert!((total - 0.5f64).abs() < 1e-15);
        let (_, zero) = composite(&[up, heavy], &[5.0, 2.0]);
        assert_eq!(zero, 0.0);
        assert!(Goal::new("maximize", 20.0, 10.0, 0.0, 1.0, 1.0, 1.0).is_err());
    }

    #[test]
    fn desirability_optimum_on_the_cube() {
        // y1 = 5 + 3 x1 (maximize toward 8), y2 = 2 + x2^2 (minimize, best at x2 = 0).
        let responses = [Response { coefficients: vec![5.0, 3.0, 0.0], model: "linear".into() },
                         Response { coefficients: vec![2.0, 0.0, 0.0, 0.0, 0.0, 1.0], model: "quadratic".into() }];
        let goals = [Goal::new("maximize", 2.0, 8.0, 0.0, 1.0, 1.0, 1.0).unwrap(), Goal::new("minimize", 0.0, 2.0, 3.0, 1.0, 1.0, 1.0).unwrap()];
        let best = optimize_desirability(&Domain::Cube(2), &responses, &goals).unwrap();
        assert!((best.point[0] - 1.0).abs() < 1e-9 && best.point[1].abs() < 1e-6);
        assert!((best.composite - 1.0).abs() < 1e-9);
        assert!(optimize_desirability(&Domain::Cube(2), &responses[..1], &goals).is_err());
    }
}
