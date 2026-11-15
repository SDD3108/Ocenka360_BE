"""Small, bounded form language. No eval, regex-from-admin or executable conditions."""

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

LANGUAGES = {"ru", "kk", "en"}
TYPES = {"text", "textarea", "integer", "decimal", "date", "choice", "boolean"}
KEY = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
DECIMAL = re.compile(r"^-?[0-9]{1,12}(?:\.[0-9]{1,4})?$")


def require(test, message):
    if not test:
        raise ValidationError(message)


def translations(value, limit=4000):
    require(isinstance(value, dict) and set(value) == LANGUAGES, "Provide ru, kk and en translations.")
    require(
        all(isinstance(v, str) and 0 < len(v.strip()) <= limit for v in value.values()),
        f"Translations must be non-empty strings up to {limit} characters.",
    )


def condition(value, fields):
    require(
        isinstance(value, dict) and set(value) == {"field", "equals"}, "Condition needs field and equals."
    )
    parent = fields.get(value["field"]) if isinstance(value["field"], str) else None
    require(
        parent is not None and parent["type"] in {"choice", "boolean"},
        "Condition must reference an earlier choice/boolean field.",
    )
    if parent["type"] == "choice":
        require(
            isinstance(value["equals"], str) and value["equals"] in {o["value"] for o in parent["options"]},
            "Condition references an unknown choice.",
        )
    else:
        require(type(value["equals"]) is bool, "Boolean condition requires true or false.")


def validate_definition(value):
    require(
        isinstance(value, dict) and set(value) == {"fields", "documents", "notice"},
        "Definition must contain fields, documents and notice only.",
    )
    try:
        require(
            len(json.dumps(value, ensure_ascii=False, allow_nan=False)) <= 100000,
            "Form definition is too large.",
        )
    except (ValueError, TypeError):
        raise ValidationError("Definition must contain JSON values.") from None
    translations(value["notice"])
    require(isinstance(value["fields"], list) and 1 <= len(value["fields"]) <= 50, "Use 1 to 50 fields.")
    fields = {}
    for field in value["fields"]:
        require(isinstance(field, dict), "Each field must be an object.")
        require({"key", "type", "label", "required"} <= set(field), "Field is missing required properties.")
        require(
            set(field)
            <= {
                "key",
                "type",
                "label",
                "required",
                "help",
                "options",
                "min",
                "max",
                "max_length",
                "visible_if",
            },
            "Unknown field property.",
        )
        key = field["key"]
        require(
            isinstance(key, str) and KEY.fullmatch(key) and key not in fields,
            "Invalid or duplicate field key.",
        )
        require(isinstance(field["type"], str) and field["type"] in TYPES, "Unknown field type.")
        require(type(field["required"]) is bool, "required must be boolean.")
        translations(field["label"], 200)
        if "help" in field:
            translations(field["help"])
        if field["type"] == "choice":
            options = field.get("options")
            require(isinstance(options, list) and 1 <= len(options) <= 30, "Choice requires 1 to 30 options.")
            seen = set()
            for option in options:
                require(isinstance(option, dict) and set(option) == {"value", "label"}, "Invalid option.")
                v = option["value"]
                require(
                    isinstance(v, str) and KEY.fullmatch(v) and v not in seen,
                    "Invalid or duplicate option value.",
                )
                translations(option["label"], 200)
                seen.add(v)
        else:
            require("options" not in field, "options is only allowed for choice.")
        if "max_length" in field:
            require(
                field["type"] in {"text", "textarea"}
                and type(field["max_length"]) is int
                and 1 <= field["max_length"] <= 4000,
                "Invalid max_length.",
            )
        for bound in ("min", "max"):
            if bound in field:
                require(field["type"] in {"integer", "decimal"}, "Bounds only apply to numbers.")
                bound_value = field[bound]
                valid = type(bound_value) is int and abs(bound_value) <= 999999999999
                if field["type"] == "decimal":
                    valid = valid or (isinstance(bound_value, str) and bool(DECIMAL.fullmatch(bound_value)))
                require(valid, "Bounds must be integers or, for decimal fields, decimal strings.")
        require(
            Decimal(field.get("min", -999999999999)) <= Decimal(field.get("max", 999999999999)),
            "min exceeds max.",
        )
        if "visible_if" in field:
            condition(field["visible_if"], fields)
        fields[key] = field
    applicant = fields.get("applicant_type", {})
    require(
        applicant.get("type") == "choice"
        and applicant.get("required") is True
        and "visible_if" not in applicant
        and {o["value"] for o in applicant.get("options", [])} == {"individual", "company"},
        "applicant_type must be an unconditional required individual/company choice.",
    )
    require(
        isinstance(value["documents"], list) and len(value["documents"]) <= 30, "Use at most 30 documents."
    )
    seen = set()
    for doc in value["documents"]:
        require(
            isinstance(doc, dict)
            and {"key", "title", "help", "required"} <= set(doc)
            and set(doc) <= {"key", "title", "help", "required", "when"},
            "Invalid document definition.",
        )
        require(
            isinstance(doc["key"], str) and KEY.fullmatch(doc["key"]) and doc["key"] not in seen,
            "Invalid or duplicate document key.",
        )
        seen.add(doc["key"])
        translations(doc["title"], 200)
        translations(doc["help"])
        require(type(doc["required"]) is bool, "Document required must be boolean.")
        if "when" in doc:
            condition(doc["when"], fields)


def matches(rule, valid_answers):
    return rule is None or (rule["field"] in valid_answers and valid_answers[rule["field"]] == rule["equals"])


def evaluate(definition, answers):
    """Shared by the preview API and future server-side order creation; never persists input."""
    errors = {}
    cleaned = {}
    visible = []
    keys = {f["key"] for f in definition["fields"]}
    for key in answers.keys() - keys:
        errors[key] = "unknown_field"
    for field in definition["fields"]:
        key = field["key"]
        if not matches(field.get("visible_if"), cleaned):
            if key in answers:
                errors[key] = "field_not_applicable"
            continue
        visible.append(key)
        if key not in answers or answers[key] is None or answers[key] == "":
            if field["required"]:
                errors[key] = "required"
            continue
        v = answers[key]
        kind = field["type"]
        error = None
        if kind in {"text", "textarea"}:
            if not isinstance(v, str):
                error = "invalid_type"
            else:
                v = v.strip()
                if not v:
                    error = "required" if field["required"] else None
                elif len(v) > field.get("max_length", 1000):
                    error = "too_long"
        elif kind == "choice":
            if not isinstance(v, str) or v not in {o["value"] for o in field["options"]}:
                error = "invalid_choice"
        elif kind == "boolean":
            if type(v) is not bool:
                error = "invalid_type"
        elif kind == "integer":
            if type(v) is not int:
                error = "invalid_type"
            elif not field.get("min", -999999999999) <= v <= field.get("max", 999999999999):
                error = "out_of_range"
        elif kind == "decimal":
            if not isinstance(v, str) or not DECIMAL.fullmatch(v):
                error = "invalid_decimal"
            else:
                try:
                    number = Decimal(v)
                    if (
                        not Decimal(field.get("min", -999999999999))
                        <= number
                        <= Decimal(field.get("max", 999999999999))
                    ):
                        error = "out_of_range"
                except InvalidOperation:
                    error = "invalid_decimal"
        elif kind == "date":
            if not isinstance(v, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v):
                error = "invalid_date"
            else:
                try:
                    date.fromisoformat(v)
                except ValueError:
                    error = "invalid_date"
        if error:
            errors[key] = error
        elif v != "":
            cleaned[key] = v
    documents = [doc for doc in definition["documents"] if matches(doc.get("when"), cleaned)]
    return {
        "valid": not errors,
        "errors": errors,
        "visible_fields": visible,
        "documents": documents,
        "cleaned_answers": cleaned,
    }


def localized_definition(definition, language):
    # Text stays plain text: clients must never render it as HTML.
    result = {"notice": definition["notice"][language], "fields": [], "documents": []}
    for field in definition["fields"]:
        item = {**field, "label": field["label"][language]}
        if "help" in field:
            item["help"] = field["help"][language]
        if "options" in field:
            item["options"] = [{**o, "label": o["label"][language]} for o in field["options"]]
        result["fields"].append(item)
    result["documents"] = [localize_document(doc, language) for doc in definition["documents"]]
    return result


def localize_document(doc, language):
    return {**doc, "title": doc["title"][language], "help": doc["help"][language]}
