"""Epoxy–amine bond/react curing: reactant/product graphs, template generation and an end-to-end cure."""
import json
import re
import shutil
from pathlib import Path

import pytest

pytest.importorskip("rdkit")
pytest.importorskip("networkx")
from workbench import crosslink, engines  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
DGEBA = "CC(C)(c1ccc(OCC2CO2)cc1)c1ccc(OCC2CO2)cc1"
EDA = "NCCN"


def test_species_and_ideal_gel_point():
    spec = crosslink.species(DGEBA, EDA)
    assert spec["smiles"]["adduct_a"] == "CC(C)(c1ccc(OCC(O)CNCCN)cc1)c1ccc(OCC2CO2)cc1"
    assert spec["epoxides_per_resin"] == 2 and spec["amine_hydrogens_per_hardener"] == 4
    centre = spec["reactions"][0]["centre"]
    product = spec["adduct_a"]
    assert product.GetBondBetweenAtoms(centre["nitrogen"], centre["carbon"]) and product.GetBondBetweenAtoms(centre["oxygen"], centre["hydrogen"])
    assert product.GetBondBetweenAtoms(centre["carbon"], centre["oxygen"]) is None
    # Macosko–Miller for a stoichiometric diepoxide (f=2) and tetrafunctional amine (f=4): 1/sqrt(3).
    assert crosslink.gel_point(spec, {"resin": 2, "hardener": 1})["epoxide_conversion"] == pytest.approx(3 ** -0.5)
    assert crosslink.gel_point(spec, {"resin": 4, "hardener": 1})["epoxide_conversion"] == pytest.approx((0.5 / 3) ** 0.5)
    assert crosslink.gel_point(spec, {"resin": 8, "hardener": 1})["epoxide_conversion"] is None  # too few amine hydrogens to gel
    with pytest.raises(ValueError, match="two terminal epoxide"):
        crosslink.species("CCC1CO1", EDA)
    with pytest.raises(ValueError, match="primary amine"):
        crosslink.species(DGEBA, "CN(C)C")
    with pytest.raises(ValueError, match="attachment"):
        crosslink.species("[*]" + DGEBA, EDA)


def _section(text, name):
    lines = text.splitlines()
    start = lines.index(name) + 2
    rows = []
    for line in lines[start:]:
        if not line.strip():
            break
        rows.append(line.split())
    return rows


def test_templates_cut_from_emc_molecules():
    spec = crosslink.species(DGEBA, EDA)
    topology = crosslink.read_topology(FIXTURES / "emc_dgeba_eda.data")
    molecules = {"resin": 1, "hardener": 3, "adduct_a": 4, "adduct_b": 5}
    templates = crosslink.build_templates(spec, topology, molecules)
    assert [t["name"] for t in templates] == ["primary", "secondary"]
    for template in templates:
        pre, post = template["pre"], template["post"]
        assert int(re.search(r"(\d+) atoms", pre).group(1)) == template["atoms"] == int(re.search(r"(\d+) atoms", post).group(1))
        pre_charge = sum(float(q) for _, q in _section(pre, "Charges"))
        post_charge = sum(float(q) for _, q in _section(post, "Charges"))
        assert pre_charge == pytest.approx(post_charge, abs=1e-6)
        # Only atoms whose charge changes are recharged; bond/react would otherwise reset edge atoms.
        fragment = _section(pre, "Fragments")[0]
        assert fragment[0] == "recharge" and len(fragment) - 1 == template["recharged_atoms"]
        edges = template["map"].split("EdgeIDs")[1].split("Equivalences")[0].split()
        assert len(edges) == template["edge_atoms"] and not set(edges) & set(fragment[1:])
        # One N-C bond formed, one ring C-O broken, one N-H moved to O-H.
        pre_bonds = {tuple(sorted(r[2:])) for r in _section(pre, "Bonds")}
        post_bonds = {tuple(sorted(r[2:])) for r in _section(post, "Bonds")}
        assert len(post_bonds - pre_bonds) == 2 and len(pre_bonds - post_bonds) == 2
        assert "InitiatorIDs" in template["map"] and len(_section(pre, "Molecules")) == template["atoms"]
    assert {r[1] for r in _section(templates[0]["pre"], "Molecules")} == {"1", "2"}


def test_lammps_input_uses_custom_charges_and_relaxes_after_cure():
    spec = crosslink.species(DGEBA, EDA)
    config = {"temperature_kelvin": 300, "cure_temperature_kelvin": 450, "seed": 3, "reaction_cutoff_angstrom": 4.0, "target_conversion": 0.8}
    steps = crosslink.schedule(0.001, 5000)
    script = crosslink.lammps_input(spec, {"adduct_a": 4, "adduct_b": 5}, {"resin": 2, "hardener": 1}, config, steps)
    assert "delete_atoms group products bond yes" in script and "custom_charges recharge" in script
    assert script.count("stabilize_steps") == 2 and "fix stop all halt" in script and "variable conversion equal (f_rxns[1]+f_rxns[2])/4" in script
    # thermo_style must drop f_rxns before the fix is removed.
    assert script.index("log cooling.log") < script.index("unfix rxns")
    assert script.index("minimize 1.0e-4 1.0e-6 5000 50000") < script.index("write_data cured.data")


@pytest.mark.skipif(not (engines.status("emc")["available"] and shutil.which("perl")), reason="EMC 9.4.4 not installed (EMC_ROOT or the engine cache)")
def test_epoxy_cure_end_to_end(tmp_path):
    pytest.importorskip("radonpy")
    from workbench.external import executable
    if not executable("lmp", "LAMMPS_EXEC"):
        pytest.skip("Requires a LAMMPS executable with REACTION and CLASS2")
    from workbench.descriptors import extract_job_features
    from workbench.models import JobConfig
    from workbench.science import run_workflow
    config = JobConfig.model_validate({"route": "polymer_atomistic", "atomistic_mode": "crosslink", "resin_count": 6, "hardener_count": 3,
                                       "protocol_scale": 0.001, "cure_steps": 6000, "target_conversion": 0.3}).model_dump()
    result = run_workflow(config, tmp_path)
    assert result["status"] == "completed", result
    complete = json.loads((tmp_path / "cure_complete.json").read_text())
    conversion = complete["conversion"]
    assert conversion["primary_reactions"] + conversion["secondary_reactions"] > 0
    assert conversion["epoxide"] == pytest.approx((conversion["primary_reactions"] + conversion["secondary_reactions"]) / 12)
    _, atoms, _ = crosslink.read_topology(tmp_path / "cured.data")
    assert sum(a["charge"] for a in atoms.values()) == pytest.approx(0, abs=1e-4)  # bond/react kept the cell neutral
    assert complete["parity"]["coefficients_matching"] == complete["parity"]["coefficients_checked"]
    assert "System is not charge neutral" not in (tmp_path / "lammps_class2.log").read_text()
    features = extract_job_features(tmp_path)
    assert features["values"]["epoxide_conversion"]["value"] == pytest.approx(conversion["epoxide"])


def test_cure_jobs_are_indexed_by_resin_and_hardener():
    from workbench.research_store import material_from_job
    job = {"config": {"route": "polymer_atomistic", "atomistic_mode": "crosslink", "repeat_smiles": "[*]CC[*]", "monomers": 24, "chains": 2,
                      "resin_smiles": DGEBA, "hardener_smiles": "C(N)CN", "resin_count": 16, "hardener_count": 8}}
    identity = material_from_job(job)["identity"]
    assert identity == {"resin_smiles": DGEBA, "hardener_smiles": "NCCN", "resin_count": 16, "hardener_count": 8, "representation": "epoxy_amine_network"}
    from workbench.screening import own_simulations
    prediction = {"repeat_smiles": "[*]CC[*]", "values": {"bulk_density": {"value": 0.85, "std": 0.02, "unit": "g/cm^3"}}}
    record = {"route": "polymer_atomistic", "config": job["config"], "material": {"repeat_smiles": "[*]CC[*]"},
              "features": {"values": {"bulk_density": {"value": 1.1, "unit": "g/cm^3"}}}}
    assert own_simulations(prediction, [record]) == []
