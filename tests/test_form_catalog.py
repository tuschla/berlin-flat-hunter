"""Offline tests for the unified application questionnaire (form catalog).

No network: we never exercise scripts/discover_forms.py against live sites — we
only assert the shape and invariants of the static catalog and its helpers.
"""

from __future__ import annotations

import pytest

from berlin_flat_hunter.forms.catalog import (
    CATALOG,
    CATALOG_PROVIDERS,
    EXCLUDED_FIELDS,
    Question,
    catalog_as_dicts,
    extra_questions,
    grouped,
    question,
    question_for_provider_field,
    questionnaire_questions,
    resolve_answer,
    values_for_provider,
)

VALID_TYPES = {"text", "email", "tel", "textarea", "select", "bool", "date", "number"}
VALID_GROUPS = set(
    ["personal", "address", "household", "message", "wbs", "consent"]
)


def test_catalog_imports_and_is_nonempty():
    assert CATALOG, "CATALOG must not be empty"
    assert all(isinstance(q, Question) for q in CATALOG)


def test_every_question_has_key_and_valid_type_and_group():
    for q in CATALOG:
        assert q.key, f"question has empty key: {q!r}"
        assert q.key.strip() == q.key, f"key has surrounding whitespace: {q.key!r}"
        assert q.type in VALID_TYPES, f"{q.key}: bad type {q.type!r}"
        assert q.group in VALID_GROUPS, f"{q.key}: bad group {q.group!r}"
        assert isinstance(q.label, str) and q.label, f"{q.key}: empty label"
        assert isinstance(q.options, list)
        assert isinstance(q.providers, dict)


def test_keys_are_unique():
    keys = [q.key for q in CATALOG]
    assert len(keys) == len(set(keys)), "duplicate question keys present"


def test_select_questions_have_options():
    for q in CATALOG:
        if q.type == "select":
            assert q.options, f"select question {q.key} has no options"


def test_applicant_vs_form_answers_partition():
    """Every question is either applicant-backed, managed, or form_answers-backed.

    ``extra_questions()`` = the form_answers-backed ones (no applicant_field, not
    managed). The three buckets must be disjoint and cover the whole catalog.
    """
    applicant_backed = [q for q in CATALOG if q.applicant_field and not q.managed]
    managed = [q for q in CATALOG if q.managed]
    extra = extra_questions()

    # extra is exactly "no applicant_field and not managed".
    assert all(not q.applicant_field and not q.managed for q in extra)

    total = len(applicant_backed) + len(managed) + len(extra)
    # managed questions may also carry an applicant_field (e.g. email), so count
    # managed separately and ensure the non-managed split is clean.
    non_managed = [q for q in CATALOG if not q.managed]
    assert len(non_managed) == len(applicant_backed) + len(extra)
    assert total == len(CATALOG)

    # Buckets are disjoint by key.
    keys_applicant = {q.key for q in applicant_backed}
    keys_extra = {q.key for q in extra}
    assert keys_applicant.isdisjoint(keys_extra)


def test_questionnaire_hides_managed():
    visible = questionnaire_questions()
    assert all(not q.managed for q in visible)
    managed_keys = {q.key for q in CATALOG if q.managed}
    assert managed_keys, "expected at least one managed question (email)"
    assert managed_keys.isdisjoint({q.key for q in visible})


def test_provider_mappings_reference_only_catalog_providers():
    for q in CATALOG:
        for provider in q.providers:
            assert provider in CATALOG_PROVIDERS, (
                f"{q.key} maps to unknown provider {provider!r}"
            )


def test_question_lookup_by_key():
    for q in CATALOG:
        assert question(q.key) is q
    assert question("does-not-exist") is None


def test_question_for_provider_field_reverse_lookup():
    # first_name -> howoge firstName
    q = question_for_provider_field("howoge", "firstName")
    assert q is not None and q.key == "first_name"
    # unknown field
    assert question_for_provider_field("howoge", "nope") is None


def test_grouped_covers_all_visible_questions_in_order():
    visible = questionnaire_questions()
    flattened = [q for _, _, qs in grouped() for q in qs]
    assert [q.key for q in flattened] == [q.key for q in visible]
    # group keys are unique and non-empty labels
    group_keys = [gk for gk, _, _ in grouped()]
    assert len(group_keys) == len(set(group_keys))
    assert all(label for _, label, _ in grouped())


def test_catalog_as_dicts_roundtrip_shape():
    dicts = catalog_as_dicts()
    assert len(dicts) == len(CATALOG)
    for d, q in zip(dicts, CATALOG):
        assert d["key"] == q.key
        assert set(d) == {
            "key", "label", "type", "group", "options", "required",
            "help", "applicant_field", "providers", "managed",
        }


class _Applicant:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_resolve_answer_prefers_applicant_then_form_answers():
    applicant = _Applicant(first_name="Ada", message="Hi")
    q_first = question("first_name")
    q_income = question("net_income")
    assert resolve_answer(q_first, applicant, {}) == "Ada"
    # form_answers-backed question falls back to the form_answers dict
    assert resolve_answer(q_income, applicant, {"net_income": "2500"}) == "2500"
    # missing -> empty string
    assert resolve_answer(q_income, applicant, {}) == ""


def test_values_for_provider_maps_field_names_to_values():
    applicant = _Applicant(
        first_name="Ada", last_name="Lovelace", email="ada@example.com",
        phone="", street="", house_number="", zip_code="", city="", message="",
    )
    form_answers = {
        "wbs_present": "ja",
        "privacy_consent": "1",
        "net_income": "2500",  # no provider mapping -> must NOT appear
    }
    wbm = values_for_provider("wbm", applicant, form_answers)
    # applicant-backed field mapped to WBM's field name
    assert wbm["vorname"] == "Ada"
    assert wbm["name"] == "Lovelace"
    # form_answers-backed WBS field mapped to its WBM field name
    assert wbm["wbsvorhanden"] == "ja"
    # empty answers are omitted
    assert "telefon" not in wbm
    # net_income has no provider mapping, so it never surfaces
    assert "net_income" not in wbm and "2500" not in wbm.values()

    howoge = values_for_provider("howoge", applicant, form_answers)
    assert howoge["firstName"] == "Ada"
    assert howoge["lastName"] == "Lovelace"


def test_values_for_provider_skips_excluded_fields():
    # No catalog question should map any provider to an excluded honeypot field.
    for q in CATALOG:
        for field_name in q.providers.values():
            assert field_name not in EXCLUDED_FIELDS


def test_unknown_provider_yields_empty_mapping():
    applicant = _Applicant(first_name="Ada")
    assert values_for_provider("nonexistent", applicant, {}) == {}
