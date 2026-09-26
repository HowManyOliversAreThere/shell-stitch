"""GUI checks that run off-screen: the settings form and the report view."""

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from helpers import NONDEFAULT  # noqa: E402
from shellstitch.options import Options  # noqa: E402


@pytest.fixture(scope="module")
def app():
    QCoreApplication.setOrganizationName("ShellStitchTests")
    return QApplication.instance() or QApplication([])


def test_settings_form_round_trips_every_setting(app):
    from shellstitch.gui.settings_form import SettingsForm
    form = SettingsForm()
    wanted = Options(**NONDEFAULT)
    form.set_options(wanted)
    assert form.options() == wanted
    form.set_options(Options())
    assert form.options() == Options()


def test_groups_show_only_when_they_have_visible_settings(app):
    from PySide6.QtWidgets import QGroupBox, QWidget
    from shellstitch.gui.settings_form import SettingsForm
    form = SettingsForm()
    form.show()
    hidden_when_basic = set()
    for advanced in (False, True):
        form.show_advanced.setChecked(advanced)
        for box in form.findChildren(QGroupBox):
            has_visible = any(w.isVisibleTo(box) for w in box.findChildren(QWidget))
            assert box.isVisible() == has_visible, box.title()
            if not advanced and not box.isVisible():
                hidden_when_basic.add(box.title())
        if advanced:
            assert all(b.isVisible() for b in form.findChildren(QGroupBox))
    assert "Performance" in hidden_when_basic


def test_theme_switch_restyles(app):
    from shellstitch.gui import theme
    theme.set_mode(app, "dark", save=False)
    assert theme.is_dark() and app.palette().window().color().name() == theme.DARK["window"]
    theme.set_mode(app, "light", save=False)
    assert not theme.is_dark() and app.palette().window().color().name() == theme.LIGHT["window"]
    theme.set_mode(app, "system", save=False)
