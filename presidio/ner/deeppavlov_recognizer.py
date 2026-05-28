"""DeepPavlov NER recognizer for Presidio.

Uses DeepPavlov ner_rus_bert model for Russian NER.
Entity mapping: PER → PERSON, LOC → LOCATION, ORG → ORGANIZATION
"""

import logging
from typing import List, Optional

from presidio_analyzer import RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

logger = logging.getLogger(__name__)

# Mapping from DeepPavlov BIO tags to Presidio entity types
DEEPPAVLOV_ENTITY_MAP = {
    "PER": "PERSON",
    "PERSON": "PERSON",
    "LOC": "LOCATION",
    "LOCATION": "LOCATION",
    "ORG": "ORGANIZATION",
    "ORGANIZATION": "ORGANIZATION",
}


def _merge_bio_tags(tokens: list[str], tags: list[str]) -> list[dict]:
    """Merge BIO-tagged tokens into entity spans.

    Args:
        tokens: List of tokens from DeepPavlov
        tags: List of BIO tags (B-PER, I-PER, O, etc.)

    Returns:
        List of dicts with keys: text, entity_type, start, end
    """
    entities = []
    current_entity = None
    current_text_parts = []

    for token, tag in zip(tokens, tags):
        if tag == "O":
            if current_entity:
                entities.append(current_entity)
                current_entity = None
                current_text_parts = []
            continue

        # Parse BIO tag
        if tag.startswith("B-"):
            if current_entity:
                entities.append(current_entity)
            bio_prefix, entity_tag = tag.split("-", 1)
            current_text_parts = [token]
            current_entity = {
                "entity_tag": entity_tag,
                "text_parts": [token],
            }
        elif tag.startswith("I-") and current_entity:
            _, entity_tag = tag.split("-", 1)
            if entity_tag == current_entity["entity_tag"]:
                current_entity["text_parts"].append(token)
            else:
                # New entity type in I-tag = treat as B
                entities.append(current_entity)
                current_entity = {
                    "entity_tag": entity_tag,
                    "text_parts": [token],
                }
        else:
            if current_entity:
                entities.append(current_entity)
            current_entity = None

    if current_entity:
        entities.append(current_entity)

    return entities


class DeepPavlovRecognizer:
    """NER recognizer using DeepPavlov ner_rus_bert model.

    This is NOT a standard Presidio PatternRecognizer.
    It's called directly by the analyzer_server to augment results.
    """

    def __init__(self):
        self._model = None

    def load_model(self):
        """Load the DeepPavlov model (lazy loading)."""
        if self._model is not None:
            return

        logger.info("Loading DeepPavlov ner_rus_bert model...")
        from deeppavlov import build_model, configs

        self._model = build_model(configs.ner.ner_rus_bert, download=False)
        logger.info("DeepPavlov model loaded successfully")

    def is_loaded(self) -> bool:
        """Check if model is loaded."""
        return self._model is not None

    def analyze(self, text: str, score_threshold: float = 0.7) -> List[RecognizerResult]:
        """Analyze text for NER entities.

        Args:
            text: Input text
            score_threshold: Minimum confidence score

        Returns:
            List of Presidio RecognizerResult objects
        """
        if not self._model:
            self.load_model()

        try:
            results_raw = self._model([text])
        except Exception as e:
            logger.error(f"DeepPavlov NER error: {e}")
            return []

        if not results_raw or len(results_raw) < 2:
            return []

        tokens_list = results_raw[0]
        tags_list = results_raw[1]

        if not tokens_list or not tags_list:
            return []

        tokens = tokens_list[0]
        tags = tags_list[0]

        # Merge BIO tags into entity spans
        entities = _merge_bio_tags(tokens, tags)

        # Convert to RecognizerResult
        presidio_results = []
        for entity in entities:
            entity_tag = entity["entity_tag"]
            entity_text = " ".join(entity["text_parts"])

            # Map to Presidio entity type
            presidio_type = DEEPPAVLOV_ENTITY_MAP.get(entity_tag)
            if not presidio_type:
                continue

            # Find position in original text
            start = text.find(entity_text)
            if start == -1:
                # Try without spaces (tokenization may differ)
                entity_text_no_space = "".join(entity["text_parts"])
                start = text.find(entity_text_no_space)
                if start == -1:
                    continue
                entity_text = entity_text_no_space

            end = start + len(entity_text)

            presidio_results.append(
                RecognizerResult(
                    entity_type=presidio_type,
                    start=start,
                    end=end,
                    score=score_threshold,
                    analysis_explanation=None,
                )
            )

        return presidio_results
