import unittest
from pathlib import Path


def main():
    suite = unittest.TestSuite()
    # Action directories are not Python packages, so each needs its own import root.
    for directory in sorted(Path(__file__).resolve().parents[2].iterdir()):
        if directory.is_dir() and any(directory.glob("test*.py")):
            suite.addTests(
                unittest.TestLoader().discover(
                    str(directory), top_level_dir=str(directory)
                )
            )
    if not suite.countTestCases():
        raise RuntimeError("No Python tests discovered")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
