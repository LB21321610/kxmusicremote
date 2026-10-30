"""Run the audit's repaired-behavior regressions in isolation."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_audit_fixes

if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(test_audit_fixes))
    sys.exit(not result.wasSuccessful())
