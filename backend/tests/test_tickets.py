from buglens import tickets


def test_parse_closing_refs_keywords_and_urls():
    text = (
        "Fixes #12 and closes: #13.\n"
        "Also resolves https://github.com/other/repo/issues/99\n"
        "and closes other/repo#100\n"
    )
    refs = tickets.parse_closing_refs(text)
    assert refs == [
        {"repo": "other/repo", "number": 99},
        {"repo": "other/repo", "number": 100},
        {"repo": None, "number": 12},
        {"repo": None, "number": 13},
    ]


def test_parse_closing_refs_ignores_code_and_quotes():
    text = (
        "```\ncloses #1\n```\n"
        "`fixes #2`\n"
        "> closes #3\n"
        "<!-- closes #4 -->\n"
        "Closes #5\n"
    )
    assert tickets.parse_closing_refs(text) == [{"repo": None, "number": 5}]


def test_parse_closing_refs_dedupes():
    assert tickets.parse_closing_refs("fixes #7\nfixes #7\n") == [{"repo": None, "number": 7}]


def test_acceptance_criteria_from_heading_checkboxes():
    body = "## Description\nstuff\n\n## Acceptance criteria\n- [ ] one\n- [x] two\n- [ ] three\n"
    assert tickets.extract_acceptance_criteria(body) == ["one", "two", "three"]


def test_acceptance_criteria_falls_back_to_bullets():
    body = "### Requirements\n- must handle nulls\n- must not throw\n"
    assert tickets.extract_acceptance_criteria(body) == ["must handle nulls", "must not throw"]


def test_format_ticket_includes_title_body_and_criteria():
    issues = [{"number": 4, "title": "Fix crash", "body": "It crashes.", "repo": "o/r"}]
    out = tickets.format_ticket(issues, {4: ["no crash"]})
    assert "o/r#4" in out and "Fix crash" in out and "no crash" in out


def test_format_ticket_empty_without_issues():
    assert tickets.format_ticket([]) == ""
