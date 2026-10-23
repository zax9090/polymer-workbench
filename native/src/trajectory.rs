//! Trajectory analysis (Materials Studio Forcite Analysis counterparts): radial distribution
//! function pair counts and mean-squared displacement.

use crate::cells::{minimum_image, norm2, CellList};
use rayon::prelude::*;

/// Ordered pair counts (i in `a`, j in `b`, i != j, optionally not in the same molecule) per distance
/// bin of width r_max / bins, summed over frames, with the minimum image. Normalising them into g(r)
/// is left to the caller (workbench/structure_analysis.py uses MDAnalysis's InterRDF convention).
pub fn rdf_counts(frames: &[Vec<[f64; 3]>], boxes: &[[f64; 3]], a: &[usize], b: &[usize], molecule: Option<&[i64]>,
                  r_max: f64, bins: usize) -> Vec<f64> {
    let width = r_max / bins as f64;
    let r2max = r_max * r_max;
    frames
        .par_iter()
        .zip(boxes.par_iter())
        .map(|(positions, length)| {
            let b_points: Vec<[f64; 3]> = b.iter().map(|&j| positions[j]).collect();
            let cells = CellList::new(&b_points, *length, r_max);
            let mut counts = vec![0.0; bins];
            for &i in a {
                let p = positions[i];
                cells.for_each_near(&p, |k| {
                    let j = b[k];
                    if j == i {
                        return;
                    }
                    if let Some(m) = molecule {
                        if m[i] == m[j] {
                            return;
                        }
                    }
                    let r2 = norm2(&minimum_image(&p, &positions[j], length));
                    if r2 < r2max {
                        let bin = (r2.sqrt() / width) as usize;
                        if bin < bins {
                            counts[bin] += 1.0;
                        }
                    }
                });
            }
            counts
        })
        .reduce(|| vec![0.0; bins], |mut x, y| {
            for (u, v) in x.iter_mut().zip(y) {
                *u += v;
            }
            x
        })
}

/// Mean-squared displacement of `atoms` for every lag 0..frames-1, averaged over all time origins and
/// atoms (the windowed estimator), from unwrapped coordinates.
pub fn msd(frames: &[Vec<[f64; 3]>], atoms: &[usize]) -> Vec<f64> {
    let f = frames.len();
    (0..f)
        .into_par_iter()
        .map(|lag| {
            if lag == 0 {
                return 0.0;
            }
            let mut total = 0.0;
            for t in 0..f - lag {
                for &i in atoms {
                    let (p, q) = (frames[t + lag][i], frames[t][i]);
                    total += (p[0] - q[0]).powi(2) + (p[1] - q[1]).powi(2) + (p[2] - q[2]).powi(2);
                }
            }
            total / ((f - lag) * atoms.len()) as f64
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rdf_counts_match_brute_force() {
        let length = [10.0, 11.0, 12.0];
        let mut x = 12345u64;
        let mut next = || {
            x = x.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (x >> 11) as f64 / (1u64 << 53) as f64
        };
        let positions: Vec<[f64; 3]> = (0..300).map(|_| [next() * 10.0, next() * 11.0, next() * 12.0]).collect();
        let all: Vec<usize> = (0..300).collect();
        let counts = rdf_counts(&[positions.clone()], &[length], &all, &all, None, 4.5, 45);
        let mut brute = vec![0.0; 45];
        for i in 0..300 {
            for j in 0..300 {
                if i != j {
                    let r = norm2(&minimum_image(&positions[i], &positions[j], &length)).sqrt();
                    if r < 4.5 {
                        brute[(r / 0.1) as usize] += 1.0;
                    }
                }
            }
        }
        assert_eq!(counts, brute);
    }

    #[test]
    fn msd_of_uniform_motion_is_quadratic() {
        let frames: Vec<Vec<[f64; 3]>> = (0..10).map(|t| vec![[t as f64 * 0.5, 0.0, 0.0], [0.0, -(t as f64), 0.0]]).collect();
        let m = msd(&frames, &[0, 1]);
        for (lag, v) in m.iter().enumerate() {
            let expected = ((0.5 * lag as f64).powi(2) + (lag as f64).powi(2)) / 2.0;
            assert!((v - expected).abs() < 1e-12);
        }
    }
}
