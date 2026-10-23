//! Free and accessible volume on a grid (the Materials Studio "Atom Volumes & Surfaces" quantities).
//!
//! For every grid point the distance to the nearest van der Waals surface, s(x) = min_i |x - p_i| - r_i,
//! is computed with the minimum image. Then
//!   occupied   : s <= 0 (inside the union of van der Waals spheres)
//!   accessible : s > probe (a probe sphere centred here overlaps no atom)
//!   occupiable : within `probe` of an accessible point (the volume a rolling probe can sweep; for
//!                probe 0 it equals the unoccupied volume)

use crate::cells::CellList;
use rayon::prelude::*;

pub struct VolumeResult {
    pub grid: [usize; 3],
    pub occupied_fraction: f64,
    pub accessible_fraction: f64,
    pub occupiable_fraction: f64,
    /// Counts of unoccupied grid points by distance to the nearest surface, bin width `bin`.
    pub surface_distance_histogram: Vec<f64>,
}

pub fn free_volume(points: &[[f64; 3]], radii: &[f64], length: [f64; 3], probe: f64, spacing: f64, bin: f64, bins: usize) -> VolumeResult {
    let n = [0, 1, 2].map(|k| ((length[k] / spacing).ceil() as usize).max(1));
    let step = [0, 1, 2].map(|k| length[k] / n[k] as f64);
    let r_max = radii.iter().cloned().fold(0.0, f64::max);
    // Distances beyond the reach of the cell search are capped; only s up to probe and the histogram matter.
    let reach = r_max + probe.max(bin * bins as f64) + step.iter().cloned().fold(0.0, f64::max);
    let cutoff = reach.min(0.5 * length.iter().cloned().fold(f64::INFINITY, f64::min));
    let cells = CellList::new(points, length, cutoff);
    let total = n[0] * n[1] * n[2];
    let index = |x: usize, y: usize, z: usize| (x * n[1] + y) * n[2] + z;
    let field: Vec<f64> = (0..total)
        .into_par_iter()
        .map(|g| {
            let (x, rest) = (g / (n[1] * n[2]), g % (n[1] * n[2]));
            let (y, z) = (rest / n[2], rest % n[2]);
            let p = [(x as f64 + 0.5) * step[0], (y as f64 + 0.5) * step[1], (z as f64 + 0.5) * step[2]];
            let mut best = cutoff;
            cells.for_each_near(&p, |i| {
                let d = crate::cells::norm2(&crate::cells::minimum_image(&p, &cells.points[i], &length)).sqrt() - radii[i];
                if d < best {
                    best = d;
                }
            });
            best
        })
        .collect();
    let accessible: Vec<bool> = field.iter().map(|&s| s > probe).collect();
    // Offsets of the probe-radius ball in grid steps (anisotropic steps handled exactly).
    let reach_steps = [0, 1, 2].map(|k| (probe / step[k]).floor() as i64);
    let mut ball = Vec::new();
    for dx in -reach_steps[0]..=reach_steps[0] {
        for dy in -reach_steps[1]..=reach_steps[1] {
            for dz in -reach_steps[2]..=reach_steps[2] {
                let d2 = (dx as f64 * step[0]).powi(2) + (dy as f64 * step[1]).powi(2) + (dz as f64 * step[2]).powi(2);
                if d2 <= probe * probe {
                    ball.push((dx, dy, dz));
                }
            }
        }
    }
    let wrap = |v: i64, m: usize| (v.rem_euclid(m as i64)) as usize;
    // The probe sweeps A + ball = A plus the balls around A's boundary (a point within `probe` of A is
    // within `probe` of A's boundary or in A), so only boundary points of A need their ball marked.
    let mut swept = accessible.clone();
    let boundary = |x: usize, y: usize, z: usize| {
        [(1i64, 0i64, 0i64), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)].iter().any(|&(dx, dy, dz)| {
            !accessible[index(wrap(x as i64 + dx, n[0]), wrap(y as i64 + dy, n[1]), wrap(z as i64 + dz, n[2]))]
        })
    };
    for g in (0..total).filter(|&g| accessible[g]) {
        let (x, rest) = (g / (n[1] * n[2]), g % (n[1] * n[2]));
        let (y, z) = (rest / n[2], rest % n[2]);
        if !boundary(x, y, z) {
            continue;
        }
        for &(dx, dy, dz) in &ball {
            swept[index(wrap(x as i64 + dx, n[0]), wrap(y as i64 + dy, n[1]), wrap(z as i64 + dz, n[2]))] = true;
        }
    }
    // Ball points lie within `probe` of an accessible centre, so none is inside an atom.
    let occupiable = swept.iter().filter(|&&v| v).count();
    let mut histogram = vec![0.0; bins];
    for &s in &field {
        if s > 0.0 {
            let b = (s / bin) as usize;
            if b < bins {
                histogram[b] += 1.0;
            }
        }
    }
    let t = total as f64;
    VolumeResult {
        grid: n,
        occupied_fraction: field.iter().filter(|&&s| s <= 0.0).count() as f64 / t,
        accessible_fraction: accessible.iter().filter(|&&a| a).count() as f64 / t,
        occupiable_fraction: occupiable as f64 / t,
        surface_distance_histogram: histogram,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::f64::consts::PI;

    #[test]
    fn one_sphere_matches_the_analytic_volumes() {
        let l = 20.0;
        let r = 3.0;
        let v = free_volume(&[[10.0, 10.0, 10.0]], &[r], [l, l, l], 1.4, 0.1, 0.1, 50);
        let sphere = |x: f64| 4.0 / 3.0 * PI * x.powi(3) / l.powi(3);
        assert!((v.occupied_fraction - sphere(r)).abs() < 0.002, "{}", v.occupied_fraction);
        assert!((v.accessible_fraction - (1.0 - sphere(r + 1.4))).abs() < 0.002);
        // A single convex sphere leaves every outside point reachable by the probe.
        assert!((v.occupiable_fraction - (1.0 - v.occupied_fraction)).abs() < 0.002);
    }

    #[test]
    fn a_crevice_too_narrow_for_the_probe_is_not_occupiable() {
        let l = 20.0;
        // Two touching spheres: the probe cannot sweep the crevice around their contact.
        let v = free_volume(&[[8.0, 10.0, 10.0], [12.0, 10.0, 10.0]], &[2.0, 2.0], [l, l, l], 1.5, 0.1, 0.1, 50);
        assert!(v.occupiable_fraction < 1.0 - v.occupied_fraction - 1e-4);
        // Periodic images: a sphere across the boundary has the same volume.
        let a = free_volume(&[[0.5, 10.0, 10.0]], &[3.0], [l, l, l], 0.0, 0.1, 0.1, 10);
        let b = free_volume(&[[10.0, 10.0, 10.0]], &[3.0], [l, l, l], 0.0, 0.1, 0.1, 10);
        assert!((a.occupied_fraction - b.occupied_fraction).abs() < 5e-4);
        assert!((a.occupiable_fraction - (1.0 - a.occupied_fraction)).abs() < 1e-12);
    }
}
