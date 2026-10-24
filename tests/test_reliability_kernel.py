"""Independent NumPy/SciPy checks for the Rust replicate density summary."""
import numpy as np
import pytest
from scipy import stats

native = pytest.importorskip('workbench_native')


@pytest.mark.parametrize('values', [[.98, 1., 1.02], np.random.default_rng(892).normal(1.1, .05, 37).tolist()])
def test_uncertainty_and_reference_difference_match_numpy_scipy(values):
    values = np.asarray(values, dtype=float)
    result = native.replicate_summary(values, reference=1.01, reference_standard_uncertainty=.012)
    mean, sd = values.mean(), values.std(ddof=1)
    se = sd / np.sqrt(len(values))
    critical = stats.t.ppf(.975, len(values)-1)
    assert result['mean'] == pytest.approx(mean, rel=1e-14)
    assert result['sample_sd'] == pytest.approx(sd, rel=1e-13)
    assert result['standard_error'] == pytest.approx(se, rel=1e-13)
    np.testing.assert_allclose(result['confidence_interval'], mean + np.array([-1,1])*critical*se, rtol=1e-12)
    comparison = result['reference_comparison']
    combined = np.hypot(se,.012)
    assert comparison['value_difference'] == pytest.approx(mean-1.01, abs=1e-14)
    assert comparison['combined_standard_uncertainty'] == pytest.approx(combined, rel=1e-13)
    assert comparison['standardized_difference'] == pytest.approx((mean-1.01)/combined, abs=1e-12)


def test_unknowns_and_constant_spread_are_explicit():
    one = native.replicate_summary(np.array([1.]))
    assert one['sample_sd'] is None and one['confidence_interval'] is None
    exact = native.replicate_summary(np.ones(3), reference=1.)
    assert exact['sample_sd'] == 0. and exact['confidence_interval'] == [1.,1.]
    assert exact['reference_comparison']['combined_standard_uncertainty'] is None
    assert any('zero' in message for message in exact['warnings'])
    huge = np.broadcast_to(np.array([1.]), (100_000_000,))
    with pytest.raises(ValueError):native.replicate_summary(huge)
    for invalid in [np.array([]),np.array([0.]),np.array([np.nan]),np.array([np.inf]),np.array([-1.])]:
        with pytest.raises(ValueError):native.replicate_summary(invalid)
    with pytest.raises((ValueError, TypeError)):native.replicate_summary(np.ones(3), reference=True)
