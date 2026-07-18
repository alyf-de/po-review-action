"""Generate a review-friendly summary for large translation PRs.

This helper runs in GitHub Actions for pull requests that change `.po` files.
It compares the trusted base checkout against the PR head translation files,
groups similarly sized file diffs, and posts one or more markdown comments with
the high-signal translation changes that are hard to inspect in GitHub's UI
(split when content would exceed GitHub's comment size limit).
"""

import argparse
import html
import io
import json
import os
import time
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError

from babel.messages.pofile import read_po

COMMENT_MARKER = "<!-- po-translation-review -->"
POT_COMMENT_MARKER = "<!-- pot-template-review -->"
MAX_COMMENT_BODY_CHARS = 60_000  # GitHub caps issue comments at 65536 characters
SIMILARITY_TOLERANCE = 0.02


@dataclass(frozen=True)
class TranslationEntry:
    """Normalized representation of a gettext entry used for diffing."""

    context: str
    msgid: str
    msgid_plural: str | None
    translation: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.context, self.msgid, self.msgid_plural or "")


def parse_hidden_po_files(value: str | None) -> set[str]:
    if not value:
        return set()

    return {name.strip() for name in value.split(",") if name.strip()}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build PR review comment(s) for .po file changes; write JSON with a `comments` array."
    )
    pr_from_env = os.environ.get("PR_NUMBER")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument(
        "--pr", type=int, default=int(pr_from_env) if pr_from_env else None
    )
    parser.add_argument("--head-sha", default=os.environ.get("PR_HEAD_SHA"))
    parser.add_argument("--hidden-po-files", default=os.environ.get("HIDDEN_PO_FILES"))
    parser.add_argument("--output", default="po-pr-review-comments.json")
    return parser.parse_args()


def request_url(url: str, *, accept: str, allow_missing: bool = False) -> bytes | None:
    """Fetch bytes from GitHub with auth, retries, and optional 404 handling."""

    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "po-review-action",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    retries = 0
    while True:
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 404 and allow_missing:
                return None

            if exc.code in {403, 429, 500, 502, 503, 504} and retries < 5:
                retries += 1
                time.sleep(retries)
                continue

            raise


def request_json(url: str) -> Any:
    response = request_url(url, accept="application/vnd.github+json")
    if response is None:
        return None
    return json.loads(response.decode("utf-8"))


def fetch_pr_files(repo: str, pr_number: int) -> list[dict[str, Any]]:
    """Return the full changed-file list for a PR, following GitHub pagination."""

    files: list[dict[str, Any]] = []
    page = 1

    while True:
        url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}"
        page_files = request_json(url) or []
        if not page_files:
            break

        files.extend(page_files)
        if len(page_files) < 100:
            break

        page += 1

    return files


def read_local_file(path: str | None) -> str | None:
    """Read a file from the trusted base checkout while preventing path traversal."""

    if not path:
        return None

    repo_root = Path.cwd().resolve()
    file_path = (repo_root / path).resolve()
    try:
        file_path.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"Unexpected repository path: {path}") from exc

    if not file_path.exists():
        return None

    return file_path.read_text(encoding="utf-8")


def fetch_file_content(repo: str, path: str | None, ref: str | None) -> str | None:
    """Fetch the raw content for a repository file at a specific git ref."""

    if not path or not ref:
        return None

    quoted_path = urllib.parse.quote(path, safe="/")
    quoted_ref = urllib.parse.quote(ref, safe="")
    url = f"https://api.github.com/repos/{repo}/contents/{quoted_path}?ref={quoted_ref}"
    response = request_url(url, accept="application/vnd.github.raw", allow_missing=True)
    if response is None:
        return None
    return response.decode("utf-8")


def is_translation_file(change: dict[str, Any], suffix: str) -> bool:
    current_path = change.get("filename", "")
    previous_path = change.get("previous_filename", "")
    return current_path.endswith(suffix) or previous_path.endswith(suffix)


def _path_with_suffix(path: str | None, suffix: str) -> str | None:
    return path if (path or "").endswith(suffix) else None


def base_path_for_file(change: dict[str, Any]) -> str | None:
    if change.get("status") == "renamed":
        return change.get("previous_filename") or change.get("filename")
    return change.get("filename")


def head_path_for_file(change: dict[str, Any]) -> str | None:
    if change.get("status") == "removed":
        return None
    return change.get("filename")


def normalize_translation(value: Any) -> tuple[str, ...]:
    if value is None:
        return ("",)
    if isinstance(value, (tuple, list)):
        return tuple("" if part is None else str(part) for part in value)
    return (str(value),)


def is_translation_empty(translation: tuple[str, ...]) -> bool:
    """Return whether every translated value in the entry is empty or whitespace."""

    return not any(part.strip() for part in translation)


def normalize_message(message: Any) -> TranslationEntry:
    if isinstance(message.id, tuple):
        msgid, msgid_plural = message.id
    else:
        msgid, msgid_plural = message.id, None

    return TranslationEntry(
        context=message.context or "",
        msgid=str(msgid),
        msgid_plural=None if msgid_plural is None else str(msgid_plural),
        translation=normalize_translation(message.string),
    )


def load_translation_entries(
    content: str | None,
) -> tuple[str | None, dict[tuple[str, str, str], TranslationEntry]]:
    """Parse `.po` content into normalized entries keyed for translation diffing."""

    if not content:
        return None, {}

    catalog = read_po(io.StringIO(content))
    language = str(catalog.locale) if catalog.locale else None
    entries: dict[tuple[str, str, str], TranslationEntry] = {}

    for message in catalog:
        if not message.id:
            continue

        entry = normalize_message(message)
        entries[entry.key] = entry

    return language, entries


def compare_entries(
    base_entries: dict[tuple[str, str, str], TranslationEntry],
    head_entries: dict[tuple[str, str, str], TranslationEntry],
) -> list[dict[str, TranslationEntry | str | None]]:
    """Return only the translations that are new or changed in the PR head."""

    changes: list[dict[str, TranslationEntry | str | None]] = []

    for key in sorted(head_entries, key=_entry_sort_key):
        head_entry = head_entries[key]
        base_entry = base_entries.get(key)

        if base_entry is None:
            if is_translation_empty(head_entry.translation):
                continue
            changes.append({"status": "added", "before": None, "after": head_entry})
            continue

        if base_entry.translation != head_entry.translation:
            changes.append(
                {"status": "changed", "before": base_entry, "after": head_entry}
            )

    return changes


def _entry_sort_key(key: tuple[str, str, str]) -> tuple[str, str, str]:
    return (key[0].lower(), key[1].lower(), key[2].lower())


def normalize_pot_msgid(value: str) -> str:
    """Normalize a template msgid for fuzzy comparison (case and whitespace)."""

    return " ".join(value.split()).casefold()


def pot_normalized_key(entry: TranslationEntry) -> tuple[str, str, str]:
    return (
        entry.context,
        normalize_pot_msgid(entry.msgid),
        normalize_pot_msgid(entry.msgid_plural or ""),
    )


def compare_pot_entries(
    base_entries: dict[tuple[str, str, str], TranslationEntry],
    head_entries: dict[tuple[str, str, str], TranslationEntry],
) -> list[dict[str, Any]]:
    """Return msgid changes between base and head template catalogs."""

    base_only_keys = sorted(
        (key for key in base_entries if key not in head_entries),
        key=_entry_sort_key,
    )
    head_only_keys = sorted(
        (key for key in head_entries if key not in base_entries),
        key=_entry_sort_key,
    )

    by_norm_base: dict[tuple[str, str, str], list[TranslationEntry]] = {}
    by_norm_head: dict[tuple[str, str, str], list[TranslationEntry]] = {}

    for key in base_only_keys:
        entry = base_entries[key]
        by_norm_base.setdefault(pot_normalized_key(entry), []).append(entry)

    for key in head_only_keys:
        entry = head_entries[key]
        by_norm_head.setdefault(pot_normalized_key(entry), []).append(entry)

    changes: list[dict[str, Any]] = []
    all_norm_keys = sorted(
        set(by_norm_base) | set(by_norm_head),
        key=_entry_sort_key,
    )

    for norm_key in all_norm_keys:
        base_list = sorted(
            by_norm_base.get(norm_key, []),
            key=lambda entry: (entry.msgid.lower(), (entry.msgid_plural or "").lower()),
        )
        head_list = sorted(
            by_norm_head.get(norm_key, []),
            key=lambda entry: (entry.msgid.lower(), (entry.msgid_plural or "").lower()),
        )

        pairs = min(len(base_list), len(head_list))
        changes.extend(
            {
                "status": "corrected",
                "before": base_list[index],
                "after": head_list[index],
            }
            for index in range(pairs)
        )
        changes.extend(
            {"status": "removed", "before": entry, "after": None}
            for entry in base_list[pairs:]
        )
        changes.extend(
            {"status": "added", "before": None, "after": entry}
            for entry in head_list[pairs:]
        )
    return changes


def within_tolerance(
    value: int, reference: float, tolerance: float = SIMILARITY_TOLERANCE
) -> bool:
    if reference == 0:
        return value == 0

    allowed_delta = max(1, round(reference * tolerance))
    return abs(value - reference) <= allowed_delta


def cluster_similar_change_sizes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group files whose added and removed line counts are within the tolerance."""

    clusters: list[dict[str, Any]] = []

    sorted_changes = sorted(
        changes,
        key=lambda item: (
            -item.get("additions", 0),
            -item.get("deletions", 0),
            item.get("filename", ""),
        ),
    )

    for change in sorted_changes:
        additions = change.get("additions", 0)
        deletions = change.get("deletions", 0)

        for cluster in clusters:
            if within_tolerance(
                additions, cluster["avg_additions"]
            ) and within_tolerance(deletions, cluster["avg_deletions"]):
                cluster["files"].append(change)
                cluster["avg_additions"] = sum(
                    file["additions"] for file in cluster["files"]
                ) / len(cluster["files"])
                cluster["avg_deletions"] = sum(
                    file["deletions"] for file in cluster["files"]
                ) / len(cluster["files"])
                break
        else:
            clusters.append(
                {
                    "files": [change],
                    "avg_additions": float(additions),
                    "avg_deletions": float(deletions),
                }
            )

    return sorted(
        [cluster for cluster in clusters if len(cluster["files"]) > 1],
        key=lambda cluster: (
            -len(cluster["files"]),
            -cluster["avg_additions"],
            -cluster["avg_deletions"],
        ),
    )


def format_translation(translation: tuple[str, ...]) -> str:
    if len(translation) == 1:
        return translation[0]

    return "\n".join(
        f"[{index}] {value or '(empty)'}" for index, value in enumerate(translation)
    )


def escape_table_cell(value: str) -> str:
    if not value:
        return "<em>empty</em>"

    return html.escape(value).replace("|", "&#124;").replace("\n", "<br>")


def render_msgid(entry: TranslationEntry) -> str:
    parts = [entry.msgid]
    if entry.msgid_plural:
        parts.append(f"[plural] {entry.msgid_plural}")
    return "\n".join(parts)


def should_hide_report_from_review(
    report: dict[str, Any], hidden_po_files: set[str]
) -> bool:
    """Return whether a file should be omitted from reviewer-facing language details."""

    return Path(str(report["path"])).name in hidden_po_files


def build_language_section(
    report: dict[str, Any], changes: list[dict[str, Any]] | None = None
) -> list[str]:
    """Render one language's added or changed translations as a markdown table."""

    rows = report["changes"] if changes is None else changes
    lines = [
        f"### `{report['language']}` (`{report['path']}`)",
        "",
        "| Status | Msgid | Previous | Current |",
        "| --- | --- | --- | --- |",
    ]

    for change in rows:
        before = change["before"]
        after = change["after"]
        previous = format_translation(before.translation) if before else ""

        lines.append(
            "| "
            + " | ".join(
                [
                    str(change["status"]),
                    escape_table_cell(render_msgid(after)),
                    escape_table_cell(previous),
                    escape_table_cell(format_translation(after.translation)),
                ]
            )
            + " |"
        )

    lines.append("")
    return lines


def _review_context(
    po_files: list[dict[str, Any]],
    language_reports: list[dict[str, Any]],
    similar_groups: list[dict[str, Any]],
    parse_errors: list[dict[str, str]],
    hidden_po_files: set[str],
) -> dict[str, Any]:
    """Shared stats and report slices used to build one or more PR comments."""

    status_counts = Counter(change.get("status", "modified") for change in po_files)
    reviewable_language_reports = [
        report
        for report in language_reports
        if not should_hide_report_from_review(report, hidden_po_files)
    ]
    translation_change_count = sum(
        len(report["changes"]) for report in reviewable_language_reports
    )
    changed_languages_count = sum(
        1 for report in reviewable_language_reports if report["changes"]
    )
    removed_reports = [
        report
        for report in reviewable_language_reports
        if report["status"] == "removed"
    ]
    metadata_only_reports = [
        report
        for report in reviewable_language_reports
        if not report["changes"] and report["status"] != "removed"
    ]

    return {
        "status_counts": status_counts,
        "total_files": len(po_files),
        "reviewable_language_reports": reviewable_language_reports,
        "grouped_files_count": sum(len(group["files"]) for group in similar_groups),
        "translation_change_count": translation_change_count,
        "changed_languages_count": changed_languages_count,
        "removed_reports": removed_reports,
        "metadata_only_reports": metadata_only_reports,
        "similar_groups": similar_groups,
        "parse_errors": parse_errors,
    }


def _build_prefix_lines(ctx: dict[str, Any]) -> list[str]:
    status_counts = ctx["status_counts"]
    total_files = ctx["total_files"]
    grouped_files_count = ctx["grouped_files_count"]
    translation_change_count = ctx["translation_change_count"]
    changed_languages_count = ctx["changed_languages_count"]
    parse_errors: list[dict[str, str]] = ctx["parse_errors"]
    similar_groups: list[dict[str, Any]] = ctx["similar_groups"]

    lines = [
        COMMENT_MARKER,
        "Here is a summary of the `.po` file changes:",
        "",
        f"- Changed files: `{total_files}`",
        f"- Added files: `{status_counts['added']}`",
        f"- Removed files: `{status_counts['removed']}`",
        f"- Files in similar change-size groups within 2% tolerance: `{grouped_files_count}`",
        f"- Added or changed translations detected: `{translation_change_count}` across `{changed_languages_count}` file(s)",
    ]

    if parse_errors:
        lines.append(f"- Files that could not be parsed: `{len(parse_errors)}`")

    lines.extend(["", "### Similar Change-Size Groups", ""])

    if similar_groups:
        for group in similar_groups:
            representative_additions = round(group["avg_additions"])
            representative_deletions = round(group["avg_deletions"])
            file_names = ", ".join(
                f"`{Path(change['filename']).name}`" for change in group["files"]
            )
            lines.append(
                f"- Around `+{representative_additions} / -{representative_deletions}` lines: "
                f"`{len(group['files'])}` files ({file_names})"
            )
    else:
        lines.append(
            "- No repeated change-size groups were found within the 2% tolerance."
        )

    return lines


def _build_report_list_section(
    heading: str, reports: list[dict[str, Any]]
) -> list[str]:
    if not reports:
        return []

    lines = [heading, ""]
    lines.extend(f"- `{report['language']}` (`{report['path']}`)" for report in reports)
    lines.append("")
    return lines


def _build_suffix_lines(ctx: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    lines.extend(
        _build_report_list_section(
            "### Metadata-Only File Changes", ctx["metadata_only_reports"]
        )
    )
    lines.extend(
        _build_report_list_section(
            "### Removed Translation Files", ctx["removed_reports"]
        )
    )
    lines.extend(_build_parse_error_lines(ctx["parse_errors"]))
    return lines


def _build_parse_error_lines(parse_errors: list[dict[str, str]]) -> list[str]:
    if not parse_errors:
        return []

    lines = ["### Parse Errors", ""]
    for error in parse_errors:
        lines.append(f"- `{error['path']}`: {html.escape(error['error'])}")
    lines.append("")
    return lines


def _continuation_marker(review_kind: str, part_index: int, total_parts: int) -> str:
    """Marker for follow-up comments; part_index and total_parts are 1-based."""

    return f"<!-- {review_kind} part {part_index}/{total_parts} -->"


PO_REVIEW_KIND = "po-translation-review"
POT_REVIEW_KIND = "pot-template-review"


def _wrap_details(summary: str, body: str) -> str:
    """Wrap markdown body in a collapsible details block."""

    return f"<details>\n<summary>{summary}</summary>\n\n{body.rstrip()}\n</details>"


def _locale_details_summary(
    report: dict[str, Any],
    *,
    part_index: int,
    total_parts: int,
    entry_count: int,
) -> str:
    label = f"`{report['language']}` (`{report['path']}`)"
    if total_parts == 1:
        return f"{label} — {entry_count} entries"
    return f"{label} (part {part_index} of {total_parts}, {entry_count} entries)"


def _pot_file_details_summary(
    report: dict[str, Any],
    changes: list[dict[str, Any]],
    *,
    part_index: int,
    total_parts: int,
) -> str:
    added = sum(1 for change in changes if change["status"] == "added")
    removed = sum(1 for change in changes if change["status"] == "removed")
    corrected = sum(1 for change in changes if change["status"] == "corrected")
    counts = f"{added} added, {removed} removed, {corrected} corrected"
    label = f"`{report['path']}`"
    if total_parts == 1:
        return f"{label} — {counts}"
    return f"{label} (part {part_index} of {total_parts}, {counts})"


def build_language_details_section(
    report: dict[str, Any],
    changes: list[dict[str, Any]] | None = None,
    *,
    part_index: int = 1,
    total_parts: int = 1,
) -> str:
    """Render one language (or chunk) as a collapsible details section."""

    rows = report["changes"] if changes is None else changes
    table = "\n".join(build_language_section(report, rows)).rstrip()
    summary = _locale_details_summary(
        report,
        part_index=part_index,
        total_parts=total_parts,
        entry_count=len(rows),
    )
    return _wrap_details(summary, table)


def build_pot_details_section(
    report: dict[str, Any],
    changes: list[dict[str, Any]] | None = None,
    *,
    part_index: int = 1,
    total_parts: int = 1,
) -> str:
    """Render one .pot file (or chunk) as a collapsible details section."""

    rows = report["changes"] if changes is None else changes
    table = "\n".join(build_pot_file_section(report, rows)).rstrip()
    summary = _pot_file_details_summary(
        report,
        rows,
        part_index=part_index,
        total_parts=total_parts,
    )
    return _wrap_details(summary, table)


def _render_packed_comment(
    *,
    part_index: int,
    total_parts: int,
    first_part_head: str,
    review_kind: str,
    section_bodies: list[str],
    suffix_text: str,
    empty_body: str | None = None,
) -> str:
    """Assemble one GitHub issue comment from pre-wrapped section bodies."""

    if part_index == 1:
        head = first_part_head
    else:
        head = f"{_continuation_marker(review_kind, part_index, total_parts)}\n\n"

    if section_bodies:
        body = "\n\n".join(section_bodies)
    else:
        body = empty_body or ""

    parts = [head.rstrip("\n")]
    if body:
        parts.append(body.rstrip("\n"))
    if suffix_text:
        parts.append(suffix_text.rstrip("\n"))
    return "\n\n".join(parts) + "\n"


def _section_fits_in_comment(
    section: str,
    *,
    render_part: Callable[..., str],
    suffix_text: str,
    max_body_chars: int,
    total_parts_upper_bound: int,
) -> bool:
    """Return whether a section can fit as a standalone comment body.

    Checks both the first-part head and a continuation head, since either may be
    longer depending on the review kind (PO prefix vs POT marker).
    """

    total_parts = max(total_parts_upper_bound, 1)
    part_indexes = {1, total_parts}
    for part_index in part_indexes:
        body = render_part(
            part_index=part_index,
            total_parts=total_parts,
            section_bodies=[section],
            suffix_text=suffix_text,
        )
        if len(body) > max_body_chars:
            return False
    return True


def _largest_fitting_change_count(
    changes: list[dict[str, Any]],
    *,
    build_section: Callable[[list[dict[str, Any]], int, int], str],
    render_part: Callable[..., str],
    suffix_text: str,
    max_body_chars: int,
    part_index: int,
    total_parts_bound: int,
    overflow_error: str,
) -> int:
    """Return the largest prefix length of changes that fits in one comment."""

    def fits(count: int) -> bool:
        section = build_section(changes[:count], part_index, total_parts_bound)
        return _section_fits_in_comment(
            section,
            render_part=render_part,
            suffix_text=suffix_text,
            max_body_chars=max_body_chars,
            total_parts_upper_bound=total_parts_bound,
        )

    if not changes:
        return 0

    if not fits(1):
        section = build_section(changes[:1], part_index, total_parts_bound)
        body = render_part(
            part_index=1,
            total_parts=max(total_parts_bound, 1),
            section_bodies=[section],
            suffix_text=suffix_text,
        )
        raise RuntimeError(
            f"{overflow_error} ({len(body)} chars). Improve splitting or raise MAX_COMMENT_BODY_CHARS."
        )

    lo = 1
    hi = len(changes)
    best = 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if fits(mid):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _expand_report_sections(
    report: dict[str, Any],
    *,
    build_section: Callable[[list[dict[str, Any]], int, int], str],
    render_part: Callable[..., str],
    suffix_text: str,
    max_body_chars: int,
    overflow_error: str,
) -> list[str]:
    """Expand one report into one or more details sections that each fit alone."""

    changes = list(report["changes"])
    full_section = build_section(changes, 1, 1)
    # Bound > 1 so the fit check also covers a longer continuation-marker head
    # (POT part-1 marker is shorter than "part N/M" heads used when packing).
    if _section_fits_in_comment(
        full_section,
        render_part=render_part,
        suffix_text=suffix_text,
        max_body_chars=max_body_chars,
        total_parts_upper_bound=999,
    ):
        return [full_section]

    total_parts_bound = max(len(changes), 1)
    slices: list[list[dict[str, Any]]] = []
    remaining = changes
    while remaining:
        count = _largest_fitting_change_count(
            remaining,
            build_section=build_section,
            render_part=render_part,
            suffix_text=suffix_text,
            max_body_chars=max_body_chars,
            part_index=len(slices) + 1,
            total_parts_bound=total_parts_bound,
            overflow_error=overflow_error,
        )
        slices.append(remaining[:count])
        remaining = remaining[count:]

    total_parts = len(slices)
    return [
        build_section(chunk, index + 1, total_parts)
        for index, chunk in enumerate(slices)
    ]


def _pack_sections_into_comments(
    flat_sections: list[str],
    *,
    render_part: Callable[..., str],
    suffix_text: str,
    max_body_chars: int,
    total_parts_guess: int,
    overflow_error: str,
) -> list[list[str]]:
    """Group section bodies into chunks that each fit GitHub comment limits."""

    groups: list[list[str]] = []
    current: list[str] = []
    index = 0

    while index < len(flat_sections):
        section = flat_sections[index]
        is_last_section = index == len(flat_sections) - 1
        trial = [*current, section]
        body = render_part(
            part_index=len(groups) + 1,
            total_parts=total_parts_guess,
            section_bodies=trial,
            suffix_text=suffix_text if is_last_section else "",
        )

        if len(body) <= max_body_chars:
            current = trial
            index += 1
            continue

        # Section doesn't fit: flush the current group and retry it in a fresh one.
        if current:
            groups.append(current)
            current = []
            continue

        raise RuntimeError(
            f"{overflow_error} ({len(body)} chars). Improve splitting or raise MAX_COMMENT_BODY_CHARS."
        )

    if current:
        groups.append(current)

    return groups


def _collect_packed_comment_bodies(
    reports_with_changes: list[dict[str, Any]],
    *,
    expand_report: Callable[[dict[str, Any]], list[str]],
    render_part: Callable[..., str],
    suffix_text: str,
    max_body_chars: int,
    overflow_error: str,
    overflow_kind: str,
) -> list[str]:
    """Expand reports into details sections, pack them, and render comment bodies."""

    flat_sections: list[str] = []
    for report in reports_with_changes:
        flat_sections.extend(expand_report(report))

    if not flat_sections:
        body = render_part(
            part_index=1,
            total_parts=1,
            section_bodies=[],
            suffix_text=suffix_text,
        )
        if len(body) > max_body_chars:
            raise RuntimeError(
                f"{overflow_kind} comment exceeds max_body_chars; shorten prefix/suffix or raise limit."
            )
        return [body]

    groups = _pack_sections_into_comments(
        flat_sections,
        render_part=render_part,
        suffix_text=suffix_text,
        max_body_chars=max_body_chars,
        total_parts_guess=max(len(flat_sections), 1),
        overflow_error=overflow_error,
    )
    total_parts = len(groups)
    bodies: list[str] = []

    for idx, sections_in_group in enumerate(groups):
        part_no = idx + 1
        is_last = part_no == total_parts
        body = render_part(
            part_index=part_no,
            total_parts=total_parts,
            section_bodies=sections_in_group,
            suffix_text=suffix_text if is_last else "",
        )
        if len(body) > max_body_chars:
            raise RuntimeError(
                f"{overflow_kind} comment part {part_no}/{total_parts} is {len(body)} characters; "
                "raise MAX_COMMENT_BODY_CHARS or improve packing."
            )
        bodies.append(body)

    return bodies


def build_comment_bodies(
    po_files: list[dict[str, Any]],
    language_reports: list[dict[str, Any]],
    similar_groups: list[dict[str, Any]],
    parse_errors: list[dict[str, str]],
    hidden_po_files: set[str] | None = None,
    max_body_chars: int = MAX_COMMENT_BODY_CHARS,
) -> list[str]:
    """Build one or more PR comment bodies, each under GitHub's size limit."""

    hidden = set() if hidden_po_files is None else hidden_po_files
    ctx = _review_context(
        po_files, language_reports, similar_groups, parse_errors, hidden
    )
    prefix_text = "\n".join(_build_prefix_lines(ctx)) + "\n\n"
    suffix_lines = _build_suffix_lines(ctx)
    suffix_text = "\n".join(suffix_lines) if suffix_lines else ""
    reviewable: list[dict[str, Any]] = ctx["reviewable_language_reports"]

    language_reports_with_changes = [
        report for report in reviewable if report["changes"]
    ]

    def render_po_part(
        *,
        part_index: int,
        total_parts: int,
        section_bodies: list[str],
        suffix_text: str,
        empty_body: str | None = None,
    ) -> str:
        return _render_packed_comment(
            part_index=part_index,
            total_parts=total_parts,
            first_part_head=prefix_text,
            review_kind=PO_REVIEW_KIND,
            section_bodies=section_bodies,
            suffix_text=suffix_text,
            empty_body=empty_body,
        )

    if not language_reports_with_changes:
        empty_message = (
            "No added or changed translations were detected. The `.po` changes appear to be metadata, "
            "comment, or source reference updates only.\n"
        )
        empty_summary = (
            f"Added or changed translations by language "
            f"({ctx['translation_change_count']} entries across "
            f"{ctx['changed_languages_count']} file(s))"
        )
        body = render_po_part(
            part_index=1,
            total_parts=1,
            section_bodies=[_wrap_details(empty_summary, empty_message)],
            suffix_text=suffix_text,
        )
        if len(body) > max_body_chars:
            raise RuntimeError(
                "Single metadata-only review comment exceeds max_body_chars; shorten prefix or raise limit."
            )
        return [body]

    def expand_po_report(report: dict[str, Any]) -> list[str]:
        def build_section(
            changes: list[dict[str, Any]], part_index: int, total_parts: int
        ) -> str:
            return build_language_details_section(
                report,
                changes,
                part_index=part_index,
                total_parts=total_parts,
            )

        return _expand_report_sections(
            report,
            build_section=build_section,
            render_part=render_po_part,
            suffix_text=suffix_text,
            max_body_chars=max_body_chars,
            overflow_error="A single translation row does not fit in one comment",
        )

    return _collect_packed_comment_bodies(
        language_reports_with_changes,
        expand_report=expand_po_report,
        render_part=render_po_part,
        suffix_text=suffix_text,
        max_body_chars=max_body_chars,
        overflow_error="A single translation section does not fit in one comment",
        overflow_kind="PO review",
    )


def _oversized_review_fallback_body(marker: str, label: str, exc: RuntimeError) -> str:
    """Short marker comment when full review output cannot be packed under GitHub limits."""

    detail = html.escape(str(exc))
    max_detail = 800
    if len(detail) > max_detail:
        detail = f"{detail[:max_detail]}…"
    return (
        f"{marker}\n\n"
        f"The automated {label} review could not be split to fit GitHub's comment size limit.\n\n"
        f"**Reason:** {detail}\n"
    )


def build_comment(
    po_files: list[dict[str, Any]],
    language_reports: list[dict[str, Any]],
    similar_groups: list[dict[str, Any]],
    parse_errors: list[dict[str, str]],
    hidden_po_files: set[str] | None = None,
) -> str:
    """Build the first PR comment body (for tests and ad-hoc use)."""

    return build_comment_bodies(
        po_files,
        language_reports,
        similar_groups,
        parse_errors,
        hidden_po_files=hidden_po_files,
    )[0]


def build_file_report(
    repo: str,
    change: dict[str, Any],
    head_sha: str,
    suffix: str,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Compare one changed translation file between the base checkout and PR head blob."""

    base_path = base_path_for_file(change)
    head_path = head_path_for_file(change)
    display_path = head_path or base_path or change.get("filename") or ""

    try:
        base_content = read_local_file(_path_with_suffix(base_path, suffix))
        head_content = fetch_file_content(
            repo, _path_with_suffix(head_path, suffix), head_sha
        )
        base_language, base_entries = load_translation_entries(base_content)
        head_language, head_entries = load_translation_entries(head_content)

        if suffix == ".po":
            language = head_language or base_language or Path(display_path).stem
            return (
                {
                    "language": language,
                    "path": display_path,
                    "status": change.get("status"),
                    "changes": compare_entries(base_entries, head_entries),
                },
                None,
            )

        changes = compare_pot_entries(base_entries, head_entries)
        return (
            {
                "path": display_path,
                "status": change.get("status"),
                "changes": changes,
            },
            None,
        )
    except Exception as exc:
        return None, {"path": display_path, "error": str(exc)}


def build_pot_file_section(
    report: dict[str, Any], changes: list[dict[str, Any]] | None = None
) -> list[str]:
    """Render one .pot file's msgid changes as a markdown table."""

    rows = report["changes"] if changes is None else changes
    lines = [
        f"### `{report['path']}`",
        "",
        "| Status | Previous | Current |",
        "| --- | --- | --- |",
    ]

    for change in rows:
        before = change["before"]
        after = change["after"]
        previous = render_msgid(before) if before else ""
        current = render_msgid(after) if after else ""

        lines.append(
            "| "
            + " | ".join(
                [
                    str(change["status"]),
                    escape_table_cell(previous),
                    escape_table_cell(current),
                ]
            )
            + " |"
        )

    lines.append("")
    return lines


def build_pot_comment_bodies(
    pot_reports: list[dict[str, Any]],
    parse_errors: list[dict[str, str]],
    max_body_chars: int = MAX_COMMENT_BODY_CHARS,
) -> list[str]:
    """Build one or more .pot review comment bodies under GitHub's size limit."""

    reports_with_changes = [report for report in pot_reports if report.get("changes")]
    if not reports_with_changes and not parse_errors:
        if not pot_reports:
            return []

        status_counts = Counter(
            report.get("status", "modified") for report in pot_reports
        )
        file_count = len(pot_reports)
        prefix_text = (
            "\n".join(
                [
                    POT_COMMENT_MARKER,
                    "Here is a summary of the `.pot` file changes:",
                    "",
                    f"- Changed files: `{file_count}`",
                    f"- Added files: `{status_counts.get('added', 0)}`",
                    f"- Removed files: `{status_counts.get('removed', 0)}`",
                ]
            )
            + "\n\n"
        )
        suffix_lines = [
            "### Metadata-Only File Changes",
            "",
            *(f"- `{report['path']}`" for report in pot_reports),
            "",
        ]
        suffix_text = "\n".join(suffix_lines)
        empty_message = (
            "No added, removed, or corrected template strings were detected. The `.pot` "
            "changes appear to be metadata, comment, or source reference updates only.\n"
        )
        file_label = "file" if file_count == 1 else "files"
        body = _render_packed_comment(
            part_index=1,
            total_parts=1,
            first_part_head=prefix_text,
            review_kind=POT_REVIEW_KIND,
            section_bodies=[
                _wrap_details(
                    f"Metadata-only template file changes ({file_count} {file_label})",
                    empty_message,
                )
            ],
            suffix_text=suffix_text,
        )
        if len(body) > max_body_chars:
            raise RuntimeError(
                "Single metadata-only .pot review comment exceeds max_body_chars; "
                "shorten prefix or raise limit."
            )
        return [body]

    suffix_text = "\n".join(_build_parse_error_lines(parse_errors))
    prefix_text = f"{POT_COMMENT_MARKER}\n\n"

    def render_pot_part(
        *,
        part_index: int,
        total_parts: int,
        section_bodies: list[str],
        suffix_text: str,
        empty_body: str | None = None,
    ) -> str:
        return _render_packed_comment(
            part_index=part_index,
            total_parts=total_parts,
            first_part_head=prefix_text,
            review_kind=POT_REVIEW_KIND,
            section_bodies=section_bodies,
            suffix_text=suffix_text,
            empty_body=empty_body,
        )

    def expand_pot_report(report: dict[str, Any]) -> list[str]:
        def build_section(
            changes: list[dict[str, Any]], part_index: int, total_parts: int
        ) -> str:
            return build_pot_details_section(
                report,
                changes,
                part_index=part_index,
                total_parts=total_parts,
            )

        return _expand_report_sections(
            report,
            build_section=build_section,
            render_part=render_pot_part,
            suffix_text=suffix_text,
            max_body_chars=max_body_chars,
            overflow_error="A single .pot row does not fit in one comment",
        )

    return _collect_packed_comment_bodies(
        reports_with_changes,
        expand_report=expand_pot_report,
        render_part=render_pot_part,
        suffix_text=suffix_text,
        max_body_chars=max_body_chars,
        overflow_error="A single .pot section does not fit in one comment",
        overflow_kind=".pot review",
    )


def main() -> None:
    """Generate PO review comment bodies for the current PR and write them as JSON."""

    args = parse_args()
    if not args.repo or not args.pr or not args.head_sha:
        raise SystemExit("Missing required pull request context.")

    hidden_po_files = parse_hidden_po_files(args.hidden_po_files)
    all_files = fetch_pr_files(args.repo, args.pr)
    po_files = [change for change in all_files if is_translation_file(change, ".po")]
    pot_files = [change for change in all_files if is_translation_file(change, ".pot")]
    language_reports: list[dict[str, Any]] = []
    po_parse_errors: list[dict[str, str]] = []
    pot_reports: list[dict[str, Any]] = []
    pot_parse_errors: list[dict[str, str]] = []

    for change in po_files:
        report, error = build_file_report(args.repo, change, args.head_sha, ".po")
        if report:
            language_reports.append(report)
        if error:
            po_parse_errors.append(error)

    for change in pot_files:
        report, error = build_file_report(args.repo, change, args.head_sha, ".pot")
        if report:
            pot_reports.append(report)
        if error:
            pot_parse_errors.append(error)

    language_reports.sort(
        key=lambda report: (
            str(report["language"]).lower(),
            str(report["path"]).lower(),
        )
    )
    pot_reports.sort(key=lambda report: str(report["path"]).lower())

    po_bodies: list[str] = []
    if po_files:
        try:
            similar_groups = cluster_similar_change_sizes(po_files)
            po_bodies = build_comment_bodies(
                po_files,
                language_reports,
                similar_groups,
                po_parse_errors,
                hidden_po_files=hidden_po_files,
            )
        except RuntimeError as exc:
            po_bodies = [
                _oversized_review_fallback_body(
                    COMMENT_MARKER, "`.po` translation", exc
                )
            ]

    pot_bodies: list[str] = []
    if pot_files:
        try:
            pot_bodies = build_pot_comment_bodies(pot_reports, pot_parse_errors)
        except RuntimeError as exc:
            pot_bodies = [
                _oversized_review_fallback_body(
                    POT_COMMENT_MARKER, "`.pot` template", exc
                )
            ]

    Path(args.output).write_text(
        json.dumps(
            {"comments": po_bodies, "pot_comments": pot_bodies}, ensure_ascii=False
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
