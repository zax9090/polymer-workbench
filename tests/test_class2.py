"""Class II parameter lookup (msi2lmp rules), the bundled LAMMPS pcff.frc and EMC/PCFF bulk cells."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from workbench import class2, engines, pcff

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
MSI2LMP = ROOT / "evidence/extensions/sources/lammps-msi2lmp/tools/msi2lmp"
PCFF = class2.read_frc(pcff.LAMMPS_PCFF)


def test_bundled_pcff_matches_the_pinned_lammps_file():
    data = pcff.LAMMPS_PCFF.read_bytes()
    assert hashlib.sha256(data).hexdigest() == pcff.LAMMPS_PCFF_SHA256
    catalog = {repo["id"]: repo for repo in json.loads((ROOT / "tools/rea/extension_catalog.json").read_text())}
    pinned = next(f for f in catalog["lammps-msi2lmp"]["source_files"] if f["path"] == "tools/msi2lmp/frc_files/pcff.frc")
    assert pinned["sha256"] == pcff.LAMMPS_PCFF_SHA256
    assert "public domain" in (ROOT / "workbench/data/licenses/LAMMPS-msi2lmp-frc_files-README.txt").read_text()


def test_frc_reader_follows_msi2lmp_conventions():
    assert PCFF.version[-1][:2] == ["pcff.frc", "4.0"]
    assert PCFF.pair("c") == [0.054, 4.01] and PCFF.mass("hc") == pytest.approx(1.00797)
    assert PCFF.bond("c", "c") == [1.53, 299.67, -501.77, 679.81]
    # Reversed and equivalence matches give the same coefficients.
    assert PCFF.bond("hc", "c") == PCFF.bond("c", "hc")
    assert PCFF.angle("hc", "c", "c") == PCFF.angle("c", "c", "hc")
    # Bond-angle constants swap with the direction of the match.
    forward, backward = PCFF.bondangle("c", "c", "hc"), PCFF.bondangle("hc", "c", "c")
    assert forward[:2] == backward[1::-1] and forward[2:] == backward[:1:-1]
    assert PCFF.increment("c", "hc") == pytest.approx(-0.053) and PCFF.increment("hc", "c") == pytest.approx(0.053)
    assert PCFF.dihedral("xx", "yy", "zz", "ww") is None


@pytest.mark.skipif(not (MSI2LMP / "test/reference").is_dir(), reason="pinned msi2lmp sources not fetched (scripts/audit_extensions.py --fetch-pinned)")
@pytest.mark.parametrize("system", ["ethane", "benzene", "naphthalene", "cnt-hexagonal", "hap_crystal", "h2-h2o"])
def test_port_reproduces_msi2lmp_reference_data(system):
    """Every coefficient msi2lmp wrote for the class2b (pcff) tests is recomputed from the same frc file."""
    frc = class2.read_frc(MSI2LMP / "frc_files/pcff.frc")
    types = []
    for line in (MSI2LMP / f"test/{system}-class2b.car").read_text().splitlines():
        tokens = line.split()
        if len(tokens) >= 9 and not line.startswith(("!", "PBC", "end", "@")):
            try:
                [float(v) for v in tokens[1:4]]
            except ValueError:
                continue
            types.append(tokens[6][:4])
    sections, current = {}, None
    for line in (MSI2LMP / f"test/reference/{system}-class2b.data").read_text().splitlines():
        body = line.split("#")[0].strip()
        if not body:
            continue
        if body[0].isalpha():
            current = body
            sections[current] = []
        elif current:
            try:
                sections[current].append([float(v) for v in body.split()])
            except ValueError:
                pass
    atom_type = {int(row[0]): int(row[2]) for row in sections["Atoms"]}
    name = {kind: types[atom - 1] for atom, kind in atom_type.items()}
    has_cp = "cp" in name.values()
    checked = 0
    for row in sections["Pair Coeffs"]:
        assert row[1:] == pytest.approx(frc.pair(name[int(row[0])]), abs=1e-4)
        checked += 1
    for topology, coefficients, method in (("Bonds", "Bond Coeffs", "bond"), ("Angles", "Angle Coeffs", "angle"),
                                           ("Angles", "BondBond Coeffs", "bondbond"), ("Angles", "BondAngle Coeffs", "bondangle"),
                                           ("Dihedrals", "Dihedral Coeffs", "dihedral"), ("Dihedrals", "MiddleBondTorsion Coeffs", "middlebondtorsion"),
                                           ("Dihedrals", "EndBondTorsion Coeffs", "endbondtorsion"), ("Dihedrals", "AngleTorsion Coeffs", "angletorsion"),
                                           ("Dihedrals", "AngleAngleTorsion Coeffs", "angleangletorsion"), ("Dihedrals", "BondBond13 Coeffs", "bondbond13")):
        if topology not in sections or coefficients not in sections:
            continue
        table = {int(row[0]): row[1:] for row in sections[coefficients]}
        seen = set()
        for row in sections[topology]:
            if int(row[1]) in seen:
                continue
            seen.add(int(row[1]))
            atoms = [types[int(a) - 1] for a in row[2:]]
            expected = frc.bondbond13(*atoms, has_cp=has_cp) if method == "bondbond13" else getattr(frc, method)(*atoms)
            assert table[int(row[1])] == pytest.approx(expected, rel=1e-3, abs=1e-3), (method, atoms)
            checked += 1
    assert checked >= 5


def test_emc_parameters_and_charges_match_lammps_pcff():
    report = class2.compare_parameters((FIXTURES / "emc_ps.params").read_text(), PCFF)
    assert report["checked"] > 100 and report["matching"] == report["checked"] and not report["differences"]
    # EMC fills c-c-c1-cp torsions (and bond-bond-1-3 terms msi2lmp demands once a cp type exists)
    # from cff91_auto fallbacks; nothing else is missing from LAMMPS' pcff.frc.
    torsions = {"dihedral", "middlebondtorsion", "endbondtorsion", "angletorsion", "angleangletorsion", "bondbond13"}
    assert report["not_in_reference"] == 30 and {m["term"] for m in report["missing"]} == torsions
    charges = class2.compare_charges((FIXTURES / "emc_ps.data").read_text(), PCFF)
    assert charges["atoms"] == 100 and charges["max_abs_difference"] < 1e-3 and not charges["missing_increments"]
    assert charges["net_charge"] == pytest.approx(0, abs=1e-6)


def test_emc_chemistry_and_protocol_text():
    assert pcff.emc_smiles("[*]CC([*])c1ccccc1") == "*CC(*)c1ccccc1"
    for bad in ("[*]CC", "[*]CC([*])[*]", "[*]CC([*])[NH3+]", "C(["):
        with pytest.raises(ValueError):
            pcff.emc_smiles(bad)
    text = pcff.chemistry("[*]CC[*]", 20, 4, 7)
    assert "field\t\tpcff" in text and "poly\t\talternate\t4" in text and "rep,20,hterm,2" in text and "seed\t\t7" in text
    script, steps = pcff.lammps_input(300, 0.001, 1234)
    assert steps["step_list"][0] == [100, 100, 100] and steps["sampling"] % steps["thermo"] == 0
    for pressure in (1000, 30000, 50000, 25000, 5000, 500):
        assert f"iso {float(pressure)} {float(pressure)} 1000.0" in script
    assert "fix shake all shake 1e-4 1000 0 m 1.0" in script and "log sampling.log" in script
    masses, atoms, lengths = pcff.read_cell(FIXTURES / "emc_ps.data")
    rg, chains = pcff.chain_rg(FIXTURES / "emc_ps.data")
    assert len(atoms) == 100 and chains == 2 and 2 < rg < 10 and lengths.min() > 10


def _emc_available():
    return engines.status("emc")["available"] and shutil.which("perl")


@pytest.mark.skipif(not _emc_available(), reason="EMC 9.4.4 not installed (EMC_ROOT or the engine cache)")
def test_pcff_bulk_cell_end_to_end(tmp_path):
    pytest.importorskip("radonpy")
    from workbench.external import executable
    if not executable("lmp", "LAMMPS_EXEC"):
        pytest.skip("Requires a LAMMPS executable with CLASS2")
    from workbench.descriptors import extract_job_features
    from workbench.models import JobConfig
    from workbench.science import run_workflow
    config = JobConfig.model_validate({"route": "polymer_atomistic", "atomistic_mode": "bulk", "atomistic_forcefield": "pcff",
                                       "repeat_smiles": "[*]CC([*])c1ccccc1", "monomers": 10, "chains": 4, "protocol_scale": 0.001}).model_dump()
    result = run_workflow(config, tmp_path)
    assert result["status"] == "completed", result
    complete = json.loads((tmp_path / "bulk_complete.json").read_text())
    assert complete["atoms"] == 648 and complete["chains"] == 4 and complete["builder"]["version"] == "9.4.4"
    parity = complete["parity"]
    assert parity["coefficients_matching"] == parity["coefficients_checked"] > 100 and parity["net_charge"] == pytest.approx(0, abs=1e-6)
    assert 0.3 < complete["properties"]["density"]["value"] < 1.5
    features = extract_job_features(tmp_path)
    assert "PCFF" in features["values"]["bulk_density"]["method"]
    assert any("Stage lengths" in w for w in result["warnings"]) and (tmp_path / "pcff_parity.json").is_file()
