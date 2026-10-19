# BrightBean upstream update — October 19, 2026

Repository synchronization only. Previous GSI main: 232b59e. Upstream:
https://github.com/brightbeanxyz/brightbean-studio commit
48801fdcd1009e6ed4863fa9942222a478a297e2 (112 commits ahead of the shared base).

Preserved GSI AI Assist/BYOK, its encrypted provider configuration and regression
tests and GSI rules-file symlinks. Corrected CI to GitHub-hosted runners because
the repository is public: public projects run free there, while self-hosted
runners would expose Ada to outside pull-request code. Resolved
additive composer imports and retained both the upstream Instagram credential
expectation and GSI AI-provider tests. Three files normalized with CI-pinned ruff.

Validation on Ada in an isolated worktree and disposable PostgreSQL instance:
- 2737 tests passed, 1 skipped; no live database or platform credentials used.
- ruff 0.15.9 check and formatting passed.
- mypy passed across 602 source files.
- makemigrations --check --dry-run: no changes detected.
- GitHub CI/build remains a separate required gate before landing.

Not a deployment. Running BrightBean uses a separately pinned source/image and
Control Center's Access overlay. A runtime upgrade needs a reviewed CI deployment
plan, database backup/migrations, worker-command review and live behavior checks.
Keep current working Resend SMTP and all org/workspace memberships during that work.

GSI and BGOL connection screens both show unavailable platforms because required
provider app credentials are missing at deployment/org level. This update does
not invent credentials, connect accounts or authorize publishing. Individual
sidebar channel shortcuts currently all point to the same generic chooser; on
that page they appear to do nothing. Treat setup guidance/navigation separately
from platform authentication and preserve the human approval requirements.

Use a normal PR merge to retain upstream ancestry for later fork synchronization.
Tracker: CTRL-124, GSI/BrightBean setup workstream.
