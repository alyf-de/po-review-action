# Review PO pull requests

A GitHub Action that posts review-friendly summaries on pull requests that change gettext `.po` files.

Large translation PRs are hard to review in GitHub's diff UI because line-number churn hides the actual `msgstr` changes. This action compares the trusted base commit against the PR head, extracts added or changed translations, groups files with similar diff sizes, and posts one or more collapsible PR comments.

The logic is based on the workflow used in [Frappe Framework](https://github.com/frappe/frappe).

## Features

- Compares base `.po` files from a trusted checkout with head files fetched by SHA
- Highlights added and changed translations in per-language tables
- Detects bulk updates via similar change-size grouping
- Splits oversized output across multiple comments within GitHub size limits
- Replaces previous bot comments on synchronize to avoid stale summaries

## Usage

Use `pull_request_target` so the action can read the trusted base commit and post comments without executing untrusted PR code. Restrict the workflow to `.po` paths in your repository.

```yaml
name: Review translation PRs

on:
  pull_request_target:
    types: [opened, reopened, synchronize, ready_for_review]
    paths:
      - "**/*.po"

concurrency:
  group: po-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review-po-pr:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
      issues: write
      pull-requests: write

    steps:
      - uses: alyf-de/po-review-action@v1
```

`pr-number`, `pr-head-sha`, and `base-sha` default to values from `github.event.pull_request`, so you only need a `with:` block when overriding them (for example, `skip-checkout: true`).

> **Note:** These defaults assume the workflow runs on `pull_request` or `pull_request_target` events. On other triggers (for example, `workflow_dispatch`), pass the inputs explicitly.

The action checks out the trusted `base-sha` by default. If your workflow already checked out the base commit, pass `skip-checkout: true`.

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `token` | No | `${{ github.token }}` | Token used to read PR files and post comments |
| `repository` | No | `${{ github.repository }}` | Repository in `owner/name` form |
| `pr-number` | No | `${{ github.event.pull_request.number }}` | Pull request number to review |
| `pr-head-sha` | No | `${{ github.event.pull_request.head.sha }}` | PR head commit SHA |
| `base-sha` | No | `${{ github.event.pull_request.base.sha }}` | Trusted base commit SHA for comparison |
| `python-version` | No | `3.12` | Python version for the review helper |
| `hidden-po-files` | No | *(empty)* | Comma-separated `.po` file names omitted from language detail tables |
| `skip-checkout` | No | `false` | Skip the built-in base checkout step |

## Outputs

| Output | Description |
| --- | --- |
| `comments-posted` | Number of review comments posted on the pull request |

## Permissions

The workflow job needs:

- `contents: read` to check out the trusted base commit
- `issues: write` and `pull-requests: write` to create and replace review comments

## Security

The action is designed for `pull_request_target` workflows:

- Base files are read from a trusted checkout of `base-sha`
- Head files are fetched from the GitHub API by SHA instead of executing PR code
- Use path filters so the workflow only runs for translation file changes

## Local development

```bash
python -m pip install .
python -m unittest discover -s tests -v
```

## License

MIT
