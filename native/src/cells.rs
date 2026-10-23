//! Periodic cell lists for orthorhombic boxes with the origin at 0.

/// Minimum-image displacement a - b in a box of edge lengths `length`.
#[inline]
pub fn minimum_image(a: &[f64; 3], b: &[f64; 3], length: &[f64; 3]) -> [f64; 3] {
    let mut d = [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
    for k in 0..3 {
        d[k] -= length[k] * (d[k] / length[k]).round();
    }
    d
}

#[inline]
pub fn norm2(d: &[f64; 3]) -> f64 {
    d[0] * d[0] + d[1] * d[1] + d[2] * d[2]
}

/// Wrap a point into [0, L) in every dimension.
#[inline]
pub fn wrap(p: &[f64; 3], length: &[f64; 3]) -> [f64; 3] {
    let mut w = *p;
    for k in 0..3 {
        w[k] -= length[k] * (w[k] / length[k]).floor();
        if w[k] >= length[k] {
            w[k] = 0.0;
        }
    }
    w
}

pub struct CellList {
    pub length: [f64; 3],
    n: [usize; 3],
    size: [f64; 3],
    cells: Vec<Vec<usize>>,
    pub points: Vec<[f64; 3]>,
}

impl CellList {
    /// Cells at least `cutoff` wide, so neighbours within `cutoff` lie in the 27 surrounding cells.
    /// The caller guarantees cutoff <= L/2 (one periodic image per pair).
    pub fn new(points: &[[f64; 3]], length: [f64; 3], cutoff: f64) -> Self {
        let mut n = [1usize; 3];
        let mut size = [0.0; 3];
        for k in 0..3 {
            n[k] = ((length[k] / cutoff).floor() as usize).max(1);
            size[k] = length[k] / n[k] as f64;
        }
        let wrapped: Vec<[f64; 3]> = points.iter().map(|p| wrap(p, &length)).collect();
        let mut cells = vec![Vec::new(); n[0] * n[1] * n[2]];
        for (i, p) in wrapped.iter().enumerate() {
            let c = Self::cell_of(p, &size, &n);
            cells[(c[0] * n[1] + c[1]) * n[2] + c[2]].push(i);
        }
        CellList { length, n, size, cells, points: wrapped }
    }

    fn cell_of(p: &[f64; 3], size: &[f64; 3], n: &[usize; 3]) -> [usize; 3] {
        let mut c = [0usize; 3];
        for k in 0..3 {
            c[k] = ((p[k] / size[k]) as usize).min(n[k] - 1);
        }
        c
    }

    /// Call `f(index)` for every point in the cells around `p` (a superset of those within cutoff).
    pub fn for_each_near<F: FnMut(usize)>(&self, p: &[f64; 3], mut f: F) {
        let w = wrap(p, &self.length);
        let c = Self::cell_of(&w, &self.size, &self.n);
        let range = |k: usize| -> Vec<usize> {
            let n = self.n[k];
            if n < 3 {
                (0..n).collect()
            } else {
                vec![(c[k] + n - 1) % n, c[k], (c[k] + 1) % n]
            }
        };
        let (rx, ry, rz) = (range(0), range(1), range(2));
        for &x in &rx {
            for &y in &ry {
                for &z in &rz {
                    for &i in &self.cells[(x * self.n[1] + y) * self.n[2] + z] {
                        f(i);
                    }
                }
            }
        }
    }
}
