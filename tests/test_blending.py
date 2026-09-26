"""Blending in isolation: two overlapping flat-colour photos with a known layout."""

import numpy as np
import pytest

from shellstitch.engine import Model, Photometric, Reporter, Sampler, composite


class TwoPhotos(Sampler):
    """A Sampler whose photos are flat grey images (100 and 200) instead of files."""

    def __init__(self, model, power):
        super().__init__(model, {0: None, 1: None}, Photometric(model), 0.0, 0.0, 1.0, power)
        self.images = {0: np.full((model.h, model.w, 3), 100, np.uint8),
                       1: np.full((model.h, model.w, 3), 200, np.uint8)}

    def image(self, k):
        return self.images[k]


def blend(power, width=200, height=100, shift=120):
    # photo 1 sits `shift` px to the right of photo 0: an 80 px overlap
    cw = width / 2
    model = Model((width, height), np.zeros(5), {0: (0.0, 0.0, 0.0), 1: (0.0, shift / cw, 0.0)})
    out = np.zeros((height, width + shift, 3), np.uint8)
    s = TwoPhotos(model, power)
    s.x0, s.y0 = -(width - 1) / 2, -(height - 1) / 2  # mosaic origin at photo 0's top-left
    s.boxes = {k: (-1, -1, width + shift + 1, height + 1) for k in (0, 1)}
    composite(s, out, Reporter(), "test")
    return out[height // 2, :, 0].astype(int)  # middle row


@pytest.mark.parametrize("power", [1.0, 3.0, 40.0, 200.0])
def test_every_pixel_is_covered(power):
    row = blend(power)
    assert (row > 0).all(), "blend weights underflowed: pixels left empty"
    assert row[0] == 100 and row[-1] == 200


def test_seam_is_sharp_and_feather_is_gradual():
    def transition_width(row):
        return int(((row > 110) & (row < 190)).sum())
    feather, seam = blend(3.0), blend(40.0)
    assert transition_width(feather) > 20  # ~28 px across this 80 px overlap
    assert transition_width(seam) < 8
    # both change monotonically from one photo to the other, meeting mid-overlap
    for row in (feather, seam):
        assert (np.diff(row) >= 0).all()
        assert abs(int(np.argmin(np.abs(row - 150))) - 160) <= 3
