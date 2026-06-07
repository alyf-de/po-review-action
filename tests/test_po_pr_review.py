import unittest

from po_pr_review import (
    COMMENT_MARKER,
    TranslationEntry,
    build_comment,
    cluster_similar_change_sizes,
    compare_entries,
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


if __name__ == "__main__":
    unittest.main()
