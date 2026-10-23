//! Free and accessible volume on a grid (the Materials Studio "Atom Volumes & Surfaces" quantities).
//!
//! For every grid point the distance to the nearest van der Waals surface, s(x) = min_i |x - p_i| - r_i,
//! is computed with the minimum image. Then
//!   occupied   : s <= 0 (inside the union of van der Waals spheres)
//!   accessible : s > probe (a probe sphere centred here overlaps no atom)
//!   occupiable : within `probe` of an accessible point (the volume a rolling probe can sweep; for
//!                probe 0 it equals the unoccupied volume)

use crate::cells::{minimum_image, norm2, wrap};
use rayon::prelude::*;

struct SurfaceNode {
    lo: [f64; 3],
    hi: [f64; 3],
    radius: f64,
    range: std::ops::Range<usize>,
    children: Option<(usize, usize)>,
}

/// A periodic bounding-volume tree for min_i (distance(x, atom_i) - radius_i).
/// A node's nearest box distance minus its largest radius is a lower bound on
/// every surface distance in that node, including spheres crossing a box edge.
struct SurfaceTree<'a> {
    points: Vec<[f64; 3]>,
    radii: &'a [f64],
    length: [f64; 3],
    order: Vec<usize>,
    nodes: Vec<SurfaceNode>,
}

impl<'a> SurfaceTree<'a> {
    fn new(points: &[[f64; 3]], radii: &'a [f64], length: [f64; 3]) -> Self {
        let mut tree = Self {
            points: points.iter().map(|p| wrap(p, &length)).collect(),
            radii,
            length,
            order: (0..points.len()).collect(),
            nodes: Vec::new(),
        };
        if !points.is_empty() {
            tree.build(0, points.len());
        }
        tree
    }

    fn build(&mut self, start: usize, end: usize) -> usize {
        let mut lo = [f64::INFINITY; 3];
        let mut hi = [f64::NEG_INFINITY; 3];
        let mut radius: f64 = 0.0;
        for &i in &self.order[start..end] {
            for k in 0..3 {
                lo[k] = lo[k].min(self.points[i][k]);
                hi[k] = hi[k].max(self.points[i][k]);
            }
            radius = radius.max(self.radii[i]);
        }
        let index = self.nodes.len();
        self.nodes.push(SurfaceNode { lo, hi, radius, range: start..end, children: None });
        if end - start > 8 {
            let axis = (0..3).max_by(|&a, &b| (hi[a] - lo[a]).total_cmp(&(hi[b] - lo[b]))).unwrap();
            let mid = start + (end - start) / 2;
            let points = &self.points;
            self.order[start..end].select_nth_unstable_by(mid - start, |&a, &b| points[a][axis].total_cmp(&points[b][axis]));
            let left = self.build(start, mid);
            let right = self.build(mid, end);
            self.nodes[index].children = Some((left, right));
        }
        index
    }

    fn lower_bound(&self, node: usize, p: &[f64; 3]) -> f64 {
        let node = &self.nodes[node];
        let mut squared = 0.0;
        for k in 0..3 {
            let d = if p[k] < node.lo[k] {
                (node.lo[k] - p[k]).min(p[k] + self.length[k] - node.hi[k])
            } else if p[k] > node.hi[k] {
                (p[k] - node.hi[k]).min(node.lo[k] + self.length[k] - p[k])
            } else {
                0.0
            };
            squared += d * d;
        }
        squared.sqrt() - node.radius
    }

    fn visit(&self, index: usize, p: &[f64; 3], best: &mut f64) {
        let node = &self.nodes[index];
        if let Some((left, right)) = node.children {
            let a = self.lower_bound(left, p);
            let b = self.lower_bound(right, p);
            let ordered = if a < b { [(left, a), (right, b)] } else { [(right, b), (left, a)] };
            for (child, bound) in ordered {
                if bound < *best {
                    self.visit(child, p, best);
                }
            }
        } else {
            for &i in &self.order[node.range.clone()] {
                let d = norm2(&minimum_image(p, &self.points[i], &self.length)).sqrt() - self.radii[i];
                *best = best.min(d);
            }
        }
    }

    fn nearest(&self, p: &[f64; 3]) -> f64 {
        let mut best = f64::INFINITY;
        if !self.nodes.is_empty() {
            self.visit(0, p, &mut best);
        }
        best
    }
}

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
    let surfaces = SurfaceTree::new(points, radii, length);
    let total = n[0] * n[1] * n[2];
    let index = |x: usize, y: usize, z: usize| (x * n[1] + y) * n[2] + z;
    let field: Vec<f64> = (0..total)
        .into_par_iter()
        .map(|g| {
            let (x, rest) = (g / (n[1] * n[2]), g % (n[1] * n[2]));
            let (y, z) = (rest / n[2], rest % n[2]);
            let p = [(x as f64 + 0.5) * step[0], (y as f64 + 0.5) * step[1], (z as f64 + 0.5) * step[2]];
            surfaces.nearest(&p)
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

    // Deliberately use explicit periodic images instead of the kernel's minimum-image helper.
    fn brute_surface(p: &[f64; 3], points: &[[f64; 3]], radii: &[f64], length: [f64; 3]) -> f64 {
        let mut best = f64::INFINITY;
        for (atom, radius) in points.iter().zip(radii) {
            for x in -1..=1 {
                for y in -1..=1 {
                    for z in -1..=1 {
                        let shift = [x, y, z];
                        let squared: f64 = (0..3).map(|k| {
                            let image = atom[k].rem_euclid(length[k]) + shift[k] as f64 * length[k];
                            (p[k] - image).powi(2)
                        }).sum();
                        best = best.min(squared.sqrt() - radius);
                    }
                }
            }
        }
        best
    }

    fn grid_points(n: [usize; 3], length: [f64; 3]) -> Vec<[f64; 3]> {
        (0..n[0]).flat_map(|x| (0..n[1]).flat_map(move |y| (0..n[2]).map(move |z| {
            let indices = [x, y, z];
            [0, 1, 2].map(|k| (indices[k] as f64 + 0.5) * length[k] / n[k] as f64)
        }))).collect()
    }

    #[test]
    fn surface_tree_matches_explicit_periodic_images_with_unequal_radii() {
        let length = [4.7, 9.3, 13.1];
        let points: Vec<_> = (0..37).map(|i| {
            [0.618_033_988_75, 0.414_213_562_37, 0.732_050_807_57].map(|a| (i as f64 * a).fract())
        }).map(|p| [p[0] * length[0] - length[0], p[1] * length[1], p[2] * length[2] + length[2]]).collect();
        let radii: Vec<_> = (0..37).map(|i| 0.2 + (i % 7) as f64 * 0.31).collect();
        let tree = SurfaceTree::new(&points, &radii, length);
        for p in grid_points([7, 9, 11], length) {
            let expected = brute_surface(&p, &points, &radii, length);
            assert!((tree.nearest(&p) - expected).abs() < 1e-12, "point {p:?}");
        }
    }

    #[test]
    fn sparse_anisotropic_box_histogram_is_not_capped_at_half_the_shortest_edge() {
        let length = [3.0, 18.0, 20.0];
        let points = [[0.2, 0.3, 0.4]];
        let radii = [0.2];
        let result = free_volume(&points, &radii, length, 0.0, 1.0, 0.5, 40);
        let mut expected = vec![0.0; 40];
        for p in grid_points(result.grid, length) {
            let s = brute_surface(&p, &points, &radii, length);
            if s > 0.0 && s < 20.0 {
                expected[(s / 0.5) as usize] += 1.0;
            }
        }
        assert_eq!(result.surface_distance_histogram, expected);
        assert!(expected[10..].iter().sum::<f64>() > 0.0);
    }

    #[test]
    fn grid_fractions_match_brute_force_probe_dilation() {
        let length = [4.5, 5.0, 5.5];
        let atoms = [[0.1, 0.2, 0.3], [2.7, 2.1, 1.9], [4.3, 4.8, 5.3], [1.2, 3.4, 4.5]];
        let radii = [0.55, 1.1, 0.8, 1.3];
        let probe = 0.7;
        let result = free_volume(&atoms, &radii, length, probe, 0.5, 0.1, 30);
        let grid = grid_points(result.grid, length);
        let field: Vec<_> = grid.iter().map(|p| brute_surface(p, &atoms, &radii, length)).collect();
        let accessible: Vec<_> = grid.iter().zip(&field).filter_map(|(p, &s)| (s > probe).then_some(*p)).collect();
        let swept = grid.iter().filter(|p| {
            accessible.iter().any(|q| {
                let distance2: f64 = (0..3).map(|k| {
                    let d = (p[k] - q[k]).abs();
                    d.min(length[k] - d).powi(2)
                }).sum();
                distance2 <= probe * probe
            })
        }).count();
        let count = grid.len() as f64;
        assert_eq!(result.occupied_fraction, field.iter().filter(|&&s| s <= 0.0).count() as f64 / count);
        assert_eq!(result.accessible_fraction, accessible.len() as f64 / count);
        assert_eq!(result.occupiable_fraction, swept as f64 / count);
    }

    #[test]
    #[ignore = "manual bounded performance comparison; run with --ignored --nocapture"]
    fn benchmark_surface_field_against_previous_cell_search() {
        use crate::cells::CellList;
        use std::time::Instant;
        let length = [20.0; 3];
        let points = grid_points([8; 3], length);
        let radii: Vec<_> = (0..points.len()).map(|i| 1.1 + (i % 7) as f64 * 0.1).collect();
        let queries = grid_points([50; 3], length);
        let pool = rayon::ThreadPoolBuilder::new().num_threads(2).build().unwrap();
        pool.install(|| {
            let old_start = Instant::now();
            let cells = CellList::new(&points, length, 10.0);
            let previous: Vec<_> = queries.par_iter().map(|p| {
                let mut best: f64 = 10.0;
                cells.for_each_near(p, |i| {
                    best = best.min(norm2(&minimum_image(p, &cells.points[i], &length)).sqrt() - radii[i]);
                });
                best
            }).collect();
            let old_elapsed = old_start.elapsed();
            let new_start = Instant::now();
            let tree = SurfaceTree::new(&points, &radii, length);
            let current: Vec<_> = queries.par_iter().map(|p| tree.nearest(p)).collect();
            let new_elapsed = new_start.elapsed();
            let max_error = previous.iter().zip(current).map(|(a, b)| (a - b).abs()).fold(0.0, f64::max);
            assert!(max_error < 1e-12);
            eprintln!("surface field: {} atoms, {} grid points, 2 threads; previous={old_elapsed:?}, tree={new_elapsed:?}, speedup={:.2}, max_abs_error={max_error:e}",
                points.len(), queries.len(), old_elapsed.as_secs_f64() / new_elapsed.as_secs_f64());
        });
    }

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
