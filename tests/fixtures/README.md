# Synthetic test fixtures

`alanine6.pdb` is a generated six-alanine molecular geometry used solely to exercise martinize2 input/output and residue counting. `CCCCCC` is an explicit test assumption, not a measured or predicted secondary-structure assignment. This fixture and the artificial measurements used by tests do not validate physical predictions, and tests never add them to the user's research store.

`villin_1vii.pdb` is the first model of PDB entry 1VII (villin headpiece, 36 residues, with hydrogens) as distributed in `mdtraj/mdtraj@8ca58170` `examples/1vii_3frames.pdb` (file SHA-256 `ba3c478d…`). PDB entry data are CC0. It is used to exercise the SAP scan and SAP-targeted PACKMOL placement; the tests do not validate aggregation behaviour.

`hspipy_hsp_example.csv` is `examples/hsp_example.csv` from `Gnpd/HSPiPy@b2cd91aa` (MIT, file SHA-256 `3b4b2f1f…`): 37 solvent scores used to compare the Hansen sphere fit with HSPiPy 1.1.8, whose classic fit reaches DATAFIT 0.97361 on it with score 1 as good. It is not a measurement of any polymer in the user's store.
