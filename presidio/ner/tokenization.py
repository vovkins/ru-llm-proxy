"""The verified tokenizer's content-only interface, without Transformers."""

import json
from pathlib import Path

from tokenizers import Tokenizer


class ContentTokenizer:
    is_fast = True

    def __init__(self, directory):
        directory = Path(directory)
        self.backend = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.backend.no_padding()
        self.backend.no_truncation()
        tokens = json.loads((directory / "special_tokens_map.json").read_text())
        ids = {}
        for name in ("cls_token", "sep_token", "pad_token"):
            value = tokens[name]
            value = value["content"] if isinstance(value, dict) else value
            ids[name] = self.backend.token_to_id(value)
            if ids[name] is None:
                raise ValueError("Missing tokenizer special token: " + name)
        self.cls_token_id = ids["cls_token"]
        self.sep_token_id = ids["sep_token"]
        self.pad_token_id = ids["pad_token"]
        if self.backend.encode("").ids != [self.cls_token_id, self.sep_token_id]:
            raise ValueError("Tokenizer special-token contract differs")

    def __len__(self):
        return self.backend.get_vocab_size()

    def __call__(self, text, *, add_special_tokens=False, padding=False,
                 return_attention_mask=False, return_offsets_mapping=True,
                 truncation=False):
        if add_special_tokens or padding or truncation or return_attention_mask or not return_offsets_mapping:
            raise ValueError("Only complete content tokenization is supported")
        encoded = self.backend.encode(text, add_special_tokens=False)
        return {"input_ids": encoded.ids, "offset_mapping": encoded.offsets}

    def build_inputs_with_special_tokens(self, input_ids):
        return [self.cls_token_id, *input_ids, self.sep_token_id]

    def create_token_type_ids_from_sequences(self, input_ids):
        return [0] * (len(input_ids) + 2)
