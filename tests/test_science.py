import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from workbench.science import (build_linear_polymer, chain_metrics, initial_chains,
                               run_workflow, soft_forces, validate_config)
from workbench.external import available, executable, protein_metrics


class ScientificChecks(unittest.TestCase):
    def test_force_is_negative_energy_gradient(self):
        # Nonbonded repulsion and harmonic forces both enter this configuration.
        positions = np.array([[0., 0., 0.], [1.1, .1, 0.], [.35, .8, .15]])
        bonds = np.array([[0, 1]])
        force, _ = soft_forces(positions, bonds)
        numerical = np.zeros_like(force)
        epsilon = 1e-6
        for i in range(3):
            for j in range(3):
                plus, minus = positions.copy(), positions.copy()
                plus[i, j] += epsilon
                minus[i, j] -= epsilon
                numerical[i, j] = -(soft_forces(plus, bonds)[1] - soft_forces(minus, bonds)[1]) / (2 * epsilon)
        np.testing.assert_allclose(force, numerical, rtol=1e-7, atol=1e-7)
        np.testing.assert_allclose(force.sum(axis=0), 0, atol=1e-12)

    def test_radius_gyration_known_chain(self):
        result = chain_metrics(np.array([[-1., 0, 0], [0., 0, 0], [1., 0, 0]]), 3, 1)
        self.assertAlmostEqual(result["rg"], np.sqrt(2/3))
        self.assertAlmostEqual(result["end_to_end"], 2)

    def test_rdkit_polymer_connectivity_and_caps(self):
        from rdkit import Chem
        mol, metadata = build_linear_polymer("[*:1]CC[*:2]", 3, 23)
        self.assertEqual(Chem.MolToSmiles(Chem.RemoveHs(mol)), "CCCCCC")
        self.assertEqual(len(Chem.GetMolFrags(mol)), 1)
        self.assertEqual(mol.GetNumAtoms(), 20)  # Hexane C6H14, not six isolated carbons.
        self.assertTrue(np.isfinite(mol.GetConformer().GetPositions()).all())
        self.assertEqual(metadata["length_unit"], "angstrom")

    def test_invalid_inputs_fail_before_execution(self):
        for raw in ({"seed": 0}, {"steps": -1}, {"temperature": float("nan")}, {"chains": 16, "monomers": 200}, {"engine": "shell"}):
            with self.assertRaises(ValueError):
                validate_config(raw)
        for unit in ("CC", "[*]C", "[*]C([*])[*]", "*=CC*"):
            with self.assertRaises(ValueError):
                build_linear_polymer(unit, 2)

    def test_numpy_reproducible_and_not_static(self):
        config = {"engine": "numpy", "monomers": 5, "chains": 1, "steps": 30, "seed": 17}
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            one, two = run_workflow(config, d1), run_workflow(config, d2)
            self.assertEqual(one["metrics"]["series"], two["metrics"]["series"])
            trajectory = json.loads((Path(d1) / "trajectory.json").read_text())
            self.assertNotEqual(trajectory["frames"][0]["positions"], trajectory["frames"][-1]["positions"])
            self.assertEqual(trajectory["unit"], "sigma")
            self.assertFalse(one["model"]["equilibrated"])
            if one["metrics"]["mdanalysis"]:
                self.assertLess(one["metrics"]["mdanalysis"]["max_rg_difference"], 1e-5)

    def test_lammps_matches_independent_potential(self):
        try:
            from lammps import lammps
            probe = lammps(cmdargs=["-screen", "none", "-log", "none"])
            probe.close()
        except (ImportError, OSError) as exc:
            self.skipTest(f"Native LAMMPS unavailable: {exc}")
        config = {"engine": "lammps", "monomers": 4, "chains": 2, "steps": 10, "seed": 31}
        positions, bonds = initial_chains(4, 2, 31)
        expected_energy = soft_forces(positions, bonds)[1]
        with tempfile.TemporaryDirectory() as directory:
            result = run_workflow(config, directory)
            self.assertAlmostEqual(result["metrics"]["series"][0]["potential_energy"], expected_energy, places=7)
            self.assertTrue((Path(directory) / "restart.lammps").is_file())

    def test_af3_export_not_misreported_as_prediction(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_workflow({"route": "protein", "protein_mode": "export", "sequence": "ACDEFGHIK", "ligand_smiles": "CCO"}, directory)
            self.assertEqual(result["status"], "prepared")
            self.assertFalse((Path(directory)/"structure.pdb").exists())
            inp = json.loads((Path(directory)/"alphafold3_input.json").read_text())
            self.assertEqual(inp["dialect"], "alphafold3")
            self.assertEqual(inp["sequences"][1]["ligand"]["smiles"], "CCO")

    def test_import_does_not_assume_bfactor_is_plddt(self):
        pdb = "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00 90.00           C\n"
        ordinary = protein_metrics(pdb)
        self.assertNotIn("mean_plddt", ordinary["summary"])
        confidence = protein_metrics(pdb, plddt=True)
        self.assertEqual(confidence["summary"]["mean_plddt"], 90)

    def test_nonfinite_protein_atom_and_confidence_rejected(self):
        pdb = "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00 90.00           C\n"
        non_ca_nan = "ATOM      2  CB  ALA A   1         nan   2.000   3.000  1.00 90.00           C\n"
        with self.assertRaises(ValueError):
            protein_metrics(pdb + non_ca_nan)
        with self.assertRaises(ValueError):
            protein_metrics(pdb[:60] + "   nan" + pdb[66:], plddt=True)

    def test_radonpy_seed_repeatability_and_temperature(self):
        if not available("radonpy") or not executable("lmp", "LAMMPS_EXEC"):
            self.skipTest("Optional native RadonPy/LAMMPS route unavailable")
        try:
            from lammps import lammps
            probe = lammps(cmdargs=["-screen", "none", "-log", "none"])
            probe.close()
        except (ImportError, OSError) as exc:
            self.skipTest(f"Native LAMMPS unavailable: {exc}")
        from rdkit import Chem
        config = {"route": "polymer_atomistic", "atomistic_mode": "simulate", "monomers": 2,
                  "chains": 1, "steps": 10, "seed": 77, "temperature_kelvin": 350.0}
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first, second = run_workflow(config, one), run_workflow(config, two)
            seed_record = first["metadata"]["random_seeds"]
            self.assertTrue(seed_record["lammps"])
            self.assertEqual(seed_record, second["metadata"]["random_seeds"])
            first_coords = Chem.MolFromMolFile(str(Path(one)/"final.mol"), removeHs=False).GetConformer().GetPositions()
            second_coords = Chem.MolFromMolFile(str(Path(two)/"final.mol"), removeHs=False).GetConformer().GetPositions()
            np.testing.assert_array_equal(first_coords, second_coords)
            lammps_input = (Path(one)/"radon_lmp.in").read_text()
            self.assertIn("nvt temp 350.000000 350.000000", lammps_input)


if __name__ == "__main__":
    unittest.main()
