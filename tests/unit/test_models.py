"""Properties of the domain models that are load-bearing at generation time.

`Finding` is not only a domain type — it is the structured-output schema sent to
the API, and the model fills it in *field order*. That makes the declaration order
in `models.py` a behavioural property rather than a stylistic one, which is the
sort of thing a well-meaning refactor reorders without a second thought.
"""

from __future__ import annotations

from reviewhive.models import Finding


class TestFindingFieldOrder:
    """Order matters because generation is autoregressive.

    Measured, not assumed: a live security call emitted its fields in exactly the
    declared order, and with `severity` declared third it was committed while only
    `file` and `line` existed. The body generated afterwards then rationalised an
    already-chosen severity instead of informing it — which is how a finding whose
    own body said "`json.dumps(...)` already does this" was filed at high severity.

    `confidence` never had the problem, because it was always declared last. That
    asymmetry is the evidence: same call, same model, different position.
    """

    def test_severity_is_emitted_after_the_evidence(self) -> None:
        order = list(Finding.model_fields)

        assert order.index("body") < order.index("severity")
        assert order.index("title") < order.index("severity")

    def test_confidence_is_emitted_after_the_evidence(self) -> None:
        """Already true, and pinned so a reorder cannot quietly undo it."""
        order = list(Finding.model_fields)

        assert order.index("body") < order.index("confidence")

    def test_the_wire_schema_preserves_the_declaration_order(self) -> None:
        """The property only holds if the JSON schema carries it to the API.

        Pydantic emits `properties` in declaration order today. This asserts the
        link rather than trusting it, because the whole fix rests on it.
        """
        properties = list(Finding.model_json_schema()["properties"])

        assert properties == list(Finding.model_fields)
        assert properties.index("body") < properties.index("severity")
