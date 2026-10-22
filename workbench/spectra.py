"""IR spectra, UV-vis absorption and turbidimetric cloud points. Values are review drafts, never labels.

Units, sampling mode, band windows and definitions are stated by the caller and recorded with each
value. Spectra come from a CSV with named columns or from one JCAMP-DX spectrum.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import threading

import numpy as np

from .ingestion import _fit, _new_draft, _points, _positive, _smoothed_derivative, _text, _window

HC_EV_NM = 1239.841984  # h c / e in eV nm, from the exact SI constants
T_FLOOR = 1e-4  # transmittance floor: absorbance is capped at 4 there and the points are flagged as saturated
# jcamp reports failed line checks with print(); stdout is captured, so parses run one at a time.
_JCAMP_LOCK = threading.Lock()

# Typical group-frequency ranges (cm-1) for polymers, from G. Socrates, Infrared and Raman Characteristic
# Group Frequencies, 3rd ed. (Wiley, 2001) and R. M. Silverstein, F. X. Webster and D. J. Kiemle,
# Spectrometric Identification of Organic Compounds, 7th ed. (Wiley, 2005). These are candidates for
# review: every range containing a peak is listed, and overlapping groups are not resolved.
IR_GROUPS = [
    (4950, 4910, "N-H combination, primary amine (NIR)", "falls as an amine hardener reacts"),
    (4640, 4600, "aromatic C-H combination (NIR)", "~4623, cure-invariant reference for epoxy conversion"),
    (4550, 4510, "epoxide combination band (NIR)", "~4530, consumed on cure"),
    (3650, 3584, "O-H stretch, free", "alcohols, phenols; sharp"),
    (3550, 3200, "O-H stretch, hydrogen-bonded", "alcohols, phenols, absorbed water; broad"),
    (3500, 3300, "N-H stretch", "amines (primary: two bands), amides (~3300)"),
    (3300, 2500, "O-H stretch, carboxylic acid dimer", "very broad, under the C-H stretches"),
    (3100, 3000, "=C-H stretch", "aromatic rings and alkenes"),
    (3000, 2840, "C-H stretch, sp3", "CH3 ~2960/2870, CH2 ~2925/2850"),
    (2400, 2300, "CO2 (atmosphere)", "~2360/2340 doublet; purge or background artefact, not the sample"),
    (2280, 2240, "N=C=O asymmetric stretch", "isocyanate; consumed on urethane formation"),
    (2260, 2210, "C≡N stretch", "nitriles, e.g. polyacrylonitrile ~2240"),
    (1870, 1800, "C=O stretch, anhydride (upper band)", "paired with a band near 1800-1740"),
    (1800, 1740, "C=O stretch, anhydride (lower band)", "paired with a band near 1870-1800"),
    (1750, 1725, "C=O stretch, saturated ester", "e.g. PMMA ~1730, PVAc ~1740"),
    (1740, 1720, "C=O stretch, aldehyde", "with C-H bands near 2820/2720"),
    (1740, 1690, "C=O stretch, urethane", "free ~1730, hydrogen-bonded ~1700"),
    (1730, 1705, "C=O stretch, conjugated or aryl ester", "e.g. PET ~1715"),
    (1725, 1705, "C=O stretch, saturated ketone", "polyolefin oxidation ~1715"),
    (1725, 1700, "C=O stretch, carboxylic acid dimer", "polyolefin oxidation ~1712"),
    (1700, 1630, "C=O stretch, urea", "hydrogen-bonded ureas ~1640"),
    (1680, 1630, "amide I (C=O stretch)", "proteins ~1650, polyamides ~1640"),
    (1680, 1620, "C=C stretch, alkene", "weak; acrylate and vinyl C=C ~1635"),
    (1650, 1630, "H-O-H bend, water", "absorbed moisture; overlaps amide I"),
    (1650, 1580, "N-H bend, primary amine", ""),
    (1650, 1550, "COO- asymmetric stretch", "carboxylate salts, ionomers"),
    (1625, 1575, "aromatic ring C=C stretch", "~1600 and ~1580"),
    (1570, 1515, "amide II (N-H bend + C-N stretch)", "proteins ~1545, polyamides ~1540"),
    (1525, 1475, "aromatic ring C=C stretch", "~1500; para-substituted rings ~1510 (DGEBA reference band)"),
    (1475, 1440, "CH2 scissoring, CH3 asymmetric bend", "polyethylene 1472/1462"),
    (1450, 1360, "COO- symmetric stretch", "carboxylate salts, ionomers"),
    (1400, 1000, "C-F stretch", "fluoropolymers, e.g. PTFE ~1200/1150"),
    (1390, 1365, "CH3 symmetric bend", "polypropylene ~1376; gem-dimethyl doublet 1385/1365"),
    (1350, 1300, "S=O asymmetric stretch, sulfone", "polysulfones ~1325"),
    (1300, 1150, "C-O stretch, ester", "esters show two strong C-O bands in 1300-1000"),
    (1275, 1200, "C-O-C asymmetric stretch, aryl alkyl ether", "e.g. DGEBA ~1245"),
    (1280, 1230, "epoxide ring breathing (symmetric C-O-C stretch)", "~1250; overlaps the aryl ether band of DGEBA"),
    (1270, 1255, "Si-CH3 symmetric deformation", "silicones ~1260"),
    (1260, 1000, "C-O stretch, alcohol", "primary ~1050, secondary ~1100, tertiary ~1150"),
    (1160, 1120, "S=O symmetric stretch, sulfone", "polysulfones ~1150"),
    (1150, 1050, "C-O-C stretch, aliphatic ether", "e.g. poly(ethylene glycol) ~1100"),
    (1130, 1000, "Si-O-Si stretch", "siloxanes ~1090/1020, silica"),
    (1075, 1020, "C-O-C symmetric stretch, aryl alkyl ether", "e.g. DGEBA ~1035"),
    (950, 810, "epoxide ring, asymmetric C-O-C stretch", "glycidyl ethers ~915 (consumed on cure), also near 860-830"),
    (860, 800, "aromatic C-H out-of-plane bend, para-disubstituted", "e.g. DGEBA ~830"),
    (850, 550, "C-Cl stretch", "PVC ~690-610"),
    (820, 780, "Si-CH3 rock", "silicones ~800"),
    (770, 730, "aromatic C-H out-of-plane bend, monosubstituted", "polystyrene ~757, with ~698"),
    (740, 715, "CH2 rock, four or more CH2 in a row", "polyethylene 730/720 doublet (crystalline)"),
    (710, 690, "aromatic ring out-of-plane bend, monosubstituted", "polystyrene ~698, with ~757"),
]
IR_SOURCE = "Socrates (2001) and Silverstein, Webster & Kiemle (2005) group-frequency tables"
# Specified area under band: Almond et al., e-Polymers 20, 369 (2020).
SAUB = {"carbonyl_window": (1650.0, 1850.0), "reference_window": (1420.0, 1500.0)}

# Molar absorption at 280 nm of folded proteins in water: Pace, Vajdos, Fee, Grimsley & Gray, Protein Sci. 4, 2411 (1995).
PACE_280 = {"W": 5500.0, "Y": 1490.0, "cystine": 125.0}
# Average residue masses (g/mol) of the ExPASy tables; one water per chain, two H lost per disulfide.
RESIDUE_MASS = {"A": 71.0788, "R": 156.1875, "N": 114.1038, "D": 115.0886, "C": 103.1388, "E": 129.1155, "Q": 128.1307,
                "G": 57.0519, "H": 137.1411, "I": 113.1594, "L": 113.1594, "K": 128.1741, "M": 131.1926, "F": 147.1766,
                "P": 97.1167, "S": 87.0782, "T": 101.1051, "W": 186.2132, "Y": 163.1760, "V": 99.1326}
WATER_MASS, DISULFIDE_LOSS = 18.01524, 2.01588

DEFAULT_COLUMNS = {"ir": ("wavenumber", "absorbance"), "uvvis": ("wavelength", "absorbance"), "turbidity": ("temperature", "transmittance")}
JCAMP_X = {"1/CM": "cm-1", "CM-1": "cm-1", "CM^-1": "cm-1", "WAVENUMBERS": "cm-1", "MICROMETERS": "um", "MICRONS": "um", "NANOMETERS": "nm"}
# The standard says TRANSMITTANCE and REFLECTANCE are fractions, but many exporters write percent under the
# same label, so their scale is left open (None) and read from the values.
JCAMP_Y = {"ABSORBANCE": ("absorbance", None), "TRANSMITTANCE": ("transmittance", None), "REFLECTANCE": ("reflectance", None),
           "%T": ("transmittance", "%"), "% TRANSMITTANCE": ("transmittance", "%"), "PERCENT TRANSMITTANCE": ("transmittance", "%")}
JCAMP_HEADER = ("title", "jcamp-dx", "data type", "origin", "owner", "xunits", "yunits", "xfactor", "yfactor", "firstx", "lastx",
                "npoints", "resolution", "spectrometer/data system", "sampling procedure", "state", "path length", "date", "longdate")


def _jcamp(text):
    try:
        import jcamp
    except ImportError as exc:
        raise ValueError("JCAMP-DX import needs the jcamp package (pip install jcamp)") from exc
    if len(text.encode("utf-8")) > 10_000_000:
        raise ValueError("Spectrum file exceeds 10 MB")
    # "$$" starts a comment anywhere on a line; the reader only skips lines that begin with it.
    text = "\n".join(line.split("$$", 1)[0].rstrip() for line in text.splitlines())
    messages = io.StringIO()
    with _JCAMP_LOCK:
        try:
            with contextlib.redirect_stdout(messages):
                data = jcamp.read(io.StringIO(text))
        except Exception as exc:  # the parser raises bare Exception, KeyError and unpacking errors on malformed files
            raise ValueError(f"The JCAMP-DX file could not be parsed: {exc!r}") from exc
    log = messages.getvalue()
    if data.get("children"):
        raise ValueError("The JCAMP-DX file links several spectra; export one spectrum per file")
    if "xydata" not in data and "xypoints" not in data:
        raise ValueError("The JCAMP-DX file has no XYDATA or XYPOINTS spectrum; a peak table alone cannot be analyzed")
    failures = [line for line in log.splitlines() if line.startswith(("Y-Check failed", "Mismatch of array lengths"))]
    if failures:
        raise ValueError("The JCAMP-DX data fails its own line checks, so the file is damaged or uses a compression form the reader "
                         f"cannot decode ({failures[0]}); export the spectrum as CSV")
    x, y = np.asarray(data["x"], dtype=float), np.asarray(data["y"], dtype=float)
    npoints = data.get("npoints")
    if isinstance(npoints, (int, float)) and len(y) != int(npoints):
        raise ValueError(f"The JCAMP-DX file declares NPOINTS={int(npoints)} but holds {len(y)} values")
    if len(x) != len(y) or len(x) < 3:
        raise ValueError("The JCAMP-DX spectrum needs at least three (x, y) points")
    if len(x) > 100000:
        raise ValueError("Spectrum exceeds 100000 data points")
    if not (np.isfinite(x).all() and np.isfinite(y).all()):
        raise ValueError("The JCAMP-DX spectrum contains nonfinite values")
    header = {key: data[key] if isinstance(data[key], (int, float)) else str(data[key])[:200] for key in JCAMP_HEADER if key in data}
    notes = ["JCAMP-DX line abscissas disagree with FIRSTX/LASTX/NPOINTS; x was rebuilt from FIRSTX/LASTX."] if "X-Check failed" in log else []
    return x, y, header, notes


def _load(kind, text, options):
    """x and y in file order with their declared units, from a CSV with named columns or one JCAMP-DX spectrum."""
    if text.lstrip("﻿ \t\r\n").startswith("##"):
        if kind == "turbidity":
            raise ValueError("Turbidity curves are read from CSV (temperature and transmittance or absorbance columns)")
        x, y, header, notes = _jcamp(text)
        xunits, yunits = str(header.get("xunits", "")).strip(), str(header.get("yunits", "")).strip()
        quantity, scale = JCAMP_Y.get(yunits.upper(), (None, None))
        declared = {"x_unit": JCAMP_X.get(xunits.upper()), "y_quantity": quantity}
        for key, value in declared.items():
            if value is not None and options.get(key) not in (None, "", value):
                raise ValueError(f"{key} {options[key]!r} contradicts the JCAMP-DX header ({value})")
        units = {key: options.get(key) or declared[key] for key in ("x_unit", "y_quantity")}
        if not units["x_unit"]:
            raise ValueError(f"The JCAMP-DX XUNITS {xunits!r} is not a wavenumber or wavelength unit; state x_unit if it is one")
        if not units["y_quantity"]:
            raise ValueError(f"The JCAMP-DX YUNITS {yunits!r} is not absorbance, transmittance or reflectance; state y_quantity if the values are one of these")
        units["y_unit"] = options.get("y_unit") or scale
        if units["y_quantity"] in {"transmittance", "reflectance"} and not units["y_unit"]:
            units["y_unit"] = "%" if np.max(np.abs(y)) > 1.5 else "fraction"
            notes.append(f"YUNITS {yunits} carries no scale; values up to {np.max(y):.4g} were read as {'percent' if units['y_unit'] == '%' else 'fractions'}.")
        source = {"format": "JCAMP-DX", "header": header}
        columns = (f"x ({header.get('xunits', 'undeclared')})", f"y ({header.get('yunits', 'undeclared')})")
    else:
        # The y column defaults to the stated quantity (absorbance, transmittance, reflectance).
        columns = (options.get("x_column", DEFAULT_COLUMNS[kind][0]), options.get("y_column", options.get("y_quantity") or DEFAULT_COLUMNS[kind][1]))
        x, y = _points(text, *columns)
        units = {key: options.get(key) for key in ("x_unit", "y_quantity", "y_unit")}
        source, notes = {"format": "CSV"}, []
    steps = np.diff(x)
    if np.all(steps > 0):
        order = "increasing"
    elif np.all(steps < 0):
        order = "decreasing"
    else:
        raise ValueError("x values must be strictly monotonic: upload one scan without repeated or unsorted points")
    return x, y, units, source, notes, columns, order


def _span(value, label):
    """[a, b] in either order (IR windows are often written high to low)."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} must contain two limits in the axis units")
    return _window(sorted(float(v) for v in value), label)


def _fraction(y, unit, label):
    if unit not in {"%", "fraction"}:
        raise ValueError(f"y_unit must state whether {label} is in % or a fraction")
    if unit == "%" and np.max(y) <= 1.5:
        raise ValueError(f"{label} is declared in % but never exceeds 1.5; is it a fraction?")
    values = y / 100 if unit == "%" else y.astype(float)
    if np.max(values) > 1.5:
        raise ValueError(f"{label} exceeds 150%; check y_unit")
    return values


def _absorbance(y, quantity, unit):
    """Absorbance and a saturation mask (transmittance at or below the floor) from absorbance or transmittance."""
    if quantity == "absorbance":
        return y.astype(float), np.zeros(len(y), dtype=bool)
    transmittance = _fraction(y, unit, "transmittance")
    return -np.log10(np.maximum(transmittance, T_FLOOR)), transmittance <= T_FLOOR


def _ranges(x, mask):
    """Contiguous x ranges where mask is set, for warnings."""
    out, start = [], None
    for i, flag in enumerate(mask):
        if flag and start is None:
            start = i
        if start is not None and (not flag or i == len(mask) - 1):
            end = i if flag else i - 1
            out.append(f"{x[start]:g}-{x[end]:g}")
            start = None
    return ", ".join(out[:5]) + (" ..." if len(out) > 5 else "")


def _vertex(x, y, i):
    """Peak position from the parabola through the maximum and its two neighbours."""
    if i == 0 or i == len(x) - 1:
        return float(x[i])
    x0, x1, x2, y0, y1, y2 = x[i - 1], x[i], x[i + 1], y[i - 1], y[i], y[i + 1]
    denominator = (x0 - x1) * (x0 - x2) * (x1 - x2)
    a = (x2 * (y1 - y0) + x1 * (y0 - y2) + x0 * (y2 - y1)) / denominator
    b = (x2 ** 2 * (y0 - y1) + x1 ** 2 * (y2 - y0) + x0 ** 2 * (y1 - y2)) / denominator
    if a >= 0:
        return float(x1)
    return float(min(max(-b / (2 * a), x0), x2))


def _peaks(x, signal, saturated, options):
    """Maxima by prominence (scipy.signal.find_peaks), widths at half prominence."""
    from scipy.signal import find_peaks, peak_widths
    span = float(np.ptp(signal))
    if span <= 0:
        raise ValueError("The spectrum is flat")
    prominence = _positive(options, "min_prominence", "min_prominence") if options.get("min_prominence") is not None else 0.02 * span
    limit = options.get("max_peaks", 25)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("max_peaks must be an integer from 1 to 100")
    found, props = find_peaks(signal, prominence=prominence)
    settings = {"min_prominence": prominence, "max_peaks": limit, "found": int(len(found)),
                "method": "scipy.signal.find_peaks by prominence; width at half prominence; position from a three-point parabola"}
    if not len(found):
        return [], settings
    _, _, left, right = peak_widths(signal, found, rel_height=0.5, prominence_data=(props["prominences"], props["left_bases"], props["right_bases"]))
    index = np.arange(len(x))
    left, right = np.interp(left, index, x), np.interp(right, index, x)
    peaks = []
    for k in np.argsort(props["prominences"])[::-1]:
        i = int(found[k])
        peak = {"position": _vertex(x, signal, i), "signal": float(signal[i]), "prominence": float(props["prominences"][k]),
                "fwhm": float(abs(right[k] - left[k])), "saturated": bool(saturated[i])}
        # Two maxima of equal height on one band top both get the full prominence; keep the first.
        if any(abs(peak["position"] - kept["position"]) < 0.5 * kept["fwhm"] for kept in peaks):
            continue
        peaks.append(peak)
        if len(peaks) == limit:
            break
    return peaks, settings


def _band(nu, absorbance, saturated, window, baseline, label):
    """Area and height of one band above a straight line joining the window limits (or above zero)."""
    low, high = window
    if low < nu[0] or high > nu[-1]:
        raise ValueError(f"{label} [{low:g}, {high:g}] lies outside the spectrum [{nu[0]:g}, {nu[-1]:g}] cm-1")
    inside = (nu >= low) & (nu <= high)
    if inside.sum() < 5:
        raise ValueError(f"{label} needs at least five data points")
    if saturated[inside].any():
        raise ValueError(f"{label} contains saturated points (transmittance at or below 0.01%), where absorbance is undefined")
    xs = np.concatenate([[low], nu[inside], [high]])
    ys = np.interp(xs, nu, absorbance)
    base = ys[0] + (ys[-1] - ys[0]) * (xs - low) / (high - low) if baseline == "two_point" else np.zeros_like(xs)
    excess = ys - base
    top = int(np.argmax(excess))
    return {"window_cm-1": [low, high], "points": int(inside.sum()), "area": float(np.trapezoid(excess, xs)),
            "height": float(excess[top]), "peak_position_cm-1": float(xs[top]), "max_absorbance": float(ys.max())}


def _ratio(numerator, denominator, measure, label, warnings):
    if denominator[measure] <= 0:
        raise ValueError(f"The {label} reference band has no absorbance above its baseline")
    value = numerator[measure] / denominator[measure]
    if value < 0:
        if value < -0.02:
            raise ValueError(f"The {label} band lies below its baseline; check the window and baseline")
        warnings.append(f"The {label} band is within noise of its baseline ({value:.4f}); reported as 0.")
        value = 0.0
    return value


def _wavenumbers(x, unit):
    if unit == "cm-1":
        return x.astype(float)
    if unit in {"um", "nm"}:
        if np.any(x <= 0):
            raise ValueError("Wavelengths must be positive")
        return (1e4 if unit == "um" else 1e7) / x
    raise ValueError("x_unit must be cm-1 (wavenumber), um or nm (wavelength)")


def _ir_absorbance(x, y, units):
    if units.get("y_quantity") not in {"absorbance", "transmittance"}:
        raise ValueError("y_quantity must be absorbance or transmittance")
    nu = _wavenumbers(x, units.get("x_unit"))
    order = np.argsort(nu)
    absorbance, saturated = _absorbance(y[order], units["y_quantity"], units.get("y_unit"))
    return nu[order], absorbance, saturated


def _ir(x, y, units, options, draft):
    sampling = options.get("sampling")
    if sampling not in {"transmission", "ATR", "reflection"}:
        raise ValueError("sampling must be transmission, ATR or reflection")
    analysis = options.get("analysis", "peaks")
    if analysis not in {"peaks", "carbonyl_index", "band_ratio", "epoxide_conversion"}:
        raise ValueError("IR analysis must be peaks, carbonyl_index, band_ratio or epoxide_conversion")
    nu, absorbance, saturated = _ir_absorbance(x, y, units)
    peaks, settings = _peaks(nu, absorbance, saturated, options)
    for peak in peaks:
        # Narrow (specific) ranges first; broad ones such as C-Cl or acid O-H overlap many bands.
        matches = sorted((high - low, low, high, group, note) for high, low, group, note in IR_GROUPS if low <= peak["position"] <= high)
        peak["assignments"] = [{"group": group, "range_cm-1": [low, high], "note": note} for _, low, high, group, note in matches]
    draft["peaks"] = sorted(peaks, key=lambda p: -p["position"])
    draft["peak_units"] = {"position": "cm-1", "signal": "absorbance", "fwhm": "cm-1", "assignments": IR_SOURCE}
    report = {"axis": "wavenumber, cm-1", "sampling": sampling, "points": int(len(nu)), "saturated_points": int(saturated.sum()), "peaks": settings}
    draft["fit"] = report
    if saturated.any():
        draft["warnings"].append(f"{int(saturated.sum())} points have transmittance at or below 0.01% and were capped at absorbance 4: {_ranges(nu, saturated)} cm-1.")
    if sampling == "ATR":
        draft["warnings"].append("ATR intensities grow with wavelength (penetration depth); compare band ratios only between spectra from the same crystal, angle and contact pressure.")
    draft["warnings"].append("Group assignments are candidates from correlation tables; confirm them with reference spectra of the material.")
    if "gas" in str(draft.get("source", {}).get("header", {}).get("state", "")).lower():
        draft["warnings"].append("The ranges are for condensed phases; gas-phase bands, notably C=O, sit 10-40 cm-1 higher.")
    if analysis == "peaks":
        return
    baseline = options.get("band_baseline")
    if baseline not in {"two_point", "none"}:
        raise ValueError("band_baseline must be two_point (straight line between the window limits) or none (absorbance above zero)")
    measure = "area" if analysis == "carbonyl_index" else options.get("measure")
    if measure not in {"area", "height"}:
        raise ValueError("measure must be area or height")
    conditions = {"sampling": sampling, "band_baseline": baseline, "measure": measure}
    if analysis == "carbonyl_index":
        carbonyl = _span(options.get("carbonyl_window", SAUB["carbonyl_window"]), "carbonyl_window")
        reference = _span(options.get("reference_window", SAUB["reference_window"]), "reference_window")
        bands = {"carbonyl": _band(nu, absorbance, saturated, carbonyl, baseline, "carbonyl_window"),
                 "reference": _band(nu, absorbance, saturated, reference, baseline, "reference_window")}
        value = _ratio(bands["carbonyl"], bands["reference"], "area", "carbonyl", draft["warnings"])
        saub = carbonyl == SAUB["carbonyl_window"] and reference == SAUB["reference_window"]
        method = ("IR carbonyl index by specified area under band (Almond et al. 2020): area 1850-1650 cm-1 / area 1500-1420 cm-1" if saub
                  else "IR carbonyl index: carbonyl band area divided by a reference band area over stated windows")
        drafts = [{"property": "carbonyl_index", "value": value, "unit": "dimensionless", "method": method,
                   "conditions": {**conditions, "carbonyl_window_cm-1": list(carbonyl), "reference_window_cm-1": list(reference)}, "analysis": {"bands": bands}}]
        if not saub:
            draft["warnings"].append("Carbonyl indices from other windows are not comparable with SAUB values.")
    elif analysis == "band_ratio":
        numerator = _span(options.get("numerator_window"), "numerator_window")
        denominator = _span(options.get("denominator_window"), "denominator_window")
        bands = {"numerator": _band(nu, absorbance, saturated, numerator, baseline, "numerator_window"),
                 "denominator": _band(nu, absorbance, saturated, denominator, baseline, "denominator_window")}
        value = _ratio(bands["numerator"], bands["denominator"], measure, "numerator", draft["warnings"])
        conditions.update({"numerator_window_cm-1": list(numerator), "denominator_window_cm-1": list(denominator)})
        if options.get("ratio_label"):
            conditions["ratio_label"] = _text(options, "ratio_label", "ratio_label")
        drafts = [{"property": "ir_band_ratio", "value": value, "unit": "dimensionless",
                   "method": f"IR band {measure} ratio over stated windows", "conditions": conditions, "analysis": {"bands": bands}}]
    else:
        epoxide = _span(options.get("epoxide_window"), "epoxide_window")
        reference = _span(options.get("reference_window"), "reference_window")
        bands = {"epoxide": _band(nu, absorbance, saturated, epoxide, baseline, "epoxide_window"),
                 "reference": _band(nu, absorbance, saturated, reference, baseline, "reference_window")}
        ratio = _ratio(bands["epoxide"], bands["reference"], measure, "epoxide", draft["warnings"])
        given = [key for key in ("reference_text", "initial_ratio") if options.get(key) is not None]
        if len(given) != 1:
            raise ValueError("Epoxide conversion needs exactly one of reference_text (the uncured spectrum) or initial_ratio")
        if given[0] == "reference_text":
            if not isinstance(options["reference_text"], str):
                raise ValueError("reference_text must be the uncured spectrum as CSV or JCAMP-DX text")
            # A JCAMP-DX reference declares its own units; a CSV reference shares the stated ones.
            jcamp_reference = options["reference_text"].lstrip("\ufeff \t\r\n").startswith("##")
            reference_options = {k: v for k, v in options.items() if k not in {"x_unit", "y_quantity", "y_unit"}} if jcamp_reference else options
            rx, ry, runits, _, _, _, _ = _load("ir", options["reference_text"], reference_options)
            rnu, rabs, rsat = _ir_absorbance(rx, ry, runits)
            initial = {"epoxide": _band(rnu, rabs, rsat, epoxide, baseline, "epoxide_window (uncured spectrum)"),
                       "reference": _band(rnu, rabs, rsat, reference, baseline, "reference_window (uncured spectrum)")}
            initial_ratio = _ratio(initial["epoxide"], initial["reference"], measure, "uncured epoxide", draft["warnings"])
            origin = {"initial_ratio_from": "uncured reference spectrum", "reference_sha256": hashlib.sha256(options["reference_text"].encode()).hexdigest()}
            report["uncured_bands"] = initial
        else:
            initial_ratio = _positive(options, "initial_ratio", "initial_ratio")
            origin = {"initial_ratio_from": "stated initial_ratio"}
        if initial_ratio <= 0:
            raise ValueError("The uncured epoxide band has no absorbance above its baseline")
        conversion = 1 - ratio / initial_ratio
        if not -0.02 <= conversion <= 1.02:
            raise ValueError(f"Conversion {conversion:.3f} lies outside 0-1; check that both spectra use the same windows, baseline and sampling")
        if not 0 <= conversion <= 1:
            draft["warnings"].append(f"Conversion {conversion:.4f} is within noise of the 0-1 bounds and was clipped.")
            conversion = min(max(conversion, 0.0), 1.0)
        report.update({"ratio": ratio, "initial_ratio": initial_ratio, **origin})
        drafts = [{"property": "epoxide_conversion", "value": conversion, "unit": "fraction",
                   "method": f"IR epoxide conversion 1 - (epoxide/reference band {measure})_t / (epoxide/reference band {measure})_0 with an internal reference band",
                   "conditions": {**conditions, "epoxide_window_cm-1": list(epoxide), "reference_window_cm-1": list(reference),
                                  "initial_ratio_from": origin["initial_ratio_from"]},
                   "analysis": {"bands": bands, "ratio": ratio, "initial_ratio": initial_ratio, **origin, **({"uncured_bands": report["uncured_bands"]} if "uncured_bands" in report else {})}}]
        draft["warnings"].append("The reference band must not change during cure (for DGEBA, the aromatic band near 1510 cm-1 or the NIR band near 4623 cm-1).")
    report["bands"] = bands
    for name, band in bands.items():
        if band["max_absorbance"] > 1.5:
            draft["warnings"].append(f"The {name} band reaches absorbance {band['max_absorbance']:.2f}; above about 1.5 IR absorbance is rarely linear in concentration.")
    draft["measurement_drafts"] = drafts


def protein_absorptivity(sequence, cystines):
    """Molar absorption at 280 nm (Pace et al. 1995) and the average molar mass of one chain."""
    residues = "".join(str(sequence).split()).upper()
    if not residues or len(residues) > 20000:
        raise ValueError("protein_sequence must hold 1-20000 one-letter residues")
    unknown = sorted(set(residues) - set(RESIDUE_MASS))
    if unknown:
        raise ValueError("protein_sequence must use the 20 standard one-letter codes; found " + ", ".join(unknown))
    counts = {code: residues.count(code) for code in "WYC"}
    if isinstance(cystines, bool) or not isinstance(cystines, int) or not 0 <= cystines <= counts["C"] // 2:
        raise ValueError(f"cystines must be an integer from 0 to {counts['C'] // 2} (disulfide-bonded cysteine pairs)")
    epsilon = counts["W"] * PACE_280["W"] + counts["Y"] * PACE_280["Y"] + cystines * PACE_280["cystine"]
    if epsilon <= 0:
        raise ValueError("The sequence has no Trp, Tyr or cystine, so its 280 nm absorption cannot be predicted")
    mass = sum(RESIDUE_MASS[code] for code in residues) + WATER_MASS - DISULFIDE_LOSS * cystines
    return {"molar_absorptivity_l_per_mol_cm": epsilon, "molar_mass_g_mol": mass, "residues": len(residues),
            "trp": counts["W"], "tyr": counts["Y"], "cys": counts["C"], "cystines": cystines,
            "source": "Pace et al., Protein Sci. 4, 2411 (1995): 5500 (Trp), 1490 (Tyr), 125 (cystine) M-1 cm-1"}


def _uvvis(x, y, units, options, draft):
    if units.get("x_unit") != "nm":
        raise ValueError("x_unit must be nm for UV-vis spectra")
    if np.any(x <= 0):
        raise ValueError("Wavelengths must be positive")
    analysis = options.get("analysis")
    if analysis not in {"peaks", "concentration", "band_gap"}:
        raise ValueError("UV-vis analysis must be peaks, concentration or band_gap")
    order = np.argsort(x)
    wavelength, y = x[order], y[order]
    quantity = units.get("y_quantity")
    if quantity == "reflectance":
        if analysis != "band_gap":
            raise ValueError("Diffuse reflectance is analyzed only for band gaps (Kubelka-Munk)")
        reflectance = _fraction(y, units.get("y_unit"), "reflectance")
        if np.any(reflectance <= 0) or np.any(reflectance > 1):
            raise ValueError("Diffuse reflectance must lie in (0, 100%] for the Kubelka-Munk function")
        signal, saturated, name = (1 - reflectance) ** 2 / (2 * reflectance), np.zeros(len(y), dtype=bool), "Kubelka-Munk F(R)"
    elif quantity in {"absorbance", "transmittance"}:
        signal, saturated = _absorbance(y, quantity, units.get("y_unit"))
        name = "absorbance"
    else:
        raise ValueError("y_quantity must be absorbance, transmittance or reflectance")
    draft["fit"] = {"axis": "wavelength, nm", "signal": name, "saturated_points": int(saturated.sum())}
    draft["measurement_drafts"] = []
    if saturated.any():
        draft["warnings"].append(f"{int(saturated.sum())} points have transmittance at or below 0.01%: {_ranges(wavelength, saturated)} nm.")
    if analysis == "peaks":
        _uv_peaks(wavelength, signal, saturated, options, draft)
    elif analysis == "concentration":
        _uv_concentration(wavelength, signal, saturated, options, draft)
    else:
        _band_gap(wavelength, signal, saturated, name, options, draft)


def _uv_peaks(wavelength, signal, saturated, options, draft):
    medium = _text(options, "medium", "medium (for example film, or solvent and concentration)", 200)
    peaks, settings = _peaks(wavelength, signal, saturated, options)
    for peak in peaks:
        peak["energy_ev"] = HC_EV_NM / peak["position"]
    draft["peaks"] = sorted(peaks, key=lambda p: p["position"])
    draft["peak_units"] = {"position": "nm", "signal": "absorbance", "fwhm": "nm"}
    draft["fit"]["peaks"] = settings
    candidates = peaks
    search = None
    if options.get("search_window") is not None:
        search = _span(options["search_window"], "search_window")
        candidates = [p for p in peaks if search[0] <= p["position"] <= search[1]]
    if not candidates:
        raise ValueError("No absorption maximum in the spectrum" + (" inside search_window" if search else ""))
    best = max(candidates, key=lambda p: p["prominence"])
    if best["saturated"]:
        raise ValueError("The strongest absorption maximum is saturated; dilute or use a shorter path")
    if best["signal"] > 2:
        draft["warnings"].append(f"Absorbance {best['signal']:.2f} at the maximum is near the photometric limit; the band may be flattened.")
    draft["measurement_drafts"] = [{"property": "absorption_max_wavelength", "value": best["position"], "unit": "nm",
                                    "method": "UV-vis wavelength of the most prominent absorption maximum (three-point parabola)", "conditions": {"medium": medium},
                                    "analysis": {"absorbance": best["signal"], "prominence": best["prominence"], "fwhm_nm": best["fwhm"],
                                                 "search_window_nm": list(search) if search else None}}]
    draft["warnings"].append("Absorption maxima shift with solvent, aggregation, film morphology and concentration.")


def _at(wavelength, signal, saturated, value, label):
    if not wavelength[0] <= value <= wavelength[-1]:
        raise ValueError(f"{label} {value:g} nm lies outside the spectrum [{wavelength[0]:g}, {wavelength[-1]:g}] nm")
    near = np.abs(wavelength - value) <= max(np.min(np.abs(np.diff(wavelength))), 1e-9) * 1.01
    if saturated[near].any():
        raise ValueError(f"The spectrum is saturated at {label}")
    return float(np.interp(value, wavelength, signal))


def _uv_concentration(wavelength, signal, saturated, options, draft):
    target = _positive(options, "wavelength_nm", "wavelength_nm")
    absorbance = _at(wavelength, signal, saturated, target, "wavelength_nm")
    record = {"wavelength_nm": target, "absorbance_at_wavelength": absorbance}
    if options.get("scatter_wavelength_nm") is not None:
        scatter_at = _positive(options, "scatter_wavelength_nm", "scatter_wavelength_nm")
        scatter = _at(wavelength, signal, saturated, scatter_at, "scatter_wavelength_nm")
        absorbance -= scatter
        record.update({"scatter_wavelength_nm": scatter_at, "scatter_absorbance": scatter})
    record["absorbance_used"] = absorbance
    if absorbance <= 0:
        raise ValueError("Absorbance at the analytical wavelength is not positive after the stated corrections")
    path = _positive(options, "path_length_cm", "path_length_cm")
    dilution = _positive(options, "dilution_factor", "dilution_factor") if options.get("dilution_factor") is not None else 1.0
    if dilution < 1:
        raise ValueError("dilution_factor is the stock-to-cuvette dilution and must be at least 1")
    modes = [key for key in ("molar_absorptivity", "mass_absorptivity", "protein_sequence", "calibration") if options.get(key) is not None]
    if len(modes) != 1:
        raise ValueError("Give exactly one of molar_absorptivity, mass_absorptivity, protein_sequence or calibration")
    mode = modes[0]
    results = []
    if mode == "molar_absorptivity":
        epsilon = _positive(options, "molar_absorptivity", "molar_absorptivity (L mol-1 cm-1)")
        molar = absorbance * dilution / (epsilon * path)
        results += [{"quantity": "concentration", "value": molar, "unit": "mol/L"}, {"quantity": "concentration", "value": molar * 1e6, "unit": "µM"}]
        record.update({"method": "Beer-Lambert A = epsilon c l", "molar_absorptivity": epsilon})
    elif mode == "mass_absorptivity":
        epsilon = _positive(options, "mass_absorptivity", "mass_absorptivity (L g-1 cm-1, the absorbance of 1 mg/mL over 1 cm)")
        results.append({"quantity": "concentration", "value": absorbance * dilution / (epsilon * path), "unit": "mg/mL"})
        record.update({"method": "Beer-Lambert A = a c l (mass absorptivity)", "mass_absorptivity": epsilon})
    elif mode == "protein_sequence":
        if abs(target - 280) > 0.5:
            raise ValueError("Sequence-based absorption coefficients (Pace et al. 1995) apply at 280 nm")
        protein = protein_absorptivity(options["protein_sequence"], options.get("cystines"))
        molar = absorbance * dilution / (protein["molar_absorptivity_l_per_mol_cm"] * path)
        results += [{"quantity": "protein concentration", "value": molar * 1e6, "unit": "µM"},
                    {"quantity": "protein concentration", "value": molar * protein["molar_mass_g_mol"], "unit": "mg/mL"},
                    {"quantity": "absorbance of 1 mg/mL over 1 cm", "value": protein["molar_absorptivity_l_per_mol_cm"] / protein["molar_mass_g_mol"], "unit": "L g-1 cm-1"}]
        record.update({"method": "Beer-Lambert with the sequence-predicted molar absorption coefficient", "protein": protein})
        draft["warnings"].append("Sequence-predicted coefficients assume a folded protein in water and are typically within about 5% of measured values; other chromophores (cofactors, tags, nucleic acid) add absorbance.")
    else:
        unit = _text(options, "concentration_unit", "concentration_unit (for example mg/mL)")
        standards = options["calibration"]
        if not isinstance(standards, list) or not 3 <= len(standards) <= 50 or any(
                not isinstance(s, (list, tuple)) or len(s) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in s) for s in standards):
            raise ValueError("calibration needs 3-50 [concentration, absorbance] standards")
        c, a = np.array([s[0] for s in standards], float), np.array([s[1] for s in standards], float)
        if not (np.isfinite(c).all() and np.isfinite(a).all()) or np.any(c < 0):
            raise ValueError("Calibration concentrations must be finite and non-negative, absorbances finite")
        slope, intercept = np.polyfit(c, a, 1)
        if slope <= 0:
            raise ValueError("The calibration absorbance does not rise with concentration")
        fitted = slope * c + intercept
        total = float(np.sum((a - a.mean()) ** 2))
        if not a.min() <= absorbance <= a.max():
            raise ValueError(f"Absorbance {absorbance:.4f} lies outside the calibrated range [{a.min():.4f}, {a.max():.4f}]; extrapolation is refused")
        results.append({"quantity": "concentration", "value": (absorbance - intercept) / slope * dilution, "unit": unit})
        record.update({"method": "linear calibration A = m c + b from stated standards", "slope": float(slope), "intercept": float(intercept),
                       "r_squared": 1 - float(np.sum((a - fitted) ** 2)) / total if total > 0 else None, "standards": len(standards)})
    record.update({"path_length_cm": path, "dilution_factor": dilution})
    draft["fit"]["concentration"] = record
    draft["results"] = results
    if absorbance > 1.0:
        draft["warnings"].append(f"Absorbance {absorbance:.3f} is above the range where Beer-Lambert linearity is usually validated (about 0.1-1); dilute and remeasure.")
    elif absorbance < 0.05:
        draft["warnings"].append(f"Absorbance {absorbance:.3f} is close to the noise floor; use a longer path or a more concentrated sample.")
    draft["warnings"].append("Concentrations describe this sample, not the material, and are not stored as measurements.")


def _band_gap(wavelength, signal, saturated, name, options, draft):
    medium = _text(options, "medium", "medium (for example film, powder or solution)", 200)
    method = options.get("gap_method")
    if method not in {"tauc_direct", "tauc_indirect", "absorption_onset"}:
        raise ValueError("gap_method must be tauc_direct, tauc_indirect or absorption_onset")
    conditions, windows = {"gap_method": method, "medium": medium, "signal": name}, {}
    if method == "absorption_onset":
        window = _span(options.get("fit_window_nm"), "fit_window_nm")
        mask = (wavelength >= window[0]) & (wavelength <= window[1])
        if saturated[mask].any():
            raise ValueError("fit_window_nm contains saturated points")
        line, fit = _fit(wavelength, signal, window, "fit_window_nm")
        # The fitted line must fall by at least 5% of the signal across the window, or there is no edge to extrapolate.
        if -line[0] * (window[1] - window[0]) <= 0.05 * float(np.max(np.abs(signal[mask]))):
            raise ValueError("The absorption edge in fit_window_nm must fall with wavelength (the long-wavelength side of the band)")
        if options.get("baseline_window_nm") is not None:
            base_window = _span(options["baseline_window_nm"], "baseline_window_nm")
            if base_window[0] < window[1]:
                raise ValueError("baseline_window_nm must lie at longer wavelengths than fit_window_nm")
            base, base_fit = _fit(wavelength, signal, base_window, "baseline_window_nm")
            onset = (base[1] - line[1]) / (line[0] - base[0])
            windows["baseline_window_nm"] = list(base_window)
        else:
            base_fit, onset = None, -line[1] / line[0]
        if onset <= window[0]:
            raise ValueError("The extrapolated onset does not lie on the long-wavelength side of fit_window_nm")
        gap = HC_EV_NM / onset
        windows["fit_window_nm"] = list(window)
        draft["fit"]["band_gap"] = {"edge_fit": fit, "baseline_fit": base_fit, "onset_wavelength_nm": float(onset), "gap_ev": gap}
        method_text = "UV-vis optical gap 1239.84/lambda_onset; onset where the tangent to the absorption edge meets the baseline"
    else:
        exponent = 2.0 if method == "tauc_direct" else 0.5
        energy = HC_EV_NM / wavelength[::-1]
        values = np.clip(signal[::-1], 0, None)
        tauc = (values * energy) ** exponent
        window = _span(options.get("fit_window_ev"), "fit_window_ev")
        mask = (energy >= window[0]) & (energy <= window[1])
        if saturated[::-1][mask].any():
            raise ValueError("fit_window_ev contains saturated points")
        line, fit = _fit(energy, tauc, window, "fit_window_ev")
        if line[0] * (window[1] - window[0]) <= 0.05 * float(np.max(np.abs(tauc[mask]))):
            raise ValueError("The Tauc plot must rise with photon energy inside fit_window_ev")
        if options.get("baseline_window_ev") is not None:
            # Makula, Pacia & Macyk, J. Phys. Chem. Lett. 9, 6814 (2018): intersect with the sub-gap baseline, not the energy axis.
            base_window = _span(options["baseline_window_ev"], "baseline_window_ev")
            if base_window[1] > window[0]:
                raise ValueError("baseline_window_ev must lie below fit_window_ev")
            base, base_fit = _fit(energy, tauc, base_window, "baseline_window_ev")
            if abs(line[0] - base[0]) <= 1e-12 * abs(line[0]):
                raise ValueError("The Tauc edge and baseline are parallel")
            gap = (base[1] - line[1]) / (line[0] - base[0])
            windows["baseline_window_ev"] = list(base_window)
        else:
            base_fit, gap = None, -line[1] / line[0]
        if not 0 < gap < window[1]:
            raise ValueError(f"The extrapolated gap {gap:.3f} eV is not below the fit window; choose the linear part of the absorption edge")
        windows["fit_window_ev"] = list(window)
        transition = "direct allowed, (alpha h nu)^2" if method == "tauc_direct" else "indirect allowed, (alpha h nu)^(1/2)"
        draft["fit"]["band_gap"] = {"tauc": transition, "edge_fit": fit, "baseline_fit": base_fit, "gap_ev": float(gap),
                                    "negative_signal_points_set_to_zero": int(np.sum(signal < 0))}
        method_text = f"UV-vis Tauc plot ({transition}) linear extrapolation" + (" to the sub-gap baseline" if base_fit else " to zero")
        draft["warnings"].append("A Tauc gap presumes the chosen transition type and a parabolic band edge; the fit window moves it by tenths of an eV. For organic and excitonic absorbers the absorption onset is the usual descriptor.")
    draft["measurement_drafts"] = [{"property": "optical_band_gap", "value": float(gap), "unit": "eV", "method": method_text, "conditions": conditions,
                                    "analysis": {**windows, **draft["fit"]["band_gap"]}}]


CLOUD_POINT_METHODS = {
    "midpoint": "Turbidimetric cloud point: first crossing, along the scan, of the level halfway between the clear and turbid plateaus",
    "transmittance_50": "Turbidimetric cloud point: first temperature along the scan where transmittance falls to 50%",
    "inflection": "Turbidimetric cloud point: steepest rise in turbidity along the scan (Savitzky-Golay derivative)",
    "onset": "Turbidimetric cloud point: extrapolated onset, where the clear-plateau line meets the tangent at the steepest rise",
}


def _crossing(t, signal, level, start):
    """First temperature after index start where the signal, below level there, reaches it (linear interpolation)."""
    if signal[start] >= level:
        return None
    above = np.flatnonzero(signal[start + 1:] >= level)
    if not len(above):
        return None
    i = start + 1 + int(above[0])
    return float(t[i - 1] + (level - signal[i - 1]) * (t[i] - t[i - 1]) / (signal[i] - signal[i - 1]))


def _turbidity(x, y, units, order, options, draft):
    temperature_unit = options.get("temperature_unit")
    if temperature_unit not in {"K", "C", "°C"}:
        raise ValueError("temperature_unit must explicitly be K or C")
    offset = 0 if temperature_unit == "K" else 273.15
    quantity = units.get("y_quantity")
    if quantity not in {"transmittance", "absorbance"}:
        raise ValueError("y_quantity must be transmittance or absorbance (optical density)")
    branch = options.get("branch")
    if branch not in {"heating", "cooling"}:
        raise ValueError("branch must be heating or cooling")
    expected = "increasing" if branch == "heating" else "decreasing"
    if order != expected:
        raise ValueError(f"A {branch} scan lists temperatures in {expected} order, as measured; these rows are {order}")
    rate = _positive(options, "rate_k_per_min", "rate_k_per_min (heating or cooling rate)")
    definition = options.get("definition")
    if definition not in CLOUD_POINT_METHODS:
        raise ValueError("definition must be midpoint, transmittance_50, inflection or onset")
    if definition == "transmittance_50" and quantity != "transmittance":
        raise ValueError("The 50% transmittance definition needs transmittance data")
    concentration = _text(options, "concentration", "concentration (for example 1 mg/mL in water)", 200)
    transmittance = _fraction(y, units.get("y_unit"), "transmittance") if quantity == "transmittance" else None
    signal = 1 - transmittance if transmittance is not None else y.astype(float)  # rises as the solution clouds
    scan = x if branch == "heating" else -x  # increases along the scan
    windows, levels, report = {}, {}, {"branch": branch, "signal": "1 - T" if transmittance is not None else "absorbance"}
    for name in ("clear_window", "turbid_window"):
        if options.get(name) is not None:
            low, high = _span(options[name], name)
            mask = (x >= low) & (x <= high)
            if mask.sum() < 3:
                raise ValueError(f"{name} must contain at least three points")
            windows[name], levels[name] = mask, float(np.mean(signal[mask]))
    if definition in {"midpoint", "onset"} and len(windows) < 2:
        raise ValueError(f"The {definition} definition needs clear_window and turbid_window")
    start, stop = 0, len(x) - 1
    if len(windows) == 2:
        last_clear = int(np.flatnonzero(windows["clear_window"])[-1])
        first_turbid = int(np.flatnonzero(windows["turbid_window"])[0])
        if first_turbid <= last_clear:
            raise ValueError("clear_window must come before turbid_window along the scan")
        rise = levels["turbid_window"] - levels["clear_window"]
        noise = max(float(np.std(signal[windows["clear_window"]])), float(np.std(signal[windows["turbid_window"]])), 1e-12)
        if rise <= 0:
            raise ValueError("The solution does not turn turbid along this scan; a clearing curve gives a clearing point, not a cloud point")
        if rise < 10 * noise:
            raise ValueError("The change between the plateaus is not clearly above their scatter")
        start, stop = last_clear, first_turbid
        report.update({"clear_level": levels["clear_window"], "turbid_level": levels["turbid_window"], "plateau_noise": noise})
    elif len(windows) == 1:
        raise ValueError("Give both clear_window and turbid_window, or neither")
    values = {}
    if len(windows) == 2:
        values["midpoint"] = _crossing(x, signal, (levels["clear_window"] + levels["turbid_window"]) / 2, start)
    if transmittance is not None:
        values["transmittance_50"] = _crossing(x, signal, 0.5, start)
    segment = slice(start, stop + 1)
    if stop - start >= 4:
        default = abs(x[stop] - x[start]) / 30
        span = _positive(options, "derivative_smoothing", "derivative_smoothing") if options.get("derivative_smoothing") is not None else default
        grid, smooth, slope, smoothing = _smoothed_derivative(scan[segment], signal[segment], span)
        i = int(np.argmax(slope))
        if slope[i] > 0:
            values["inflection"] = float(grid[i] if branch == "heating" else -grid[i])
            report.update({"steepest_slope_per_K": float(slope[i]), "derivative": smoothing})
            if len(windows) == 2:
                clear_fit = np.polyfit(scan[windows["clear_window"]], signal[windows["clear_window"]], 1)
                if abs(slope[i] - clear_fit[0]) > 1e-12:
                    meet = (clear_fit[1] - smooth[i] + slope[i] * grid[i]) / (slope[i] - clear_fit[0])
                    values["onset"] = float(meet if branch == "heating" else -meet)
    if values.get(definition) is None:
        raise ValueError(f"The {definition} cloud point is undefined for this curve; check the windows or the y data")
    kelvin = values[definition] + offset
    if kelvin <= 0:
        raise ValueError("Cloud point is below absolute zero")
    report["cloud_point_K"] = {name: value + offset for name, value in values.items() if value is not None}
    draft["fit"] = report
    conditions = {"definition": definition, "branch": branch, "rate_k_per_min": rate, "concentration": concentration}
    for key in ("wavelength_nm", "path_length_cm"):
        if options.get(key) is not None:
            conditions[key] = _positive(options, key, key)
    draft["measurement_drafts"] = [{"property": "cloud_point_temperature", "value": kelvin, "unit": "K", "method": CLOUD_POINT_METHODS[definition],
                                    "conditions": conditions, "analysis": report}]
    draft["warnings"].append("Cloud points depend on concentration, scan rate, wavelength, path length and definition; heating and cooling branches differ by hysteresis.")


def analyze_spectrum(kind: str, text: str, options: dict) -> dict:
    x, y, units, source, notes, columns, order = _load(kind, text, options)
    echoed = {key: value for key, value in options.items() if key != "reference_text"}
    if isinstance(options.get("reference_text"), str):
        echoed["reference_sha256"] = hashlib.sha256(options["reference_text"].encode()).hexdigest()
    draft = _new_draft(kind, text, echoed, x, y, *columns)
    draft["source"] = {**source, "units": units, "x_order": order}
    draft["warnings"].extend(notes)
    draft["measurement_drafts"] = []
    if kind == "ir":
        _ir(x, y, units, options, draft)
        draft["preview"]["reverse_x"] = units.get("x_unit") == "cm-1"  # spectra are drawn from high to low wavenumber
    elif kind == "uvvis":
        _uvvis(x, y, units, options, draft)
    else:
        _turbidity(x, y, units, order, options, draft)
    return draft
