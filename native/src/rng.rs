//! Deterministic per-sample random streams shared by the kernels.

/// xoshiro256** seeded through SplitMix64.
pub struct Rng([u64; 4]);

impl Rng {
    pub fn new(seed: u64, stream: u64) -> Self {
        let mut x = seed ^ stream.wrapping_mul(0x9E37_79B9_7F4A_7C15).rotate_left(17);
        let mut next = || {
            x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
            let mut z = x;
            z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
            z ^ (z >> 31)
        };
        Rng([next(), next(), next(), next()])
    }

    pub fn next_u64(&mut self) -> u64 {
        let s = &mut self.0;
        let result = s[1].wrapping_mul(5).rotate_left(7).wrapping_mul(9);
        let t = s[1] << 17;
        s[2] ^= s[0];
        s[3] ^= s[1];
        s[1] ^= s[2];
        s[0] ^= s[3];
        s[2] ^= t;
        s[3] = s[3].rotate_left(45);
        result
    }

    /// Uniform in [0, 1).
    pub fn uniform(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / (1u64 << 53) as f64)
    }

    pub fn normal(&mut self) -> f64 {
        // Box-Muller; 1 - u keeps the logarithm finite.
        let (u, v) = (1.0 - self.uniform(), self.uniform());
        (-2.0 * u.ln()).sqrt() * (2.0 * std::f64::consts::PI * v).cos()
    }

    pub fn unit_vector(&mut self) -> [f64; 3] {
        loop {
            let v = [self.normal(), self.normal(), self.normal()];
            let n = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt();
            if n > 1e-12 {
                return [v[0] / n, v[1] / n, v[2] / n];
            }
        }
    }

    /// Uniformly distributed rotation matrix (from a random unit quaternion).
    pub fn rotation(&mut self) -> [[f64; 3]; 3] {
        let q = loop {
            let q = [self.normal(), self.normal(), self.normal(), self.normal()];
            let n = (q.iter().map(|x| x * x).sum::<f64>()).sqrt();
            if n > 1e-12 {
                break [q[0] / n, q[1] / n, q[2] / n, q[3] / n];
            }
        };
        let [a, b, c, d] = q;
        [
            [a * a + b * b - c * c - d * d, 2.0 * (b * c - a * d), 2.0 * (b * d + a * c)],
            [2.0 * (b * c + a * d), a * a - b * b + c * c - d * d, 2.0 * (c * d - a * b)],
            [2.0 * (b * d - a * c), 2.0 * (c * d + a * b), a * a - b * b - c * c + d * d],
        ]
    }
}
