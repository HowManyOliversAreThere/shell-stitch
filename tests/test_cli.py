"""The command line exposes every setting and passes it through unchanged."""

from dataclasses import fields

import pytest

from helpers import NONDEFAULT
from shellstitch import cli
from shellstitch.options import Options, default_of


def args_for(name, value):
    f = next(f for f in fields(Options) if f.name == name)
    flag = f.metadata["cli"].split("/")[-1]
    default = default_of(f)
    if isinstance(default, bool):
        return [flag] if value != default else []
    if isinstance(default, list):
        return [a for v in value for a in (flag, v)]
    return [flag, str(value)]


def parse(argv):
    captured = {}
    orig = cli.stitch_folder
    cli.stitch_folder = lambda folder, opts, rep: captured.setdefault("opts", opts)
    try:
        cli.main(argv)
    finally:
        cli.stitch_folder = orig
    return captured["opts"]


def test_defaults():
    assert parse(["somefolder"]) == Options()


@pytest.mark.parametrize("name", sorted(NONDEFAULT))
def test_each_setting(name):
    value = NONDEFAULT[name]
    opts = parse(["somefolder", *args_for(name, value)])
    if name == "exclude":  # --exclude adds to the default patterns
        value = Options().exclude + value
    assert getattr(opts, name) == value
    others = {k: v for k, v in opts.to_dict().items() if k != name}
    assert others == {k: v for k, v in Options().to_dict().items() if k != name}


def test_help_lists_every_setting(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    text = capsys.readouterr().out
    for f in fields(Options):
        for flag in f.metadata["cli"].split("/"):
            assert flag in text
