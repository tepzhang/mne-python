from os import path as op
from pathlib import Path

import numpy as np
import pytest
from numpy.polynomial import legendre
from numpy.testing import (
    assert_allclose,
    assert_array_almost_equal,
    assert_array_equal,
    assert_equal,
)
from scipy.interpolate import interp1d

import mne
from mne import Epochs, make_fixed_length_events, pick_types, read_evokeds
from mne.datasets import testing
from mne.forward import _make_surface_mapping, make_field_map
from mne.forward._field_interpolation import _setup_dots
from mne.forward._lead_dots import (
    _comp_sum_eeg,
    _comp_sums_meg,
    _do_cross_dots,
    _get_legen_table,
)
from mne.forward._make_forward import _create_meg_coils
from mne.io import read_raw_fif
from mne.surface import get_head_surf, get_meg_helmet_surf

base_dir = op.join(op.dirname(__file__), "..", "..", "io", "tests", "data")
raw_fname = op.join(base_dir, "test_raw.fif")
evoked_fname = op.join(base_dir, "test-ave.fif")
raw_ctf_fname = op.join(base_dir, "test_ctf_raw.fif")

data_path = testing.data_path(download=False)
trans_fname = op.join(data_path, "MEG", "sample", "sample_audvis_trunc-trans.fif")
subjects_dir = op.join(data_path, "subjects")


@testing.requires_testing_data
def test_field_map_ctf():
    """Test that field mapping can be done with CTF data."""
    raw = read_raw_fif(raw_ctf_fname).crop(0, 1)
    raw.apply_gradient_compensation(3)
    events = make_fixed_length_events(raw, duration=0.5)
    evoked = Epochs(raw, events).average()
    evoked.pick(evoked.ch_names[:50])  # crappy mapping but faster
    # smoke test - passing trans_fname as pathlib.Path as additional check
    make_field_map(
        evoked, trans=Path(trans_fname), subject="sample", subjects_dir=subjects_dir
    )


def test_legendre_val():
    """Test Legendre polynomial (derivative) equivalence."""
    rng = np.random.RandomState(0)
    # check table equiv
    xs = np.linspace(-1.0, 1.0, 1000)
    n_terms = 100

    # True, numpy
    vals_np = legendre.legvander(xs, n_terms - 1)

    # Table approximation
    for nc, interp in zip([100, 50], ["nearest", "linear"]):
        lut, n_fact = _get_legen_table("eeg", n_coeff=nc, force_calc=True)
        lut_fun = interp1d(np.linspace(-1, 1, lut.shape[0]), lut, interp, axis=0)
        vals_i = lut_fun(xs)
        # Need a "1:" here because we omit the first coefficient in our table!
        assert_allclose(
            vals_np[:, 1 : vals_i.shape[1] + 1], vals_i, rtol=1e-2, atol=5e-3
        )

        # Now let's look at our sums
        ctheta = rng.rand(20, 30) * 2.0 - 1.0
        beta = rng.rand(20, 30) * 0.8
        c1 = _comp_sum_eeg(beta.flatten(), ctheta.flatten(), lut_fun, n_fact)
        c1.shape = beta.shape

        # compare to numpy
        n = np.arange(1, n_terms, dtype=float)[:, np.newaxis, np.newaxis]
        coeffs = np.zeros((n_terms,) + beta.shape)
        coeffs[1:] = (
            np.cumprod([beta] * (n_terms - 1), axis=0)
            * (2.0 * n + 1.0)
            * (2.0 * n + 1.0)
            / n
        )
        # can't use tensor=False here b/c it isn't in old numpy
        c2 = np.empty((20, 30))
        for ci1 in range(20):
            for ci2 in range(30):
                c2[ci1, ci2] = legendre.legval(ctheta[ci1, ci2], coeffs[:, ci1, ci2])
        assert_allclose(c1, c2, 1e-2, 1e-3)  # close enough...

    # compare fast and slow for MEG
    ctheta = rng.rand(20 * 30) * 2.0 - 1.0
    beta = rng.rand(20 * 30) * 0.8
    lut, n_fact = _get_legen_table("meg", n_coeff=10, force_calc=True)
    fun = interp1d(np.linspace(-1, 1, lut.shape[0]), lut, "nearest", axis=0)
    coeffs = _comp_sums_meg(beta, ctheta, fun, n_fact, False)
    lut, n_fact = _get_legen_table("meg", n_coeff=20, force_calc=True)
    fun = interp1d(np.linspace(-1, 1, lut.shape[0]), lut, "linear", axis=0)
    coeffs = _comp_sums_meg(beta, ctheta, fun, n_fact, False)


def test_legendre_table():
    """Test Legendre table calculation."""
    # double-check our table generation
    n = 10
    for ch_type in ["eeg", "meg"]:
        lut1, n_fact1 = _get_legen_table(ch_type, n_coeff=25, force_calc=True)
        lut1 = lut1[:, : n - 1].copy()
        n_fact1 = n_fact1[: n - 1].copy()
        lut2, n_fact2 = _get_legen_table(ch_type, n_coeff=n, force_calc=True)
        assert_allclose(lut1, lut2)
        assert_allclose(n_fact1, n_fact2)


@testing.requires_testing_data
def test_make_field_map_eeg():
    """Test interpolation of EEG field onto head."""
    evoked = read_evokeds(evoked_fname, condition="Left Auditory")
    evoked.info["bads"] = ["MEG 2443", "EEG 053"]  # add some bads
    surf = get_head_surf("sample", subjects_dir=subjects_dir)
    # we must have trans if surface is in MRI coords
    pytest.raises(ValueError, _make_surface_mapping, evoked.info, surf, "eeg")

    evoked.pick(picks="eeg")
    fmd = make_field_map(
        evoked, trans_fname, subject="sample", subjects_dir=subjects_dir
    )

    # trans is necessary for EEG only
    pytest.raises(
        RuntimeError,
        make_field_map,
        evoked,
        None,
        subject="sample",
        subjects_dir=subjects_dir,
    )

    fmd = make_field_map(
        evoked, trans_fname, subject="sample", subjects_dir=subjects_dir
    )
    assert len(fmd) == 1
    assert_array_equal(fmd[0]["data"].shape, (642, 59))  # maps data onto surf
    assert len(fmd[0]["ch_names"]) == 59


@testing.requires_testing_data
@pytest.mark.slowtest
def test_make_field_map_meg():
    """Test interpolation of MEG field onto helmet | head."""
    evoked = read_evokeds(evoked_fname, condition="Left Auditory")
    info = evoked.info
    surf = get_meg_helmet_surf(info)
    # let's reduce the number of channels by a bunch to speed it up
    info["bads"] = info["ch_names"][:200]
    # bad ch_type
    pytest.raises(ValueError, _make_surface_mapping, info, surf, "foo")
    # bad mode
    pytest.raises(ValueError, _make_surface_mapping, info, surf, "meg", mode="foo")
    # no picks
    evoked_eeg = evoked.copy().pick(picks="eeg")
    pytest.raises(RuntimeError, _make_surface_mapping, evoked_eeg.info, surf, "meg")
    # bad surface def
    nn = surf["nn"]
    del surf["nn"]
    pytest.raises(KeyError, _make_surface_mapping, info, surf, "meg")
    surf["nn"] = nn
    cf = surf["coord_frame"]
    del surf["coord_frame"]
    pytest.raises(KeyError, _make_surface_mapping, info, surf, "meg")
    surf["coord_frame"] = cf

    # now do it with make_field_map
    evoked.pick(picks="meg")
    evoked.info.normalize_proj()  # avoid projection warnings
    fmd = make_field_map(evoked, None, subject="sample", subjects_dir=subjects_dir)
    assert len(fmd) == 1
    assert_array_equal(fmd[0]["data"].shape, (304, 106))  # maps data onto surf
    assert len(fmd[0]["ch_names"]) == 106

    pytest.raises(ValueError, make_field_map, evoked, ch_type="foobar")

    # now test the make_field_map on head surf for MEG
    evoked.pick(picks="meg")
    evoked.info.normalize_proj()
    fmd = make_field_map(
        evoked,
        trans_fname,
        meg_surf="head",
        subject="sample",
        subjects_dir=subjects_dir,
    )
    assert len(fmd) == 1
    assert_array_equal(fmd[0]["data"].shape, (642, 106))  # maps data onto surf
    assert len(fmd[0]["ch_names"]) == 106

    pytest.raises(
        ValueError,
        make_field_map,
        evoked,
        meg_surf="foobar",
        subjects_dir=subjects_dir,
        trans=trans_fname,
    )


@testing.requires_testing_data
def test_make_field_map_meeg():
    """Test making a M/EEG field map onto helmet & head."""
    evoked = read_evokeds(evoked_fname, baseline=(-0.2, 0.0))[0]
    picks = pick_types(evoked.info, meg=True, eeg=True)
    picks = picks[::10]
    evoked.pick([evoked.ch_names[p] for p in picks])
    evoked.info.normalize_proj()
    maps = make_field_map(
        evoked,
        trans_fname,
        subject="sample",
        subjects_dir=subjects_dir,
        verbose="debug",
    )
    assert_equal(maps[0]["data"].shape, (642, 6))  # EEG->Head
    assert_equal(maps[1]["data"].shape, (304, 31))  # MEG->Helmet
    # reasonable ranges
    maxs = (1.2, 2.0)  # before #4418, was (1.1, 2.0)
    mins = (-0.8, -1.3)  # before #4418, was (-0.6, -1.2)
    assert_equal(len(maxs), len(maps))
    for map_, max_, min_ in zip(maps, maxs, mins):
        assert_allclose(map_["data"].max(), max_, rtol=5e-2)
        assert_allclose(map_["data"].min(), min_, rtol=5e-2)
    # calculated from correct looking mapping on 2015/12/26
    assert_allclose(
        np.sqrt(np.sum(maps[0]["data"] ** 2)), 19.0903, atol=1e-3, rtol=1e-3  # 16.6088,
    )
    assert_allclose(
        np.sqrt(np.sum(maps[1]["data"] ** 2)), 19.4748, atol=1e-3, rtol=1e-3  # 20.1245,
    )


def _setup_args(info):
    """Configure args for test_as_meg_type_evoked."""
    coils = _create_meg_coils(info["chs"], "normal", info["dev_head_t"])
    int_rad, _, lut_fun, n_fact = _setup_dots("fast", info, coils, "meg")
    my_origin = np.array([0.0, 0.0, 0.04])
    args_dict = dict(
        intrad=int_rad,
        volume=False,
        coils1=coils,
        r0=my_origin,
        ch_type="meg",
        lut=lut_fun,
        n_fact=n_fact,
    )
    return args_dict


@testing.requires_testing_data
def test_as_meg_type_evoked():
    """Test interpolation of data on to virtual channels."""
    # validation tests
    raw = read_raw_fif(raw_fname)
    events = mne.find_events(raw)
    picks = pick_types(
        raw.info,
        meg=True,
        eeg=True,
        stim=True,
        ecg=True,
        eog=True,
        include=["STI 014"],
        exclude="bads",
    )
    epochs = mne.Epochs(raw, events, picks=picks)
    evoked = epochs.average()

    with pytest.raises(ValueError, match="Invalid value for the 'ch_type'"):
        evoked.as_type("meg")
    with pytest.raises(ValueError, match="Invalid value for the 'ch_type'"):
        evoked.copy().pick(picks="grad").as_type("meg")

    # channel names
    ch_names = evoked.info["ch_names"]
    virt_evoked = evoked.copy().pick(ch_names[:10:1])
    virt_evoked.info.normalize_proj()
    virt_evoked = virt_evoked.as_type("mag")
    assert all(ch.endswith("_v") for ch in virt_evoked.info["ch_names"])

    # pick from and to channels
    evoked_from = evoked.copy().pick(ch_names[2:10:3])
    evoked_to = evoked.copy().pick(ch_names[0:10:3])

    info_from, info_to = evoked_from.info, evoked_to.info

    # set up things
    args1, args2 = _setup_args(info_from), _setup_args(info_to)
    args1.update(coils2=args2["coils1"])
    args2.update(coils2=args1["coils1"])

    # test cross dots
    cross_dots1 = _do_cross_dots(**args1)
    cross_dots2 = _do_cross_dots(**args2)

    assert_array_almost_equal(cross_dots1, cross_dots2.T)

    # correlation test
    evoked = evoked.pick(ch_names[:10:]).copy()
    data1 = evoked.pick("grad").data.ravel()
    data2 = evoked.as_type("grad").data.ravel()
    assert np.corrcoef(data1, data2)[0, 1] > 0.95

    # Do it with epochs
    virt_epochs = epochs.copy().load_data().pick(ch_names[:10:1])
    virt_epochs.info.normalize_proj()
    virt_epochs = virt_epochs.as_type("mag")
    assert all(ch.endswith("_v") for ch in virt_epochs.info["ch_names"])
    assert_allclose(virt_epochs.get_data(copy=False).mean(0), virt_evoked.data)
