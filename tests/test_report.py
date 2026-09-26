"""The human-readable report built from a real stitch of a synthetic section."""

import pytest

from helpers import stitch
from shellstitch import report as rpt
from shellstitch.synthetic import make_section


@pytest.fixture(scope="module")
def stitched(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("report")
    truth = make_section(str(tmp / "SEC-1"), gains=True, copy=True, odd_zoom=True)
    result, _, _, _ = stitch(truth, tmp / "out", preview_only=True)
    return rpt.load(result["outputs"]["report"]), truth


def test_summary(stitched):
    r, truth = stitched
    label, _, level = rpt.verdict(r)
    assert label == "Excellent alignment" and level == "good"
    facts = dict(rpt.facts(r))
    assert facts["Photos placed"].startswith(f"{len(truth.photos)} of {len(truth.photos)}")
    assert "1 more left out" in facts["Photos placed"]
    assert "µm per pixel" in facts["Resolution"]


def test_warnings(stitched):
    r, truth = stitched
    titles = [t for t, _ in rpt.warnings(r)]
    assert "Photos with different camera settings" in titles
    assert "No full-resolution image" in titles  # preview-only run
    detail = dict(rpt.warnings(r))["Photos with different camera settings"]
    assert truth.excluded[0] in detail


def test_html_and_export(stitched, tmp_path):
    r, _ = stitched
    html = rpt.to_html(r)
    assert "Excellent alignment" in html and r["section"] in html
    out = tmp_path / "report.html"
    rpt.export_html(r, str(out))
    page = out.read_text(encoding="utf-8")
    assert "<table" in page and all(i["file"] in page for i in r["images"])


def test_find_reports_sorting(stitched, tmp_path):
    r, _ = stitched
    import os
    found = rpt.find_reports(os.path.dirname(r["_path"]), "name")
    assert [x["section"] for x in found] == ["SEC-1"]
    assert rpt.find_reports(str(tmp_path / "missing")) == []
