"""Blends-style chi: the Rust kernel against closed-form cases and the numpy reference, and the API."""
import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from workbench import blends
from workbench.server import create_app


def sphere(epsilon, radius=1.9, sigma=3.4):
    return {"smiles": "X", "atoms": 1, "mass": 40.0, "positions": np.zeros((1, 3)), "sigma": np.array([sigma]),
            "epsilon": np.array([epsilon]), "charge": np.zeros(1), "radius": np.array([radius])}


@pytest.mark.parametrize("use_native", [True, False])
def test_lj_spheres_give_the_geometric_mean_chi(use_native, monkeypatch):
    if use_native and blends.native is None:
        pytest.skip("Rust kernel workbench_native is not built")
    if not use_native:
        monkeypatch.setattr(blends, "native", None)
    a, b = sphere(0.2), sphere(0.05)
    comps = {"b": a, "s": b}
    stats = {}
    for pair in blends.PAIRS:
        energies, z = blends.contact_statistics(comps[pair[0]], comps[pair[1]], 2000, 60 if use_native else 25, 5)
        stats[pair] = {"energies": energies, "z": z}
    # Spheres touch at r = 2 x radius in every orientation, so every contact has the same energy.
    r = 3.8
    shape = 4 * ((3.4 / r) ** 12 - (3.4 / r) ** 6)
    assert stats["bs"]["energies"] == pytest.approx(math.sqrt(0.2 * 0.05) * shape)
    row = blends.chi_from_statistics(stats, [300.0])[0]
    z = {pair: stats[pair]["z"].mean() for pair in blends.PAIRS}
    expected = 0.5 * shape * (z["bs"] * math.sqrt(0.01) + z["sb"] * math.sqrt(0.01) - z["bb"] * 0.2 - z["ss"] * 0.05) / (blends.R_KCAL * 300)
    assert row["chi"] == pytest.approx(expected)
    # Equal-size spheres: Z ~ 7-10 for every pair, and chi > 0 as (sqrt(eps1) - sqrt(eps2))^2 > 0 demands.
    assert all(6 <= v <= 12 for v in z.values()) and row["chi"] > 0


def test_rust_contacts_match_the_numpy_energy():
    if blends.native is None:
        pytest.skip("Rust kernel workbench_native is not built")
    toluene, methanol = blends.prepare("Cc1ccccc1", seed=2), blends.prepare("CO", seed=3)
    centered = toluene["positions"] - toluene["positions"].mean(axis=0)
    for index in range(20):
        placed, energy, closest = blends.native.sample_contact(blends._arrays(toluene), blends._arrays(methanol), 9, index)
        assert closest == pytest.approx(1.0, abs=1e-9)
        assert energy == pytest.approx(blends.pair_energy(centered, toluene, np.asarray(placed), methanol), rel=1e-9, abs=1e-9)


def test_identical_statistics_give_zero_and_phase_behaviour_follows_flory_huggins():
    energies = np.array([-2.0, -1.0, -0.5])
    same = {pair: {"energies": energies, "z": np.array([8.0, 9.0])} for pair in blends.PAIRS}
    assert blends.chi_from_statistics(same, [300.0])[0]["chi"] == pytest.approx(0.0, abs=1e-12)
    phase = blends.phase_behaviour(-0.01, 10.0, 1000, 1000)
    chi_c = 0.5 * (2 / math.sqrt(1000)) ** 2
    assert phase["critical_chi"] == pytest.approx(chi_c) and phase["kind"] == "UCST"
    assert phase["critical_temperature_kelvin"] == pytest.approx(10.0 / (chi_c + 0.01))
    assert min(p["temperature_kelvin"] for p in phase["spinodal"]) < phase["critical_temperature_kelvin"] + 1e-6
    lcst = blends.phase_behaviour(3.0, -300.0, 1, 1)  # chi rises with T and reaches chi_c = 2 at 300 K
    assert lcst["kind"] == "LCST" and lcst["critical_temperature_kelvin"] == pytest.approx(300.0)


def test_blend_chi_reports_every_part(monkeypatch):
    result = blends.blend_chi("*CC(*)c1ccccc1", "c1ccccc1", temperatures=[298.15, 350.0], samples=4000, clusters=40, seed=3, bootstrap=10,
                              n_base=100, n_screen=1)
    assert result["components"]["base"]["smiles"] == "CCc1ccccc1" and result["kernel"]["engine"] in ("rust", "numpy")
    assert [row["temperature_kelvin"] for row in result["chi_vs_temperature"]] == [298.15, 350.0]
    assert math.isfinite(result["chi_reference"]["chi"]) and result["chi_reference"]["bootstrap_std"] > 0
    assert set(result["coordination"]) == set(blends.PAIRS) and result["phase"]["critical_chi"] == pytest.approx(0.5 * (0.1 + 1) ** 2)
    with pytest.raises(ValueError):
        blends.blend_chi("[Na+].[Cl-]", "C", samples=4000, clusters=40)


def test_blend_contacts_api(tmp_path):
    with TestClient(create_app(tmp_path / "jobs")) as client:
        response = client.post("/api/research/screening/blend-contacts",
                               json={"base_smiles": "CC", "screen_smiles": "CO", "samples": 3000, "clusters": 30, "temperatures": [300]})
        assert response.status_code == 200 and response.json()["chi_vs_temperature"][0]["temperature_kelvin"] == 300
        assert client.post("/api/research/screening/blend-contacts", json={"base_smiles": "not smiles", "screen_smiles": "CO", "samples": 3000, "clusters": 30}).status_code == 422
        assert client.post("/api/research/screening/blend-contacts", json={"base_smiles": "CC", "screen_smiles": "CO", "samples": 10}).status_code == 422
