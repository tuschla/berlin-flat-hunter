"""The unified application questionnaire.

The Genossenschaften ask overlapping-but-different questions on their application
forms (scraped live: Howoge = 3 fields, WBM = 16 incl. WBS/income, Gewobag =
wohnungshelden SPA). Rather than make the user answer each provider's form, we
merge every discovered question into ONE canonical, de-duplicated catalog. Each
canonical ``Question`` records which provider field(s) it maps to, so the
applicator can fill any provider's form from a single set of answers.

Questions backed by an ``applicant_field`` reuse the data already in the Profile
form (name, email, address, message). The rest — mostly WBS/income questions and
consent — are stored in ``form_answers`` keyed by the question ``key``.

This catalog is the *fixed* questionnaire generated from the live scrape. To
regenerate it (e.g. after a site changes, or to fill in Gewobag's browser-only
fields), see ``scripts/discover_forms.py``.

Pure stdlib (dataclasses only) — safe to import from anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Providers whose forms feed this catalog (the auto-send Genossenschaften).
CATALOG_PROVIDERS = ("howoge", "wbm", "gewobag")


@dataclass
class Question:
    key: str                       # canonical, stable id (also the form_answers key)
    label: str                     # German label shown to the user
    type: str                      # text|email|tel|textarea|select|bool|date|number
    group: str                     # personal|address|message|wbs|consent
    options: list[str] = field(default_factory=list)
    required: bool = False
    help: str = ""
    # If set, the answer comes from the applicant profile's <applicant_field>
    # rather than form_answers (so we never duplicate the basics).
    applicant_field: str = ""
    # provider -> the form field name on that provider's form. A leading "@" marks
    # a special handling token the sender interprets (e.g. consent checkbox).
    providers: dict[str, str] = field(default_factory=dict)
    # Managed elsewhere (e.g. email is set per-provider on the Emails page), so it
    # is NOT shown in the questionnaire — but still mapped for form filling.
    managed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "type": self.type,
            "group": self.group, "options": list(self.options),
            "required": self.required, "help": self.help,
            "applicant_field": self.applicant_field, "providers": dict(self.providers),
            "managed": self.managed,
        }


# --------------------------------------------------------------------------- #
# The merged catalog. Order = display order.
# --------------------------------------------------------------------------- #
CATALOG: list[Question] = [
    # ---- personal -------------------------------------------------------- #
    Question(
        key="salutation", label="Anrede", type="select", group="personal",
        options=["Frau", "Herr", "Offen"],
        help="Nur WBM fragt die Anrede ab.",
        providers={"wbm": "anrede"},
    ),
    Question(
        key="first_name", label="Vorname", type="text", group="personal",
        required=True, applicant_field="first_name",
        providers={"howoge": "firstName", "wbm": "vorname", "gewobag": "firstName"},
    ),
    Question(
        key="last_name", label="Nachname", type="text", group="personal",
        required=True, applicant_field="last_name",
        providers={"howoge": "lastName", "wbm": "name", "gewobag": "lastName"},
    ),
    Question(
        key="email", label="E-Mail", type="email", group="personal",
        required=True, applicant_field="email", managed=True,
        help="Set on the Emails page (real address + per-landlord addy.io aliases).",
        providers={"howoge": "email", "wbm": "e_mail", "gewobag": "email"},
    ),
    Question(
        key="phone", label="Telefon", type="tel", group="personal",
        applicant_field="phone",
        providers={"wbm": "telefon", "gewobag": "phoneNumber"},
    ),
    # ---- address --------------------------------------------------------- #
    Question(
        key="street", label="Straße", type="text", group="address",
        applicant_field="street",
        providers={"wbm": "strasse", "gewobag": "street"},
    ),
    Question(
        key="house_number", label="Hausnummer", type="text", group="address",
        applicant_field="house_number",
        providers={"gewobag": "houseNumber"},
    ),
    Question(
        key="zip_code", label="PLZ", type="text", group="address",
        applicant_field="zip_code",
        providers={"wbm": "plz", "gewobag": "zipCode"},
    ),
    Question(
        key="city", label="Ort", type="text", group="address",
        applicant_field="city",
        providers={"wbm": "ort", "gewobag": "city"},
    ),
    Question(
        key="address_extra", label="Adresszusatz", type="text", group="address",
        help="Optionaler Zusatz (z. B. c/o, Wohnung Nr.). Nur Gewobag fragt ihn ab.",
        providers={"gewobag": "additionalAddressInformation"},
    ),
    # ---- household & income --------------------------------------------- #
    Question(
        key="household_size", label="Einziehende Personen (gesamt)", type="number",
        group="household",
        help="Gesamtzahl der einziehenden Personen (Erwachsene + Kinder). Gewobag "
             "fragt das ab; nützlich für die Bewerbung.",
    ),
    Question(
        key="adults", label="davon Erwachsene", type="number", group="household",
    ),
    Question(
        key="children", label="davon Kinder", type="number", group="household",
    ),
    Question(
        key="net_income", label="Netto-Haushaltseinkommen (€/Monat)", type="number",
        group="household",
        help="Monatliches Nettoeinkommen aller einziehenden Personen zusammen. Viele "
             "Genossenschaften/Vermieter fragen danach (Bonität / Mietobergrenze).",
    ),
    Question(
        key="employment", label="Beschäftigungsverhältnis", type="select",
        group="household",
        options=["", "unbefristet angestellt", "befristet angestellt", "selbstständig",
                 "verbeamtet", "in Ausbildung/Studium", "Rente/Pension", "sonstiges"],
        help="Art deines Arbeitsverhältnisses (oft im Bewerbungsbogen gefragt).",
    ),
    Question(
        key="schufa_ok", label="SCHUFA / Bonität in Ordnung", type="bool",
        group="household",
        help="Bestätigung, dass keine negativen SCHUFA-Einträge vorliegen.",
    ),
    # ---- message --------------------------------------------------------- #
    Question(
        key="message", label="Anschreiben (Basis)", type="textarea", group="message",
        applicant_field="message",
        help="Basis-Text deiner Bewerbung. Wird pro Inserat von Claude personalisiert "
             "(falls aktiviert) und in das Nachrichtenfeld eingetragen.",
        providers={"gewobag": "applicantMessage"},
    ),
    # ---- WBS / income (mostly WBM) --------------------------------------- #
    Question(
        key="wbs_present", label="WBS vorhanden?", type="select", group="wbs",
        options=["", "ja", "nein"],
        help="Wohnberechtigungsschein vorhanden? Viele WBM-Wohnungen sind WBS-gebunden.",
        providers={"wbm": "wbsvorhanden"},
    ),
    Question(
        key="wbs_valid_until", label="WBS gültig bis", type="date", group="wbs",
        providers={"wbm": "wbsgueltigbis"},
    ),
    Question(
        key="wbs_rooms", label="WBS Zimmeranzahl", type="select", group="wbs",
        options=["", "1", "2", "3", "4", "5", "6", "7"],
        providers={"wbm": "wbszimmeranzahl"},
    ),
    Question(
        key="wbs_income_limit", label="Einkommensgrenze (§9)", type="select", group="wbs",
        options=["", "WBS 100", "WBS 140", "WBS 160", "WBS 180", "WBS 220"],
        help="Einkommensgrenze nach Einkommensbescheinigung § 9.",
        providers={"wbm": "einkommensgrenzenacheinkommensbescheinigung9"},
    ),
    Question(
        key="wbs_special_need", label="WBS mit besonderem Wohnbedarf", type="bool", group="wbs",
        providers={"wbm": "wbsmitbesonderemwohnbedarf"},
    ),
    # ---- consent --------------------------------------------------------- #
    Question(
        key="privacy_consent", label="Datenschutz akzeptiert", type="bool", group="consent",
        required=True,
        help="Bestätigt die Datenschutzhinweise des Anbieters. Für das Absenden nötig.",
        providers={"wbm": "datenschutzhinweis", "gewobag": "@privacy", "howoge": "@implicit"},
    ),
]

# honeypot / anti-spam fields we deliberately NEVER expose or fill.
EXCLUDED_FIELDS = {"__hp", "honeypot"}

GROUP_LABELS = {
    "personal": "Persönliche Angaben",
    "address": "Adresse",
    "household": "Haushalt & Einkommen",
    "message": "Anschreiben",
    "wbs": "WBS",
    "consent": "Einwilligung",
}

_BY_KEY = {q.key: q for q in CATALOG}


def question(key: str) -> Question | None:
    return _BY_KEY.get(key)


def extra_questions() -> list[Question]:
    """Questions NOT backed by the applicant (i.e. stored in form_answers)."""
    return [q for q in CATALOG if not q.applicant_field and not q.managed]


def questionnaire_questions() -> list[Question]:
    """Questions shown in the questionnaire UI (managed ones are hidden)."""
    return [q for q in CATALOG if not q.managed]


def grouped() -> list[tuple[str, str, list[Question]]]:
    """(group_key, group_label, questions) in display order, for rendering.

    Managed questions (e.g. email, set on the Emails page) are excluded.
    """
    visible = questionnaire_questions()
    out: list[tuple[str, str, list[Question]]] = []
    seen: list[str] = []
    for q in visible:
        if q.group not in seen:
            seen.append(q.group)
            out.append((q.group, GROUP_LABELS.get(q.group, q.group),
                        [x for x in visible if x.group == q.group]))
    return out


def catalog_as_dicts() -> list[dict[str, Any]]:
    return [q.to_dict() for q in CATALOG]


def resolve_answer(q: Question, applicant: Any, form_answers: dict[str, str]) -> str:
    """The user's value for a question (from applicant or form_answers)."""
    if q.applicant_field:
        return str(getattr(applicant, q.applicant_field, "") or "")
    return str(form_answers.get(q.key, "") or "")


def question_for_provider_field(provider: str, field_name: str) -> Question | None:
    """Reverse lookup: which catalog Question maps to this provider form field."""
    for q in CATALOG:
        if q.providers.get(provider) == field_name:
            return q
    return None


def values_for_provider(
    provider: str, applicant: Any, form_answers: dict[str, str]
) -> dict[str, str]:
    """Map a provider's form field names -> the value to fill.

    Returns ``{field_name: value}`` for every catalog question that (a) targets
    this provider and (b) has a non-empty answer. The applicator uses this to
    fill *all* discovered fields, not just the hardcoded core.
    """
    out: dict[str, str] = {}
    for q in CATALOG:
        field_name = q.providers.get(provider)
        if not field_name or field_name in EXCLUDED_FIELDS:
            continue
        value = resolve_answer(q, applicant, form_answers)
        if value:
            out[field_name] = value
    return out
