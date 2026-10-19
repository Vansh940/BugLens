from buglens.rules import DEFAULTS, config_candidates, load_rules, path_ignored


def test_defaults_are_safe():
    r = load_rules()
    assert r.source == "defaults"
    assert r.require_evidence is True
    assert r.skip_drafts is True
    assert r.max_comments >= 1


def test_repo_config_overrides_defaults():
    rules = load_rules({".buglens.yml": "max_comments: 3\nmin_severity: major\nfocus: [security]\n"})
    assert rules.max_comments == 3
    assert rules.min_severity == "major"
    assert rules.focus == ["security"]
    assert rules.source == ".buglens.yml"


def test_list_and_bool_parsing_from_subset_parser():
    text = "skip_nits: false\nignore_paths:\n  - dist/*\n  - vendor/**\ncustom_rules: [no print statements]\n"
    rules = load_rules({".buglens.yml": text})
    assert rules.skip_nits is False
    assert "dist/*" in rules.ignore_paths
    assert rules.custom_rules == ["no print statements"]


def test_unknown_keys_are_ignored():
    rules = load_rules({".buglens.yml": "totally_unknown: 5\nmax_comments: 7\n"})
    assert rules.max_comments == 7
    assert not hasattr(rules, "totally_unknown")


def test_path_ignored_globs():
    assert path_ignored("dist/app.js", ["dist/**"])
    assert path_ignored("vendor/x/y.py", ["vendor/"])
    assert not path_ignored("src/app.py", ["dist/**"])


def test_consensus_is_configurable():
    assert load_rules().consensus == 1
    assert load_rules({".buglens.yml": "consensus: 3\n"}).consensus == 3


def test_defaults_dict_is_not_mutated():
    before = dict(DEFAULTS)
    load_rules({".buglens.yml": "max_comments: 1\n"})
    assert DEFAULTS == before


def test_config_candidates_root_first_then_nearest_last():
    out = config_candidates(["src/api/handlers.py", "docs/x.md"])
    assert out[0] == ".buglens.yml"                     # root first
    assert out.index("src/.buglens.yml") < out.index("src/api/.buglens.yml")
    assert "src/api/AGENTS.md" in out
    assert "docs/.buglens.yml" in out


def test_nested_config_overrides_root():
    configs = {
        ".buglens.yml": "max_comments: 10\nmin_severity: minor\n",
        "src/api/.buglens.yml": "max_comments: 2\n",
    }
    rules = load_rules(configs)
    assert rules.max_comments == 2                      # nearest wins
    assert rules.min_severity == "minor"                # root value survives
    assert rules.source == "src/api/.buglens.yml"


def test_instruction_files_become_custom_rules():
    rules = load_rules({
        ".buglens.yml": "custom_rules: [no print statements]\n",
        "src/AGENTS.md": "# Rules\n- never block on network calls\n- log with structlog\n",
    })
    assert "no print statements" in rules.custom_rules
    assert "never block on network calls" in rules.custom_rules
    assert "log with structlog" in rules.custom_rules


def test_nested_json_config_is_parsed():
    rules = load_rules({"pkg/.buglens.json": '{"skip_nits": false, "max_comments": 4}'})
    assert rules.skip_nits is False and rules.max_comments == 4


# ── iteration 17: deterministic adapters + check-run knobs ────────────────────
def test_linters_config_forms():
    assert load_rules().linters == []
    assert load_rules({".buglens.yml": "linters: auto\n"}).linters == ["auto"]
    assert load_rules({".buglens.yml": "linters: [Ruff, ESLint]\n"}).linters == ["ruff", "eslint"]
    assert load_rules({".buglens.yml": "linters:\n  - bandit\n  - hadolint\n"}).linters == ["bandit", "hadolint"]


def test_check_run_knobs_default_to_non_blocking():
    r = load_rules()
    assert r.post_check_run is False
    assert r.check_run_conclusion == "neutral"
    assert r.required_status_check is False
    r2 = load_rules({".buglens.yml": "post_check_run: true\ncheck_run_conclusion: failure\nrequired_status_check: true\n"})
    assert r2.post_check_run is True and r2.check_run_conclusion == "failure" and r2.required_status_check is True


def test_check_run_new_keys_defaults():
    r = load_rules()
    assert r.check_run_input == "pr_metadata"
    assert r.check_run_grouping == "single"
    assert r.check_run_max_annotations == 200


def test_check_run_new_keys_from_yaml():
    r = load_rules({".buglens.yml": "check_run_input: full_diff\ncheck_run_grouping: per_lens\ncheck_run_max_annotations: 25\n"})
    assert r.check_run_input == "full_diff"
    assert r.check_run_grouping == "per_lens"
    assert r.check_run_max_annotations == 25


def test_check_run_keys_are_validated():
    r = load_rules({".buglens.yml": "check_run_input: nonsense\ncheck_run_grouping: nonsense\ncheck_run_max_annotations: -5\n"})
    assert r.check_run_input == "pr_metadata"
    assert r.check_run_grouping == "single"
    assert r.check_run_max_annotations == 0


def test_describe_includes_check_run_keys():
    from buglens.rules import describe

    described = describe(load_rules())
    assert described["check_run_input"] == "pr_metadata"
    assert described["check_run_grouping"] == "single"
    assert described["check_run_max_annotations"] == 200


def test_pre_merge_and_request_changes_defaults():
    r = load_rules()
    assert r.pre_merge_checks is True and r.request_changes is False
    r2 = load_rules({".buglens.yml": "pre_merge_checks: false\nrequest_changes: true\n"})
    assert r2.pre_merge_checks is False and r2.request_changes is True


# ── iteration 16: re-review cap / convergence knobs ───────────────────────────
def test_round_knobs_load_and_validate():
    from buglens.rules import describe

    r = load_rules({".buglens.yml": "max_rounds: 5\nconverge_rounds: 2\n"})
    assert r.max_rounds == 5 and r.converge_rounds == 2

    defaults = load_rules()
    assert defaults.max_rounds == DEFAULTS["max_rounds"] == 10
    assert defaults.converge_rounds == DEFAULTS["converge_rounds"] == 3

    clamped = load_rules({".buglens.yml": "max_rounds: -3\nconverge_rounds: -1\n"})
    assert clamped.max_rounds == 0 and clamped.converge_rounds == 0

    described = describe(defaults)
    assert described["max_rounds"] == 10 and described["converge_rounds"] == 3


# ── Wave G: per-lens comment budget caps ──────────────────────────────────────
def test_lens_caps_defaults_empty():
    assert load_rules().lens_caps == {}


def test_lens_caps_from_yaml():
    r = load_rules({".buglens.yml": "lens_caps: {security: 3, ui: 2}\n"})
    assert r.lens_caps == {"security": 3, "ui": 2}


def test_lens_caps_validated():
    r = load_rules({".buglens.yml": 'lens_caps: {security: -2, ui: "4", tests: true, 5: 3}\n'})
    assert r.lens_caps == {"security": 0, "ui": 4}


def test_describe_includes_lens_caps():
    from buglens.rules import describe

    assert "lens_caps" in describe(load_rules())


# ── Wave H: optional external context providers ───────────────────────────────
def test_external_context_defaults_empty():
    assert load_rules().external_context == []


def test_external_context_from_yaml():
    r = load_rules({".buglens.yml": 'external_context: [jira, " Sentry ", bogus]\n'})
    assert r.external_context == ["jira", "sentry"]


def test_external_context_accepts_a_single_string():
    r = load_rules({".buglens.yml": "external_context: linear\n"})
    assert r.external_context == ["linear"]


def test_describe_includes_external_context():
    from buglens.rules import describe

    assert "external_context" in describe(load_rules())
