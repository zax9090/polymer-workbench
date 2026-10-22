"""IR, UV-vis and turbidity curve analysis, JCAMP-DX import, and the DSC/TGA/GPC additions (synthetic curves with analytic answers)."""
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from workbench.ingestion import analyze_curve
from workbench.research_store import validate_measurement
from workbench.spectra import HC_EV_NM, protein_absorptivity

# Mature hen egg-white lysozyme (UniProt P00698, residues 19-147): 6 Trp, 3 Tyr, 8 Cys in 4 disulfides.
HEWL = ("KVFGRCELAAAMKRHGLDNYRGYSLGNWVCAAKFESNFNTQATNRNTDGSTDYGILQINSRWWCNDGRTPGSRNLCNIPCSALLSSDITASVNCAKKIVSDGNGM"
        "NAWVAWRNRCKGTDVQAWIRGCRL")


def _csv(x, y, names):
    return f"{names[0]},{names[1]}\n" + "\n".join(f"{a:.6f},{b:.10f}" for a, b in zip(x, y))


def _gauss(x, centre, sigma, height):
    return height * np.exp(-0.5 * ((x - centre) / sigma) ** 2)


def _cosine(x, centre, half_width, height):
    """Raised-cosine band: exactly zero outside centre +/- half_width, area height * half_width."""
    return np.where(np.abs(x - centre) <= half_width, height / 2 * (1 + np.cos(np.pi * (x - centre) / half_width)), 0.0)


def _polystyrene_like():
    nu = np.arange(4000, 599.99, -1.0)  # descending, as most FTIR software exports
    bands = [(3026, 6, 0.35), (2924, 8, 0.4), (1601, 3, 0.2), (1493, 3, 0.5), (1452, 4, 0.45), (757, 4, 0.7), (698, 4, 0.9)]
    return nu, 0.02 + 1e-6 * (4000 - nu) + sum(_gauss(nu, c, s, h) for c, s, h in bands), bands


def test_ir_peaks_from_percent_transmittance_with_widths_and_ranked_assignments():
    nu, absorbance, bands = _polystyrene_like()
    draft = analyze_curve("ir", _csv(nu, 100 * 10 ** -absorbance, ("wavenumber", "transmittance")),
                          {"x_unit": "cm-1", "y_quantity": "transmittance", "y_unit": "%", "sampling": "transmission"})
    peaks = draft["peaks"]
    assert [round(p["position"]) for p in peaks] == [c for c, _, _ in bands]  # high to low wavenumber
    for peak, (_, sigma, height) in zip(peaks, bands):
        assert peak["fwhm"] == pytest.approx(2 * np.sqrt(2 * np.log(2)) * sigma, abs=0.1)
        assert peak["signal"] == pytest.approx(height + 0.02 + 1e-6 * (4000 - peak["position"]), abs=1e-3)
    groups = {round(p["position"]): p["assignments"][0]["group"] for p in peaks}
    assert groups[3026] == "=C-H stretch" and groups[1601] == "aromatic ring C=C stretch"
    assert groups[757] == "aromatic C-H out-of-plane bend, monosubstituted"  # narrow range ranked before C-Cl
    assert draft["measurement_drafts"] == [] and draft["measurement_draft"] is None
    assert draft["preview"]["reverse_x"] is True and draft["source"]["x_order"] == "decreasing"
    # Wavelength input (um) gives the same bands.
    micrometres = analyze_curve("ir", _csv(1e4 / nu[::-1], absorbance[::-1], ("wavelength", "absorbance")),
                                {"x_unit": "um", "y_quantity": "absorbance", "sampling": "transmission", "x_column": "wavelength"})
    assert [round(p["position"]) for p in micrometres["peaks"]] == [c for c, _, _ in bands]


def test_ir_saturation_is_flagged_and_refused_inside_quantitative_bands():
    nu = np.arange(4000, 599.99, -2.0)
    transmittance = 100 * 10 ** -(0.05 + _gauss(nu, 1465, 10, 0.3) + _gauss(nu, 1715, 15, 0.2))
    transmittance[(nu > 1710) & (nu < 1720)] = 0.0  # opaque carbonyl band
    text = _csv(nu, transmittance, ("wavenumber", "transmittance"))
    options = {"x_unit": "cm-1", "y_quantity": "transmittance", "y_unit": "%", "sampling": "ATR"}
    draft = analyze_curve("ir", text, options)
    assert any("capped at absorbance 4" in w for w in draft["warnings"]) and any("ATR" in w for w in draft["warnings"])
    with pytest.raises(ValueError, match="saturated"):
        analyze_curve("ir", text, {**options, "analysis": "carbonyl_index", "band_baseline": "two_point"})
    for broken, message in (({"sampling": None}, "sampling"), ({"y_unit": None}, "y_unit"), ({"x_unit": "Hz"}, "x_unit"),
                            ({"y_unit": "fraction"}, "150%")):
        with pytest.raises(ValueError, match=message):
            analyze_curve("ir", text, {**options, **broken})
    with pytest.raises(ValueError, match="monotonic"):
        analyze_curve("ir", "wavenumber,absorbance\n1000,0.1\n1002,0.2\n1001,0.1\n", {"x_unit": "cm-1", "y_quantity": "absorbance", "sampling": "ATR"})


def test_carbonyl_index_by_specified_area_under_band():
    nu = np.arange(4000, 599.99, -1.0)
    background = 0.05 + 2e-5 * (nu - 600)  # sloped: removed exactly by the two-point line
    absorbance = background + _cosine(nu, 1715, 60, 0.1) + _cosine(nu, 1462, 35, 0.3)
    options = {"x_unit": "cm-1", "y_quantity": "absorbance", "sampling": "ATR", "analysis": "carbonyl_index", "band_baseline": "two_point"}
    draft = analyze_curve("ir", _csv(nu, absorbance, ("wavenumber", "absorbance")), options)
    index = draft["measurement_draft"]
    assert index["property"] == "carbonyl_index" and index["value"] == pytest.approx((0.1 * 60) / (0.3 * 35), rel=1e-4)
    assert "Almond" in index["method"] and index["conditions"]["carbonyl_window_cm-1"] == [1650.0, 1850.0]
    assert draft["fit"]["bands"]["carbonyl"]["peak_position_cm-1"] == pytest.approx(1715)
    # Windows written high-to-low are accepted; other windows are labelled as non-SAUB.
    custom = analyze_curve("ir", _csv(nu, absorbance, ("wavenumber", "absorbance")), {**options, "carbonyl_window": [1800, 1640]})
    assert "Almond" not in custom["measurement_draft"]["method"] and any("not comparable" in w for w in custom["warnings"])
    clean = analyze_curve("ir", _csv(nu, background + _cosine(nu, 1462, 35, 0.3), ("wavenumber", "absorbance")), options)
    assert clean["measurement_draft"]["value"] == pytest.approx(0, abs=1e-9)
    row = validate_measurement({**index, "material": {"repeat_smiles": "[*]CC[*]"}, "origin": "experimental", "source": "synthetic"})
    assert row["unit"] == "dimensionless"


def test_epoxide_conversion_against_the_uncured_spectrum_or_a_stated_ratio():
    nu = np.arange(4000, 599.99, -1.0)

    def spectrum(epoxide_left):
        return _csv(nu, 0.04 + _cosine(nu, 915, 25, 0.4 * epoxide_left) + _cosine(nu, 1510, 20, 0.6), ("wavenumber", "absorbance"))
    options = {"x_unit": "cm-1", "y_quantity": "absorbance", "sampling": "transmission", "analysis": "epoxide_conversion",
               "band_baseline": "two_point", "measure": "area", "epoxide_window": [945, 885], "reference_window": [1535, 1485],
               "reference_text": spectrum(1.0)}
    draft = analyze_curve("ir", spectrum(0.3), options)
    conversion = draft["measurement_draft"]
    assert conversion["property"] == "epoxide_conversion" and conversion["value"] == pytest.approx(0.7, abs=1e-6)
    assert conversion["conditions"]["initial_ratio_from"] == "uncured reference spectrum"
    assert "reference_text" not in draft["options"] and len(draft["options"]["reference_sha256"]) == 64
    stated = analyze_curve("ir", spectrum(0.3), {**{k: v for k, v in options.items() if k != "reference_text"},
                                                 "initial_ratio": draft["fit"]["initial_ratio"], "measure": "area"})
    assert stated["measurement_draft"]["value"] == pytest.approx(0.7, abs=1e-6)
    heights = analyze_curve("ir", spectrum(0.3), {**options, "measure": "height"})
    assert heights["measurement_draft"]["value"] == pytest.approx(0.7, abs=1e-6)
    with pytest.raises(ValueError, match="exactly one"):
        analyze_curve("ir", spectrum(0.3), {**options, "initial_ratio": 1.0})
    with pytest.raises(ValueError, match="outside 0-1"):
        analyze_curve("ir", spectrum(1.5), options)
    with pytest.raises(ValueError, match="measure"):
        analyze_curve("ir", spectrum(0.3), {**options, "measure": None})
    assert validate_measurement({**conversion, "value": 70, "unit": "%", "material": {"repeat_smiles": "[*]CC[*]"}, "origin": "experimental",
                                 "source": "synthetic"})["value"] == pytest.approx(0.7)
    with pytest.raises(ValueError, match="cannot exceed"):
        validate_measurement({**conversion, "value": 1.2, "material": {"repeat_smiles": "[*]CC[*]"}, "origin": "experimental", "source": "synthetic"})


def test_band_ratio_reports_the_label_and_windows():
    nu = np.arange(1800, 1199.99, -1.0)
    absorbance = 0.02 + _cosine(nu, 1636, 12, 0.2) + _cosine(nu, 1720, 30, 0.8)
    draft = analyze_curve("ir", _csv(nu, absorbance, ("wavenumber", "absorbance")),
                          {"x_unit": "cm-1", "y_quantity": "absorbance", "sampling": "ATR", "analysis": "band_ratio", "band_baseline": "two_point",
                           "measure": "height", "numerator_window": [1650, 1620], "denominator_window": [1760, 1680], "ratio_label": "acrylate C=C / C=O"})
    ratio = draft["measurement_draft"]
    assert ratio["property"] == "ir_band_ratio" and ratio["value"] == pytest.approx(0.25, rel=1e-3)
    assert ratio["conditions"]["ratio_label"] == "acrylate C=C / C=O" and ratio["conditions"]["numerator_window_cm-1"] == [1620.0, 1650.0]


def test_installed_jcamp_is_the_rea_audited_release():
    jcamp = pytest.importorskip("jcamp")
    record = json.loads((Path(__file__).resolve().parents[1] / "evidence/artifacts/jcamp.json").read_text())
    assert record["status"] == "verified_installed_artifact" and record["version"] == "1.3.2"
    assert hashlib.sha256(Path(jcamp.__file__).read_bytes()).hexdigest() == record["files"][0]["artifact_sha256"]


# JCAMP-DX writers for the tests: AFFN and the compressed DIFDUP form with the standard's Y-checks.
def _sqz(n):
    digits = str(abs(n))
    return ("@ABCDEFGHI" if n >= 0 else "@abcdefghi")[int(digits[0])] + digits[1:]


def _dif(n):
    digits = str(abs(n))
    return ("%JKLMNOPQR" if n >= 0 else "%jklmnopqr")[int(digits[0])] + digits[1:]


def _dup(count):
    digits = str(count)
    return "STUVWXYZs"[int(digits[0]) - 1] + digits[1:]


def _jcamp(values, first, last, form="difdup", per_line=10, max_run=9, yfactor=1e-5, yunits="ABSORBANCE", extra=""):
    n = len(values)
    step = (last - first) / (n - 1)
    lines = []
    if form == "affn":
        for i in range(0, n, per_line):
            lines.append(f"{first + i * step:.0f} " + " ".join(str(v) for v in values[i:i + per_line]))
    else:
        i = 0
        while i < n - 1:
            chunk = values[i:i + per_line + 1]  # each line after the first starts with the previous line's last value
            tokens = [_sqz(chunk[0])] + [_dif(b - a) for a, b in zip(chunk, chunk[1:])]
            out, j = [tokens[0]], 1
            while j < len(tokens):
                k = j
                while k + 1 < len(tokens) and tokens[k + 1] == tokens[j] and k + 2 - j <= max_run:
                    k += 1
                out.append(tokens[j] + (_dup(k - j + 1) if k > j else ""))
                j = k + 1
            lines.append(f"{first + i * step:.0f}" + "".join(out))
            i += len(chunk) - 1
    header = [f"##TITLE=synthetic {form} test spectrum $$ written by tests/test_spectra.py", "##JCAMP-DX=4.24", "##DATA TYPE=INFRARED SPECTRUM",
              "##XUNITS=1/CM", f"##YUNITS={yunits}", "##XFACTOR=1", f"##YFACTOR={yfactor}", f"##FIRSTX={first}", f"##LASTX={last}",
              f"##NPOINTS={n}", f"##FIRSTY={values[0] * yfactor}", extra, "##XYDATA=(X++(Y..Y))"]
    return "\n".join([h for h in header if h] + lines + ["##END="]) + "\n"


def test_jcamp_dx_affn_and_difdup_read_like_the_csv():
    pytest.importorskip("jcamp")
    nu, absorbance, bands = _polystyrene_like()
    values = [int(round(a * 1e5)) for a in absorbance]
    expected = analyze_curve("ir", _csv(nu, np.array(values) * 1e-5, ("wavenumber", "absorbance")),
                             {"x_unit": "cm-1", "y_quantity": "absorbance", "sampling": "transmission"})
    for form in ("affn", "difdup"):
        text = _jcamp(values, 4000, 600, form=form)
        if form == "difdup":
            assert any(c in text for c in "STUVWXYZs") and "%" in text  # really compressed
        draft = analyze_curve("ir", text, {"sampling": "transmission"})
        assert draft["source"]["format"] == "JCAMP-DX" and draft["source"]["units"]["x_unit"] == "cm-1"
        assert draft["points"] == len(values) and draft["source"]["header"]["title"] == "synthetic " + form + " test spectrum"
        assert [round(p["position"], 6) for p in draft["peaks"]] == [round(p["position"], 6) for p in expected["peaks"]]
    with pytest.raises(ValueError, match="contradicts"):
        analyze_curve("ir", _jcamp(values, 4000, 600), {"sampling": "transmission", "y_quantity": "transmittance"})


def test_jcamp_dx_damage_and_unsupported_forms_are_refused():
    pytest.importorskip("jcamp")
    values = [1000 + (i % 7) * 3 for i in range(60)] + [1200] * 30 + [1000 + i for i in range(60)]
    good = _jcamp(values, 2000, 1851)
    assert analyze_curve("ir", good, {"sampling": "ATR"})["points"] == len(values)
    lines = good.splitlines()
    data = [i for i, line in enumerate(lines) if line[:1].isdigit()]
    damaged = lines[:]
    damaged[data[3]] = damaged[data[3]][:-1] + ("J" if damaged[data[3]][-1] != "J" else "K")  # one corrupted difference
    with pytest.raises(ValueError, match="line checks"):
        analyze_curve("ir", "\n".join(damaged), {"sampling": "ATR"})
    # Runs of ten or more equal differences need multi-digit DUP counts, which the reader does not decode.
    with pytest.raises(ValueError, match="line checks|NPOINTS"):
        analyze_curve("ir", _jcamp(values, 2000, 1851, per_line=40, max_run=40), {"sampling": "ATR"})
    linked = "##TITLE=link\n##JCAMP-DX=5.0\n##DATA TYPE=LINK\n##BLOCKS=1\n" + good + "##END=\n"
    with pytest.raises(ValueError, match="several spectra"):
        analyze_curve("ir", linked, {"sampling": "ATR"})
    with pytest.raises(ValueError, match="YUNITS"):
        analyze_curve("ir", _jcamp(values, 2000, 1851, yunits="ARBITRARY UNITS"), {"sampling": "ATR"})
    # Percent written under TRANSMITTANCE is read by its range, with a note.
    percent = analyze_curve("ir", _jcamp([9000 - v for v in values], 2000, 1851, yfactor=0.01, yunits="TRANSMITTANCE"), {"sampling": "ATR"})
    assert percent["source"]["units"]["y_unit"] == "%" and any("read as percent" in w for w in percent["warnings"])
    with pytest.raises(ValueError, match="CSV"):
        analyze_curve("turbidity", good, {})


def test_uvvis_absorption_maximum_and_beer_lambert_concentration():
    wavelength = np.arange(250, 800.1, 0.5)
    absorbance = _gauss(wavelength, 452.3, 30, 0.8) + _gauss(wavelength, 320, 15, 0.3) + 0.01
    text = _csv(wavelength[::-1], absorbance[::-1], ("wavelength", "absorbance"))  # scanned from red to blue
    peaks = analyze_curve("uvvis", text, {"x_unit": "nm", "y_quantity": "absorbance", "analysis": "peaks", "medium": "CHCl3, 0.01 mg/mL"})
    maximum = peaks["measurement_draft"]
    assert maximum["property"] == "absorption_max_wavelength" and maximum["value"] == pytest.approx(452.3, abs=0.01)
    assert [round(p["position"]) for p in peaks["peaks"]] == [320, 452] and peaks["peaks"][1]["energy_ev"] == pytest.approx(HC_EV_NM / 452.3, abs=1e-4)
    near_uv = analyze_curve("uvvis", text, {"x_unit": "nm", "y_quantity": "absorbance", "analysis": "peaks", "medium": "CHCl3", "search_window": [280, 360]})
    assert near_uv["measurement_draft"]["value"] == pytest.approx(320, abs=0.01)
    base = {"x_unit": "nm", "y_quantity": "absorbance", "analysis": "concentration", "wavelength_nm": 452.5, "path_length_cm": 0.5, "dilution_factor": 10}
    molar = analyze_curve("uvvis", text, {**base, "molar_absorptivity": 25000})
    a = _gauss(452.5, 452.3, 30, 0.8) + _gauss(452.5, 320, 15, 0.3) + 0.01  # on a grid point, so no interpolation error
    assert molar["results"][0] == {"quantity": "concentration", "value": pytest.approx(a * 10 / (25000 * 0.5)), "unit": "mol/L"}
    assert molar["measurement_drafts"] == [] and any("not stored" in w for w in molar["warnings"])
    scattered = analyze_curve("uvvis", text, {**base, "mass_absorptivity": 2.0, "scatter_wavelength_nm": 700})
    assert scattered["results"][0]["value"] == pytest.approx((a - 0.01) * 10 / (2.0 * 0.5), rel=1e-6) and scattered["results"][0]["unit"] == "mg/mL"
    standards = [[c, 0.02 + 0.3 * c] for c in (0.5, 1, 2, 3)]
    calibrated = analyze_curve("uvvis", text, {**base, "dilution_factor": 1, "calibration": standards, "concentration_unit": "mg/mL"})
    assert calibrated["results"][0]["value"] == pytest.approx((a - 0.02) / 0.3) and calibrated["fit"]["concentration"]["r_squared"] == pytest.approx(1)
    with pytest.raises(ValueError, match="extrapolation"):
        analyze_curve("uvvis", text, {**base, "dilution_factor": 1, "calibration": [[c, 0.3 * c] for c in (0.1, 0.2, 0.3)], "concentration_unit": "mg/mL"})
    with pytest.raises(ValueError, match="exactly one"):
        analyze_curve("uvvis", text, {**base, "molar_absorptivity": 1e4, "mass_absorptivity": 1.0})
    with pytest.raises(ValueError, match="dilution_factor"):
        analyze_curve("uvvis", text, {**base, "molar_absorptivity": 1e4, "dilution_factor": 0.5})


def test_protein_concentration_from_the_sequence():
    protein = protein_absorptivity(HEWL, 4)
    assert protein["residues"] == 129 and (protein["trp"], protein["tyr"], protein["cys"]) == (6, 3, 8)
    assert protein["molar_absorptivity_l_per_mol_cm"] == 37970  # 6 x 5500 + 3 x 1490 + 4 x 125
    assert protein["molar_mass_g_mol"] == pytest.approx(14313.14 - 4 * 2.01588, abs=0.01)  # ExPASy average mass, oxidized
    assert protein_absorptivity(HEWL, 0)["molar_absorptivity_l_per_mol_cm"] == 37470
    for cystines in (5, -1, 2.0, True):
        with pytest.raises(ValueError, match="cystines"):
            protein_absorptivity(HEWL, cystines)
    with pytest.raises(ValueError, match="standard one-letter"):
        protein_absorptivity("ACDXZ", 0)
    wavelength = np.arange(240, 350.1, 1.0)
    text = _csv(wavelength, _gauss(wavelength, 280, 12, 0.759) + 0.004, ("wavelength", "absorbance"))
    draft = analyze_curve("uvvis", text, {"x_unit": "nm", "y_quantity": "absorbance", "analysis": "concentration", "wavelength_nm": 280,
                                          "scatter_wavelength_nm": 340, "path_length_cm": 1, "protein_sequence": HEWL, "cystines": 4})
    corrected = 0.759 + 0.004 - (0.004 + _gauss(340, 280, 12, 0.759))
    results = {r["unit"]: r["value"] for r in draft["results"]}
    assert results["µM"] == pytest.approx(corrected / 37970 * 1e6, rel=1e-6)
    assert results["mg/mL"] == pytest.approx(corrected / 37970 * protein["molar_mass_g_mol"], rel=1e-6)
    with pytest.raises(ValueError, match="280 nm"):
        analyze_curve("uvvis", text, {"x_unit": "nm", "y_quantity": "absorbance", "analysis": "concentration", "wavelength_nm": 275,
                                      "path_length_cm": 1, "protein_sequence": HEWL, "cystines": 4})


def test_tauc_and_onset_optical_gaps():
    wavelength = np.arange(300, 900.1, 0.5)
    energy = HC_EV_NM / wavelength
    gap = 2.40
    direct = np.sqrt(np.clip(energy - gap, 0, None)) / energy  # (alpha E)^2 is linear in E above the gap
    base = {"x_unit": "nm", "y_quantity": "absorbance", "analysis": "band_gap", "medium": "spin-coated film"}
    result = analyze_curve("uvvis", _csv(wavelength, direct, ("wavelength", "absorbance")), {**base, "gap_method": "tauc_direct", "fit_window_ev": [2.6, 3.4]})
    assert result["measurement_draft"]["property"] == "optical_band_gap" and result["measurement_draft"]["value"] == pytest.approx(gap, abs=1e-4)
    indirect = (np.clip(energy - gap, 0, None)) ** 2 / energy  # (alpha E)^(1/2) linear in E
    result = analyze_curve("uvvis", _csv(wavelength, indirect, ("wavelength", "absorbance")), {**base, "gap_method": "tauc_indirect", "fit_window_ev": [2.6, 3.4]})
    assert result["measurement_draft"]["value"] == pytest.approx(gap, abs=1e-4)
    # A constant sub-gap absorption: intersecting the baseline (Makula et al. 2018) recovers the gap; the energy axis does not.
    tail = (np.sqrt(direct ** 2 + (0.05 / energy) ** 2))  # (alpha E)^2 = (E - gap) + 0.0025
    shifted = analyze_curve("uvvis", _csv(wavelength, tail, ("wavelength", "absorbance")), {**base, "gap_method": "tauc_direct", "fit_window_ev": [2.6, 3.4]})
    corrected = analyze_curve("uvvis", _csv(wavelength, tail, ("wavelength", "absorbance")),
                              {**base, "gap_method": "tauc_direct", "fit_window_ev": [2.6, 3.4], "baseline_window_ev": [1.5, 2.2]})
    assert shifted["measurement_draft"]["value"] == pytest.approx(gap - 0.0025, abs=1e-4)
    assert corrected["measurement_draft"]["value"] == pytest.approx(gap, abs=1e-4)
    edge = np.clip(1.0 - (wavelength - 500) / 50, 0, None) * (wavelength >= 500) + (wavelength < 500)  # straight edge to zero at 550 nm
    onset = analyze_curve("uvvis", _csv(wavelength, edge, ("wavelength", "absorbance")), {**base, "gap_method": "absorption_onset", "fit_window_nm": [505, 540]})
    assert onset["fit"]["band_gap"]["onset_wavelength_nm"] == pytest.approx(550, abs=1e-6) and onset["measurement_draft"]["value"] == pytest.approx(HC_EV_NM / 550)
    with pytest.raises(ValueError, match="fall with wavelength"):
        analyze_curve("uvvis", _csv(wavelength, edge, ("wavelength", "absorbance")), {**base, "gap_method": "absorption_onset", "fit_window_nm": [400, 480]})
    with pytest.raises(ValueError, match="gap_method"):
        analyze_curve("uvvis", _csv(wavelength, edge, ("wavelength", "absorbance")), base)


def test_kubelka_munk_band_gap_from_diffuse_reflectance():
    wavelength = np.arange(250, 800.1, 0.5)
    energy = HC_EV_NM / wavelength
    target = np.sqrt(np.clip(energy - 3.2, 0, None)) / energy * 5 + 1e-4  # F(R) of a TiO2-like powder
    reflectance = 1 + target - np.sqrt(target ** 2 + 2 * target)  # inverse of F = (1 - R)^2 / 2R
    draft = analyze_curve("uvvis", _csv(wavelength, 100 * reflectance, ("wavelength", "reflectance")),
                          {"x_unit": "nm", "y_quantity": "reflectance", "y_unit": "%", "analysis": "band_gap", "medium": "powder",
                           "gap_method": "tauc_direct", "fit_window_ev": [3.4, 4.2], "baseline_window_ev": [2.0, 2.8]})
    assert draft["fit"]["signal"] == "Kubelka-Munk F(R)" and draft["measurement_draft"]["value"] == pytest.approx(3.2, abs=2e-3)
    with pytest.raises(ValueError, match="band gaps"):
        analyze_curve("uvvis", _csv(wavelength, 100 * reflectance, ("wavelength", "reflectance")),
                      {"x_unit": "nm", "y_quantity": "reflectance", "y_unit": "%", "analysis": "peaks", "medium": "powder"})


def _lcst(temperatures, centre=32.0, width=0.5):
    return 100 - 90 / (1 + np.exp(-(temperatures - centre) / width))


def test_cloud_point_definitions_on_a_heating_scan():
    temperature = np.arange(20, 45.001, 0.05)
    text = _csv(temperature, _lcst(temperature), ("temperature", "transmittance"))
    options = {"temperature_unit": "C", "y_quantity": "transmittance", "y_unit": "%", "branch": "heating", "rate_k_per_min": 1.0,
               "concentration": "PNIPAM 1 mg/mL in water", "wavelength_nm": 500, "clear_window": [20, 26], "turbid_window": [38, 45]}
    values = {}
    for definition in ("midpoint", "inflection", "onset", "transmittance_50"):
        draft = analyze_curve("turbidity", text, {**options, "definition": definition})
        values[definition] = draft["measurement_draft"]["value"] - 273.15
        assert draft["measurement_draft"]["property"] == "cloud_point_temperature" and draft["measurement_draft"]["conditions"]["definition"] == definition
    assert values["midpoint"] == pytest.approx(32.0, abs=1e-3) and values["inflection"] == pytest.approx(32.0, abs=0.03)
    assert values["onset"] == pytest.approx(31.0, abs=0.05)  # logistic: tangent at the centre meets the plateau 2 widths earlier
    assert values["transmittance_50"] == pytest.approx(32 - 0.5 * np.log(0.8), abs=1e-3)
    with pytest.raises(ValueError, match="clear_window and turbid_window"):
        analyze_curve("turbidity", text, {**{k: v for k, v in options.items() if k != "turbid_window"}, "definition": "midpoint"})
    with pytest.raises(ValueError, match="decreasing order"):
        analyze_curve("turbidity", text, {**options, "branch": "cooling", "definition": "midpoint"})
    clearing = _csv(temperature, 110 - _lcst(temperature), ("temperature", "transmittance"))
    with pytest.raises(ValueError, match="clearing"):
        analyze_curve("turbidity", clearing, {**options, "definition": "midpoint"})


def test_ucst_cloud_point_on_a_cooling_scan_in_absorbance():
    temperature = np.arange(60, 29.999, -0.1)  # measured while cooling
    optical_density = 1.2 / (1 + np.exp((temperature - 41.5) / 0.8)) + 0.01
    draft = analyze_curve("turbidity", _csv(temperature, optical_density, ("temperature", "absorbance")),
                          {"temperature_unit": "C", "y_quantity": "absorbance", "branch": "cooling",
                           "rate_k_per_min": 0.5, "concentration": "2 wt% in ethanol/water", "definition": "midpoint",
                           "clear_window": [50, 60], "turbid_window": [30, 34]})
    assert draft["measurement_draft"]["value"] == pytest.approx(41.5 + 273.15, abs=1e-3)
    onset = analyze_curve("turbidity", _csv(temperature, optical_density, ("temperature", "absorbance")),
                          {"temperature_unit": "C", "y_quantity": "absorbance", "branch": "cooling", "rate_k_per_min": 0.5,
                           "concentration": "2 wt%", "definition": "onset", "clear_window": [50, 60], "turbid_window": [30, 34]})
    assert onset["measurement_draft"]["value"] - 273.15 == pytest.approx(41.5 + 2 * 0.8, abs=0.08)  # onset comes first on cooling


def test_dsc_tg_onset_inflection_endset_and_heat_capacity_step():
    temperature = np.arange(40, 160.01, 0.2)
    heat_flow = -0.2 / (1 + np.exp(-(temperature - 100) / 2.0))  # step down by 0.2 W/g
    text = _csv(temperature, heat_flow, ("temperature", "heat_flow"))
    options = {"temperature_unit": "C", "heat_flow_unit": "W/g", "baseline_before": [50, 80], "baseline_after": [120, 150], "heating_rate_k_per_min": 10}
    values = {}
    for definition in ("midpoint", "inflection", "onset", "endset"):
        draft = analyze_curve("dsc", text, {**options, "tg_definition": definition})
        values[definition] = draft["measurement_draft"]["value"] - 273.15
    assert values["midpoint"] == pytest.approx(100, abs=1e-6) and values["inflection"] == pytest.approx(100, abs=0.1)
    assert values["onset"] == pytest.approx(96, abs=0.1) and values["endset"] == pytest.approx(104, abs=0.1)
    assert draft["fit"]["heat_capacity_step_j_per_g_k"] == pytest.approx(0.2 / (10 / 60), rel=1e-3)
    assert draft["measurement_draft"]["method"].startswith("DSC Tg extrapolated end")
    midpoint = analyze_curve("dsc", text, options)["measurement_draft"]
    assert midpoint["method"] == "DSC halfway-heat-flow crossing between two manually selected linear baselines"
    with pytest.raises(ValueError, match="tg_definition"):
        analyze_curve("dsc", text, {**options, "tg_definition": "peak"})


def _pla_like(temperature, rate=10.0):
    def peak(centre, sigma, enthalpy):
        return enthalpy * (rate / 60) / (sigma * np.sqrt(2 * np.pi)) * np.exp(-0.5 * ((temperature - centre) / sigma) ** 2)
    return 0.05 + 0.0005 * (temperature - 40) - peak(100, 3, 20) + peak(170, 4, 60)


def test_melting_with_cold_crystallization_and_filler_fraction():
    temperature = np.arange(40, 220.01, 0.1)
    text = _csv(temperature, _pla_like(temperature), ("temperature", "heat_flow"))
    options = {"analysis": "melting", "temperature_unit": "C", "heat_flow_unit": "W/g", "endotherm": "up", "heating_rate_k_per_min": 10,
               "integration_window": [150, 190], "cold_crystallization_window": [85, 115], "reference_enthalpy_j_per_g": 93.7,
               "baseline": "linear", "polymer_weight_fraction": 0.8}
    draft = analyze_curve("dsc", text, options)
    values = {d["property"]: d for d in draft["measurement_drafts"]}
    assert values["melting_enthalpy"]["value"] == pytest.approx(60, rel=1e-4)
    assert draft["fit"]["cold_crystallization"]["enthalpy_j_per_g"] == pytest.approx(20, rel=1e-4)
    assert values["crystallinity"]["value"] == pytest.approx((60 - 20) / (0.8 * 93.7), rel=1e-4)
    assert values["crystallinity"]["analysis"]["polymer_weight_fraction"] == 0.8 and "cold-crystallization" in values["crystallinity"]["method"]
    # Only protocol settings are conditions, so replicates with their own windows still pool for training.
    assert values["crystallinity"]["conditions"] == {"heating_rate_k_per_min": 10.0, "baseline": "linear", "reference_enthalpy_j_per_g": 93.7}
    with pytest.raises(ValueError, match="baseline linear"):
        analyze_curve("dsc", text, {**options, "baseline": "arpls"})
    with pytest.raises(ValueError, match="overlap"):
        analyze_curve("dsc", text, {**options, "cold_crystallization_window": [140, 160]})


def test_arpls_melting_warns_when_an_exotherm_displaces_the_baseline():
    pytest.importorskip("pybaselines")
    temperature = np.arange(40, 220.01, 0.1)
    options = {"analysis": "melting", "temperature_unit": "C", "heat_flow_unit": "W/g", "endotherm": "up", "heating_rate_k_per_min": 10,
               "integration_window": [150, 190]}
    distorted = analyze_curve("dsc", _csv(temperature, _pla_like(temperature), ("temperature", "heat_flow")), options)
    assert any("displaced the baseline" in w for w in distorted["warnings"])
    linear = analyze_curve("dsc", _csv(temperature, _pla_like(temperature), ("temperature", "heat_flow")), {**options, "baseline": "linear"})
    assert linear["measurement_drafts"][1]["value"] == pytest.approx(60, rel=1e-4)
    assert not any("displaced the baseline" in w for w in linear["warnings"])


def test_tga_dtg_peak_extrapolated_onset_and_char_yield():
    temperature = np.arange(30, 800.01, 0.5)
    mass = 10 * (1 - 0.6 / (1 + np.exp(-(temperature - 400) / 10)))
    text = _csv(temperature, mass, ("temperature", "mass"))
    options = {"temperature_unit": "C", "mass_unit": "mg", "reference_temperature": 150, "mass_loss_percent": 5, "atmosphere": "N2",
               "heating_rate_K_min": 10, "residue_temperature": 700}
    draft = analyze_curve("tga", text, options)
    values = {d["property"]: d for d in draft["measurement_drafts"]}
    assert list(values) == ["decomposition_temperature", "dtg_peak_temperature", "char_yield"]
    assert values["dtg_peak_temperature"]["value"] == pytest.approx(400 + 273.15, abs=0.05)
    assert values["char_yield"]["value"] == pytest.approx(0.4, abs=1e-4) and values["char_yield"]["unit"] == "fraction"
    assert draft["fit"]["dtg"]["peak_rate_percent_per_K"] == pytest.approx(60 / (4 * 10), rel=0.01)
    # ISO 11358-1 construction on a logistic step: the tangent at the centre meets the start line two widths earlier.
    onset = analyze_curve("tga", text, {**options, "td_definition": "onset"})["measurement_draft"]
    assert onset["value"] == pytest.approx(380 + 273.15, abs=0.2) and onset["method"].startswith("TGA extrapolated onset")
    air = analyze_curve("tga", text, {**options, "atmosphere": "air"})
    assert any("ash" in w for w in air["warnings"])
    with pytest.raises(ValueError, match="residue_temperature"):
        analyze_curve("tga", text, {**options, "residue_temperature": 900})
    two_steps = 10 * (1 - 0.3 / (1 + np.exp(-(temperature - 300) / 8)) - 0.4 / (1 + np.exp(-(temperature - 480) / 12)))
    stepped = analyze_curve("tga", _csv(temperature, two_steps, ("temperature", "mass")), {**options, "dtg_window": [400, 600]})
    assert len(stepped["fit"]["dtg"]["peaks"]) == 2 and any("2 mass-loss steps" in w for w in stepped["warnings"])
    assert stepped["measurement_drafts"][1]["value"] == pytest.approx(480 + 273.15, abs=0.1)


def test_gpc_mz_and_universal_calibration():
    from tests.test_research_data import gpc_fixture
    text, options = gpc_fixture()
    draft = analyze_curve("gpc", text, options)
    values = {d["property"]: d["value"] for d in draft["measurement_drafts"]}
    sigma, mu = np.log(10) * 0.3 * 0.6, np.log(10) * (8 - 0.3 * 16)
    assert values["mz"] == pytest.approx(np.exp(mu + 3 * sigma ** 2 / 2), rel=1e-5)  # log-normal: Mz = exp(mu + 3 sigma^2 / 2)
    same = {"standard": {"K": 0.0114, "a": 0.716}, "sample": {"K": 0.0114, "a": 0.716}, "K_unit": "mL/g", "source": "identity check"}
    identical = analyze_curve("gpc", text, {**options, "mark_houwink": same})
    assert {d["property"]: d["value"] for d in identical["measurement_drafts"]} == pytest.approx(values, rel=1e-12)
    other = {**same, "sample": {"K": 0.0128, "a": 0.69}}
    universal = analyze_curve("gpc", text, {**options, "mark_houwink": other})
    peak = draft["fit"]["peak_molar_mass_g_mol"]
    assert universal["fit"]["peak_molar_mass_g_mol"] == pytest.approx((0.0114 / 0.0128 * peak ** 1.716) ** (1 / 1.69), rel=1e-9)
    assert universal["measurement_draft"]["conditions"]["universal_calibration"] == {"standard": {"K": 0.0114, "a": 0.716}, "K_unit": "mL/g"}
    assert universal["measurement_draft"]["analysis"]["mark_houwink"]["sample"] == {"K": 0.0128, "a": 0.69}
    assert "universal calibration" in universal["measurement_draft"]["method"]
    for broken in ({"K_unit": "L/mol"}, {"source": ""}, {"sample": {"K": -1, "a": 0.7}}, {"standard": {"K": 0.01, "a": 2.5}}):
        with pytest.raises(ValueError, match="mark_houwink"):
            analyze_curve("gpc", text, {**options, "mark_houwink": {**other, **broken}})


def test_new_properties_convert_units():
    material = {"material": {"repeat_smiles": "[*]CC[*]"}, "origin": "experimental", "source": "synthetic", "method": "test"}
    assert validate_measurement({"property": "cloud_point_temperature", "value": 32, "unit": "C", **material})["value"] == pytest.approx(305.15)
    assert validate_measurement({"property": "char_yield", "value": 40, "unit": "%", **material})["value"] == pytest.approx(0.4)
    assert validate_measurement({"property": "absorption_max_wavelength", "value": 0.45, "unit": "um", **material})["value"] == pytest.approx(450)
    assert validate_measurement({"property": "mz", "value": 2, "unit": "kg/mol", **material})["value"] == pytest.approx(2000)
    for prop, value, unit in (("optical_band_gap", 0, "eV"), ("char_yield", 120, "%"), ("optical_band_gap", 2, "nm")):
        with pytest.raises(ValueError):
            validate_measurement({"property": prop, "value": value, "unit": unit, **material})


def test_curve_api_accepts_the_new_kinds(tmp_path):
    from fastapi.testclient import TestClient
    from workbench.server import create_app
    nu, absorbance, _ = _polystyrene_like()
    with TestClient(create_app(tmp_path / "jobs")) as client:
        response = client.post("/api/research/curves", json={"kind": "ir", "text": _csv(nu, absorbance, ("wavenumber", "absorbance")),
                                                             "options": {"x_unit": "cm-1", "y_quantity": "absorbance", "sampling": "ATR"}})
        assert response.status_code == 200, response.text
        assert len(response.json()["peaks"]) == 7
        bad = client.post("/api/research/curves", json={"kind": "uvvis", "text": _csv(nu, absorbance, ("wavelength", "absorbance")),
                                                        "options": {"x_unit": "cm-1", "y_quantity": "absorbance", "analysis": "peaks"}})
        assert bad.status_code == 422 and "nm" in bad.json()["detail"]


def test_epoxide_reference_in_jcamp_keeps_its_own_units():
    pytest.importorskip("jcamp")
    nu = np.arange(1700, 799.99, -1.0)

    def absorbance(left):
        return 0.04 + _cosine(nu, 915, 25, 0.4 * left) + _cosine(nu, 1510, 20, 0.6)
    uncured = _jcamp([int(round(1e5 * 10 ** -a)) for a in absorbance(1.0)], 1700, 800, yunits="TRANSMITTANCE")  # fractions x 1e-5
    options = {"x_unit": "cm-1", "y_quantity": "absorbance", "sampling": "transmission", "analysis": "epoxide_conversion", "band_baseline": "two_point",
               "measure": "area", "epoxide_window": [945, 885], "reference_window": [1535, 1485], "reference_text": uncured}
    draft = analyze_curve("ir", _csv(nu, absorbance(0.25), ("wavenumber", "absorbance")), options)
    assert draft["measurement_draft"]["value"] == pytest.approx(0.75, abs=1e-3)  # transmittance rounding to 1e-5
