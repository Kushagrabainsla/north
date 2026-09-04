"""Run a sentence-transformer encoder from its ONNX export, with no framework.

This is deliberately not `fastembed` or `sentence-transformers`. fastembed
depends on loguru, and CODING_STYLE §22 bans third-party logging frameworks -
the Ledger is the log - and it also drags in pillow, tqdm, mmh3 and a stemmer
for features north does not use. sentence-transformers wants torch, which is
about two gigabytes to do arithmetic north can do with numpy.

What an encoder actually needs is small enough to own: tokenize, one forward
pass, pool, normalize. That is the whole file. The dependencies left are
onnxruntime (the forward pass), tokenizers (the same Rust tokenizer the model
was trained with) and huggingface-hub (fetch the weights once, cache on disk).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Sequences longer than this are truncated. The encoder's own limit is 512; the
# texts north embeds - a fact, a tool description, a code chunk - are far shorter,
# and a smaller window keeps the quadratic attention cost down on batches.
_MAX_TOKENS = 512


class OnnxTextEncoder:
    """A BERT-family encoder served from its ONNX export.

    Construction downloads the weights on first use (cached on disk afterwards)
    and raises if that fails, so the caller can degrade to keyword search.
    """

    def __init__(self, model_id: str, onnx_path: str = "onnx/model.onnx") -> None:
        import onnxruntime
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        self._model_id = model_id
        tokenizer_file = hf_hub_download(model_id, "tokenizer.json")
        weights = hf_hub_download(model_id, onnx_path)

        self._tokenizer = Tokenizer.from_file(tokenizer_file)
        self._tokenizer.enable_truncation(max_length=_MAX_TOKENS)
        self._tokenizer.enable_padding()

        options = onnxruntime.SessionOptions()
        # north already runs embedding calls in a worker thread and may have
        # several in flight; letting onnxruntime spawn its own pool per session
        # on top of that oversubscribes the CPU rather than using it.
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3  # warnings and worse only
        self._session = onnxruntime.InferenceSession(
            weights, sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self._session.get_inputs()}

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts*, returning one L2-normalised vector each, in order."""
        import numpy as np

        if not texts:
            return []
        encodings = self._tokenizer.encode_batch(texts)
        feed: dict[str, Any] = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
        }
        # BERT exports take token_type_ids; a model exported without them (some
        # RoBERTa-family encoders) rejects the key outright, so it is only sent
        # when the graph declares it.
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.array([e.type_ids for e in encodings], dtype=np.int64)

        hidden = self._session.run(None, feed)[0]
        # CLS pooling: bge takes the first token's hidden state as the sentence
        # vector (1_Pooling/config.json sets pooling_mode_cls_token). Mean-pooling
        # it instead silently produces a worse embedding, not an error.
        pooled = hidden[:, 0]
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        # A zero vector cannot be normalised; leave it as zeros rather than
        # dividing, and let cosine_similarity report 0.0 against it as it does.
        pooled = np.divide(pooled, norms, out=np.zeros_like(pooled), where=norms != 0)
        return pooled.astype(float).tolist()
