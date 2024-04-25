"""
Implementation for Phi architecture.
TODO: add docstring
"""
import dataclasses
from typing import Any, Dict, Optional, Union

from tvm import te, tir
from tvm.relax.frontend import nn
from tvm.relax.frontend.nn import Tensor, op
from tvm.relax.frontend.nn.op import (
    broadcast_to,
    concat,
    matmul,
    permute_dims,
    reshape,
    softmax,
    wrap_nested,
)
from mlc_llm import op as op_ext
from mlc_llm.support import logging
from mlc_llm.support import tensor_parallel as tp
from mlc_llm.support.config import ConfigBase
from mlc_llm.support.style import bold
from mlc_llm.nn import FlashInferPagedKVCache, PagedKVCache, RopeMode, TIRPagedKVCache
from .vit_model import ImpVisionConfig, SigLipVisionModel
from tvm.relax.op import arange, strided_slice
logger = logging.getLogger(__name__)

@dataclasses.dataclass
class Phi1Config(ConfigBase):  # pylint: disable=too-many-instance-attributes
    """Configuration of the Phi-1/Phi-1.5 model."""

    vocab_size: int = 51200
    hidden_size: int = 2048
    intermediate_size: int = 8192
    num_hidden_layers: int = 24
    num_attention_heads: int = 32
    layer_norm_eps: float = 1e-5
    position_embedding_base: int = 0
    partial_rotary_factor: float = 0.5
    num_key_value_heads: int = 0
    context_window_size: int = 0
    prefill_chunk_size: int = 0
    head_dim: int = 0
    tensor_parallel_shards: int = 1
    max_batch_size: int = 1
    kwargs: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        if self.position_embedding_base == 0:
            if "rope_theta" in self.kwargs:
                self.position_embedding_base = self.kwargs.pop("rope_theta")
            else:
                self.position_embedding_base = 10000
        if self.context_window_size == 0:
            for name in ["max_position_embeddings", "max_sequence_length"]:
                if name in self.kwargs:
                    self.context_window_size = self.kwargs.pop(name)
                    logger.info(
                        "%s not found in config.json. Falling back to %s (%d)",
                        bold("context_window_size"),
                        bold(name),
                        self.context_window_size,
                    )
                    break
            else:
                raise ValueError(
                    "Unable to determine the maxmimum sequence length, because none of "
                    "`context_window_size`, `max_position_embeddings` or `max_sequence_length` is "
                    "provided in `config.json`."
                )
        if self.prefill_chunk_size == 0:
            self.prefill_chunk_size = self.context_window_size
        if self.prefill_chunk_size > self.context_window_size:
            self.prefill_chunk_size = self.context_window_size
        if self.num_key_value_heads == 0 or self.num_key_value_heads is None:
            self.num_key_value_heads = self.num_attention_heads
        if self.intermediate_size == 0 or self.intermediate_size is None:
            self.intermediate_size = 4 * self.hidden_size
        if self.head_dim == 0:
            self.head_dim = self.hidden_size // self.num_attention_heads
        assert self.head_dim * self.num_attention_heads == self.hidden_size
        assert self.num_attention_heads % self.num_key_value_heads == 0


@dataclasses.dataclass
class PhiConfig(ConfigBase):  # pylint: disable=too-many-instance-attributes
    """Configuration of the Phi-2 model."""

    model_type: str  # "phi", "phi-msft", "mixformer-sequential"
    vocab_size: int = 51200
    n_positions: int = 3072
    n_embd: int = 2560
    n_layer: int = 32
    n_inner: int = 0
    n_head: int = 32
    rotary_dim: int = 32
    position_embedding_base: int = 0
    layer_norm_epsilon: float = 1e-5
    context_window_size: int = 3072
    prefill_chunk_size: int = 3072
    n_head_kv: int = 0
    head_dim: int = 0
    tensor_parallel_shards: int = 1
    image_token_index: int = 50296
    image_token: str = "<image>"
    dtype: str = "float16"
    kwargs: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        if self.position_embedding_base == 0:
            if "rope_theta" in self.kwargs:
                self.position_embedding_base = self.kwargs.pop("rope_theta")
            else:
                self.position_embedding_base = 10000
        if self.context_window_size == 0:
            for name in ["max_position_embeddings", "max_sequence_length"]:
                if name in self.kwargs:
                    self.context_window_size = self.kwargs.pop(name)
                    logger.info(
                        "%s not found in config.json. Falling back to %s (%d)",
                        bold("context_window_size"),
                        bold(name),
                        self.context_window_size,
                    )
                    break
            else:
                self.context_window_size = self.n_positions
                logger.info(
                    "%s not found in config.json. Falling back to %s (%d)",
                    bold("context_window_size"),
                    "n_positions",
                    self.context_window_size,
                )
        if self.prefill_chunk_size == 0:
            self.prefill_chunk_size = self.context_window_size
        if self.prefill_chunk_size > self.context_window_size:
            self.prefill_chunk_size = self.context_window_size
        if self.n_head_kv == 0 or self.n_head_kv is None:
            self.n_head_kv = self.n_head
        if self.n_inner == 0 or self.n_inner is None:
            self.n_inner = 4 * self.n_embd
        if self.head_dim == 0:
            self.head_dim = self.n_embd // self.n_head
        assert self.head_dim * self.n_head == self.n_embd
        assert self.n_head % self.n_head_kv == 0

    @staticmethod
    def from_phi1(config: Phi1Config) -> "PhiConfig":
        "Build PhiConig from a Phi1Config."
        return PhiConfig(
            model_type="phi",
            vocab_size=config.vocab_size,
            n_positions=config.context_window_size,
            n_embd=config.hidden_size,
            n_layer=config.num_hidden_layers,
            n_inner=config.intermediate_size,
            n_head=config.num_attention_heads,
            rotary_dim=int(config.partial_rotary_factor * config.head_dim),
            position_embedding_base=config.position_embedding_base,
            layer_norm_epsilon=config.layer_norm_eps,
            context_window_size=config.context_window_size,
            prefill_chunk_size=config.prefill_chunk_size,
            n_head_kv=config.num_key_value_heads,
            head_dim=config.head_dim,
            tensor_parallel_shards=config.tensor_parallel_shards,
            kwargs=config.kwargs,
        )


# pylint: disable=invalid-name,missing-docstring


class PhiMLP(nn.Module):
    def __init__(self, config: PhiConfig):
        super().__init__()
        self.intermediate_size = config.n_inner // config.tensor_parallel_shards
        self.fc1 = nn.Linear(config.n_embd, self.intermediate_size)
        self.fc2 = nn.Linear(self.intermediate_size, config.n_embd)

    def forward(self, hidden_states: Tensor):
        hidden_states = self.fc1(hidden_states)
        hidden_states = op.gelu(hidden_states, approximate="tanh")
        hidden_states = self.fc2(hidden_states)

        return hidden_states


class PhiMHA(nn.Module):  # pylint: disable=too-many-instance-attributes
    def __init__(self, config: PhiConfig):
        self.num_q_heads = config.n_head // config.tensor_parallel_shards
        assert (
            config.n_head % config.tensor_parallel_shards == 0
        ), f"n_head({config.n_head}) must be divisible by tensor_parallel_shards"
        self.n_head_kv = config.n_head_kv // config.tensor_parallel_shards
        assert (
            config.n_head_kv % config.tensor_parallel_shards == 0
        ), f"n_head({config.n_head_kv}) must be divisible by tensor_parallel_shards"
        self.head_dim = config.head_dim
        op_size = self.head_dim * (self.num_q_heads + 2 * self.n_head_kv)
        hidden_size = config.n_embd

        self.Wqkv = nn.Linear(hidden_size, op_size, bias=True)
        self.out_proj = nn.Linear(self.num_q_heads * self.head_dim, hidden_size, bias=True)

    def forward(self, hidden_states: Tensor, paged_kv_cache: PagedKVCache, layer_id: int):
        d, h_q, h_kv = self.head_dim, self.num_q_heads, self.n_head_kv
        b, s, _ = hidden_states.shape
        # QKV Projection
        qkv = self.Wqkv(hidden_states)
        qkv = op.reshape(qkv, (b, s, h_q + h_kv + h_kv, d))
        # Attention
        output = op.reshape(
            paged_kv_cache.attention_with_fused_qkv(layer_id, qkv, self.num_q_heads),
            (b, s, h_q * d),
        )
        return self.out_proj(output)


class PhiParallelBlock(nn.Module):
    def __init__(self, config: PhiConfig):
        super().__init__()

        self.ln = nn.LayerNorm(config.n_embd, eps=config.layer_norm_epsilon)
        self.mixer = PhiMHA(config)
        self.mlp = PhiMLP(config)

        def _set_tp():
            def _set(param, hint):
                param.attrs["shard_strategy"] = hint

            hd = config.head_dim
            q = self.mixer.num_q_heads * hd
            k = self.mixer.n_head_kv * hd
            v = self.mixer.n_head_kv * hd
            _set(
                self.mixer.Wqkv.weight,
                tp.ShardSingleDim("_shard_qkv_weight", segs=[q, k, v], dim=0),
            )
            _set(
                self.mixer.Wqkv.bias,
                tp.ShardSingleDim("_shard_qkv_bias", segs=[q, k, v], dim=0),
            )
            _set(self.mixer.out_proj.weight, tp.ShardSingleDim("_shard_o_weight", dim=1))
            _set(self.mlp.fc1.weight, tp.ShardSingleDim("_shard_mlp_fc1_weight", dim=0))
            _set(self.mlp.fc1.bias, tp.ShardSingleDim("_shard_mlp_fc1_bias", dim=0))
            _set(self.mlp.fc2.weight, tp.ShardSingleDim("_shard_mlp_fc2_weight", dim=1))

        self.tensor_parallel_shards = config.tensor_parallel_shards
        _set_tp()

    def forward(self, hidden_states: Tensor, paged_kv_cache: PagedKVCache, layer_id: int):
        residual = hidden_states
        hidden_states = self.ln(hidden_states)

        with tp.shard_bias(self.mixer.out_proj, self.tensor_parallel_shards), tp.shard_bias(
            self.mlp.fc2, self.tensor_parallel_shards
        ):
            attn_outputs = self.mixer(hidden_states, paged_kv_cache, layer_id)
            feed_forward_hidden_states = self.mlp(hidden_states)

        hidden_states = self._apply_parallel_residual(
            attn_outputs, feed_forward_hidden_states, residual
        )

        return hidden_states

    def _apply_parallel_residual(self, attn_out, mlp_out, residual):
        if self.tensor_parallel_shards > 1:
            return op.ccl_allreduce(
                attn_out + mlp_out + residual / self.tensor_parallel_shards, "sum"
            )
        return attn_out + mlp_out + residual


class PhiCausalLMHead(nn.Module):
    def __init__(self, config: PhiConfig) -> None:
        super().__init__()

        self.ln = nn.LayerNorm(config.n_embd, eps=config.layer_norm_epsilon)
        self.linear = nn.Linear(config.n_embd, config.vocab_size)

    def forward(self, hidden_states: Tensor):
        hidden_states = self.ln(hidden_states)
        logits = self.linear(hidden_states)

        if logits.dtype != "float32":
            logits = logits.astype("float32")
        return logits


class PhiModel(nn.Module):
    def __init__(self, config: PhiConfig) -> None:
        super().__init__()
        self.embd = nn.Embedding(config.vocab_size, config.n_embd)
        self.h = nn.ModuleList([PhiParallelBlock(config) for _ in range(config.n_layer)])

    def forward(self, input_embed: Tensor, paged_kv_cache: PagedKVCache):
        hidden_states = input_embed
        for layer_id, layer in enumerate(self.h):
            hidden_states = layer(hidden_states, paged_kv_cache, layer_id)

        return hidden_states


class PhiForCausalLM(nn.Module):
    # pylint: disable=too-many-instance-attributes
    def __init__(self, config: Union[PhiConfig, Phi1Config]) -> None:
        super().__init__()

        if isinstance(config, Phi1Config):
            config = PhiConfig.from_phi1(config)

        self.transformer = PhiModel(config)
        self.lm_head = PhiCausalLMHead(config)
        self.num_hidden_layers = config.n_layer
        self.num_attention_heads = config.n_head
        self.num_key_value_heads = config.n_head_kv
        self.head_dim = config.head_dim
        self.hidden_size = config.n_embd
        self.vocab_size = config.vocab_size
        self.rope_theta = config.position_embedding_base
        self.tensor_parallel_shards = config.tensor_parallel_shards
        self.rotary_dim = config.rotary_dim
        self.dtype = "float32"

    def to(self, dtype: Optional[str] = None):
        super().to(dtype=dtype)
        if dtype is not None:
            self.dtype = dtype

    def batch_forward(
        self,
        input_embeds: Tensor,
        paged_kv_cache: PagedKVCache,
        logit_positions: Optional[Tensor] = None,
    ):
        op_ext.configure()

        hidden_states = self.transformer(input_embeds, paged_kv_cache)
        if logit_positions is not None:
            hidden_states = op.take(hidden_states, logit_positions, axis=1)
        lm_logits = self.lm_head(hidden_states)
        if lm_logits.dtype != "float32":
            lm_logits = lm_logits.astype("float32")
        return lm_logits

    def prefill(self, input_embed: Tensor, paged_kv_cache: PagedKVCache):
        op_ext.configure()

        def _index(x: te.Tensor):
            b, s, d = x.shape
            return te.compute((b, 1, d), lambda i, _, k: x[i, s - 1, k], name="index")

        hidden_states = self.transformer(input_embed, paged_kv_cache)
        hidden_states = op.tensor_expr_op(_index, name_hint="index", args=[hidden_states])
        logits = self.lm_head(hidden_states)

        if logits.dtype != "float32":
            logits = logits.astype("float32")

        return logits, paged_kv_cache

    def decode(self, input_embed: Tensor, paged_kv_cache: PagedKVCache):
        op_ext.configure()

        hidden_states = self.transformer(input_embed, paged_kv_cache)
        logits = self.lm_head(hidden_states)
        if logits.dtype != "float32":
            logits = logits.astype("float32")
        return logits, paged_kv_cache

    def batch_prefill(
        self, input_embeds: Tensor, logit_positions: Tensor, paged_kv_cache: PagedKVCache
    ):
        logits = self.batch_forward(input_embeds, paged_kv_cache, logit_positions)
        return logits, paged_kv_cache

    def batch_decode(self, input_embeds: Tensor, paged_kv_cache: PagedKVCache):
        logits = self.batch_forward(input_embeds, paged_kv_cache)
        return logits, paged_kv_cache

    def batch_verify(self, input_embeds: Tensor, paged_kv_cache: PagedKVCache):
        logits = self.batch_forward(input_embeds, paged_kv_cache)
        return logits, paged_kv_cache

    def softmax_with_temperature(self, logits: Tensor, temperature: Tensor):
        return op.softmax(logits / op.reshape(temperature, (temperature.shape[0], 1, 1)), axis=-1)

    def embed(self, input_ids: Tensor):
        if self.tensor_parallel_shards > 1:
            input_ids = op.ccl_broadcast_from_worker0(input_ids)
        embeds = self.transformer.embd(input_ids)
        return embeds

    def create_flashinfer_paged_kv_cache(
        self,
        max_batch_size: tir.Var,
        max_total_seq_len: tir.Var,
        prefill_chunk_size: tir.Var,
        page_size: tir.Var,
    ) -> PagedKVCache:
        # Note: Right now we only have FlashInfer-based KV cache supported.
        # TIR version will be introduced soon.
        return FlashInferPagedKVCache(
            max_batch_size=max_batch_size,
            max_total_seq_len=max_total_seq_len,
            prefill_chunk_size=prefill_chunk_size,
            page_size=page_size,
            num_hidden_layers=self.num_hidden_layers,
            num_attention_heads=self.num_attention_heads // self.tensor_parallel_shards,
            num_key_value_heads=self.num_key_value_heads // self.tensor_parallel_shards,
            head_dim=self.head_dim,
            rope_mode=RopeMode.NORMAL,
            rope_scale=1,
            rope_theta=self.rope_theta,
            rotary_dim=self.rotary_dim,
            dtype=self.dtype,
        )
    
    def get_default_spec(self):
        mod_spec = {
            "embed": {
                "input_ids": nn.spec.Tensor(["seq_len"], "int32"),
                "$": {
                    "param_mode": "packed",
                    "effect_mode": "none",
                },
            },
            "prefill": {
                "input_embed": nn.spec.Tensor([1, "seq_len", self.hidden_size], self.dtype),
                "paged_kv_cache": nn.spec.Object(object_type=PagedKVCache),
                "$": {
                    "param_mode": "packed",
                    "effect_mode": "none",
                },
            },
            "decode": {
                "input_embed": nn.spec.Tensor([1, 1, self.hidden_size], self.dtype),
                "paged_kv_cache": nn.spec.Object(object_type=PagedKVCache),
                "$": {
                    "param_mode": "packed",
                    "effect_mode": "none",
                },
            },
            "batch_prefill": {
                "input_embeds": nn.spec.Tensor([1, "seq_len", self.hidden_size], self.dtype),
                "logit_positions": nn.spec.Tensor(["batch_size"], "int32"),
                "paged_kv_cache": nn.spec.Object(object_type=PagedKVCache),
                "$": {
                    "param_mode": "packed",
                    "effect_mode": "none",
                },
            },
            "batch_decode": {
                "input_embeds": nn.spec.Tensor(["batch_size", 1, self.hidden_size], self.dtype),
                "paged_kv_cache": nn.spec.Object(object_type=PagedKVCache),
                "$": {
                    "param_mode": "packed",
                    "effect_mode": "none",
                },
            },
            "batch_verify": {
                "input_embeds": nn.spec.Tensor([1, "seq_len", self.hidden_size], self.dtype),
                "paged_kv_cache": nn.spec.Object(object_type=PagedKVCache),
                "$": {
                    "param_mode": "packed",
                    "effect_mode": "none",
                },
            },
            "softmax_with_temperature": {
                "logits": nn.spec.Tensor(["batch_size", 1, "vocab_size"], "float32"),
                "temperature": nn.spec.Tensor(["batch_size"], "float32"),
                "$": {
                    "param_mode": "none",
                    "effect_mode": "none",
                },
            },
            "create_flashinfer_paged_kv_cache": {
                "max_batch_size": int,
                "max_total_seq_len": int,
                "prefill_chunk_size": int,
                "page_size": int,
                "$": {
                    "param_mode": "none",
                    "effect_mode": "none",
                },
            },
        }
        return nn.spec.ModuleSpec.from_raw(mod_spec, self)


def corner_pooling(input_tensor: Tensor, num_patches: int, conv_size: int) -> Tensor:
    batch_size = input_tensor.shape[0]
    input_tensor = op.reshape(input_tensor, shape=(batch_size, num_patches, num_patches, -1))
    # print(input_tensor)
    input_tensor = op.concat(op.split(input_tensor, num_patches, axis=1)[::conv_size], dim=1)
    input_tensor = op.concat(op.split(input_tensor, num_patches, axis=2)[::conv_size], dim=2)
    new_patches= (num_patches-1) // conv_size + 1
    input_tensor = op.reshape(input_tensor, shape=(batch_size, new_patches ** 2, -1))
    return input_tensor

class ImpMultiModalProjector(nn.Module):
    def __init__(self, config: PhiConfig):
        super().__init__()
        self.vit_config = ImpVisionConfig()
        self.linear_1 = nn.Linear(self.vit_config.hidden_size, config.n_embd)
        self.act = nn.GELU()
        self.linear_2 = nn.Linear(config.n_embd, config.n_embd)

    def forward(self, image_features: Tensor) -> Tensor:
        # num_patches = self.vit_config.image_size // self.vit_config.patch_size
        # image_features = corner_pooling(image_features, num_patches, 2)
        # print(image_features)
        hidden_states = self.linear_1(image_features)
        hidden_states = self.act(hidden_states)
        hidden_states = self.linear_2(hidden_states)
        return hidden_states


class ImpForCasualLM(nn.Module):
    def __init__(self, config: PhiConfig):
        super().__init__()
        self.config = config
        self.vit_config = ImpVisionConfig()
        self.vision_tower = SigLipVisionModel(self.vit_config)
        self.multi_modal_projector = ImpMultiModalProjector(config)
        self.language_model = PhiForCausalLM(config)
        self.vocab_size = config.vocab_size
        self.dtype = config.dtype

    def _embed_input_ids(self, input_ids: Tensor) -> Tensor:
        return self.language_model.embed(input_ids)

    def _embed_pixel_values_and_input_ids(self, pixel_values: Tensor, input_ids: Tensor) -> Tensor:
        def _index(x, value, batch_size, seq_len):
            return te.compute(
                (batch_size, seq_len),
                lambda i, j: tir.if_then_else(
                    x[i, j] == value,
                    j,
                    tir.IntImm("int32", 0),
                ),
                name="index",
            )

        def _concat(x: Tensor, y: Tensor, new_shape: tuple, insert_index: Tensor):
            return te.compute(
                (new_shape),
                lambda b, i, j: tir.if_then_else(
                    i < insert_index[0],
                    x[b, i, j],
                    tir.if_then_else(
                        i < insert_index[0] + y.shape[1],
                        y[b, i - insert_index[0], j],
                        x[b, i - y.shape[1] + 1, j],
                    ),
                ),
            )

        input_embeddings = self._embed_input_ids(input_ids)
        image_features = self.vision_tower.forward(pixel_values)
        # image_features = wrap_nested(
        #     strided_slice(
        #         image_features_all._expr,  # pylint: disable=protected-access
        #         axes=[1],
        #         begin=[1],
        #         end=[image_features_all.shape[1]],
        #     ),
        #     name="slice",
        # )
        image_features = self.multi_modal_projector(image_features)
        batch_size, seq_len = input_ids.shape
        image_index_tensor = op.tensor_expr_op(
            _index,
            name_hint="index",
            args=[
                input_ids,
                tir.IntImm("int32", self.config.image_token_index),
                batch_size,
                seq_len,
            ],
        ).astype("int32")
        ##! Assume only one <IMAGE> token in input
        ##! Also assume batch_size = 1 for now
        # TODO: Support image_count > 1 and batch_size > 1 # pylint: disable=fixme
        insert_index = op.sum(image_index_tensor, axis=1)
        new_shape = (
            batch_size,
            seq_len + tir.IntImm("int32", image_features.shape[1]-1),
            self.config.n_embd,
        )
        combined_embeddings = op.tensor_expr_op(
            _concat,
            name_hint="combined_embeddings",
            args=[input_embeddings, image_features, new_shape, insert_index],
        )
        print(combined_embeddings)
        return combined_embeddings

    def embed(self, input_ids: Tensor) -> Tensor:
        return self._embed_input_ids(input_ids)

    def embed_with_pixel_values(self, pixel_values: Tensor, input_ids: Tensor) -> Tensor:
        pixel_values = pixel_values.astype("float16")
        return self._embed_pixel_values_and_input_ids(pixel_values, input_ids)

    def prefill(self, input_embed: Tensor, paged_kv_cache: PagedKVCache):
        op_ext.configure()

        return self.language_model.prefill(input_embed, paged_kv_cache)

    def decode(self, input_embed: Tensor, paged_kv_cache: PagedKVCache):
        op_ext.configure()

        return self.language_model.decode(input_embed, paged_kv_cache)

    def softmax_with_temperature(self, logits: Tensor, temperature: Tensor):
        return op.softmax(logits / op.reshape(temperature, (temperature.shape[0], 1, 1)), axis=-1)

    def create_paged_kv_cache(  # pylint: disable=too-many-arguments
        self,
        max_batch_size: tir.Var,
        max_total_seq_len: tir.Var,
        prefill_chunk_size: tir.Var,
        page_size: tir.Var,
        support_sliding_window: tir.Var,
    ) -> PagedKVCache:
        return PagedKVCache.create_generic(
            max_batch_size=max_batch_size,
            max_total_seq_len=max_total_seq_len,
            prefill_chunk_size=prefill_chunk_size,
            page_size=page_size,
            support_sliding_window=support_sliding_window,
            num_hidden_layers=self.config.n_layer,
            num_attention_heads=self.config.n_head
            // self.config.tensor_parallel_shards,
            num_key_value_heads=self.config.n_head_kv
            // self.config.tensor_parallel_shards,
            head_dim=self.config.head_dim,
            rope_mode=RopeMode.NORMAL,
            rope_scale=1,
            rope_theta=self.language_model.rope_theta,
            rotary_dim=self.config.rotary_dim,
            dtype=self.dtype,
        )
    
    def get_default_spec(self):
        mod_spec = {
            "embed": {
                "input_ids": nn.spec.Tensor([1, "seq_len"], "int32"),
                "$": {
                    "param_mode": "packed",
                    "effect_mode": "none",
                },
            },
            "embed_with_pixel_values": {
                "pixel_values": nn.spec.Tensor(
                    [
                        1,
                        3,
                        self.vit_config.image_size,
                        self.vit_config.image_size,
                    ],
                    "float32"
                    # self.dtype
                ),
                "input_ids": nn.spec.Tensor([1, "seq_len"], "int32"),
                "$": {
                    "param_mode": "packed",
                    "effect_mode": "none",
                },
            },
            "prefill": {
                "input_embed": nn.spec.Tensor(
                    [1, "seq_len", self.config.n_embd], self.dtype
                ),
                "paged_kv_cache": nn.spec.Object(object_type=PagedKVCache),
                "$": {
                    "param_mode": "packed",
                    "effect_mode": "none",
                },
            },
            "decode": {
                "input_embed": nn.spec.Tensor(
                    [1, 1, self.config.n_embd], self.dtype
                ),
                "paged_kv_cache": nn.spec.Object(object_type=PagedKVCache),
                "$": {
                    "param_mode": "packed",
                    "effect_mode": "none",
                },
            },
            "softmax_with_temperature": {
                "logits": nn.spec.Tensor(["batch_size", 1, "vocab_size"], "float32"),
                "temperature": nn.spec.Tensor(["batch_size"], "float32"),
                "$": {
                    "param_mode": "none",
                    "effect_mode": "none",
                },
            },
            "create_paged_kv_cache": {
                "max_batch_size": int,
                "max_total_seq_len": int,
                "prefill_chunk_size": int,
                "page_size": int,
                "support_sliding_window": int,
                "$": {
                    "param_mode": "none",
                    "effect_mode": "none",
                },
            },
        }
 
        return nn.spec.ModuleSpec.from_raw(mod_spec, self)