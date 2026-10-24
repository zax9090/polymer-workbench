//! Molecular connectivity indices of a hydrogen-suppressed graph and the backbone of a repeat unit.
//!
//! Randić (J. Am. Chem. Soc. 97, 6609, 1975) and Kier & Hall (Molecular Connectivity in Chemistry
//! and Drug Research, 1976):
//!   chi0 = Σ_atoms δ^-1/2,  chi1 = Σ_bonds (δ_i δ_j)^-1/2
//! with δ the number of bonded heavy atoms. The valence indices use
//!   δv = (Zv − h) / (Z − Zv − 1)
//! where Z is the atomic number, Zv the number of valence electrons and h the attached hydrogens;
//! the denominator is 1 for first- and second-row atoms. Formal charge is ignored, as in RDKit.
//!
//! A polymer repeat unit is passed as a ring closed by the bond between its two attachment atoms
//! (a self-loop when both attachments sit on one atom). Each atom then has the neighbours it has
//! in the middle of an infinitely long chain, so the indices are values per repeat unit.

#[derive(Debug, Clone, PartialEq)]
pub struct Indices {
    pub chi0: f64,
    pub chi1: f64,
    pub chi0v: f64,
    pub chi1v: f64,
    pub delta: Vec<f64>,
    pub delta_v: Vec<f64>,
}

const NOBLE: [u32; 6] = [2, 10, 18, 36, 54, 86];

/// Valence electrons of a main-group element (s and p electrons outside the noble-gas core).
pub fn valence_electrons(z: u32) -> Result<u32, String> {
    if z == 0 || z > 86 {
        return Err(format!("unsupported atomic number {z}"));
    }
    let core = NOBLE.iter().rev().find(|&&n| n < z).copied().unwrap_or(0);
    let outer = z - core;
    // Rows 4-6 fill ten d (and fourteen f) electrons before the p shell.
    let filled = match core {
        18 | 36 => 10,
        54 => 24,
        _ => 0,
    };
    if outer <= 2 {
        return Ok(outer);
    }
    if outer <= filled {
        return Err(format!("transition-metal atomic number {z} has no Kier-Hall valence delta here"));
    }
    Ok(outer - filled)
}

fn check_bonds(n: usize, bonds: &[(usize, usize)]) -> Result<(), String> {
    match bonds.iter().find(|&&(a, b)| a >= n || b >= n) {
        Some(&(a, b)) => Err(format!("bond ({a}, {b}) refers to an atom outside 0..{n}")),
        None => Ok(()),
    }
}

/// Simple and valence connectivity indices; a self-loop (i, i) counts twice in δ_i.
pub fn indices(atomic_numbers: &[u32], hydrogens: &[u32], bonds: &[(usize, usize)]) -> Result<Indices, String> {
    let n = atomic_numbers.len();
    if hydrogens.len() != n {
        return Err("atomic_numbers and hydrogens must have the same length".into());
    }
    check_bonds(n, bonds)?;
    let mut degree = vec![0u32; n];
    for &(a, b) in bonds {
        degree[a] += 1;
        degree[b] += 1;
    }
    let mut delta_v = Vec::with_capacity(n);
    for (i, (&z, &h)) in atomic_numbers.iter().zip(hydrogens).enumerate() {
        let zv = valence_electrons(z)?;
        if h > zv {
            return Err(format!("atom {i} has more hydrogens ({h}) than valence electrons ({zv})"));
        }
        let value = (zv - h) as f64 / (z as i64 - zv as i64 - 1).max(1) as f64;
        if degree[i] > 0 && value <= 0.0 {
            return Err(format!("atom {i} has a non-positive valence delta"));
        }
        delta_v.push(value);
    }
    let delta: Vec<f64> = degree.iter().map(|&d| d as f64).collect();
    // Atoms without heavy neighbours (methane, water) have no defined term and are left out.
    let zeroth = |d: &[f64]| d.iter().zip(&degree).filter(|(_, &k)| k > 0).map(|(x, _)| x.powf(-0.5)).sum::<f64>();
    let first = |d: &[f64]| bonds.iter().map(|&(a, b)| (d[a] * d[b]).powf(-0.5)).sum::<f64>();
    Ok(Indices { chi0: zeroth(&delta), chi1: first(&delta), chi0v: zeroth(&delta_v), chi1v: first(&delta_v), delta, delta_v })
}

/// Atoms of the biconnected component that contains `closure` (Hopcroft-Tarjan with an explicit stack).
/// For a repeat unit closed into a ring this is every atom on a path between the attachment atoms,
/// including whole rings in the main chain, and none of the singly attached side groups.
pub fn block_of(n: usize, bonds: &[(usize, usize)], closure: usize) -> Result<Vec<bool>, String> {
    check_bonds(n, bonds)?;
    let &(ca, cb) = bonds.get(closure).ok_or("closure bond index is out of range")?;
    let mut in_block = vec![false; n];
    if ca == cb {
        in_block[ca] = true;
        return Ok(in_block);
    }
    let mut adjacency = vec![Vec::new(); n];
    for (e, &(a, b)) in bonds.iter().enumerate() {
        if a != b {
            adjacency[a].push((b, e));
            adjacency[b].push((a, e));
        }
    }
    let (mut order, mut low) = (vec![usize::MAX; n], vec![0usize; n]);
    let (mut counter, mut edges): (usize, Vec<usize>) = (0, Vec::new());
    let mut stack: Vec<(usize, usize, usize)> = Vec::new(); // (vertex, parent edge, next neighbour)
    order[ca] = 0;
    low[ca] = 0;
    counter += 1;
    stack.push((ca, usize::MAX, 0));
    while let Some(&mut (v, parent, ref mut next)) = stack.last_mut() {
        if *next < adjacency[v].len() {
            let (w, e) = adjacency[v][*next];
            *next += 1;
            if e == parent {
                continue;
            }
            if order[w] == usize::MAX {
                edges.push(e);
                order[w] = counter;
                low[w] = counter;
                counter += 1;
                stack.push((w, e, 0));
            } else if order[w] < order[v] {
                edges.push(e);
                low[v] = low[v].min(order[w]);
            }
            continue;
        }
        stack.pop();
        if let Some(&(u, _, _)) = stack.last() {
            low[u] = low[u].min(low[v]);
            if low[v] >= order[u] {
                // Edges above and including the tree edge (u, v) form one block.
                let mut block = Vec::new();
                while let Some(e) = edges.pop() {
                    block.push(e);
                    if e == parent {
                        break;
                    }
                }
                if block.contains(&closure) {
                    for e in block {
                        in_block[bonds[e].0] = true;
                        in_block[bonds[e].1] = true;
                    }
                }
            }
        }
    }
    Ok(in_block)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn close(a: f64, b: f64) -> bool {
        (a - b).abs() < 1e-12
    }

    #[test]
    fn ring_and_aromatic_values_match_hand_sums() {
        let ring: Vec<(usize, usize)> = (0..6).map(|i| (i, (i + 1) % 6)).collect();
        let cyclohexane = indices(&[6; 6], &[2; 6], &ring).unwrap();
        assert!(close(cyclohexane.chi0, 6.0 / 2f64.sqrt()) && close(cyclohexane.chi1, 3.0));
        let benzene = indices(&[6; 6], &[1; 6], &ring).unwrap();
        assert!(close(benzene.chi0v, 6.0 / 3f64.sqrt()) && close(benzene.chi1v, 2.0));
    }

    #[test]
    fn higher_row_valence_delta_uses_the_core_denominator() {
        assert_eq!(valence_electrons(17).unwrap(), 7);
        assert_eq!(valence_electrons(35).unwrap(), 7);
        assert_eq!(valence_electrons(53).unwrap(), 7);
        assert_eq!(valence_electrons(14).unwrap(), 4);
        assert!(valence_electrons(26).is_err());
        let chloromethyl = indices(&[6, 17], &[3, 0], &[(0, 1)]).unwrap();
        assert!(close(chloromethyl.delta_v[1], 7.0 / 9.0));
    }

    #[test]
    fn one_atom_repeat_unit_closes_on_itself() {
        // Polymethylene per CH2: delta 2, one backbone bond of (2*2)^-1/2.
        let unit = indices(&[6], &[2], &[(0, 0)]).unwrap();
        assert!(close(unit.chi0, 2f64.powf(-0.5)) && close(unit.chi1, 0.5));
        assert_eq!(block_of(1, &[(0, 0)], 0).unwrap(), vec![true]);
    }

    #[test]
    fn backbone_keeps_main_chain_rings_and_drops_side_groups() {
        // Ring 0-5 in the main chain (closure 0-3 through the ring's para positions), side group 6-7 on atom 1.
        let mut bonds: Vec<(usize, usize)> = (0..6).map(|i| (i, (i + 1) % 6)).collect();
        bonds.push((1, 6));
        bonds.push((6, 7));
        bonds.push((0, 3));
        let closure = bonds.len() - 1;
        let block = block_of(8, &bonds, closure).unwrap();
        assert_eq!(block, vec![true, true, true, true, true, true, false, false]);
        assert!(block_of(8, &bonds, 99).is_err());
    }
}
