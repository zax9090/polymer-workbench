"""Rust analyses against independent estimators and strict LAMMPS input checks."""
import json
import math

import numpy as np
import pytest

from workbench import structure_analysis as analysis


def write_cell(directory, positions, *, length=8.0, types=None, epsilon=0.1, dump=True):
    """A tiny synthetic atom_style full cell; coordinates are intentionally written out of ID order."""
    positions = np.asarray(positions, dtype=float)
    if positions.ndim == 2:
        positions = positions[None, :, :]
    count = positions.shape[1]
    types = list(types or [1] * count)
    lines = ["Synthetic periodic cell", "", f"{count} atoms", "2 atom types", "",
             f"0 {length} xlo xhi", f"0 {length} ylo yhi", f"0 {length} zlo zhi", "",
             "Masses", "", "1 12.011", "2 1.008", "", "Pair Coeffs # lj/cut", "",
             f"1 {epsilon} 3.4", f"2 {epsilon} 2.5", "", "Atoms # full", ""]
    for i in reversed(range(count)):
        x, y, z = positions[-1, i] % length
        lines.append(f"{i + 1} {i // 2 + 1} {types[i]} 0 {x:.12g} {y:.12g} {z:.12g}")
    (directory / "eq3_last.data").write_text("\n".join(lines) + "\n")
    if dump:
        lines = []
        for f, coords in enumerate(positions):
            lines += ["ITEM: TIMESTEP", str(f * 10), "ITEM: NUMBER OF ATOMS", str(count),
                      "ITEM: BOX BOUNDS pp pp pp", f"0 {length}", f"0 {length}", f"0 {length}",
                      "ITEM: ATOMS id type x y z ix iy iz"]
            for i in reversed(range(count)):
                x, y, z = coords[i] % length
                ix, iy, iz = np.floor(coords[i] / length).astype(int)
                lines.append(f"{i + 1} {types[i]} {x:.12g} {y:.12g} {z:.12g} {ix} {iy} {iz}")
        (directory / "eq3.dump").write_text("\n".join(lines) + "\n")
    (directory / "bulk_complete.json").write_text(json.dumps({"time_step_fs": 1.0, "temperature_kelvin": 300.0}))
    return positions


@pytest.fixture
def kernel():
    if analysis.native is None:
        pytest.skip("Rust kernel workbench_native is not built")
    return analysis.native


@pytest.fixture
def trajectory(tmp_path):
    rng = np.random.default_rng(14)
    origin = rng.integers(0, 64, (12, 3)) / 8.0
    increments = rng.integers(-4, 5, (16, 12, 3)) / 32.0
    positions = origin + increments.cumsum(axis=0)
    return tmp_path, write_cell(tmp_path, positions, types=[1, 2] * 6)


def test_rdf_matches_mdanalysis_with_identical_self_pair_exclusions(kernel, trajectory):
    mda = pytest.importorskip("MDAnalysis")
    from MDAnalysis.analysis.rdf import InterRDF

    directory, positions = trajectory
    universe = mda.Universe.empty(12)
    universe.load_new(positions % 8.0, dimensions=np.tile([8, 8, 8, 90, 90, 90], (len(positions), 1)))
    carbon, hydrogen = universe.atoms[::2], universe.atoms[1::2]
    same = InterRDF(carbon, carbon, nbins=37, range=(0.0, 3.73), exclusion_block=(1, 1)).run()
    different = InterRDF(carbon, hydrogen, nbins=37, range=(0.0, 3.73)).run()
    result = analysis.rdf(directory, pairs=["C-C", "C-H"], r_max=3.73, bins=37)
    # InterRDF's default includes self-pairs; exclusion_block also subtracts those pairs from N.
    np.testing.assert_allclose(result["pairs"][0]["g"], same.results.rdf, rtol=1e-4, atol=1e-12)
    np.testing.assert_allclose(result["pairs"][1]["g"], different.results.rdf, rtol=1e-4, atol=1e-12)
    assert result["frames"] == len(positions)
    json.dumps(result, allow_nan=False)


def test_msd_matches_mdanalysis_on_unwrapped_periodic_positions(kernel, trajectory):
    mda = pytest.importorskip("MDAnalysis")
    from MDAnalysis.analysis.msd import EinsteinMSD

    directory, positions = trajectory
    universe = mda.Universe.empty(6)
    universe.load_new(positions[:, ::2], dimensions=np.tile([8, 8, 8, 90, 90, 90], (len(positions), 1)))
    reference = EinsteinMSD(universe, select="all", msd_type="xyz", fft=False).run()
    result = analysis.msd(directory, selection="C", remove_drift=False)
    np.testing.assert_allclose(result["msd_angstrom2"], reference.results.timeseries, rtol=0, atol=8e-8)
    assert result["time_ps"][1] == pytest.approx(0.01)
    json.dumps(result, allow_nan=False)


def test_ballistic_motion_and_static_drift_corrected_msd_do_not_claim_diffusion(kernel, tmp_path):
    positions = np.array([[[1 + t * 0.5, 2, 2], [3 + t * 0.5, 2, 2]] for t in range(8)])
    write_cell(tmp_path, positions)
    moving = analysis.msd(tmp_path, remove_drift=False)
    assert moving["fit"]["loglog_exponent"] == pytest.approx(2.0)
    assert moving["fit"]["diffusive_window"] is False
    assert moving["fit"]["diffusion_m2_s"] is None
    corrected = analysis.msd(tmp_path, remove_drift=True)
    assert corrected["msd_angstrom2"] == pytest.approx(np.zeros(8))
    assert corrected["fit"]["loglog_exponent"] is None
    assert corrected["fit"]["diffusion_m2_s"] is None
    json.dumps(corrected, allow_nan=False)


def test_translating_box_origin_does_not_create_lab_frame_msd(kernel, tmp_path):
    write_cell(tmp_path, np.tile([[3, 3, 3], [5, 5, 5]], (8, 1, 1)))
    path = tmp_path / "eq3.dump"
    lines = path.read_text().splitlines()
    headers = [i for i, line in enumerate(lines) if line == "ITEM: BOX BOUNDS pp pp pp"]
    for frame, start in enumerate(headers):
        lines[start + 1] = f"{frame * 0.1} {8 + frame * 0.1}"
    path.write_text("\n".join(lines) + "\n")
    result = analysis.msd(tmp_path, remove_drift=False)
    np.testing.assert_array_equal(result["msd_angstrom2"], np.zeros(8))
    assert result["fit"]["diffusion_m2_s"] is None


def test_free_volume_reports_measured_grid_difference_and_analytic_sphere(kernel, tmp_path):
    write_cell(tmp_path, [[4, 4, 4]], dump=False)
    fine = analysis.free_volume(tmp_path, probes=[0.0, 1.0], spacing=0.1)
    coarse = analysis.free_volume(tmp_path, probes=[0.0, 1.0], spacing=0.2)
    expected_occupied = 4 * math.pi * 1.7 ** 3 / 3 / 8 ** 3
    expected_accessible = 1 - 4 * math.pi * 2.7 ** 3 / 3 / 8 ** 3
    assert fine["occupied_fraction"] == pytest.approx(expected_occupied, abs=0.002)
    assert fine["probes"][1]["accessible_fraction"] == pytest.approx(expected_accessible, abs=0.002)
    assert fine["grid_comparison"]["occupied_fraction_absolute_difference"] == pytest.approx(abs(fine["occupied_fraction"] - coarse["occupied_fraction"]))
    assert fine["grid_comparison"]["coarse_grid"] == coarse["grid"]
    assert fine["grid_comparison"]["fine_spacing_angstrom"] == pytest.approx([0.1] * 3)
    for small, large in zip(fine["probes"], coarse["probes"]):
        assert small["occupiable_fraction_absolute_difference"] == pytest.approx(abs(small["occupiable_fraction"] - large["occupiable_fraction"]))
    assert "not an error bound" in fine["grid_comparison"]["note"]
    json.dumps(fine, allow_nan=False)


def test_final_volume_does_not_mistake_truncated_dump_for_last_frame(kernel, tmp_path):
    write_cell(tmp_path, [[4, 4, 4]], dump=False)
    # The final snapshot remains usable independently of an unavailable trajectory.
    (tmp_path / "eq3.dump").write_text("not a readable trajectory")
    result = analysis.free_volume(tmp_path, probes=[0], spacing=0.5)
    assert result["source"]["trajectory"] is None
    assert result["source"]["data"] == "eq3_last.data"


def test_widom_noninteracting_gas_matches_ideal_henry_limit(kernel, tmp_path):
    write_cell(tmp_path, [[4, 4, 4]], epsilon=0, dump=False)
    result = analysis.widom(tmp_path, gas="Ar", insertions=1000, cutoff=3.5)
    assert result["boltzmann_factor"]["mean"] == pytest.approx(1)
    assert result["excess_chemical_potential_kcal_mol"] == pytest.approx(0)
    assert result["solubility_cm3stp_per_cm3_atm"] == pytest.approx(273.15 / 300)
    assert result["boltzmann_factor"]["standard_error"] is None
    json.dumps(result, allow_nan=False)


def test_widom_zero_samples_report_unresolved_mu_instead_of_infinity(kernel, tmp_path, monkeypatch):
    write_cell(tmp_path, [[4, 4, 4]], dump=False)
    monkeypatch.setattr(kernel, "widom_insertion", lambda *args: np.zeros(1))
    result = analysis.widom(tmp_path, insertions=10, cutoff=3.5)
    assert result["excess_chemical_potential_kcal_mol"] is None
    assert any("All sampled" in warning for warning in result["warnings"])
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("second_x,expected_mean,expected_error", [(10, 2.0, 1.0), (20, 7 / 3, 8 / 9)])
def test_widom_uses_npt_volume_weighted_ratio_and_linearized_error(kernel, tmp_path, monkeypatch, second_x, expected_mean, expected_error):
    write_cell(tmp_path, [[[4, 4, 4]], [[4, 4, 4]]], length=10)
    path = tmp_path / "eq3.dump"
    lines = path.read_text().splitlines()
    headers = [i for i, line in enumerate(lines) if line == "ITEM: BOX BOUNDS pp pp pp"]
    lines[headers[1] + 1] = f"0 {second_x}"
    path.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(kernel, "widom_insertion", lambda *args: np.array([1.0, 3.0]))
    result = analysis.widom(tmp_path, insertions=10, cutoff=3.5)
    assert result["boltzmann_factor"]["mean"] == pytest.approx(expected_mean)
    assert result["boltzmann_factor"]["standard_error"] == pytest.approx(expected_error)
    assert result["excess_chemical_potential_kcal_mol"] == pytest.approx(-analysis.R_KCAL * 300 * math.log(expected_mean))
    assert result["estimator"]["volume_weighted"] is True


@pytest.mark.parametrize("method,parameters", [
    ("rdf", {"pairs": []}), ("rdf", {"bins": 0}), ("rdf", {"r_max": float("nan")}),
    ("rdf", {"every": 0}), ("rdf", {"pairs": ["all-X"]}), ("rdf", {"pairs": ["all"]}),
    ("msd", {"fit_window": [0.8, 0.2]}), ("msd", {"time_step_fs": 0}), ("msd", {"time_step_fs": 1e-300}),
    ("msd", {"max_frames": 2001}), ("msd", {"remove_drift": "false"}),
    ("free_volume", {"probes": []}), ("free_volume", {"probes": [float("inf")]}),
    ("free_volume", {"frame": -1}), ("free_volume", {"frame": 2000}),
    ("free_volume", {"spacing": 0}), ("widom", {"temperature": 0}),
    ("widom", {"insertions": 0}), ("widom", {"insertions": 200001}),
    ("widom", {"seed": -1}), ("widom", {"cutoff": 0}),
])
def test_invalid_analysis_parameters_fail_before_computation(kernel, trajectory, method, parameters):
    with pytest.raises(ValueError):
        analysis.ANALYSES[method](trajectory[0], **parameters)


def test_no_pairs_after_exclusion_is_an_error(kernel, tmp_path):
    write_cell(tmp_path, [[1, 1, 1], [2, 2, 2]], dump=False)
    with pytest.raises(ValueError, match="원자 쌍"):
        analysis.rdf(tmp_path, pairs=["C-C"], r_max=3, exclude_same_molecule=True)


@pytest.mark.parametrize("replacement", ["ITEM: BOX BOUNDS xy xz yz pp pp pp", "ITEM: BOX BOUNDS ff pp pp"])
def test_dump_requires_orthorhombic_periodic_boundaries(tmp_path, replacement):
    write_cell(tmp_path, [[[1, 1, 1]], [[2, 2, 2]]])
    path = tmp_path / "eq3.dump"
    path.write_text(path.read_text().replace("ITEM: BOX BOUNDS pp pp pp", replacement))
    with pytest.raises(ValueError, match="직교 주기"):
        analysis.read_dump(path)


def test_later_frame_id_and_type_changes_are_rejected(kernel, trajectory):
    directory, _ = trajectory
    path = directory / "eq3.dump"
    original = path.read_text()
    lines = original.splitlines()
    atom_headers = [i for i, line in enumerate(lines) if line.startswith("ITEM: ATOMS")]
    for column, replacement in ((0, "100"), (1, "1")):
        changed = lines.copy()
        row = changed[atom_headers[-1] + 1].split()  # Last frame's ID 12 is hydrogen (type 2).
        row[column] = replacement
        changed[atom_headers[-1] + 1] = " ".join(row)
        path.write_text("\n".join(changed) + "\n")
        with pytest.raises(ValueError, match="원자가 일치"):
            analysis.rdf(directory, pairs=["C-H"], r_max=3)
    path.write_text(original)


def test_irregular_or_duplicate_dump_times_are_rejected(kernel, trajectory):
    directory, _ = trajectory
    path = directory / "eq3.dump"
    original = path.read_text()
    for invalid_step in (0, 11):
        path.write_text(original.replace("ITEM: TIMESTEP\n10\n", f"ITEM: TIMESTEP\n{invalid_step}\n"))
        with pytest.raises(ValueError):
            analysis.msd(directory)


def test_msd_and_widom_require_recorded_units_or_explicit_values(kernel, trajectory):
    directory, _ = trajectory
    (directory / "bulk_complete.json").unlink()
    with pytest.raises(ValueError, match="time_step_fs"):
        analysis.msd(directory)
    with pytest.raises(ValueError, match="temperature"):
        analysis.widom(directory, insertions=10)


def test_read_data_rejects_triclinic_duplicate_ids_and_nonfinite_coordinates(tmp_path):
    write_cell(tmp_path, [[1, 1, 1], [2, 2, 2]], dump=False)
    path = tmp_path / "eq3_last.data"
    original = path.read_text()
    for changed in (original.replace("Masses", "0 0 0 xy xz yz\n\nMasses"),
                    original.replace("2 1 1 0 2 2 2", "1 1 1 0 2 2 2"),
                    original.replace("2 1 1 0 2 2 2", "2 1 1 0 nan 2 2")):
        path.write_text(changed)
        with pytest.raises(ValueError):
            analysis.read_data(path)


def test_data_atom_count_must_match_rows_and_empty_style_comment_is_safe(tmp_path):
    write_cell(tmp_path, [[1, 1, 1], [2, 2, 2]], dump=False)
    path = tmp_path / "eq3_last.data"
    original = path.read_text()
    path.write_text(original.replace("Atoms # full", "Atoms #"))
    assert len(analysis.read_data(path)["atoms"]["id"]) == 2
    path.write_text(original.rsplit("\n", 2)[0] + "\n")
    with pytest.raises(ValueError):
        analysis.read_data(path)


def test_pcff_widom_is_rejected(kernel, tmp_path):
    write_cell(tmp_path, [[4, 4, 4]], dump=False)
    (tmp_path / "eq3_last.data").rename(tmp_path / "final.data")
    with pytest.raises(ValueError, match="12-6 LJ"):
        analysis.widom(tmp_path, insertions=10)


def test_input_symlinks_are_not_followed(tmp_path):
    target = tmp_path / "outside"
    target.write_text("data")
    link = tmp_path / "eq3_last.data"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="일반 입력"):
        analysis.read_data(link)


def test_missing_native_kernel_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(analysis, "native", None)
    with pytest.raises(ValueError, match="Rust 계산 커널"):
        analysis.rdf(tmp_path)
