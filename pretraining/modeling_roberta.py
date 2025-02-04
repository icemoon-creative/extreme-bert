# coding=utf-8
# Copyright 2022 Statistics and Machine Learning Research Group at HKUST. All rights reserved.
# code taken from commit: ea000838156e3be251699ad6a3c8b1339c76e987
# https://github.com/IntelLabs/academic-budget-bert
# Copyright 2021 Intel Corporation. All rights reserved.
# DeepSpeed note, code taken from commit 3d59216cec89a363649b4fe3d15295ba936ced0f
# https://github.com/NVIDIA/DeepLearningExamples/blob/master/PyTorch/LanguageModeling/BERT/modeling.py
# Deepspeed code taken from commit: 35b4582486fe096a5c669b6ca35a3d5c6a1ec56b
# https://github.com/microsoft/DeepSpeedExamples/tree/master/bing_bert
# RMS Norm taken from: https://github.com/EleutherAI/gpt-neox/blob/main/megatron/model/norms.py
#
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""PyTorch BERT model."""

from __future__ import absolute_import, division, print_function, unicode_literals
import torch
import copy
import logging
import math
import os
import sys

import torch.nn.functional as F
import torch.nn.init as init
from torch import nn
from torch.nn import CrossEntropyLoss, Module
from torch.nn.modules.loss import MSELoss
from torch.nn.parameter import Parameter
from torch.utils import checkpoint
from transformers import RobertaConfig, PreTrainedModel
from transformers.modeling_outputs import SequenceClassifierOutput

logger = logging.getLogger(__name__)


def get_deepspeed_config(args):
    if hasattr(args, "deepspeed_config") and args.deepspeed_config:
        from deepspeed import DeepSpeedConfig

        return DeepSpeedConfig(None, param_dict=args.ds_config)
    else:
        raise RuntimeError("deepspeed_config is not found in args.")


@torch.jit.script
def f_gelu(x):
    return F.gelu(x)


# @torch.jit.script
# def f_gelu(x):
#     pdtype = x.dtype
#     x = x.float()
#     y = x * 0.5 * (1.0 + torch.erf(x / math.sqrt(2.0)))
#     return y.to(pdtype)


# @torch.jit.script
def bias_gelu(bias, y):
    x = bias + y
    return F.gelu(x)


# def bias_gelu(bias, y):
#     x = bias + y
#     return x * 0.5 * (1.0 + torch.erf(x / 1.41421))


@torch.jit.script
def bias_relu(bias, y):
    x = bias + y
    return F.relu(x)


# @torch.jit.script
# def bias_gelu(bias, y):
#     x = bias + y
#     return x * 0.5 * (1.0 + torch.erf(x / 1.41421))


@torch.jit.script
def bias_tanh(bias, y):
    x = bias + y
    return torch.tanh(x)


def gelu(x):
    """Implementation of the gelu activation function.
    For information: OpenAI GPT's gelu is slightly different (and gives slightly different results):
    0.5 * x * (1 + torch.tanh(math.sqrt(2 / math.pi) * (x + 0.044715 * torch.pow(x, 3))))
    Also see https://arxiv.org/abs/1606.08415
    """
    return f_gelu(x)


def swish(x):
    return x * torch.sigmoid(x)


ACT2FN = {"gelu": F.gelu, "relu": F.relu, "swish": swish, "tanh": F.tanh}


class LinearActivation(Module):
    r"""Fused Linear and activation Module."""
    __constants__ = ["bias"]

    def __init__(self, in_features, out_features, act="gelu", bias=True):
        super(LinearActivation, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.fused_gelu = False
        self.fused_tanh = False
        self.fused_relu = False
        if isinstance(act, str) or (sys.version_info[0] == 2 and isinstance(act, unicode)):
            if bias and act == "gelu":
                self.fused_gelu = True
            elif bias and act == "tanh":
                self.fused_tanh = True
            elif bias and act == "relu":
                self.fused_relu = True
            else:
                self.act_fn = ACT2FN[act]
        else:
            self.act_fn = act
        self.weight = Parameter(torch.Tensor(out_features, in_features))
        if bias:
            self.bias = Parameter(torch.Tensor(out_features))
        else:
            self.register_parameter("bias", None)
        self.reset_parameters()

    def reset_parameters(self):
        init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        if self.bias is not None:
            fan_in, _ = init._calculate_fan_in_and_fan_out(self.weight)
            bound = 1 / math.sqrt(fan_in)
            init.uniform_(self.bias, -bound, bound)

    def forward(self, inp):
        if self.fused_gelu:
            return bias_gelu(self.bias, F.linear(inp, self.weight, None))
        elif self.fused_tanh:
            return bias_tanh(self.bias, F.linear(inp, self.weight, None))
        elif self.fused_relu:
            return bias_relu(self.bias, F.linear(inp, self.weight, None))
        else:
            return self.act_fn(F.linear(inp, self.weight, self.bias))

    def extra_repr(self):
        return "in_features={}, out_features={}, bias={}".format(
            self.in_features, self.out_features, self.bias is not None
        )


class RegularLinearActivation(Module):
    """Regular Linear activation module with"""

    def __init__(self, in_features, out_features, act="gelu"):
        super(RegularLinearActivation, self).__init__()
        self.dense = nn.Linear(in_features, out_features)
        if isinstance(act, str) or (sys.version_info[0] == 2 and isinstance(act, unicode)):
            self.act = ACT2FN[act]

    def forward(self, hidden_states):
        return self.act(self.dense(hidden_states))


def get_apex_layer_norm():
    try:
        # # ----Use Nvidia Apex----
        # import apex

        # # apex.amp.register_half_function(apex.normalization.fused_layer_norm, 'FusedLayerNorm')
        # import apex.normalization

        # # apex.amp.register_float_function(apex.normalization.FusedLayerNorm, 'forward')
        # return apex.normalization.FusedLayerNorm
        # # ----Use Nvidia Apex----

        # ----Use Torch Apex----
        return torch.nn.LayerNorm
        # ----Use Torch Apex----
    except ImportError:
        raise Exception(f"Layer norm of type apex is not available, apex not installed.")


class RMSNorm(torch.nn.Module):
    def __init__(self, dim, p=-1.0, eps=1e-8, bias=False):
        """
            Root Mean Square Layer Normalization
        :param dim: model size
        :param p: partial RMSNorm, valid value [0, 1], default -1.0 (disabled)
        :param eps:  epsilon value, default 1e-8
        :param bias: whether use bias term for RMSNorm, disabled by
            default because RMSNorm doesn't enforce re-centering invariance.
        """
        super(RMSNorm, self).__init__()

        self.eps = eps
        self.d = dim
        self.p = p
        self.bias = bias

        self.scale = torch.nn.Parameter(torch.ones(dim))
        self.register_parameter("scale", self.scale)

        if self.bias:
            self.offset = torch.nn.Parameter(torch.zeros(dim))
            self.register_parameter("offset", self.offset)

    def forward(self, x):
        if self.p < 0.0 or self.p > 1.0:
            norm_x = x.norm(2, dim=-1, keepdim=True)
            d_x = self.d
        else:
            partial_size = int(self.d * self.p)
            partial_x, _ = torch.split(x, [partial_size, self.d - partial_size], dim=-1)

            norm_x = partial_x.norm(2, dim=-1, keepdim=True)
            d_x = partial_size

        rms_x = norm_x * d_x ** (-1.0 / 2)
        x_normed = x / (rms_x + self.eps)

        if self.bias:
            return self.scale * x_normed + self.offset

        return self.scale * x_normed


LAYER_NORM_TYPES = {"pytorch": nn.LayerNorm, "apex": get_apex_layer_norm(), "rms_norm": RMSNorm}


def get_layer_norm_type(config):
    if config.layer_norm_type in LAYER_NORM_TYPES:
        return LAYER_NORM_TYPES[config.layer_norm_type]
    else:
        raise Exception(f"Layer norm of type {config.layer_norm_type} is not available.")


class RobertaEmbeddings(nn.Module):
    """Construct the embeddings from word, position and token_type embeddings."""

    def __init__(self, config):
        super(RobertaEmbeddings, self).__init__()
        self.config = config
        self.word_embeddings = nn.Embedding(config.vocab_size, config.hidden_size)
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.hidden_size)
        self.token_type_embeddings = nn.Embedding(config.type_vocab_size, config.hidden_size)
        self.layernorm_embedding = False
        if hasattr(config, "config.layernorm_embedding"):
            self.layernorm_embedding = config.layernorm_embedding
        if self.layernorm_embedding:
            RobertaLayerNorm = get_layer_norm_type(config)
            self.LayerNorm = RobertaLayerNorm(config.hidden_size, eps=1e-12)

        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, input_ids, token_type_ids=None):
        seq_length = input_ids.size(1)
        position_ids = torch.arange(seq_length, dtype=torch.long, device=input_ids.device)
        position_ids = position_ids.unsqueeze(0).expand_as(input_ids)
        if token_type_ids is None:
            token_type_ids = torch.zeros_like(input_ids)

        words_embeddings = self.word_embeddings(input_ids)
        position_embeddings = self.position_embeddings(position_ids)
        token_type_embeddings = self.token_type_embeddings(token_type_ids)

        embeddings = words_embeddings + position_embeddings + token_type_embeddings

        if self.layernorm_embedding:
            embeddings = self.LayerNorm(embeddings)

        embeddings = self.dropout(embeddings)
        return embeddings


class RobertaNgramEmbeddings(nn.Module):
    """Construct the embeddings from ngram, position and token_type embeddings.
    """

    def __init__(self, config, args):
        super(RobertaNgramEmbeddings, self).__init__()
        self.word_embeddings = nn.Embedding(config.Ngram_size, config.hidden_size, padding_idx=config.pad_token_id)
        self.token_type_embeddings = nn.Embedding(config.type_vocab_size, config.hidden_size)
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, input_ids, token_type_ids=None):
        if token_type_ids is None:
            token_type_ids = torch.zeros_like(input_ids)

        words_embeddings = self.word_embeddings(input_ids)
        token_type_embeddings = self.token_type_embeddings(token_type_ids)

        embeddings = words_embeddings + token_type_embeddings
        embeddings = self.LayerNorm(embeddings)
        embeddings = self.dropout(embeddings)
        return embeddings


class RobertaSelfAttention(nn.Module):
    def __init__(self, config):
        super(RobertaSelfAttention, self).__init__()
        if config.hidden_size % config.num_attention_heads != 0:
            raise ValueError(
                "The hidden size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (config.hidden_size, config.num_attention_heads)
            )
        self.num_attention_heads = config.num_attention_heads
        self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = nn.Linear(config.hidden_size, self.all_head_size)
        self.key = nn.Linear(config.hidden_size, self.all_head_size)
        self.value = nn.Linear(config.hidden_size, self.all_head_size)

        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)
        self.softmax = nn.Softmax(dim=-1)

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def transpose_key_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 3, 1)

    def forward(self, hidden_states, attention_mask):
        mixed_query_layer = self.query(hidden_states)
        mixed_key_layer = self.key(hidden_states)
        mixed_value_layer = self.value(hidden_states)

        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_key_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.matmul(query_layer, key_layer)
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
        attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = self.softmax(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        context_layer = torch.matmul(attention_probs, value_layer)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)
        return context_layer, attention_probs


class RobertaSelfOutput(nn.Module):
    def __init__(self, config):
        super(RobertaSelfOutput, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.dense.bert_output_layer = True
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        return hidden_states


class RobertaAttention(nn.Module):
    def __init__(self, config):
        super(RobertaAttention, self).__init__()
        self.self = RobertaSelfAttention(config)
        self.output = RobertaSelfOutput(config)

    def forward(self, input_tensor, attention_mask):
        context_layer, attention_probs = self.self(input_tensor, attention_mask)
        attention_output = self.output(context_layer, input_tensor)
        output = (
            attention_output,
            attention_probs,
        )
        return output


class RobertaIntermediate(nn.Module):
    def __init__(self, config):
        super(RobertaIntermediate, self).__init__()
        if config.fused_linear_layer:
            linear_layer = LinearActivation
        else:
            linear_layer = RegularLinearActivation
        self.dense = linear_layer(
            config.hidden_size, config.intermediate_size, act=config.hidden_act
        )

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        return hidden_states


class RobertaOutput(nn.Module):
    def __init__(self, config):
        super(RobertaOutput, self).__init__()
        self.dense = nn.Linear(config.intermediate_size, config.hidden_size)
        self.dense.bert_output_layer = True
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        return hidden_states


class RobertaLayer(nn.Module):
    def __init__(self, config):
        super(RobertaLayer, self).__init__()
        self.attention = RobertaAttention(config)
        self.config = config

        RobertaLayerNorm = get_layer_norm_type(config)

        self.PreAttentionLayerNorm = RobertaLayerNorm(config.hidden_size, eps=1e-12)
        self.PostAttentionLayerNorm = RobertaLayerNorm(config.hidden_size, eps=1e-12)
        self.intermediate = RobertaIntermediate(config)
        self.output = RobertaOutput(config)

    def maybe_layer_norm(self, hidden_states, layer_norm, current_ln_mode):
        if self.config.useLN and self.config.encoder_ln_mode in current_ln_mode:
            return layer_norm(hidden_states)
        else:
            return hidden_states

    def forward(self, hidden_states, attention_mask, action=1, keep_prob=1.0):
        attention_probs = None
        intermediate_input = None

        if action == 0:
            intermediate_input = hidden_states
        else:
            pre_attn_input = self.maybe_layer_norm(
                hidden_states, self.PreAttentionLayerNorm, "pre-ln"
            )
            self_attn_out = self.attention(pre_attn_input, attention_mask)

            attention_output, attention_probs = self_attn_out
            attention_output = attention_output * 1 / keep_prob

            intermediate_input = hidden_states + attention_output
            intermediate_input = self.maybe_layer_norm(
                intermediate_input, self.PreAttentionLayerNorm, "post-ln"
            )

        if action == 0:
            layer_output = intermediate_input
        else:
            intermediate_pre_ffn = self.maybe_layer_norm(
                intermediate_input, self.PostAttentionLayerNorm, "pre-ln"
            )
            intermediate_output = self.intermediate(intermediate_pre_ffn)

            layer_output = self.output(intermediate_output)
            layer_output = layer_output * 1 / keep_prob

            layer_output = layer_output + intermediate_input
            layer_output = self.maybe_layer_norm(
                layer_output, self.PostAttentionLayerNorm, "post-ln"
            )

        output = (
            layer_output,
            attention_probs,
        )
        return output


class RobertaEncoder(nn.Module):
    def __init__(self, config, args):
        super(RobertaEncoder, self).__init__()
        self.config = config
        RobertaLayerNorm = get_layer_norm_type(config)
        self.FinalLayerNorm = RobertaLayerNorm(config.hidden_size, eps=1e-12)
        self.is_transformer_kernel = (
                hasattr(args, "deepspeed_transformer_kernel") and args.deepspeed_transformer_kernel
        )

        self.num_hidden_Ngram_layers = config.num_hidden_Ngram_layers  
        self.is_Ngram = args.is_Ngram 

        if hasattr(args, "deepspeed_transformer_kernel") and args.deepspeed_transformer_kernel:
            from deepspeed import DeepSpeedTransformerConfig, DeepSpeedTransformerLayer

            ds_config = get_deepspeed_config(args)
            has_huggingface = hasattr(args, "huggingface")
            ds_transformer_config = DeepSpeedTransformerConfig(
                batch_size=ds_config.train_micro_batch_size_per_gpu,
                hidden_size=config.hidden_size,
                intermediate_size=config.intermediate_size,
                heads=config.num_attention_heads,
                attn_dropout_ratio=config.attention_probs_dropout_prob,
                hidden_dropout_ratio=config.hidden_dropout_prob,
                num_hidden_layers=config.num_hidden_layers,
                initializer_range=config.initializer_range,
                local_rank=args.local_rank if hasattr(args, "local_rank") else -1,
                seed=args.seed,
                fp16=ds_config.fp16_enabled,
                pre_layer_norm=True if "pre-ln" in config.encoder_ln_mode else False,
                normalize_invertible=args.normalize_invertible,
                gelu_checkpoint=args.gelu_checkpoint,
                adjust_init_range=True,
                attn_dropout_checkpoint=args.attention_dropout_checkpoint,
                stochastic_mode=args.stochastic_mode,
                huggingface=has_huggingface,
                training=self.training,
            )

            self.layer = nn.ModuleList(
                [
                    copy.deepcopy(DeepSpeedTransformerLayer(ds_transformer_config))
                    for _ in range(config.num_hidden_layers)
                ]
            )
        else:
            layer = RobertaLayer(config)
            self.layer = nn.ModuleList(
                [copy.deepcopy(layer) for _ in range(self.config.num_hidden_layers)]
            )

        if self.is_Ngram:
            self.Ngram_layer = nn.ModuleList([RobertaLayer(config) for _ in range(self.num_hidden_Ngram_layers)])
        # self.layer为token的attention module,self.Ngram_layer为N_gram的attention module

    def add_attention(self, all_attentions, attention_probs):
        if attention_probs is not None:
            all_attentions.append(attention_probs)

        return all_attentions

    def forward(
            self,
            hidden_states,
            attention_mask,
            Ngram_hidden_states=None,  
            Ngram_position_matrix=None,  
            Ngram_attention_mask=None,
            output_all_encoded_layers=True,
            checkpoint_activations=False,
            output_attentions=False,
    ):
        all_encoder_layers = []
        all_attentions = []

        def custom(start, end):
            def custom_forward(*inputs):
                layers = self.layer[start:end]
                x_ = inputs[0]
                for layer in layers:
                    x_ = layer(x_, inputs[1])
                return x_

            return custom_forward

        if checkpoint_activations:
            l = 0
            num_layers = len(self.layer)
            chunk_length = math.ceil(math.sqrt(num_layers))
            while l < num_layers:
                hidden_states = checkpoint.checkpoint(
                    custom(l, l + chunk_length), hidden_states, attention_mask * 1
                )
                l += chunk_length
            # decoder layers
        else:
            for i, layer_module in enumerate(self.layer):
                if self.is_transformer_kernel:
                    # using Deepspeed Transformer kernel
                    hidden_states = layer_module(hidden_states, attention_mask)
                else:
                    layer_out = layer_module(
                        hidden_states,
                        attention_mask,
                    )
                    hidden_states, attention_probs = layer_out
                    # get all attention_probs from layers
                    if output_attentions:
                        all_attentions = self.add_attention(all_attentions, attention_probs)
              
                if self.is_Ngram:
                    if i < self.num_hidden_Ngram_layers:
                        # [batch_size,max_len_seq,hidden_size]
                        Ngram_hidden_states = self.Ngram_layer[i](Ngram_hidden_states, Ngram_attention_mask)[0]
                        # [batch_size,max_seq,max_len_seq]
                        Ngram_states = torch.bmm(Ngram_position_matrix.float(), Ngram_hidden_states.float())
                        hidden_states += Ngram_states
                

                if output_all_encoded_layers:
                    all_encoder_layers.append(hidden_states)

        if not output_all_encoded_layers or checkpoint_activations:
            if self.config.useLN and self.config.encoder_ln_mode in "pre-ln":
                hidden_states = self.FinalLayerNorm(hidden_states)

            all_encoder_layers.append(hidden_states)
        outputs = (all_encoder_layers,)
        if output_attentions:
            outputs += (all_attentions,)
        return outputs 


class RobertaPooler(nn.Module):
    def __init__(self, config):
        super(RobertaPooler, self).__init__()
        if config.fused_linear_layer:
            linear_layer = LinearActivation
        else:
            linear_layer = RegularLinearActivation
        self.dense_act = linear_layer(config.hidden_size, config.hidden_size, act="tanh")

    def forward(self, hidden_states):
        # We "pool" the model by simply taking the hidden state corresponding
        # to the first token.
        first_token_tensor = hidden_states[:, 0]
        pooled_output = self.dense_act(first_token_tensor)
        return pooled_output


class RobertaPredictionHeadTransform(nn.Module):
    def __init__(self, config):
        super(RobertaPredictionHeadTransform, self).__init__()
        self.config = config
        if config.fused_linear_layer:
            linear_layer = LinearActivation
        else:
            linear_layer = RegularLinearActivation
        self.dense_act = linear_layer(config.hidden_size, config.hidden_size, act=config.hidden_act)
        RobertaLayerNorm = get_layer_norm_type(config)
        self.LayerNorm = RobertaLayerNorm(config.hidden_size, eps=1e-12)

    def forward(self, hidden_states):
        hidden_states = self.dense_act(hidden_states)
        if self.config.useLN:
            hidden_states = self.LayerNorm(hidden_states)

        return hidden_states


class RobertaLMPredictionHead(nn.Module):
    def __init__(self, config, roberta_model_embedding_weights):
        super(RobertaLMPredictionHead, self).__init__()
        self.transform = RobertaPredictionHeadTransform(config)
        self.decoder = nn.Linear(
            roberta_model_embedding_weights.size(1), roberta_model_embedding_weights.size(0), bias=False
        )
        self.decoder.weight = roberta_model_embedding_weights
        self.bias = nn.Parameter(torch.zeros(roberta_model_embedding_weights.size(0)))
        self.sparse_predict = config.sparse_mask_prediction
        if not config.sparse_mask_prediction:
            self.decoder.bias = self.bias

    def forward(self, hidden_states, masked_token_indexes):
        if self.sparse_predict:
            if masked_token_indexes is not None:
                hidden_states = hidden_states.view(-1, hidden_states.shape[-1])[
                    masked_token_indexes
                ]
        hidden_states = self.transform(hidden_states)
        hidden_states = self.decoder(hidden_states)
        if not self.sparse_predict:
            hidden_states = torch.index_select(
                hidden_states.view(-1, hidden_states.shape[-1]), 0, masked_token_indexes
            )
        return hidden_states


class RobertaOnlyMLMHead(nn.Module):
    def __init__(self, config, bert_model_embedding_weights):
        super(RobertaOnlyMLMHead, self).__init__()
        self.predictions = RobertaLMPredictionHead(config, bert_model_embedding_weights)

    def forward(self, sequence_output, masked_token_indexes=None):
        prediction_scores = self.predictions(sequence_output, masked_token_indexes)
        return prediction_scores


class RobertaOnlyNSPHead(nn.Module):
    def __init__(self, config):
        super(RobertaOnlyNSPHead, self).__init__()
        self.seq_relationship = nn.Linear(config.hidden_size, 2)

    def forward(self, pooled_output):
        seq_relationship_score = self.seq_relationship(pooled_output)
        return seq_relationship_score


class RobertaPreTrainingHeads(nn.Module):
    def __init__(self, config, bert_model_embedding_weights):
        super(RobertaPreTrainingHeads, self).__init__()
        self.predictions = RobertaLMPredictionHead(config, bert_model_embedding_weights)
        self.seq_relationship = nn.Linear(config.hidden_size, 2)

    def forward(self, sequence_output, pooled_output, masked_token_indexes=None):
        prediction_scores = self.predictions(sequence_output, masked_token_indexes)
        seq_relationship_score = self.seq_relationship(pooled_output)
        return prediction_scores, seq_relationship_score


class RobertaPreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = RobertaConfig
    base_model_prefix = "roberta"
    authorized_missing_keys = [r"position_ids"]

    def __init__(self, config, *inputs, **kwargs):
        super().__init__(config, inputs=inputs, kwargs=kwargs)
        print(inputs)
        print(kwargs)

    def _init_weights(self, module):
        """Initialize the weights"""
        if isinstance(module, (nn.Linear, nn.Embedding)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)

        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()


class RobertaModel(RobertaPreTrainedModel):

    def __init__(self, config, args=None):
        super(RobertaModel, self).__init__(config)
        self.embeddings = RobertaEmbeddings(config)
        # set pad_token_id that is used for sparse attention padding
        self.is_Ngram = args.is_Ngram
        if self.is_Ngram:
            self.Ngram_embeddings = RobertaNgramEmbeddings(config, args)
        self.pad_token_id = (
            config.pad_token_id
            if hasattr(config, "pad_token_id") and config.pad_token_id is not None
            else 0
        )
        self.encoder = RobertaEncoder(config, args)
        self.pooler = RobertaPooler(config)

        logger.info("Init ROBERTA pretrain model")

    def forward(
            self,
            input_ids,
            token_type_ids=None,
            attention_mask=None,
            input_Ngram_ids=None,  
            Ngram_attention_mask=None,
            Ngram_token_type_ids=None,
            Ngram_position_matrix=None,
            output_all_encoded_layers=True,
            checkpoint_activations=False,
            output_attentions=False,
    ):
        if attention_mask is None:
            attention_mask = torch.ones_like(input_ids)
        if token_type_ids is None:
            token_type_ids = torch.zeros_like(input_ids)

        # We create a 3D attention mask from a 2D tensor mask.
        # Sizes are [batch_size, 1, 1, to_seq_length]
        # So we can broadcast to [batch_size, num_heads, from_seq_length, to_seq_length]
        # this attention mask is more simple than the triangular masking of causal attention
        # used in OpenAI GPT, we just need to prepare the broadcast dimension here.

        extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)
        # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
        # masked positions, this operation will create a tensor which is 0.0 for
        # positions we want to attend and -10000.0 for masked positions.
        # Since we are adding it to the raw scores before the softmax, this is
        # effectively the same as removing these entirely.

        extended_attention_mask = extended_attention_mask.to(
            dtype=self.embeddings.word_embeddings.weight.dtype  # should be of same dtype
        )  # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
        embedding_output = self.embeddings(input_ids, token_type_ids)

        Ngram_embedding_output = None
        extended_Ngram_attention_mask = None
        if self.is_Ngram:
            if Ngram_attention_mask is None:
                Ngram_attention_mask = torch.ones_like(input_Ngram_ids)
            if Ngram_token_type_ids is None:
                Ngram_token_type_ids = torch.zeros_like(input_Ngram_ids)

            extended_Ngram_attention_mask = Ngram_attention_mask.unsqueeze(1).unsqueeze(2)

            extended_Ngram_attention_mask = extended_Ngram_attention_mask.to(
                dtype=self.Ngram_embeddings.word_embeddings.weight.dtype
            )
            extended_Ngram_attention_mask = (1.0 - extended_Ngram_attention_mask) * -10000.0

            Ngram_embedding_output = self.Ngram_embeddings(input_Ngram_ids, Ngram_token_type_ids)

        encoder_output = self.encoder(
            hidden_states=embedding_output,
            attention_mask=extended_attention_mask,
            Ngram_hidden_states=Ngram_embedding_output,
            Ngram_position_matrix=Ngram_position_matrix,
            Ngram_attention_mask=extended_Ngram_attention_mask,
            output_all_encoded_layers=output_all_encoded_layers,
            checkpoint_activations=checkpoint_activations,
            output_attentions=output_attentions,
        )

        encoded_layers = encoder_output[0]
        sequence_output = encoded_layers[-1]

        pooled_output = self.pooler(sequence_output)

        if not output_all_encoded_layers:
            encoded_layers = encoded_layers[-1]
        output = (
            encoded_layers,
            pooled_output,
        )
        if output_attentions:
            output += (encoder_output[-1],)
        return output 


class RobertaForPreTraining(RobertaPreTrainedModel):

    def __init__(self, config, args):
        super(RobertaForPreTraining, self).__init__(config)
        self.roberta = RobertaModel(config, args)
        self.cls = RobertaPreTrainingHeads(config, self.roberta.embeddings.word_embeddings.weight)
        self._init_weights(self.roberta)

    def forward(self, batch):
        input_ids = batch[1]
        token_type_ids = batch[3]
        attention_mask = batch[2]
        masked_lm_labels = batch[5]
        next_sentence_label = batch[4]
        checkpoint_activations = False

        sequence_output, pooled_output = self.roberta(
            input_ids,
            token_type_ids,
            attention_mask,
            output_all_encoded_layers=False,
            checkpoint_activations=checkpoint_activations,
        )

        if masked_lm_labels is not None and next_sentence_label is not None:
            # filter out all masked labels.
            masked_token_indexes = torch.nonzero(
                (masked_lm_labels + 1).view(-1), as_tuple=False
            ).view(-1)
            prediction_scores, seq_relationship_score = self.cls(
                sequence_output, pooled_output, masked_token_indexes
            )
            target = torch.index_select(masked_lm_labels.view(-1), 0, masked_token_indexes)

            loss_fct = CrossEntropyLoss(ignore_index=-1)
            masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), target)
            next_sentence_loss = loss_fct(
                seq_relationship_score.view(-1, 2), next_sentence_label.view(-1)
            )
            total_loss = masked_lm_loss + next_sentence_loss
            return total_loss
        else:
            prediction_scores, seq_relationship_score = self.cls(sequence_output, pooled_output)
            return prediction_scores, seq_relationship_score


class RobertaLMHeadModel(RobertaPreTrainedModel):

    def __init__(self, config, args):
        super(RobertaLMHeadModel, self).__init__(config)
        self.roberta = RobertaModel(config, args)
        self.is_Ngram = args.is_Ngram
        self.cls = RobertaOnlyMLMHead(config, self.roberta.embeddings.word_embeddings.weight)
        self._init_weights(self.roberta)

    def forward(self, batch, output_attentions=False):
        input_ids = batch[1]
        token_type_ids = batch[3]
        attention_mask = batch[2]
        masked_lm_labels = batch[4]
        input_Ngram_ids = None
        Ngram_attention_mask = None
        Ngram_token_type_ids = None
        Ngram_position_matrix = None
        if self.is_Ngram:
            input_Ngram_ids = batch[5]
            Ngram_attention_mask = batch[6]
            Ngram_token_type_ids = batch[7]
            Ngram_position_matrix = batch[8]

        checkpoint_activations = False

        roberta_output = self.roberta(
            input_ids=input_ids,
            token_type_ids=token_type_ids,
            attention_mask=attention_mask,
            input_Ngram_ids=input_Ngram_ids,
            Ngram_attention_mask=Ngram_attention_mask,
            Ngram_token_type_ids=Ngram_token_type_ids,
            Ngram_position_matrix=Ngram_position_matrix,
            output_all_encoded_layers=False,
            checkpoint_activations=checkpoint_activations,
        )

        sequence_output = roberta_output[0]

        if masked_lm_labels is None:
            prediction_scores = self.cls(sequence_output)
            return prediction_scores

        masked_token_indexes = torch.nonzero((masked_lm_labels + 1).view(-1), as_tuple=False).view(
            -1
        )
        prediction_scores = self.cls(sequence_output, masked_token_indexes)

        if masked_lm_labels is not None:
            loss_fct = CrossEntropyLoss(ignore_index=-1)
            target = torch.index_select(masked_lm_labels.view(-1), 0, masked_token_indexes)
            masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), target)

            outputs = (masked_lm_loss,)
            if output_attentions:
                outputs += (roberta_output[-1],)
            return outputs
        else:
            return prediction_scores


class RobertaForSequenceClassification(RobertaPreTrainedModel):

    def __init__(self, config, args=None):
        super(RobertaForSequenceClassification, self).__init__(config)
        self.num_labels = config.num_labels
        self.roberta = RobertaModel(config, args)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.is_Ngram = args.is_Ngram
        self.classifier = nn.Linear(config.hidden_size, self.num_labels)
        self._init_weights(self.roberta)

    def forward(
            self,
            input_ids,
            token_type_ids=None,
            attention_mask=None,
            input_Ngram_ids=None,
            Ngram_attention_mask=None,
            Ngram_token_type_ids=None,
            Ngram_position_matrix=None,
            labels=None,
            checkpoint_activations=False,
            **kwargs,
    ):
        if not self.is_Ngram:
            input_Ngram_ids = None
            Ngram_attention_mask = None
            Ngram_token_type_ids = None
            Ngram_position_matrix = None

        outputs = self.roberta(
            input_ids=input_ids,
            token_type_ids=token_type_ids,
            attention_mask=attention_mask,
            input_Ngram_ids=input_Ngram_ids,
            Ngram_attention_mask=Ngram_attention_mask,
            Ngram_token_type_ids=Ngram_token_type_ids,
            Ngram_position_matrix=Ngram_position_matrix,
            output_all_encoded_layers=False,
            checkpoint_activations=checkpoint_activations,
        )

        pooled_output = outputs[1]

        pooled_output = self.dropout(pooled_output)
        logits = self.classifier(pooled_output)

        loss = None
        if labels is not None:
            if self.num_labels == 1:
                #  We are doing regression
                loss_fct = MSELoss()
                loss = loss_fct(logits.view(-1), labels.view(-1))
            else:
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))

        return SequenceClassifierOutput(
            loss=loss,
            logits=logits,
            hidden_states=None,
            attentions=None,
        )
