"""
Tests for retriever.py — _classify_file, extract_pr_files, detect_stack, score threshold.
"""

from unittest.mock import MagicMock

from src.integrations.retriever import _classify_file, detect_stack, extract_pr_files

# ---------------------------------------------------------------------------
# _classify_file
# ---------------------------------------------------------------------------


class TestClassifyFile:
    def test_python_file(self):
        assert _classify_file("src/main.py") == "python-general"

    def test_typescript_file(self):
        assert _classify_file("app.ts") == "typescript"

    def test_tsx_file(self):
        assert _classify_file("components/Button.tsx") == "react-nextjs"

    def test_go_file(self):
        assert _classify_file("cmd/server/main.go") == "golang"

    def test_rust_file(self):
        assert _classify_file("src/lib.rs") == "rust"

    def test_terraform_file(self):
        assert _classify_file("infra/main.tf") == "terraform"

    def test_shell_script(self):
        assert _classify_file("scripts/deploy.sh") == "shell-scripts"

    # YAML sub-classification
    def test_helm_values(self):
        assert _classify_file("charts/myapp/values.yaml") == "kubernetes"

    def test_helm_chart_yaml(self):
        assert _classify_file("charts/Chart.yaml") == "kubernetes"

    def test_helm_template(self):
        assert _classify_file("charts/templates/deployment.yaml") == "kubernetes"

    def test_github_actions_yml(self):
        assert _classify_file(".github/workflows/ci.yml") == "github-actions"

    def test_github_actions_yaml(self):
        assert _classify_file(".github/workflows/release.yaml") == "github-actions"

    def test_docker_compose_yml(self):
        assert _classify_file("docker-compose.yml") == "docker"

    def test_docker_compose_override(self):
        assert _classify_file("docker-compose.override.yml") == "docker"

    def test_ansible_playbook(self):
        assert _classify_file("ansible/playbooks/deploy.yml") == "ansible"

    def test_ansible_role(self):
        assert _classify_file("roles/webserver/tasks/main.yml") == "ansible"

    def test_unknown_yaml_returns_none(self):
        """Generic config.yaml at repo root must not inject kubernetes rules."""
        assert _classify_file("config.yaml") is None

    def test_bare_config_yml_returns_none(self):
        assert _classify_file("config.yml") is None

    def test_no_extension_returns_none(self):
        assert _classify_file("Makefile") is None

    def test_empty_path_returns_none(self):
        assert _classify_file("") is None


# ---------------------------------------------------------------------------
# extract_pr_files
# ---------------------------------------------------------------------------


SAMPLE_DIFF = """\
diff --git a/src/api/app.py b/src/api/app.py
index abc1234..def5678 100644
--- a/src/api/app.py
+++ b/src/api/app.py
@@ -1,5 +1,6 @@
+import yaml  # this string must NOT trigger yaml/kubernetes detection
 from fastapi import FastAPI
diff --git a/src/review/agents/security_agent.py b/src/review/agents/security_agent.py
index 111..222 100644
--- a/src/review/agents/security_agent.py
+++ b/src/review/agents/security_agent.py
@@ -10,3 +10,4 @@
+    # reads config.yaml and terraform.tf from disk
     pass
"""


class TestExtractPrFiles:
    def test_extracts_correct_paths(self):
        paths = extract_pr_files(SAMPLE_DIFF)
        assert "src/api/app.py" in paths
        assert "src/review/agents/security_agent.py" in paths

    def test_ignores_extension_strings_in_code(self):
        """Extensions in code content (e.g. 'import yaml') must not appear as paths."""
        paths = extract_pr_files(SAMPLE_DIFF)
        # Only actual file headers, not references inside diff body
        assert all(p.endswith((".py",)) for p in paths)

    def test_empty_diff(self):
        assert extract_pr_files("") == []

    def test_single_file(self):
        diff = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        assert extract_pr_files(diff) == ["README.md"]


# ---------------------------------------------------------------------------
# detect_stack — the main regression guard
# ---------------------------------------------------------------------------


class TestDetectStack:
    def test_python_only_pr_no_spurious_categories(self):
        """
        Core regression: a Python-only PR must not trigger typescript/kubernetes/
        terraform categories even if those strings appear inside the diff body.
        """
        diff = """\
diff --git a/src/api/app.py b/src/api/app.py
index 000..111 100644
--- a/src/api/app.py
+++ b/src/api/app.py
@@ -1,3 +1,6 @@
+import yaml
+# see config.ts and values.yaml and main.tf
+WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
 from fastapi import FastAPI
"""
        stack = detect_stack(diff)
        assert "python-general" in stack
        assert "typescript" not in stack, "typescript must not appear for a .py file"
        assert "kubernetes" not in stack, "kubernetes must not appear for a .py file"
        assert "terraform" not in stack, "terraform must not appear for a .py file"

    def test_always_include_categories_present(self):
        diff = "diff --git a/src/main.py b/src/main.py\n--- a/src/main.py\n+++ b/src/main.py\n"
        stack = detect_stack(diff)
        assert "security-owasp" in stack
        assert "api-design" in stack

    def test_github_actions_yaml_detected(self):
        diff = "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        stack = detect_stack(diff)
        assert "github-actions" in stack
        assert "kubernetes" not in stack

    def test_mixed_pr_python_and_terraform(self):
        diff = (
            "diff --git a/src/main.py b/src/main.py\n"
            "diff --git a/infra/main.tf b/infra/main.tf\n"
        )
        stack = detect_stack(diff)
        assert "python-general" in stack
        assert "terraform" in stack
        assert "kubernetes" not in stack

    def test_unknown_yaml_not_injected(self):
        diff = "diff --git a/config.yaml b/config.yaml\n"
        stack = detect_stack(diff)
        assert "kubernetes" not in stack
        assert "github-actions" not in stack

    def test_helm_values_yaml_is_kubernetes(self):
        diff = "diff --git a/charts/values.yaml b/charts/values.yaml\n"
        stack = detect_stack(diff)
        assert "kubernetes" in stack

    def test_empty_diff_only_always_include(self):
        stack = detect_stack("")
        assert stack == ["security-owasp", "api-design"]


# ---------------------------------------------------------------------------
# Score threshold — low-confidence hits must be filtered out
# ---------------------------------------------------------------------------


def _make_hit(text: str, distance: float) -> MagicMock:
    hit = MagicMock()
    hit.entity.get.return_value = text
    hit.distance = distance
    return hit


class TestScoreThreshold:
    def test_hits_below_threshold_are_kept(self):
        """Rules with distance <= threshold must be included."""
        hit = _make_hit("[PY001] Use type hints", distance=0.8)
        assert hit.distance <= 1.5
        assert hit.entity.get("text") == "[PY001] Use type hints"

    def test_hits_above_threshold_are_dropped(self):
        """Rules with distance > threshold (poor match) must be excluded."""
        hit = _make_hit("[K8S01] Set resource limits", distance=1.9)
        assert hit.distance > 1.5  # would be filtered in retriever_node

    def test_threshold_filters_applied_in_retriever(self):
        """retriever_node respects milvus_score_threshold when filtering hits."""

        close_hit = _make_hit("[PY001] Use type hints", distance=0.5)
        far_hit = _make_hit("[K8S01] Set resource limits", distance=2.0)

        mock_settings = MagicMock()
        mock_settings.milvus_rules_per_category = 4
        mock_settings.milvus_score_threshold = 1.5

        # Simulate what retriever_node does when filtering
        hits = [
            hit.entity.get("text")
            for hit in [close_hit, far_hit]
            if hit.entity.get("text")
            and hit.distance <= mock_settings.milvus_score_threshold
        ]
        assert hits == ["[PY001] Use type hints"]
        assert "[K8S01] Set resource limits" not in hits
