import unittest

from po_pr_review import (
    COMMENT_MARKER,
    POT_COMMENT_MARKER,
    TranslationEntry,
    build_comment,
    build_pot_comment_bodies,
    cluster_similar_change_sizes,
    compare_entries,
    compare_pot_entries,
    load_translation_entries,
    parse_hidden_po_files,
    should_hide_report_from_review,
)


class TestPoPrReview(unittest.TestCase):
    def test_compare_entries_detects_changed_translation(self):
        base_po = """
msgid ""
msgstr ""

msgid "Hello"
msgstr "Hallo"
"""
        head_po = """
msgid ""
msgstr ""

msgid "Hello"
msgstr "Servus"
"""
        _, base_entries = load_translation_entries(base_po)
        _, head_entries = load_translation_entries(head_po)

        changes = compare_entries(base_entries, head_entries)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["status"], "changed")

    def test_compare_entries_skips_empty_new_translations(self):
        base_po = """
msgid ""
msgstr ""
"""
        head_po = """
msgid ""
msgstr ""

msgid "New string"
msgstr ""
"""
        _, base_entries = load_translation_entries(base_po)
        _, head_entries = load_translation_entries(head_po)

        changes = compare_entries(base_entries, head_entries)

        self.assertEqual(changes, [])

    def test_cluster_similar_change_sizes_groups_bulk_updates(self):
        changes = [
            {"filename": "de.po", "additions": 100, "deletions": 98},
            {"filename": "fr.po", "additions": 101, "deletions": 99},
            {"filename": "es.po", "additions": 99, "deletions": 100},
            {"filename": "it.po", "additions": 10, "deletions": 2},
        ]

        groups = cluster_similar_change_sizes(changes)

        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["files"]), 3)

    def test_build_comment_includes_marker_and_summary(self):
        po_files = [{"filename": "de.po", "status": "modified", "additions": 2, "deletions": 1}]
        language_reports = [
            {
                "language": "de",
                "path": "locale/de.po",
                "status": "modified",
                "changes": [
                    {
                        "status": "changed",
                        "before": None,
                        "after": TranslationEntry(
                            context="",
                            msgid="Hello",
                            msgid_plural=None,
                            translation=("Hallo",),
                        ),
                    }
                ],
            }
        ]

        comment = build_comment(po_files, language_reports, [], [])

        self.assertTrue(comment.startswith(COMMENT_MARKER))
        self.assertIn("Changed files: `1`", comment)
        self.assertIn("Hello", comment)

    def test_hidden_po_files_empty_by_default(self):
        hidden = parse_hidden_po_files(None)

        self.assertEqual(hidden, set())
        self.assertFalse(
            should_hide_report_from_review({"path": "locale/eo.po"}, hidden)
        )

    def test_hidden_po_files_can_be_configured(self):
        hidden = parse_hidden_po_files("eo.po, test.po ")

        self.assertEqual(hidden, {"eo.po", "test.po"})
        self.assertTrue(
            should_hide_report_from_review({"path": "locale/eo.po"}, hidden)
        )
        self.assertFalse(
            should_hide_report_from_review({"path": "locale/de.po"}, hidden)
        )

    def test_compare_pot_entries_detects_added_and_removed(self):
        base_pot = """
msgid ""
msgstr ""

msgid "Hello"
msgstr ""

msgid "Goodbye"
msgstr ""
"""
        head_pot = """
msgid ""
msgstr ""

msgid "Hello"
msgstr ""

msgid "Welcome"
msgstr ""
"""
        _, base_entries = load_translation_entries(base_pot)
        _, head_entries = load_translation_entries(head_pot)

        changes = compare_pot_entries(base_entries, head_entries)

        self.assertEqual(len(changes), 2)
        added = [change for change in changes if change["status"] == "added"]
        removed = [change for change in changes if change["status"] == "removed"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["after"].msgid, "Welcome")
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["before"].msgid, "Goodbye")

    def test_compare_pot_entries_treats_case_change_as_corrected(self):
        base_pot = """
msgid ""
msgstr ""

msgid "Auto Reserve Stock"
msgstr ""

msgid "Enable Stock Reservation"
msgstr ""
"""
        head_pot = """
msgid ""
msgstr ""

msgid "Auto reserve stock"
msgstr ""

msgid "Enable stock reservation"
msgstr ""
"""
        _, base_entries = load_translation_entries(base_pot)
        _, head_entries = load_translation_entries(head_pot)

        changes = compare_pot_entries(base_entries, head_entries)

        self.assertEqual(len(changes), 2)
        self.assertTrue(all(change["status"] == "corrected" for change in changes))
        self.assertEqual(changes[0]["before"].msgid, "Auto Reserve Stock")
        self.assertEqual(changes[0]["after"].msgid, "Auto reserve stock")

    def test_compare_pot_entries_treats_whitespace_change_as_corrected(self):
        base_pot = """
msgid ""
msgstr ""

msgid "Save   changes"
msgstr ""
"""
        head_pot = """
msgid ""
msgstr ""

msgid "Save changes"
msgstr ""
"""
        _, base_entries = load_translation_entries(base_pot)
        _, head_entries = load_translation_entries(head_pot)

        changes = compare_pot_entries(base_entries, head_entries)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["status"], "corrected")
        self.assertEqual(changes[0]["before"].msgid, "Save   changes")
        self.assertEqual(changes[0]["after"].msgid, "Save changes")

    def test_compare_pot_entries_ignores_reference_only_changes(self):
        base_pot = """
msgid ""
msgstr ""

#: old/path.js:1
msgid "Hello"
msgstr ""
"""
        head_pot = """
msgid ""
msgstr ""

#: new/path.js:99
msgid "Hello"
msgstr ""
"""
        _, base_entries = load_translation_entries(base_pot)
        _, head_entries = load_translation_entries(head_pot)

        changes = compare_pot_entries(base_entries, head_entries)

        self.assertEqual(changes, [])

    def test_build_pot_comment_includes_marker_and_changes(self):
        pot_reports = [
            {
                "path": "locale/main.pot",
                "status": "modified",
                "changes": [
                    {
                        "status": "added",
                        "before": None,
                        "after": TranslationEntry("", "Configure", None, ("",)),
                    },
                    {
                        "status": "removed",
                        "before": TranslationEntry("", "Administration", None, ("",)),
                        "after": None,
                    },
                ],
            }
        ]

        bodies = build_pot_comment_bodies(pot_reports, [])

        self.assertEqual(len(bodies), 1)
        self.assertTrue(bodies[0].startswith(POT_COMMENT_MARKER))
        self.assertIn("Configure", bodies[0])
        self.assertIn("Administration", bodies[0])
        self.assertIn("| Status | Previous | Current |", bodies[0])
        self.assertIn("| added |", bodies[0])
        self.assertIn("| removed |", bodies[0])

    def test_build_pot_comment_includes_corrected_rows(self):
        pot_reports = [
            {
                "path": "locale/main.pot",
                "status": "modified",
                "changes": [
                    {
                        "status": "corrected",
                        "before": TranslationEntry("", "Auto Reserve Stock", None, ("",)),
                        "after": TranslationEntry("", "Auto reserve stock", None, ("",)),
                    },
                ],
            }
        ]

        bodies = build_pot_comment_bodies(pot_reports, [])

        self.assertEqual(len(bodies), 1)
        self.assertIn("1 corrected", bodies[0])
        self.assertIn("| corrected |", bodies[0])
        self.assertIn("Auto Reserve Stock", bodies[0])
        self.assertIn("Auto reserve stock", bodies[0])

    def test_build_pot_comment_empty_when_no_msgid_changes(self):
        pot_reports = [
            {
                "path": "locale/main.pot",
                "status": "modified",
                "changes": [],
            }
        ]

        bodies = build_pot_comment_bodies(pot_reports, [])

        self.assertEqual(bodies, [])


if __name__ == "__main__":
    unittest.main()
