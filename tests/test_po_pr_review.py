import unittest

from po_pr_review import (
    COMMENT_MARKER,
    POT_COMMENT_MARKER,
    TranslationEntry,
    build_comment,
    build_comment_bodies,
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
        po_files = [
            {"filename": "de.po", "status": "modified", "additions": 2, "deletions": 1}
        ]
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
                        "before": TranslationEntry(
                            "", "Auto Reserve Stock", None, ("",)
                        ),
                        "after": TranslationEntry(
                            "", "Auto reserve stock", None, ("",)
                        ),
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

    def test_build_pot_comment_metadata_only_when_no_msgid_changes(self):
        pot_reports = [
            {
                "path": "locale/main.pot",
                "status": "modified",
                "changes": [],
            }
        ]

        bodies = build_pot_comment_bodies(pot_reports, [])

        self.assertEqual(len(bodies), 1)
        self.assertTrue(bodies[0].startswith(POT_COMMENT_MARKER))
        self.assertIn("Changed files: `1`", bodies[0])
        self.assertIn("Metadata-only template file changes (1 file)", bodies[0])
        self.assertIn("metadata, comment, or source reference updates only", bodies[0])
        self.assertIn("locale/main.pot", bodies[0])

    def _po_change(self, msgid: str, translation: str = "x") -> dict:
        return {
            "status": "changed",
            "before": None,
            "after": TranslationEntry("", msgid, None, (translation,)),
        }

    def _po_report(self, language: str, path: str, msgids: list[str]) -> dict:
        return {
            "language": language,
            "path": path,
            "status": "modified",
            "changes": [self._po_change(msgid) for msgid in msgids],
        }

    def _pot_change(self, status: str, msgid: str) -> dict:
        entry = TranslationEntry("", msgid, None, ("",))
        if status == "added":
            return {"status": "added", "before": None, "after": entry}
        if status == "removed":
            return {"status": "removed", "before": entry, "after": None}
        return {"status": "corrected", "before": entry, "after": entry}

    def test_build_comment_wraps_each_locale_in_details(self):
        po_files = [
            {"filename": "de.po", "status": "modified", "additions": 1, "deletions": 0},
            {"filename": "fr.po", "status": "modified", "additions": 1, "deletions": 0},
        ]
        language_reports = [
            self._po_report("de", "locale/de.po", ["Hello"]),
            self._po_report("fr", "locale/fr.po", ["Bonjour"]),
        ]

        bodies = build_comment_bodies(po_files, language_reports, [], [])

        self.assertEqual(len(bodies), 1)
        body = bodies[0]
        self.assertEqual(body.count("<details>"), 2)
        self.assertEqual(body.count("</details>"), 2)
        self.assertIn("<summary>`de` (`locale/de.po`) — 1 entries</summary>", body)
        self.assertIn("<summary>`fr` (`locale/fr.po`) — 1 entries</summary>", body)
        # No single outer details wrapping both locale headings.
        first_details = body.index("<details>")
        self.assertLess(body.index("### `de`"), body.index("</details>", first_details))

    def test_build_comment_packs_locales_into_separate_comments_when_needed(self):
        po_files = [
            {"filename": "de.po", "status": "modified", "additions": 1, "deletions": 0},
            {"filename": "fr.po", "status": "modified", "additions": 1, "deletions": 0},
        ]
        language_reports = [
            self._po_report("de", "locale/de.po", ["Hello"]),
            self._po_report("fr", "locale/fr.po", ["Bonjour"]),
        ]

        bodies = build_comment_bodies(
            po_files, language_reports, [], [], max_body_chars=650
        )

        self.assertGreaterEqual(len(bodies), 2)
        for body in bodies:
            self.assertIn("<details>", body)
            self.assertIn("</details>", body)

    def test_build_comment_splits_oversized_locale_across_comments(self):
        msgids = [f"String {index:04d}" for index in range(40)]
        po_files = [
            {"filename": "de.po", "status": "modified", "additions": 40, "deletions": 0}
        ]
        language_reports = [self._po_report("de", "locale/de.po", msgids)]

        bodies = build_comment_bodies(
            po_files, language_reports, [], [], max_body_chars=1_200
        )

        self.assertGreaterEqual(len(bodies), 2)
        joined = "\n".join(bodies)
        self.assertNotIn("Too many changes to fit into a comment", joined)
        self.assertIn("part 1 of", joined)
        self.assertIn("part 2 of", joined)
        for msgid in msgids:
            self.assertIn(msgid, joined)

    def test_build_pot_comment_wraps_each_file_in_details(self):
        pot_reports = [
            {
                "path": "locale/a.pot",
                "status": "modified",
                "changes": [self._pot_change("added", "Alpha")],
            },
            {
                "path": "locale/b.pot",
                "status": "modified",
                "changes": [self._pot_change("removed", "Beta")],
            },
        ]

        bodies = build_pot_comment_bodies(pot_reports, [])

        self.assertEqual(len(bodies), 1)
        body = bodies[0]
        self.assertEqual(body.count("<details>"), 2)
        self.assertIn(
            "<summary>`locale/a.pot` — 1 added, 0 removed, 0 corrected</summary>", body
        )
        self.assertIn(
            "<summary>`locale/b.pot` — 0 added, 1 removed, 0 corrected</summary>", body
        )

    def test_build_pot_comment_splits_oversized_file_across_comments(self):
        changes = [self._pot_change("added", f"Msg {index:04d}") for index in range(40)]
        pot_reports = [
            {"path": "locale/main.pot", "status": "modified", "changes": changes}
        ]

        bodies = build_pot_comment_bodies(pot_reports, [], max_body_chars=900)

        self.assertGreaterEqual(len(bodies), 2)
        joined = "\n".join(bodies)
        self.assertNotIn("Too many changes to fit into a comment", joined)
        self.assertIn("part 1 of", joined)
        self.assertIn("part 2 of", joined)
        for change in changes:
            self.assertIn(change["after"].msgid, joined)


if __name__ == "__main__":
    unittest.main()
