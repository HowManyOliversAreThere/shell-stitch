import os
import sys

# helpers.py lives next to the tests; GUI tests render off-screen
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
