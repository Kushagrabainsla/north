"""Unit tests for claims-vs-evidence verification (orchestrator/verification.py)."""

from __future__ import annotations

from orchestrator.verification import verify_claims


def test_empty_output_has_no_violations() -> None:
    assert verify_claims("", []) == []
    assert verify_claims("", ["write_file"]) == []


def test_file_claim_without_evidence_flagged() -> None:
    violations = verify_claims("I created the file config.py for you.", [])
    assert len(violations) == 1
    assert "creating or editing a file or briefing" in violations[0]


def test_file_claim_with_write_evidence_ok() -> None:
    assert verify_claims("I created config.py.", ["write_file"]) == []


def test_file_claim_with_patch_evidence_ok() -> None:
    assert verify_claims("I updated the module user.py.", ["patch_file"]) == []


def test_test_pass_claim_without_bash_flagged() -> None:
    violations = verify_claims("All tests pass now.", ["read_file"])
    assert any("running a check, test, or verification" in v for v in violations)


def test_test_pass_claim_with_bash_ok() -> None:
    assert verify_claims("All tests pass now.", ["bash"]) == []


def test_commit_claim_without_git_flagged() -> None:
    violations = verify_claims("I committed and pushed the changes.", [])
    assert any("committing or pushing changes" in v for v in violations)


def test_commit_claim_with_git_ok() -> None:
    assert verify_claims("I committed the changes.", ["git"]) == []


def test_citation_without_source_tool_flagged() -> None:
    violations = verify_claims("According to the latest figures, revenue grew 12%.", ["read_file"])
    assert any("citing external information" in v for v in violations)


def test_citation_with_web_search_ok() -> None:
    assert verify_claims("According to recent reports, revenue grew.", ["web_search"]) == []


def test_citation_with_fetch_url_ok() -> None:
    assert verify_claims("Studies show a strong correlation.", ["fetch_url"]) == []


def test_studies_show_pattern_flagged() -> None:
    violations = verify_claims("Research shows that intermittent fasting helps.", [])
    assert any("citing external information" in v for v in violations)


def test_intent_is_not_a_completion_claim() -> None:
    # A plan / hypothetical must not be treated as a claim of completion.
    assert verify_claims("I should create the file config.py next.", []) == []
    assert verify_claims("Let's write a test for this.", []) == []
    assert verify_claims("I plan to commit the changes.", []) == []


def test_multiple_independent_violations() -> None:
    output = "I created app.py, all tests pass, and I pushed to main."
    violations = verify_claims(output, [])
    labels = " ".join(violations)
    assert "creating or editing a file or briefing" in labels
    assert "running a check, test, or verification" in labels
    assert "committing or pushing changes" in labels


def test_no_actionable_claims_no_violations() -> None:
    assert verify_claims("Here is a summary of the options you could consider.", []) == []


# ---------------------------------------------------------------------------
# Wider claim coverage (#7): fixed / implemented / refactored / verified / types
# ---------------------------------------------------------------------------


def test_fixed_file_claim_without_evidence_flagged() -> None:
    violations = verify_claims("I fixed the off-by-one in parser.py.", [])
    assert any("creating or editing a file or briefing" in v for v in violations)


def test_implemented_claim_with_evidence_ok() -> None:
    assert verify_claims("I implemented the cache in store.py.", ["patch_file"]) == []


def test_refactored_claim_without_evidence_flagged() -> None:
    violations = verify_claims("I refactored the module utils.py.", [])
    assert any("creating or editing a file or briefing" in v for v in violations)


def test_typecheck_claim_without_evidence_flagged() -> None:
    violations = verify_claims("Types are clean now.", [])
    assert any("running a check, test, or verification" in v for v in violations)


def test_typecheck_claim_with_check_types_ok() -> None:
    assert verify_claims("No type errors.", ["check_types"]) == []


def test_verified_claim_without_evidence_flagged() -> None:
    violations = verify_claims("I verified that the fix works.", [])
    assert any("running a check, test, or verification" in v for v in violations)


def test_generic_fix_without_file_noun_not_flagged() -> None:
    # No file/code noun -> conservative, not a completion claim.
    assert verify_claims("I fixed the misunderstanding.", []) == []


# ---------------------------------------------------------------------------
# Briefing / digest / report / summary claims (#8)
# ---------------------------------------------------------------------------


def test_compiled_briefing_without_write_flagged() -> None:
    violations = verify_claims("I compiled a daily briefing.", [])
    assert any("file or briefing" in v for v in violations)


def test_briefing_saved_without_write_flagged() -> None:
    violations = verify_claims("Briefing saved to ~/.north/news/2026-07-25.md", [])
    assert any("file or briefing" in v for v in violations)


def test_compiled_briefing_with_write_ok() -> None:
    assert verify_claims("I compiled a daily briefing.", ["write_file"]) == []


def test_generated_report_without_write_flagged() -> None:
    violations = verify_claims("The summary report was generated.", [])
    assert any("file or briefing" in v for v in violations)


def test_produced_digest_without_write_flagged() -> None:
    violations = verify_claims("I produced a news digest for you.", [])
    assert any("file or briefing" in v for v in violations)


def test_planning_to_compile_is_not_a_claim() -> None:
    assert verify_claims("I am planning to compile a briefing.", []) == []


# ---------------------------------------------------------------------------
# Physical file existence verification (deterministic ground-truth)
# ---------------------------------------------------------------------------


def test_claimed_nonexistent_file_path_flagged_physically(tmp_path) -> None:
    missing_file = tmp_path / "never_created.md"
    output = f"Briefing saved to {missing_file}"
    # Even if write_file was recorded as a tool call, if the physical file doesn't exist, it's flagged!
    violations = verify_claims(output, ["write_file"])
    assert any("no such file exists on disk" in v for v in violations)


def test_claimed_existing_file_path_with_write_tool_ok(tmp_path) -> None:
    real_file = tmp_path / "actual_briefing.md"
    real_file.write_text("Daily briefing content")
    output = f"Briefing saved to {real_file}"
    violations = verify_claims(output, ["write_file"])
    assert violations == []

