import tempfile
import time
import unittest
from pathlib import Path

from strategies.selectors.impact.diffparse import parse_patch
from strategies.selectors.impact.impact import Analyzer
from strategies.selectors.impact.selector import Plan, _filter_import_masks, build_overlay, run_all


class FakeIndex:
    def __init__(self, importers):
        self.mask = importers

    def importers(self, path):
        return self.mask

    def file_mask(self, path):
        return 0


class RenamePatchTests(unittest.TestCase):
    fixtures = Path(__file__).parent / "fixtures"
    old_source = "def parse(text):\n    return text.strip()\n\ndef helper():\n    return 1\n"

    def test_git_generated_pure_rename_is_import_checked(self):
        patch_text = (self.fixtures / "pure-rename.patch").read_text()
        patches = parse_patch(patch_text)
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0].old_path, "sqlglot/parser.py")
        self.assertEqual(patches[0].new_path, "sqlglot/parser_moved.py")
        self.assertEqual(patches[0].hunks, [])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sqlglot").mkdir()
            (root / "sqlglot/__init__.py").write_text("")
            (root / "sqlglot/parser.py").write_text(self.old_source)
            importers = (1 << 1) | (1 << 3)
            impact = Analyzer(FakeIndex(importers), directory).analyze(patches)
            self.assertEqual(impact.import_checks, ["sqlglot.parser"])
            self.assertEqual(impact.import_fail_masks, {"sqlglot.parser": importers})
            self.assertEqual(impact.candidates, importers)

            with tempfile.TemporaryDirectory() as work:
                plan = Plan("rename")
                plan.pending_imports = _filter_import_masks(impact.import_checks, impact.import_fail_masks, {1, 3})
                plan.root = build_overlay(directory, impact.new_files, work)
                run_all([plan], {}, {}, time.time() + 5)
                self.assertEqual(sorted(plan.include | plan.failed_imports), [1, 3])

    def test_edited_git_rename_does_not_duplicate_file_patch(self):
        patch_text = (self.fixtures / "edited-rename.patch").read_text()
        patches = parse_patch(patch_text)
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0].old_path, "sqlglot/parser.py")
        self.assertEqual(patches[0].new_path, "sqlglot/parser_moved.py")
        self.assertEqual(len(patches[0].hunks), 1)
        self.assertEqual(patches[0].hunks[0].lines[1:3], ["-    return text.strip()", "+    return text.rstrip()"])

    def test_import_masks_are_filtered_to_catalog_numbers(self):
        masks = {"sqlglot.parser": (1 << 1) | (1 << 3) | (1 << 9)}
        self.assertEqual(
            _filter_import_masks(["sqlglot.parser"], masks, {1, 3}),
            {"sqlglot.parser": (1 << 1) | (1 << 3)},
        )


if __name__ == "__main__":
    unittest.main()
