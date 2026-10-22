# Synthetic test fixtures

`alanine6.pdb` is a generated six-alanine molecular geometry used solely to exercise martinize2 input/output and residue counting. `CCCCCC` is an explicit test assumption, not a measured or predicted secondary-structure assignment. This fixture and the artificial measurements used by tests do not validate physical predictions, and tests never add them to the user's research store.

`villin_1vii.pdb` is the first model of PDB entry 1VII (villin headpiece, 36 residues, with hydrogens) as distributed in `mdtraj/mdtraj@8ca58170` `examples/1vii_3frames.pdb` (file SHA-256 `ba3c478d…`). PDB entry data are CC0. It is used to exercise the SAP scan and SAP-targeted PACKMOL placement; the tests do not validate aggregation behaviour.

`hspipy_hsp_example.csv` is `examples/hsp_example.csv` from `Gnpd/HSPiPy@b2cd91aa` (MIT, file SHA-256 `3b4b2f1f…`): 37 solvent scores used to compare the Hansen sphere fit with HSPiPy 1.1.8, whose classic fit reaches DATAFIT 0.97361 on it with score 1 as good. It is not a measurement of any polymer in the user's store.

`propka_1HPX.pdb` and `propka_1HPX.json` are `tests/pdb/1HPX.pdb` and `tests/results/1HPX.json` from `jensengroup/propka@94142ffc` (tag v3.5.1; SHA-256 `4112a180…` and `d78f9383…`). The structure is PDB entry 1HPX (HIV-1 protease with inhibitor KNI-272, PDB data CC0) and the JSON holds PROPKA's own reference pKa values and isoelectric points, used to check that Polymer Lab reproduces the upstream regression.
