"""Text embeddings: the real bge-small model and a deterministic stand-in for tests.

bge-small-en-v1.5 is a 33M-parameter BERT encoder. Turning text into a vector takes
four visible steps here instead of one library call:

1. the WordPiece tokenizer turns text into token IDs (at most 512 per text);
2. the ONNX model returns one 384-number vector per token;
3. the vector of the first token ([CLS]) represents the whole text, as the model's
   authors recommend for this model;
4. the vector is scaled to length 1, so a dot product between two vectors is their
   cosine similarity.

Queries get an instruction prefix that the model was trained with for short-question
to passage retrieval. Passages get none.
"""

import hashlib
import re
from typing import Protocol

import numpy as np

MODEL_ID = "BAAI/bge-small-en-v1.5"
# Pinned commit of the model repository, so every run embeds with identical weights.
MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
MAX_TOKENS = 512


class Embedder(Protocol):
    name: str

    def embed_passages(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-12)


class BgeSmallEmbedder:
    """bge-small through onnxruntime; downloads about 134 MB on first use."""

    def __init__(self, *, use_instruction: bool = True, batch_size: int = 16):
        import onnxruntime
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        model = hf_hub_download(MODEL_ID, "onnx/model.onnx", revision=MODEL_REVISION, token=False)
        vocab = hf_hub_download(MODEL_ID, "tokenizer.json", revision=MODEL_REVISION, token=False)
        self.tokenizer = Tokenizer.from_file(vocab)
        self.tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 4
        self.session = onnxruntime.InferenceSession(
            model, options, providers=["CPUExecutionProvider"]
        )
        self.input_names = {item.name for item in self.session.get_inputs()}
        self.use_instruction = use_instruction
        self.batch_size = batch_size
        suffix = "" if use_instruction else "-noinstruction"
        self.name = f"{MODEL_ID}@{MODEL_REVISION[:12]}{suffix}"

    def _encode(self, texts: list[str]) -> np.ndarray:
        vectors = []
        for start in range(0, len(texts), self.batch_size):
            batch = self.tokenizer.encode_batch(texts[start : start + self.batch_size])
            feeds = {
                "input_ids": np.array([e.ids for e in batch], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in batch], dtype=np.int64),
                "token_type_ids": np.array([e.type_ids for e in batch], dtype=np.int64),
            }
            feeds = {name: value for name, value in feeds.items() if name in self.input_names}
            token_vectors = self.session.run(None, feeds)[0]  # (batch, tokens, 384)
            vectors.append(token_vectors[:, 0, :])  # [CLS]
        return _normalize(np.concatenate(vectors).astype(np.float32))

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts)

    def embed_query(self, text: str) -> np.ndarray:
        prefix = QUERY_INSTRUCTION if self.use_instruction else ""
        return self._encode([prefix + text])[0]


class HashingEmbedder:
    """Bag-of-words feature hashing. Deterministic, needs no download; for tests only.

    It has no notion of meaning, so it must never be used to report retrieval quality.
    """

    name = "hashing-512"

    def __init__(self, dimensions: int = 512):
        self.dimensions = dimensions

    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dimensions, dtype=np.float32)
        for token in re.findall(r"[a-z0-9_]+", text.lower()):
            bucket = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest())
            vector[bucket % self.dimensions] += 1.0
        return vector

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return _normalize(np.stack([self._vector(t) for t in texts]))

    def embed_query(self, text: str) -> np.ndarray:
        return _normalize(self._vector(text)[None, :])[0]
