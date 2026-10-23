"""The public Rust bindings reject malformed arrays before indexing or allocating."""
import numpy as np
import pytest


native = pytest.importorskip("workbench_native", reason="Rust analysis extension is not installed")


def volume(**changes):
    args = dict(positions=np.array([[2., 2., 2.]]), radii=np.array([1.]),
                box_length=[8., 8., 8.], probe=0.0, spacing=0.5, bin=0.1, bins=100)
    args.update(changes)
    return native.free_volume(**args)


@pytest.mark.parametrize("changes", [
    {"positions": np.zeros((1, 2))},
    {"positions": np.zeros((1, 4))},
    {"positions": np.empty((0, 3)), "radii": np.array([])},
    {"positions": np.array([[0., np.nan, 0.]])},
    {"positions": np.array([[0., np.inf, 0.]])},
    {"radii": np.array([])},
    {"radii": np.array([-1.])},
    {"radii": np.array([np.inf])},
    {"probe": np.inf}, {"probe": np.nan}, {"probe": -1.},
    {"spacing": np.inf}, {"spacing": 0.0},
    {"bin": np.inf}, {"bins": 0}, {"bins": 10_001},
    {"box_length": [0., 8., 8.]},
    {"box_length": [1e200, 1e200, 1e200]},
    {"spacing": 1e-100},  # Reject the grid before attempting an allocation.
    {"probe": 1e10},  # Reject the dilation stencil before enumerating offsets.
])
def test_volume_invalid_inputs_raise_value_error(changes):
    with pytest.raises(ValueError):
        volume(**changes)


def rdf(**changes):
    args = dict(frames=np.array([[[1., 1., 1.], [2., 1., 1.]]]),
                boxes=np.array([[8., 8., 8.]]), a=np.array([0], dtype=np.int64),
                b=np.array([1], dtype=np.int64), r_max=3., bins=6)
    args.update(changes)
    return native.rdf_counts(**args)


@pytest.mark.parametrize("changes", [
    {"frames": np.empty((0, 2, 3)), "boxes": np.empty((0, 3))},
    {"frames": np.empty((1, 0, 3))},
    {"frames": np.zeros((1, 2, 2))},
    {"frames": np.full((1, 2, 3), np.nan)},
    {"frames": np.broadcast_to(np.zeros((1, 1, 3)), (2001, 1, 3))},
    {"frames": np.broadcast_to(np.zeros((1, 1, 3)), (2000, 10001, 3))},
    {"boxes": np.array([[8., 0., 8.]])},
    {"boxes": np.array([[8., np.inf, 8.]])},
    {"a": np.array([], dtype=np.int64)},
    {"a": np.array([-1], dtype=np.int64)},
    {"b": np.array([2], dtype=np.int64)},
    {"a": np.array([0, 0], dtype=np.int64)},
    {"r_max": 1e-100}, {"r_max": np.inf},
    {"bins": 0}, {"bins": 10001},
    {"molecule": np.array([1], dtype=np.int64)},
])
def test_rdf_invalid_inputs_raise_value_error(changes):
    with pytest.raises(ValueError):
        rdf(**changes)


def test_rdf_retains_ordered_pair_counts():
    counts = rdf(a=np.array([0, 1], dtype=np.int64), b=np.array([0, 1], dtype=np.int64))
    assert counts.tolist() == [0., 0., 2., 0., 0., 0.]


def test_msd_rejects_duplicate_selection_and_overflow():
    with pytest.raises(ValueError, match="unique"):
        native.msd(np.zeros((2, 2, 3)), np.array([0, 0], dtype=np.int64))
    with pytest.raises(ValueError, match="overflow"):
        native.msd(np.array([[[1e308, 0., 0.]], [[-1e308, 0., 0.]]]), np.array([0], dtype=np.int64))


def widom(**changes):
    args = dict(frames=np.array([[[1., 1., 1.]]]), boxes=np.array([[10., 10., 10.]]),
                sigma=np.array([3.]), epsilon=np.array([0.1]), probe_sigma=3.4, probe_epsilon=0.2,
                temperature=300., insertions=20, cutoff=4., seed=1, tail=True)
    args.update(changes)
    return native.widom_insertion(**args)


@pytest.mark.parametrize("changes", [
    {"sigma": np.array([])}, {"sigma": np.array([-1.])}, {"sigma": np.array([np.inf])},
    {"epsilon": np.array([-0.1])}, {"epsilon": np.array([np.nan])},
    {"probe_sigma": 0.}, {"probe_sigma": np.inf},
    {"probe_epsilon": -0.1}, {"probe_epsilon": np.inf},
    {"temperature": 0.}, {"temperature": np.inf},
    {"cutoff": 1e-100}, {"cutoff": 6.}, {"cutoff": np.inf},
    {"insertions": 0}, {"insertions": 20_000_001},
])
def test_widom_invalid_inputs_raise_value_error(changes):
    with pytest.raises(ValueError):
        widom(**changes)


def test_zero_interaction_widom_has_unit_boltzmann_factor():
    factors = widom(epsilon=np.array([0.]), insertions=100)
    np.testing.assert_array_equal(factors, [1.])
