"""Compare diagnostics against the production serializer, not raw NER data."""
import json


ENTITY_FIELDS = frozenset({"start", "end", "entity_type", "score"})


def matches_serialized_entities(serialized, cached):
    """The reference is a production-serialized entry; retain no source values."""
    if not isinstance(serialized, str) or not isinstance(cached, list):
        return False
    try:
        expected = json.loads(serialized)["entities"]
    except (ValueError, TypeError, KeyError):
        return False
    return (isinstance(expected, list) and cached == expected
            and all(isinstance(item, dict) and set(item) == ENTITY_FIELDS for item in cached))
