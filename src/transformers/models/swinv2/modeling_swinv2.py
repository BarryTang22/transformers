# coding=utf-8
# Copyright 2022 Microsoft Research and The HuggingFace Inc. team. All rights reserved.
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
"""PyTorch Swinv2 Transformer model."""

import collections.abc
import math
import random
import warnings
from dataclasses import dataclass
from typing import Optional, Tuple, Union

import torch
import torch.utils.checkpoint
from torch import Tensor, nn
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss

from ...activations import ACT2FN
from ...modeling_outputs import BackboneOutput
from ...modeling_utils import PreTrainedModel
from ...pytorch_utils import find_pruneable_heads_and_indices, meshgrid, prune_linear_layer
from ...utils import (
    ModelOutput,
    add_code_sample_docstrings,
    add_start_docstrings,
    add_start_docstrings_to_model_forward,
    logging,
    replace_return_docstrings,
    torch_int,
)
from ...utils.backbone_utils import BackboneMixin
from .configuration_swinv2 import Swinv2Config
from .swinv2_utils import get_2d_sincos_pos_embed

import torch.nn.functional as F

from timm.layers import trunc_normal_
from timm.models.vision_transformer import Block


logger = logging.get_logger(__name__)

# General docstring
_CONFIG_FOR_DOC = "Swinv2Config"

# Base docstring
_CHECKPOINT_FOR_DOC = "microsoft/swinv2-tiny-patch4-window8-256"
_EXPECTED_OUTPUT_SHAPE = [1, 64, 768]

# Image classification docstring
_IMAGE_CLASS_CHECKPOINT = "microsoft/swinv2-tiny-patch4-window8-256"
_IMAGE_CLASS_EXPECTED_OUTPUT = "Egyptian cat"


# drop_path, Swinv2PatchEmbeddings, Swinv2PatchMerging and Swinv2DropPath are from https://github.com/rwightman/pytorch-image-models/blob/master/timm/models/swin_transformer_v2.py.


@dataclass
# Copied from transformers.models.swin.modeling_swin.SwinEncoderOutput with Swin->Swinv2
class Swinv2EncoderOutput(ModelOutput):
    """
    Swinv2 encoder's outputs, with potential hidden states and attentions.

    Args:
        last_hidden_state (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`):
            Sequence of hidden-states at the output of the last layer of the model.
        hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings + one for the output of each stage) of
            shape `(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        attentions (`tuple(torch.FloatTensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `torch.FloatTensor` (one for each stage) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.
        reshaped_hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings + one for the output of each stage) of
            shape `(batch_size, hidden_size, height, width)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs reshaped to
            include the spatial dimensions.
    """

    last_hidden_state: torch.FloatTensor = None
    hidden_states: Optional[Tuple[torch.FloatTensor, ...]] = None
    attentions: Optional[Tuple[torch.FloatTensor, ...]] = None
    reshaped_hidden_states: Optional[Tuple[torch.FloatTensor, ...]] = None
    final_mixing_mask: Optional[torch.FloatTensor] = None

@dataclass
# Copied from transformers.models.swin.modeling_swin.SwinModelOutput with Swin->Swinv2
class Swinv2ModelOutput(ModelOutput):
    """
    Swinv2 model's outputs that also contains a pooling of the last hidden states.

    Args:
        last_hidden_state (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`):
            Sequence of hidden-states at the output of the last layer of the model.
        pooler_output (`torch.FloatTensor` of shape `(batch_size, hidden_size)`, *optional*, returned when `add_pooling_layer=True` is passed):
            Average pooling of the last layer hidden-state.
        hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings + one for the output of each stage) of
            shape `(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        attentions (`tuple(torch.FloatTensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `torch.FloatTensor` (one for each stage) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.
        reshaped_hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings + one for the output of each stage) of
            shape `(batch_size, hidden_size, height, width)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs reshaped to
            include the spatial dimensions.
    """

    last_hidden_state: torch.FloatTensor = None
    pooler_output: Optional[torch.FloatTensor] = None
    hidden_states: Optional[Tuple[torch.FloatTensor, ...]] = None
    attentions: Optional[Tuple[torch.FloatTensor, ...]] = None
    reshaped_hidden_states: Optional[Tuple[torch.FloatTensor, ...]] = None


@dataclass
# Copied from transformers.models.swin.modeling_swin.SwinMaskedImageModelingOutput with Swin->Swinv2
class Swinv2MaskedImageModelingOutput(ModelOutput):
    """
    Swinv2 masked image model outputs.

    Args:
        loss (`torch.FloatTensor` of shape `(1,)`, *optional*, returned when `bool_masked_pos` is provided):
            Masked image modeling (MLM) loss.
        reconstruction (`torch.FloatTensor` of shape `(batch_size, num_channels, height, width)`):
            Reconstructed pixel values.
        hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings + one for the output of each stage) of
            shape `(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        attentions (`tuple(torch.FloatTensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `torch.FloatTensor` (one for each stage) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.
        reshaped_hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings + one for the output of each stage) of
            shape `(batch_size, hidden_size, height, width)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs reshaped to
            include the spatial dimensions.
    """

    loss: Optional[torch.FloatTensor] = None
    reconstruction: torch.FloatTensor = None
    hidden_states: Optional[Tuple[torch.FloatTensor, ...]] = None
    attentions: Optional[Tuple[torch.FloatTensor, ...]] = None
    reshaped_hidden_states: Optional[Tuple[torch.FloatTensor, ...]] = None

    @property
    def logits(self):
        warnings.warn(
            "logits attribute is deprecated and will be removed in version 5 of Transformers."
            " Please use the reconstruction attribute to retrieve the final output instead.",
            FutureWarning,
        )
        return self.reconstruction


@dataclass
# Copied from transformers.models.swin.modeling_swin.SwinImageClassifierOutput with Swin->Swinv2
class Swinv2ImageClassifierOutput(ModelOutput):
    """
    Swinv2 outputs for image classification.

    Args:
        loss (`torch.FloatTensor` of shape `(1,)`, *optional*, returned when `labels` is provided):
            Classification (or regression if config.num_labels==1) loss.
        logits (`torch.FloatTensor` of shape `(batch_size, config.num_labels)`):
            Classification (or regression if config.num_labels==1) scores (before SoftMax).
        hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings + one for the output of each stage) of
            shape `(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        attentions (`tuple(torch.FloatTensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `torch.FloatTensor` (one for each stage) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.
        reshaped_hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings + one for the output of each stage) of
            shape `(batch_size, hidden_size, height, width)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs reshaped to
            include the spatial dimensions.
    """

    loss: Optional[torch.FloatTensor] = None
    logits: torch.FloatTensor = None
    hidden_states: Optional[Tuple[torch.FloatTensor, ...]] = None
    attentions: Optional[Tuple[torch.FloatTensor, ...]] = None
    reshaped_hidden_states: Optional[Tuple[torch.FloatTensor, ...]] = None


# Copied from transformers.models.swin.modeling_swin.window_partition
def window_partition(input_feature, window_size):
    """
    Partitions the given input into windows.
    """
    batch_size, height, width, num_channels = input_feature.shape
    input_feature = input_feature.view(
        batch_size, height // window_size, window_size, width // window_size, window_size, num_channels
    )
    windows = input_feature.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, window_size, window_size, num_channels)
    return windows


# Copied from transformers.models.swin.modeling_swin.window_reverse
def window_reverse(windows, window_size, height, width):
    """
    Merges windows to produce higher resolution features.
    """
    num_channels = windows.shape[-1]
    windows = windows.view(-1, height // window_size, width // window_size, window_size, window_size, num_channels)
    windows = windows.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, height, width, num_channels)
    return windows


# Copied from transformers.models.swin.modeling_swin.drop_path
def drop_path(input: torch.Tensor, drop_prob: float = 0.0, training: bool = False) -> torch.Tensor:
    """
    Drop paths (Stochastic Depth) per sample (when applied in main path of residual blocks).

    Comment by Ross Wightman: This is the same as the DropConnect impl I created for EfficientNet, etc networks,
    however, the original name is misleading as 'Drop Connect' is a different form of dropout in a separate paper...
    See discussion: https://github.com/tensorflow/tpu/issues/494#issuecomment-532968956 ... I've opted for changing the
    layer and argument names to 'drop path' rather than mix DropConnect as a layer name and use 'survival rate' as the
    argument.
    """
    if drop_prob == 0.0 or not training:
        return input
    keep_prob = 1 - drop_prob
    shape = (input.shape[0],) + (1,) * (input.ndim - 1)  # work with diff dim tensors, not just 2D ConvNets
    random_tensor = keep_prob + torch.rand(shape, dtype=input.dtype, device=input.device)
    random_tensor.floor_()  # binarize
    output = input.div(keep_prob) * random_tensor
    return output


# Copied from transformers.models.swin.modeling_swin.SwinDropPath with Swin->Swinv2
class Swinv2DropPath(nn.Module):
    """Drop paths (Stochastic Depth) per sample (when applied in main path of residual blocks)."""

    def __init__(self, drop_prob: Optional[float] = None) -> None:
        super().__init__()
        self.drop_prob = drop_prob

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        return drop_path(hidden_states, self.drop_prob, self.training)

    def extra_repr(self) -> str:
        return "p={}".format(self.drop_prob)


class Swinv2GroupPatchEmbeddings(nn.Module):
    """
    Multi-spectral patch embeddings that handle grouped channels separately.
    """

    def __init__(self, config):
        super().__init__()
        image_size, patch_size = config.image_size, config.patch_size
        image_size = image_size if isinstance(image_size, collections.abc.Iterable) else (image_size, image_size)
        patch_size = patch_size if isinstance(patch_size, collections.abc.Iterable) else (patch_size, patch_size)
        
        self.group_channel_indices = config.group_channel_indices
        self.group_embed_dims = config.group_embed_dims
        
        assert sum(self.group_embed_dims) == config.embed_dim, \
            f"Sum of group embed dims {sum(self.group_embed_dims)} must equal total embed_dim {config.embed_dim}"
        assert len(self.group_channel_indices) == len(self.group_embed_dims), \
            "Number of channel groups must match number of embedding dimensions"
        
        # Basic configuration
        self.image_size = image_size
        self.patch_size = patch_size
        self.num_patches = (image_size[1] // patch_size[1]) * (image_size[0] // patch_size[0])
        self.grid_size = (image_size[0] // patch_size[0], image_size[1] // patch_size[1])
        
        # Create separate projection layers for each group
        self.projections = nn.ModuleList([
            nn.Conv2d(
                in_channels=len(channel_indices),
                out_channels=embed_dim,
                kernel_size=patch_size,
                stride=patch_size
            )
            for channel_indices, embed_dim in zip(self.group_channel_indices, self.group_embed_dims)
        ])

    def maybe_pad(self, pixel_values, height, width):
        if width % self.patch_size[1] != 0:
            pad_values = (0, self.patch_size[1] - width % self.patch_size[1])
            pixel_values = nn.functional.pad(pixel_values, pad_values)
        if height % self.patch_size[0] != 0:
            pad_values = (0, 0, 0, self.patch_size[0] - height % self.patch_size[0])
            pixel_values = nn.functional.pad(pixel_values, pad_values)
        return pixel_values

    def forward(self, pixel_values: Optional[torch.FloatTensor]) -> Tuple[torch.Tensor, Tuple[int]]:
        batch_size, total_channels, height, width = pixel_values.shape
        pixel_values = self.maybe_pad(pixel_values, height, width)
        # Process each group separately
        group_embeddings = []

        for i, channel_indices in enumerate(self.group_channel_indices):
            # Extract channels for this group
            group_pixels = pixel_values[:, channel_indices, :, :]
            
            # Project this group
            embeddings = self.projections[i](group_pixels)  # [B, embed_dim_i, H', W']
            
            # Save output dimensions from first group
            if i == 0:
                _, _, out_height, out_width = embeddings.shape
                output_dimensions = (out_height, out_width)
            
            # Reshape to [B, embed_dim_i, num_patches]
            embeddings = embeddings.flatten(2)
            
            group_embeddings.append(embeddings)
            
        # Concatenate all group embeddings along embedding dimension
        embeddings = torch.cat(group_embeddings, dim=1)  # [B, total_embed_dim, num_patches]
        embeddings = embeddings.transpose(1, 2)  # [B, num_patches, total_embed_dim]
        
        return embeddings, output_dimensions 

# Copied from transformers.models.swin.modeling_swin.SwinEmbeddings with Swin->Swinv2
class Swinv2Embeddings(nn.Module):
    """
    Construct the patch and position embeddings. Optionally, also the mask token.
    """

    def __init__(self, config, use_mask_token=False):
        super().__init__()

        self.patch_embeddings = Swinv2GroupPatchEmbeddings(config)
        num_patches = self.patch_embeddings.num_patches
        self.patch_grid = self.patch_embeddings.grid_size
        self.mask_token = nn.Parameter(torch.zeros(1, 1, config.embed_dim)) if use_mask_token else None

        if config.use_absolute_embeddings:
            self.position_embeddings = nn.Parameter(torch.zeros(1, num_patches + 1, config.embed_dim))
        else:
            self.position_embeddings = None

        self.norm = nn.LayerNorm(config.embed_dim)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.patch_size = config.patch_size
        self.config = config

    # Copied from transformers.models.vit.modeling_vit.ViTEmbeddings.interpolate_pos_encoding
    def interpolate_pos_encoding(self, embeddings: torch.Tensor, height: int, width: int) -> torch.Tensor:
        """
        This method allows to interpolate the pre-trained position encodings, to be able to use the model on higher resolution
        images. This method is also adapted to support torch.jit tracing.

        Adapted from:
        - https://github.com/facebookresearch/dino/blob/de9ee3df6cf39fac952ab558447af1fa1365362a/vision_transformer.py#L174-L194, and
        - https://github.com/facebookresearch/dinov2/blob/e1277af2ba9496fbadf7aec6eba56e8d882d1e35/dinov2/models/vision_transformer.py#L179-L211
        """

        num_patches = embeddings.shape[1] - 1
        num_positions = self.position_embeddings.shape[1] - 1

        # always interpolate when tracing to ensure the exported model works for dynamic input shapes
        if not torch.jit.is_tracing() and num_patches == num_positions and height == width:
            return self.position_embeddings

        class_pos_embed = self.position_embeddings[:, :1]
        patch_pos_embed = self.position_embeddings[:, 1:]

        dim = embeddings.shape[-1]

        new_height = height // self.patch_size
        new_width = width // self.patch_size

        sqrt_num_positions = torch_int(num_positions**0.5)
        patch_pos_embed = patch_pos_embed.reshape(1, sqrt_num_positions, sqrt_num_positions, dim)
        patch_pos_embed = patch_pos_embed.permute(0, 3, 1, 2)

        patch_pos_embed = nn.functional.interpolate(
            patch_pos_embed,
            size=(new_height, new_width),
            mode="bicubic",
            align_corners=False,
        )

        patch_pos_embed = patch_pos_embed.permute(0, 2, 3, 1).view(1, -1, dim)

        return torch.cat((class_pos_embed, patch_pos_embed), dim=1)

    def forward(
        self,
        pixel_values: Optional[torch.FloatTensor],
        bool_masked_pos: Optional[torch.BoolTensor] = None,
        interpolate_pos_encoding: bool = False,
    ) -> Tuple[torch.Tensor]:
        _, num_channels, height, width = pixel_values.shape
        embeddings, output_dimensions = self.patch_embeddings(pixel_values)
        embeddings = self.norm(embeddings)
        batch_size, seq_len, _ = embeddings.size()

        if bool_masked_pos is not None:
            mask_tokens = self.mask_token.expand(batch_size, seq_len, -1)
            # replace the masked visual tokens by mask_tokens
            mask = bool_masked_pos.unsqueeze(-1).type_as(mask_tokens)
            embeddings = embeddings * (1.0 - mask) + mask_tokens * mask

        if self.position_embeddings is not None:
            if interpolate_pos_encoding:
                embeddings = embeddings + self.interpolate_pos_encoding(embeddings, height, width)
            else:
                embeddings = embeddings + self.position_embeddings

        embeddings = self.dropout(embeddings)

        return embeddings, output_dimensions


# Copied from transformers.models.swin.modeling_swin.SwinPatchEmbeddings with Swin->Swinv2
class Swinv2PatchEmbeddings(nn.Module):
    """
    This class turns `pixel_values` of shape `(batch_size, num_channels, height, width)` into the initial
    `hidden_states` (patch embeddings) of shape `(batch_size, seq_length, hidden_size)` to be consumed by a
    Transformer.
    """

    def __init__(self, config):
        super().__init__()
        image_size, patch_size = config.image_size, config.patch_size
        num_channels, hidden_size = config.num_channels, config.embed_dim
        image_size = image_size if isinstance(image_size, collections.abc.Iterable) else (image_size, image_size)
        patch_size = patch_size if isinstance(patch_size, collections.abc.Iterable) else (patch_size, patch_size)
        num_patches = (image_size[1] // patch_size[1]) * (image_size[0] // patch_size[0])
        self.image_size = image_size
        self.patch_size = patch_size
        self.num_channels = num_channels
        self.num_patches = num_patches
        self.grid_size = (image_size[0] // patch_size[0], image_size[1] // patch_size[1])

        self.projection = nn.Conv2d(num_channels, hidden_size, kernel_size=patch_size, stride=patch_size)

    def maybe_pad(self, pixel_values, height, width):
        if width % self.patch_size[1] != 0:
            pad_values = (0, self.patch_size[1] - width % self.patch_size[1])
            pixel_values = nn.functional.pad(pixel_values, pad_values)
        if height % self.patch_size[0] != 0:
            pad_values = (0, 0, 0, self.patch_size[0] - height % self.patch_size[0])
            pixel_values = nn.functional.pad(pixel_values, pad_values)
        return pixel_values

    def forward(self, pixel_values: Optional[torch.FloatTensor]) -> Tuple[torch.Tensor, Tuple[int]]:
        _, num_channels, height, width = pixel_values.shape
        # pad the input to be divisible by self.patch_size, if needed
        pixel_values = self.maybe_pad(pixel_values, height, width)
        embeddings = self.projection(pixel_values)
        _, _, height, width = embeddings.shape
        output_dimensions = (height, width)
        embeddings = embeddings.flatten(2).transpose(1, 2)

        return embeddings, output_dimensions


class Swinv2PatchMerging(nn.Module):
    """
    Patch Merging Layer.

    Args:
        input_resolution (`Tuple[int]`):
            Resolution of input feature.
        dim (`int`):
            Number of input channels.
        norm_layer (`nn.Module`, *optional*, defaults to `nn.LayerNorm`):
            Normalization layer class.
    """

    def __init__(self, input_resolution: Tuple[int], dim: int, norm_layer: nn.Module = nn.LayerNorm) -> None:
        super().__init__()
        self.input_resolution = input_resolution
        self.dim = dim
        self.reduction = nn.Linear(4 * dim, 2 * dim, bias=False)
        self.norm = norm_layer(2 * dim)

    def maybe_pad(self, input_feature, height, width):
        should_pad = (height % 2 == 1) or (width % 2 == 1)
        if should_pad:
            pad_values = (0, 0, 0, width % 2, 0, height % 2)
            input_feature = nn.functional.pad(input_feature, pad_values)

        return input_feature

    def forward(self, input_feature: torch.Tensor, input_dimensions: Tuple[int, int]) -> torch.Tensor:
        height, width = input_dimensions
        # `dim` is height * width
        batch_size, dim, num_channels = input_feature.shape

        input_feature = input_feature.view(batch_size, height, width, num_channels)
        # pad input to be disible by width and height, if needed
        input_feature = self.maybe_pad(input_feature, height, width)
        # [batch_size, height/2, width/2, num_channels]
        input_feature_0 = input_feature[:, 0::2, 0::2, :]
        # [batch_size, height/2, width/2, num_channels]
        input_feature_1 = input_feature[:, 1::2, 0::2, :]
        # [batch_size, height/2, width/2, num_channels]
        input_feature_2 = input_feature[:, 0::2, 1::2, :]
        # [batch_size, height/2, width/2, num_channels]
        input_feature_3 = input_feature[:, 1::2, 1::2, :]
        # [batch_size, height/2 * width/2, 4*num_channels]
        input_feature = torch.cat([input_feature_0, input_feature_1, input_feature_2, input_feature_3], -1)
        input_feature = input_feature.view(batch_size, -1, 4 * num_channels)  # [batch_size, height/2 * width/2, 4*C]

        input_feature = self.reduction(input_feature)
        input_feature = self.norm(input_feature)

        return input_feature


class Swinv2SelfAttention(nn.Module):
    def __init__(self, config, dim, num_heads, window_size, pretrained_window_size=[0, 0]):
        super().__init__()
        if dim % num_heads != 0:
            raise ValueError(
                f"The hidden size ({dim}) is not a multiple of the number of attention heads ({num_heads})"
            )

        self.num_attention_heads = num_heads
        self.attention_head_size = int(dim / num_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size
        self.window_size = (
            window_size if isinstance(window_size, collections.abc.Iterable) else (window_size, window_size)
        )
        self.pretrained_window_size = pretrained_window_size
        self.logit_scale = nn.Parameter(torch.log(10 * torch.ones((num_heads, 1, 1))))
        # mlp to generate continuous relative position bias
        self.continuous_position_bias_mlp = nn.Sequential(
            nn.Linear(2, 512, bias=True), nn.ReLU(inplace=True), nn.Linear(512, num_heads, bias=False)
        )

        # get relative_coords_table
        relative_coords_h = torch.arange(-(self.window_size[0] - 1), self.window_size[0], dtype=torch.int64).float()
        relative_coords_w = torch.arange(-(self.window_size[1] - 1), self.window_size[1], dtype=torch.int64).float()
        relative_coords_table = (
            torch.stack(meshgrid([relative_coords_h, relative_coords_w], indexing="ij"))
            .permute(1, 2, 0)
            .contiguous()
            .unsqueeze(0)
        )  # [1, 2*window_height - 1, 2*window_width - 1, 2]
        if pretrained_window_size[0] > 0:
            relative_coords_table[:, :, :, 0] /= pretrained_window_size[0] - 1
            relative_coords_table[:, :, :, 1] /= pretrained_window_size[1] - 1
        elif window_size > 1:
            relative_coords_table[:, :, :, 0] /= self.window_size[0] - 1
            relative_coords_table[:, :, :, 1] /= self.window_size[1] - 1
        relative_coords_table *= 8  # normalize to -8, 8
        relative_coords_table = (
            torch.sign(relative_coords_table) * torch.log2(torch.abs(relative_coords_table) + 1.0) / math.log2(8)
        )
        # set to same dtype as mlp weight
        relative_coords_table = relative_coords_table.to(next(self.continuous_position_bias_mlp.parameters()).dtype)
        self.register_buffer("relative_coords_table", relative_coords_table, persistent=False)

        # get pair-wise relative position index for each token inside the window
        coords_h = torch.arange(self.window_size[0])
        coords_w = torch.arange(self.window_size[1])
        coords = torch.stack(meshgrid([coords_h, coords_w], indexing="ij"))
        coords_flatten = torch.flatten(coords, 1)
        relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]
        relative_coords = relative_coords.permute(1, 2, 0).contiguous()
        relative_coords[:, :, 0] += self.window_size[0] - 1
        relative_coords[:, :, 1] += self.window_size[1] - 1
        relative_coords[:, :, 0] *= 2 * self.window_size[1] - 1
        relative_position_index = relative_coords.sum(-1)
        self.register_buffer("relative_position_index", relative_position_index, persistent=False)

        self.query = nn.Linear(self.all_head_size, self.all_head_size, bias=config.qkv_bias)
        self.key = nn.Linear(self.all_head_size, self.all_head_size, bias=False)
        self.value = nn.Linear(self.all_head_size, self.all_head_size, bias=config.qkv_bias)
        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.FloatTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        output_attentions: Optional[bool] = False,
        mixing_mask: Optional[torch.FloatTensor] = None,
    ) -> Tuple[torch.Tensor]:
        batch_size, dim, num_channels = hidden_states.shape
        mixed_query_layer = self.query(hidden_states)

        key_layer = self.transpose_for_scores(self.key(hidden_states))
        value_layer = self.transpose_for_scores(self.value(hidden_states))
        query_layer = self.transpose_for_scores(mixed_query_layer)

        # cosine attention
        attention_scores = nn.functional.normalize(query_layer, dim=-1) @ nn.functional.normalize(
            key_layer, dim=-1
        ).transpose(-2, -1)
        logit_scale = torch.clamp(self.logit_scale, max=math.log(1.0 / 0.01)).exp()
        attention_scores = attention_scores * logit_scale
        relative_position_bias_table = self.continuous_position_bias_mlp(self.relative_coords_table).view(
            -1, self.num_attention_heads
        )
        # [window_height*window_width,window_height*window_width,num_attention_heads]
        relative_position_bias = relative_position_bias_table[self.relative_position_index.view(-1)].view(
            self.window_size[0] * self.window_size[1], self.window_size[0] * self.window_size[1], -1
        )
        # [num_attention_heads,window_height*window_width,window_height*window_width]
        relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()  # nH, Wh*Ww, Wh*Ww
        relative_position_bias = 16 * torch.sigmoid(relative_position_bias)
        attention_scores = attention_scores + relative_position_bias.unsqueeze(0)

        # Handle mixing mask before regular attention mask
        if mixing_mask is not None:
            mixing_mask = mixing_mask.reshape(batch_size, 1, 1, dim)
            mixing_attn_mask = mixing_mask * mixing_mask.transpose(2, 3) + (1 - mixing_mask) * (1 - mixing_mask).transpose(2, 3)
            mixing_attn_mask = 1 - mixing_attn_mask
            attention_scores = attention_scores - 1e30 * mixing_attn_mask

        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in Swinv2Model forward() function)
            mask_shape = attention_mask.shape[0]
            attention_scores = attention_scores.view(
                batch_size // mask_shape, mask_shape, self.num_attention_heads, dim, dim
            ) + attention_mask.unsqueeze(1).unsqueeze(0)
            attention_scores = attention_scores + attention_mask.unsqueeze(1).unsqueeze(0)
            attention_scores = attention_scores.view(-1, self.num_attention_heads, dim, dim)

        # Normalize the attention scores to probabilities.
        attention_probs = nn.functional.softmax(attention_scores, dim=-1)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        context_layer = torch.matmul(attention_probs, value_layer)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(new_context_layer_shape)

        outputs = (context_layer, attention_probs) if output_attentions else (context_layer,)

        return outputs


# Copied from transformers.models.swin.modeling_swin.SwinSelfOutput with Swin->Swinv2
class Swinv2SelfOutput(nn.Module):
    def __init__(self, config, dim):
        super().__init__()
        self.dense = nn.Linear(dim, dim)
        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)

    def forward(self, hidden_states: torch.Tensor, input_tensor: torch.Tensor) -> torch.Tensor:
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)

        return hidden_states


class Swinv2Attention(nn.Module):
    def __init__(self, config, dim, num_heads, window_size, pretrained_window_size=0):
        super().__init__()
        self.self = Swinv2SelfAttention(
            config=config,
            dim=dim,
            num_heads=num_heads,
            window_size=window_size,
            pretrained_window_size=pretrained_window_size
            if isinstance(pretrained_window_size, collections.abc.Iterable)
            else (pretrained_window_size, pretrained_window_size),
        )
        self.output = Swinv2SelfOutput(config, dim)
        self.pruned_heads = set()

    def prune_heads(self, heads):
        if len(heads) == 0:
            return
        heads, index = find_pruneable_heads_and_indices(
            heads, self.self.num_attention_heads, self.self.attention_head_size, self.pruned_heads
        )

        # Prune linear layers
        self.self.query = prune_linear_layer(self.self.query, index)
        self.self.key = prune_linear_layer(self.self.key, index)
        self.self.value = prune_linear_layer(self.self.value, index)
        self.output.dense = prune_linear_layer(self.output.dense, index, dim=1)

        # Update hyper params and store pruned heads
        self.self.num_attention_heads = self.self.num_attention_heads - len(heads)
        self.self.all_head_size = self.self.attention_head_size * self.self.num_attention_heads
        self.pruned_heads = self.pruned_heads.union(heads)

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.FloatTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        output_attentions: Optional[bool] = False,
        mixing_mask: Optional[torch.FloatTensor] = None,
    ) -> Tuple[torch.Tensor]:
        self_outputs = self.self(hidden_states, attention_mask, head_mask, output_attentions, mixing_mask)
        attention_output = self.output(self_outputs[0], hidden_states)
        outputs = (attention_output,) + self_outputs[1:]  # add attentions if we output them
        return outputs


# Copied from transformers.models.swin.modeling_swin.SwinIntermediate with Swin->Swinv2
class Swinv2Intermediate(nn.Module):
    def __init__(self, config, dim):
        super().__init__()
        self.dense = nn.Linear(dim, int(config.mlp_ratio * dim))
        if isinstance(config.hidden_act, str):
            self.intermediate_act_fn = ACT2FN[config.hidden_act]
        else:
            self.intermediate_act_fn = config.hidden_act

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        hidden_states = self.dense(hidden_states)
        hidden_states = self.intermediate_act_fn(hidden_states)
        return hidden_states


# Copied from transformers.models.swin.modeling_swin.SwinOutput with Swin->Swinv2
class Swinv2Output(nn.Module):
    def __init__(self, config, dim):
        super().__init__()
        self.dense = nn.Linear(int(config.mlp_ratio * dim), dim)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        return hidden_states


class Swinv2Layer(nn.Module):
    def __init__(
        self, config, dim, input_resolution, num_heads, drop_path_rate=0.0, shift_size=0, pretrained_window_size=0
    ):
        super().__init__()
        self.input_resolution = input_resolution
        window_size, shift_size = self._compute_window_shift(
            (config.window_size, config.window_size), (shift_size, shift_size)
        )
        self.window_size = window_size[0]
        self.shift_size = shift_size[0]
        self.attention = Swinv2Attention(
            config=config,
            dim=dim,
            num_heads=num_heads,
            window_size=self.window_size,
            pretrained_window_size=pretrained_window_size
            if isinstance(pretrained_window_size, collections.abc.Iterable)
            else (pretrained_window_size, pretrained_window_size),
        )
        self.layernorm_before = nn.LayerNorm(dim, eps=config.layer_norm_eps)
        self.drop_path = Swinv2DropPath(drop_path_rate) if drop_path_rate > 0.0 else nn.Identity()
        self.intermediate = Swinv2Intermediate(config, dim)
        self.output = Swinv2Output(config, dim)
        self.layernorm_after = nn.LayerNorm(dim, eps=config.layer_norm_eps)

    def _compute_window_shift(self, target_window_size, target_shift_size) -> Tuple[Tuple[int, int], Tuple[int, int]]:
        window_size = [r if r <= w else w for r, w in zip(self.input_resolution, target_window_size)]
        shift_size = [0 if r <= w else s for r, w, s in zip(self.input_resolution, window_size, target_shift_size)]
        return window_size, shift_size

    def get_attn_mask(self, height, width, dtype):
        if self.shift_size > 0:
            # calculate attention mask for shifted window multihead self attention
            img_mask = torch.zeros((1, height, width, 1), dtype=dtype)
            height_slices = (
                slice(0, -self.window_size),
                slice(-self.window_size, -self.shift_size),
                slice(-self.shift_size, None),
            )
            width_slices = (
                slice(0, -self.window_size),
                slice(-self.window_size, -self.shift_size),
                slice(-self.shift_size, None),
            )
            count = 0
            for height_slice in height_slices:
                for width_slice in width_slices:
                    img_mask[:, height_slice, width_slice, :] = count
                    count += 1

            mask_windows = window_partition(img_mask, self.window_size)
            mask_windows = mask_windows.view(-1, self.window_size * self.window_size)
            attn_mask = mask_windows.unsqueeze(1) - mask_windows.unsqueeze(2)
            attn_mask = attn_mask.masked_fill(attn_mask != 0, float(-100.0)).masked_fill(attn_mask == 0, float(0.0))
        else:
            attn_mask = None
        return attn_mask

    def maybe_pad(self, hidden_states, height, width):
        pad_right = (self.window_size - width % self.window_size) % self.window_size
        pad_bottom = (self.window_size - height % self.window_size) % self.window_size
        pad_values = (0, 0, 0, pad_right, 0, pad_bottom)
        hidden_states = nn.functional.pad(hidden_states, pad_values)
        return hidden_states, pad_values

    def forward(
        self,
        hidden_states: torch.Tensor,
        input_dimensions: Tuple[int, int],
        head_mask: Optional[torch.FloatTensor] = None,
        output_attentions: Optional[bool] = False,
        mixing_mask: Optional[torch.FloatTensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        height, width = input_dimensions
        batch_size, _, channels = hidden_states.size()
        shortcut = hidden_states
        
        # pad hidden_states to multiples of window size
        hidden_states = hidden_states.view(batch_size, height, width, channels)
        
        hidden_states, pad_values = self.maybe_pad(hidden_states, height, width)
        _, height_pad, width_pad, _ = hidden_states.shape
        
        if mixing_mask is not None:
            mixing_mask = mixing_mask.view(batch_size, height, width, 1)
            mixing_mask, _ = self.maybe_pad(mixing_mask, height, width)
            
            if self.shift_size > 0:
                mixing_mask = torch.roll(mixing_mask, shifts=(-self.shift_size, -self.shift_size), dims=(1, 2))
            
            mixing_mask_windows = window_partition(mixing_mask, self.window_size)
            mixing_mask_windows = mixing_mask_windows.view(-1, self.window_size * self.window_size, 1)

        # cyclic shift
        if self.shift_size > 0:
            shifted_hidden_states = torch.roll(hidden_states, shifts=(-self.shift_size, -self.shift_size), dims=(1, 2))
        else:
            shifted_hidden_states = hidden_states

        # partition windows
        hidden_states_windows = window_partition(shifted_hidden_states, self.window_size)
        hidden_states_windows = hidden_states_windows.view(-1, self.window_size * self.window_size, channels)
        attn_mask = self.get_attn_mask(height_pad, width_pad, dtype=hidden_states.dtype)
        if attn_mask is not None:
            attn_mask = attn_mask.to(hidden_states_windows.device)

        attention_outputs = self.attention(
            hidden_states_windows, attn_mask, head_mask, output_attentions=output_attentions, 
            mixing_mask=mixing_mask_windows if mixing_mask is not None else None
        )

        attention_output = attention_outputs[0]

        attention_windows = attention_output.view(-1, self.window_size, self.window_size, channels)
        shifted_windows = window_reverse(attention_windows, self.window_size, height_pad, width_pad)

        # reverse cyclic shift
        if self.shift_size > 0:
            attention_windows = torch.roll(shifted_windows, shifts=(self.shift_size, self.shift_size), dims=(1, 2))
        else:
            attention_windows = shifted_windows

        was_padded = pad_values[3] > 0 or pad_values[5] > 0
        if was_padded:
            attention_windows = attention_windows[:, :height, :width, :].contiguous()

        attention_windows = attention_windows.view(batch_size, height * width, channels)
        hidden_states = self.layernorm_before(attention_windows)
        hidden_states = shortcut + self.drop_path(hidden_states)

        layer_output = self.intermediate(hidden_states)
        layer_output = self.output(layer_output)
        layer_output = hidden_states + self.drop_path(self.layernorm_after(layer_output))

        layer_outputs = (layer_output, attention_outputs[1]) if output_attentions else (layer_output,)
        return layer_outputs


class Swinv2Stage(nn.Module):
    def __init__(
        self, config, dim, input_resolution, depth, num_heads, drop_path, downsample, pretrained_window_size=0
    ):
        super().__init__()
        self.config = config
        self.dim = dim
        blocks = []
        for i in range(depth):
            block = Swinv2Layer(
                config=config,
                dim=dim,
                input_resolution=input_resolution,
                num_heads=num_heads,
                drop_path_rate=drop_path[i],
                shift_size=0, #if (i % 2 == 0) else config.window_size // 2,
                pretrained_window_size=pretrained_window_size,
            )
            blocks.append(block)
        self.blocks = nn.ModuleList(blocks)

        # patch merging layer
        if downsample is not None:
            self.downsample = downsample(input_resolution, dim=dim, norm_layer=nn.LayerNorm)
        else:
            self.downsample = None

        self.pointing = False

    def forward(
        self,
        hidden_states: torch.Tensor,
        input_dimensions: Tuple[int, int],
        head_mask: Optional[torch.FloatTensor] = None,
        output_attentions: Optional[bool] = False,
        mixing_mask: Optional[torch.FloatTensor] = None,
    ) -> Tuple[torch.Tensor]:
        height, width = input_dimensions

        for i, layer_module in enumerate(self.blocks):
            layer_head_mask = head_mask[i] if head_mask is not None else None

            layer_outputs = layer_module(
                hidden_states,
                input_dimensions,
                layer_head_mask,
                output_attentions,
                mixing_mask,
            )

            hidden_states = layer_outputs[0]

        hidden_states_before_downsampling = hidden_states
        if self.downsample is not None:
            height_downsampled, width_downsampled = (height + 1) // 2, (width + 1) // 2
            output_dimensions = (height, width, height_downsampled, width_downsampled)
            hidden_states = self.downsample(hidden_states_before_downsampling, input_dimensions)
            
            # downsample mixing mask if provided
            if mixing_mask is not None:
                B = mixing_mask.shape[0]
                mixing_mask = mixing_mask.view(B, height, width, -1)
                mixing_mask = F.interpolate(
                    mixing_mask.permute(0, 3, 1, 2),
                    size=(height_downsampled, width_downsampled),
                    mode='nearest'
                )
                mixing_mask = mixing_mask.permute(0, 2, 3, 1).reshape(B, -1, 1)

        else:
            output_dimensions = (height, width, height, width)

        stage_outputs = (hidden_states, hidden_states_before_downsampling, output_dimensions, mixing_mask)

        if output_attentions:
            stage_outputs += layer_outputs[1:]
        return stage_outputs


class Swinv2Encoder(nn.Module):
    def __init__(self, config, grid_size, pretrained_window_sizes=(0, 0, 0, 0)):
        super().__init__()
        self.num_layers = len(config.depths)
        self.config = config
        if self.config.pretrained_window_sizes is not None:
            pretrained_window_sizes = config.pretrained_window_sizes
        dpr = [x.item() for x in torch.linspace(0, config.drop_path_rate, sum(config.depths))]

        layers = []
        for i_layer in range(self.num_layers):
            stage = Swinv2Stage(
                config=config,
                dim=int(config.embed_dim * 2**i_layer),
                input_resolution=(grid_size[0] // (2**i_layer), grid_size[1] // (2**i_layer)),
                depth=config.depths[i_layer],
                num_heads=config.num_heads[i_layer],
                drop_path=dpr[sum(config.depths[:i_layer]) : sum(config.depths[: i_layer + 1])],
                downsample=Swinv2PatchMerging if (i_layer < self.num_layers - 1) else None,
                pretrained_window_size=pretrained_window_sizes[i_layer],
            )
            layers.append(stage)
        self.layers = nn.ModuleList(layers)

        self.gradient_checkpointing = False

    def forward(
        self,
        hidden_states: torch.Tensor,
        input_dimensions: Tuple[int, int],
        head_mask: Optional[torch.FloatTensor] = None,
        output_attentions: Optional[bool] = False,
        output_hidden_states: Optional[bool] = False,
        output_hidden_states_before_downsampling: Optional[bool] = False,
        return_dict: Optional[bool] = True,
        mixing_mask: Optional[torch.FloatTensor] = None,
    ) -> Union[Tuple, Swinv2EncoderOutput]:
        all_hidden_states = () if output_hidden_states else None
        all_reshaped_hidden_states = () if output_hidden_states else None
        all_self_attentions = () if output_attentions else None

        if output_hidden_states:
            batch_size, _, hidden_size = hidden_states.shape
            # rearrange b (h w) c -> b c h w
            reshaped_hidden_state = hidden_states.view(batch_size, *input_dimensions, hidden_size)
            reshaped_hidden_state = reshaped_hidden_state.permute(0, 3, 1, 2)
            all_hidden_states += (hidden_states,)
            all_reshaped_hidden_states += (reshaped_hidden_state,)

        for i, layer_module in enumerate(self.layers):
            layer_head_mask = head_mask[i] if head_mask is not None else None

            if self.gradient_checkpointing and self.training:
                layer_outputs = self._gradient_checkpointing_func(
                    layer_module.__call__, hidden_states, input_dimensions, layer_head_mask, mixing_mask
                )
            else:
                layer_outputs = layer_module(
                    hidden_states,
                    input_dimensions,
                    layer_head_mask,
                    output_attentions,
                    mixing_mask,
                )

            hidden_states = layer_outputs[0]
            hidden_states_before_downsampling = layer_outputs[1]
            output_dimensions = layer_outputs[2]
            mixing_mask = layer_outputs[3]
            input_dimensions = (output_dimensions[-2], output_dimensions[-1])

            if output_hidden_states and output_hidden_states_before_downsampling:
                batch_size, _, hidden_size = hidden_states_before_downsampling.shape
                # rearrange b (h w) c -> b c h w
                # here we use the original (not downsampled) height and width
                reshaped_hidden_state = hidden_states_before_downsampling.view(
                    batch_size, *(output_dimensions[0], output_dimensions[1]), hidden_size
                )
                reshaped_hidden_state = reshaped_hidden_state.permute(0, 3, 1, 2)
                all_hidden_states += (hidden_states_before_downsampling,)
                all_reshaped_hidden_states += (reshaped_hidden_state,)
            elif output_hidden_states and not output_hidden_states_before_downsampling:
                batch_size, _, hidden_size = hidden_states.shape
                # rearrange b (h w) c -> b c h w
                reshaped_hidden_state = hidden_states.view(batch_size, *input_dimensions, hidden_size)
                reshaped_hidden_state = reshaped_hidden_state.permute(0, 3, 1, 2)
                all_hidden_states += (hidden_states,)
                all_reshaped_hidden_states += (reshaped_hidden_state,)

            if output_attentions:
                all_self_attentions += layer_outputs[3:]

        if not return_dict:
            return tuple(
                v
                for v in [hidden_states, all_hidden_states, all_self_attentions, all_reshaped_hidden_states, mixing_mask]
                if v is not None
            )

        return Swinv2EncoderOutput(
            last_hidden_state=hidden_states,
            hidden_states=all_hidden_states,
            attentions=all_self_attentions,
            reshaped_hidden_states=all_reshaped_hidden_states,
            final_mixing_mask=mixing_mask
        )
        

class Swinv2MixMAEDecoder(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        
        self.encoder_stride = config.encoder_stride
        self.base_num_patches = (config.image_size // self.encoder_stride) ** 2
        
        # mask token and position embedding
        self.mask_token = nn.Parameter(torch.zeros(1, 1, config.decoder_dim))
        trunc_normal_(self.mask_token, mean=0., std=.02)
        
        self.decoder_pos_embed = nn.Parameter(
            torch.zeros(1, self.base_num_patches, config.decoder_dim),
            requires_grad=False
        )
        
        # decoder components
        self.decoder_embed = nn.Linear(config.hidden_size, config.decoder_dim)
        self.decoder_blocks = nn.ModuleList([
            Block(
                dim=config.decoder_dim,
                num_heads=config.decoder_num_heads,
                mlp_ratio=config.mlp_ratio,
                qkv_bias=True,
                norm_layer=nn.LayerNorm
            ) for _ in range(config.decoder_depth)
        ])
        
        self.decoder_norm = nn.LayerNorm(config.decoder_dim, eps=config.layer_norm_eps)
        self.decoder_pred = nn.Linear(
            config.decoder_dim, 
            self.encoder_stride ** 2 * config.num_channels, 
            bias=True
        )
        
        self.gradient_checkpointing = False
        self.initialize_weights()

    def initialize_weights(self):
        # initialize decoder position embedding
        decoder_pos_embed = get_2d_sincos_pos_embed(
            self.decoder_pos_embed.shape[-1],
            int(self.base_num_patches**.5),
            cls_token=False
        )
        self.decoder_pos_embed.data.copy_(torch.from_numpy(decoder_pos_embed).float().unsqueeze(0))
        self.apply(self._init_weights)
        
    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            # we use xavier_uniform following official JAX ViT:
            torch.nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
            
    def interpolate_pos_encoding(self, embeddings: torch.Tensor) -> torch.Tensor:
        """
        Interpolate positional embeddings when input size differs from default size.
        Args:
            embeddings: Input embeddings to get the target size from
        """
        B, L, C = embeddings.shape
        
        # Get current grid size from sequence length
        current_size = int(L ** 0.5)
        # Get base grid size from initialized pos embeddings
        base_size = int(self.base_num_patches ** 0.5)
        
        # Skip interpolation if sizes match
        if current_size == base_size:
            return self.decoder_pos_embed.expand(B * 2, -1, -1)
            
        # Reshape pos embedding to 2D grid
        pos_embed = self.decoder_pos_embed  # [1, base_size*base_size, decoder_dim]
        pos_embed = pos_embed.reshape(1, base_size, base_size, -1)  # [1, base_size, base_size, decoder_dim]
        pos_embed = pos_embed.permute(0, 3, 1, 2)  # [1, decoder_dim, base_size, base_size]
        
        # Interpolate
        pos_embed = F.interpolate(
            pos_embed, 
            size=(current_size, current_size),
            mode='bicubic',
            align_corners=False
        )
        
        # Reshape back
        pos_embed = pos_embed.permute(0, 2, 3, 1)  # [1, current_size, current_size, decoder_dim]
        pos_embed = pos_embed.reshape(1, current_size * current_size, -1)  # [1, L, decoder_dim]
        
        # Expand to match batch size (including the doubled batch from mixing)
        pos_embed = pos_embed.expand(B * 2, -1, -1)
        
        return pos_embed
 
    def forward(self, hidden_states, mixing_mask):
        x = self.decoder_embed(hidden_states)
        B, L, C = x.shape
        
        mask_tokens = self.mask_token.expand(B, L, C)
        x1 = x * (1 - mixing_mask) + mask_tokens * mixing_mask
        x2 = x * mixing_mask + mask_tokens * (1 - mixing_mask)
        x = torch.cat([x1, x2], dim=0)
        
        x = x + self.interpolate_pos_encoding(hidden_states)
        
        for blk in self.decoder_blocks:
            x = blk(x)
        x = self.decoder_norm(x)
        
        x = self.decoder_pred(x)
        
        return x
        
# Copied from transformers.models.swin.modeling_swin.SwinPreTrainedModel with Swin->Swinv2,swin->swinv2
class Swinv2PreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = Swinv2Config
    base_model_prefix = "swinv2"
    main_input_name = "pixel_values"
    supports_gradient_checkpointing = True
    _no_split_modules = ["Swinv2Stage"]

    def _init_weights(self, module):
        """Initialize the weights"""
        if isinstance(module, (nn.Linear, nn.Conv2d)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)


SWINV2_START_DOCSTRING = r"""
    This model is a PyTorch [torch.nn.Module](https://pytorch.org/docs/stable/nn.html#torch.nn.Module) sub-class. Use
    it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general usage and
    behavior.

    Parameters:
        config ([`Swinv2Config`]): Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the
            configuration. Check out the [`~PreTrainedModel.from_pretrained`] method to load the model weights.
"""

SWINV2_INPUTS_DOCSTRING = r"""
    Args:
        pixel_values (`torch.FloatTensor` of shape `(batch_size, num_channels, height, width)`):
            Pixel values. Pixel values can be obtained using [`AutoImageProcessor`]. See [`ViTImageProcessor.__call__`]
            for details.
        head_mask (`torch.FloatTensor` of shape `(num_heads,)` or `(num_layers, num_heads)`, *optional*):
            Mask to nullify selected heads of the self-attention modules. Mask values selected in `[0, 1]`:

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.

        output_attentions (`bool`, *optional*):
            Whether or not to return the attentions tensors of all attention layers. See `attentions` under returned
            tensors for more detail.
        output_hidden_states (`bool`, *optional*):
            Whether or not to return the hidden states of all layers. See `hidden_states` under returned tensors for
            more detail.
        interpolate_pos_encoding (`bool`, *optional*, default `False`):
            Whether to interpolate the pre-trained position encodings.
        return_dict (`bool`, *optional*):
            Whether or not to return a [`~utils.ModelOutput`] instead of a plain tuple.
"""

class Swinv2MixMAE(nn.Module):
    def __init__(self,
                 config
                 ):
        super().__init__()
        self.config = config
        
        self.mask_tokens = nn.ParameterList([
            nn.Parameter(torch.zeros(1, 1, dim))
            for dim in config.group_embed_dims
        ])
        for mask_token in self.mask_tokens:
            trunc_normal_(mask_token, std=.02)
        
        self.rgb_group_idx = 0
        self.rgb_only_prob = getattr(config, 'rgb_only_prob', 0.3)
        
        # split image into non-overlapping patches
        self.embeddings = Swinv2Embeddings(config)
        self.encoder = Swinv2Encoder(config, self.embeddings.patch_grid)
        
        # decoder
        self.decoder = Swinv2MixMAEDecoder(config)
        
        self.norm_pix_loss = getattr(config, 'norm_pix_loss', False)
        self.range_mask_ratio = getattr(config, 'range_mask_ratio', 0.0)
        
    def mask_non_rgb_embeddings(self, embedding_output):
        """Randomly mask groups for samples in the batch:
        1. RGB-only samples: mask all non-RGB groups
        2. Full spectral samples: randomly mask any spectral group"""
        B, L, _ = embedding_output.shape
        
        if self.training:
            is_rgb_only = torch.rand(B, device=embedding_output.device) < self.rgb_only_prob
            group_mask_prob = 0.25
        else:
            is_rgb_only = torch.zeros(B, device=embedding_output.device, dtype=torch.bool)
            group_mask_prob = 0.0
        
        group_dims = self.config.group_embed_dims
        start_idx = 0
        masked_embeddings = []
        
        for i, dim in enumerate(group_dims):
            group_embedding = embedding_output[:, :, start_idx:start_idx + dim]
            mask = self.mask_tokens[i].expand(B, L, dim)
            
            if i != self.rgb_group_idx:
                should_mask = is_rgb_only
            else:
                should_mask = torch.zeros_like(is_rgb_only, dtype=torch.bool)
                
            should_mask_group = (torch.rand(B, device=embedding_output.device) < group_mask_prob) & ~is_rgb_only
            
            group_embedding = torch.where(
                (should_mask | should_mask_group).view(B, 1, 1),
                mask,
                group_embedding
            )
                
            masked_embeddings.append(group_embedding)
            start_idx += dim
        
        return torch.cat(masked_embeddings, dim=-1)
        
    def patchify(self, imgs):
        p = self.config.encoder_stride
        assert imgs.shape[2] == imgs.shape[3] and imgs.shape[2] % p == 0
        
        h = w = imgs.shape[2] // p
        x = imgs.reshape(shape=(imgs.shape[0], self.config.num_channels, h, p, w, p))
        x = torch.einsum('nchpwq->nhwpqc', x)
        x = x.reshape(shape=(imgs.shape[0], h*w, p**2 * self.config.num_channels))
        return x
    
    def unpatchify(self, x):
        p = self.config.encoder_stride
        h = w = int(x.shape[1]**.5)
        assert h * w == x.shape[1]

        x = x.reshape(shape=(x.shape[0], h, w, p, p, self.config.num_channels))
        x = torch.einsum('nhwpqc->nchpwq', x)
        imgs = x.reshape(shape=(x.shape[0], self.config.num_channels, h * p, h * p))
        return imgs
    
    def random_masking(self, x, mask_ratio=0.5):
        B, C, H, W = x.shape
        
        # Calculate smallest scale size (final stage)
        final_scale = self.config.encoder_stride  # e.g., 32
        out_H = H // final_scale  # e.g., 224//32 = 7
        out_W = W // final_scale
        
        # Generate mask at smallest scale (like MixMAE)
        num_patches_small = out_H * out_W
        mask = torch.zeros(1, 1, num_patches_small, device=x.device)
        
        # Add random range if specified
        mask_ratio = mask_ratio + random.uniform(0.0, self.range_mask_ratio)
        
        # Generate noise and create mask at smallest scale
        noise = torch.rand(1, 1, num_patches_small, device=x.device)
        mask_idx = torch.argsort(noise, dim=2)[:, :, :int(num_patches_small * mask_ratio)]
        mask.scatter_(2, mask_idx, 1)
        
        # Upsample to highest resolution (patch grid size)
        mask = mask.reshape(1, 1, out_H, out_W)
        patch_grid_size = (H // self.config.patch_size, W // self.config.patch_size)
        mask = F.interpolate(mask, size=patch_grid_size, mode='nearest')
        
        # Format mask as before for compatibility
        mixing_mask = mask.reshape(1, -1, 1).expand(B, -1, -1)
        # mixing_mask = mixing_mask.transpose(1, 2)
        
        return mixing_mask
        
    def forward_loss(self, imgs, pred, mixing_mask, lambda_deriv: float = 0.1,
            lambda_bounds: float = 0.01, min_val: float = 0.0, max_val: float = 1.2):
        B, L, C = pred.shape
        
        # Split reconstructions
        img1_rec = pred[:B//2]
        img2_rec = pred[B//2:]

        # Unmix reconstructions and compute loss
        unmixed_rec = img1_rec * mixing_mask + img2_rec.flip(0) * (1 - mixing_mask)
        unmixed_rec = self.unpatchify(unmixed_rec)

        # Normalize if needed - now operating on full images
        if self.norm_pix_loss:
            # Compute mean and var across spatial dimensions (H, W)
            mean = imgs.mean(dim=(2, 3), keepdim=True)  # [B, C, 1, 1]
            var = imgs.var(dim=(2, 3), keepdim=True)    # [B, C, 1, 1]
            
            # Normalize both target and prediction
            imgs = (imgs - mean) / (var + 1.e-6)**.5
            unmixed_rec = (unmixed_rec - mean) / (var + 1.e-6)**.5

        # 1. Reconstruction Loss (pixel-wise)
        recon_loss = (unmixed_rec - imgs) ** 2
        recon_loss = recon_loss.mean()

        # 2. Spectral Derivative Loss (between adjacent spectral bands)
        pred_diff = unmixed_rec[:, 1:] - unmixed_rec[:, :-1]  # [B, C-1, H, W]
        target_diff = imgs[:, 1:] - imgs[:, :-1]  # [B, C-1, H, W]
        deriv_loss = (pred_diff - target_diff) ** 2
        deriv_loss = deriv_loss.mean()
        
        # 3. Reflectance Bounds Loss (pixel-wise)
        bounds_loss = F.relu(min_val - unmixed_rec) + F.relu(unmixed_rec - max_val)
        bounds_loss = bounds_loss.mean()
        
        # Combine losses
        total_loss = (
            recon_loss + 
            lambda_deriv * deriv_loss + 
            lambda_bounds * bounds_loss
        )
        
        return total_loss, unmixed_rec

    def forward(self, pixel_values, mask_ratio=0.5):
        mixing_mask = self.random_masking(pixel_values, mask_ratio)
        embedding_output, input_dimensions = self.embeddings(pixel_values)
        
        embedding_output = self.mask_non_rgb_embeddings(embedding_output)
        # print(embedding_output[:, 1, :])
        
        # Mix patches from different images
        mixed_embeddings = (
            embedding_output * (1 - mixing_mask) + 
            embedding_output.flip(0) * mixing_mask
        )
        
        encoder_outputs = self.encoder(
            mixed_embeddings,
            input_dimensions,
            mixing_mask=mixing_mask
        )

        hidden_states = encoder_outputs[0]
        final_mixing_mask = encoder_outputs.final_mixing_mask if isinstance(encoder_outputs, Swinv2EncoderOutput) else encoder_outputs[4]
        
        reconstructed = self.decoder(hidden_states, final_mixing_mask)
        loss = None
        if self.training:
            loss, rec_imgs = self.forward_loss(pixel_values, reconstructed, final_mixing_mask)

        output = (rec_imgs, final_mixing_mask)
        return ((loss,) + output) if loss is not None else output
                

@add_start_docstrings(
    "The bare Swinv2 Model transformer outputting raw hidden-states without any specific head on top.",
    SWINV2_START_DOCSTRING,
)
# Copied from transformers.models.swin.modeling_swin.SwinModel with SWIN->SWINV2,Swin->Swinv2
class Swinv2Model(Swinv2PreTrainedModel):
    def __init__(self, config, add_pooling_layer=True, use_mask_token=False):
        super().__init__(config)
        self.config = config
        self.num_layers = len(config.depths)
        self.num_features = int(config.embed_dim * 2 ** (self.num_layers - 1))

        self.embeddings = Swinv2Embeddings(config, use_mask_token=use_mask_token)
        self.encoder = Swinv2Encoder(config, self.embeddings.patch_grid)

        self.layernorm = nn.LayerNorm(self.num_features, eps=config.layer_norm_eps)
        self.pooler = nn.AdaptiveAvgPool1d(1) if add_pooling_layer else None

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.embeddings.patch_embeddings

    def _prune_heads(self, heads_to_prune):
        """
        Prunes heads of the model. heads_to_prune: dict of {layer_num: list of heads to prune in this layer} See base
        class PreTrainedModel
        """
        for layer, heads in heads_to_prune.items():
            self.encoder.layer[layer].attention.prune_heads(heads)

    @add_start_docstrings_to_model_forward(SWINV2_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        checkpoint=_CHECKPOINT_FOR_DOC,
        output_type=Swinv2ModelOutput,
        config_class=_CONFIG_FOR_DOC,
        modality="vision",
        expected_output=_EXPECTED_OUTPUT_SHAPE,
    )
    def forward(
        self,
        pixel_values: Optional[torch.FloatTensor] = None,
        bool_masked_pos: Optional[torch.BoolTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        interpolate_pos_encoding: bool = False,
        return_dict: Optional[bool] = None,
        mixing_mask: Optional[torch.FloatTensor] = None,
    ) -> Union[Tuple, Swinv2ModelOutput]:
        r"""
        bool_masked_pos (`torch.BoolTensor` of shape `(batch_size, num_patches)`, *optional*):
            Boolean masked positions. Indicates which patches are masked (1) and which aren't (0).
        """
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if pixel_values is None:
            raise ValueError("You have to specify pixel_values")

        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape bsz x n_heads x N x N
        # input head_mask has shape [num_heads] or [num_hidden_layers x num_heads]
        # and head_mask is converted to shape [num_hidden_layers x batch x num_heads x seq_length x seq_length]
        head_mask = self.get_head_mask(head_mask, len(self.config.depths))

        embedding_output, input_dimensions = self.embeddings(
            pixel_values, bool_masked_pos=bool_masked_pos, interpolate_pos_encoding=interpolate_pos_encoding
        )

        encoder_outputs = self.encoder(
            embedding_output,
            input_dimensions,
            head_mask=head_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            mixing_mask=mixing_mask,
        )

        sequence_output = encoder_outputs[0]
        sequence_output = self.layernorm(sequence_output)

        pooled_output = None
        if self.pooler is not None:
            pooled_output = self.pooler(sequence_output.transpose(1, 2))
            pooled_output = torch.flatten(pooled_output, 1)

        if not return_dict:
            output = (sequence_output, pooled_output) + encoder_outputs[1:]

            return output

        return Swinv2ModelOutput(
            last_hidden_state=sequence_output,
            pooler_output=pooled_output,
            hidden_states=encoder_outputs.hidden_states,
            attentions=encoder_outputs.attentions,
            reshaped_hidden_states=encoder_outputs.reshaped_hidden_states,
        )


@add_start_docstrings(
    """Swinv2 Model with a decoder on top for masked image modeling, as proposed in
[SimMIM](https://arxiv.org/abs/2111.09886).

    <Tip>

    Note that we provide a script to pre-train this model on custom data in our [examples
    directory](https://github.com/huggingface/transformers/tree/main/examples/pytorch/image-pretraining).

    </Tip>
    """,
    SWINV2_START_DOCSTRING,
)
# Copied from transformers.models.swin.modeling_swin.SwinForMaskedImageModeling with swin->swinv2, base-simmim-window6-192->tiny-patch4-window8-256,SWIN->SWINV2,Swin->Swinv2,192->256
class Swinv2ForMaskedImageModeling(Swinv2PreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.swinv2 = Swinv2Model(config, add_pooling_layer=False, use_mask_token=True)

        num_features = int(config.embed_dim * 2 ** (config.num_layers - 1))
        self.decoder = nn.Sequential(
            nn.Conv2d(
                in_channels=num_features, out_channels=config.encoder_stride**2 * config.num_channels, kernel_size=1
            ),
            nn.PixelShuffle(config.encoder_stride),
        )

        # Initialize weights and apply final processing
        self.post_init()

    @add_start_docstrings_to_model_forward(SWINV2_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=Swinv2MaskedImageModelingOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        pixel_values: Optional[torch.FloatTensor] = None,
        bool_masked_pos: Optional[torch.BoolTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        interpolate_pos_encoding: bool = False,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, Swinv2MaskedImageModelingOutput]:
        r"""
        bool_masked_pos (`torch.BoolTensor` of shape `(batch_size, num_patches)`):
            Boolean masked positions. Indicates which patches are masked (1) and which aren't (0).

        Returns:

        Examples:
        ```python
        >>> from transformers import AutoImageProcessor, Swinv2ForMaskedImageModeling
        >>> import torch
        >>> from PIL import Image
        >>> import requests

        >>> url = "http://images.cocodataset.org/val2017/000000039769.jpg"
        >>> image = Image.open(requests.get(url, stream=True).raw)

        >>> image_processor = AutoImageProcessor.from_pretrained("microsoft/swinv2-tiny-patch4-window8-256")
        >>> model = Swinv2ForMaskedImageModeling.from_pretrained("microsoft/swinv2-tiny-patch4-window8-256")

        >>> num_patches = (model.config.image_size // model.config.patch_size) ** 2
        >>> pixel_values = image_processor(images=image, return_tensors="pt").pixel_values
        >>> # create random boolean mask of shape (batch_size, num_patches)
        >>> bool_masked_pos = torch.randint(low=0, high=2, size=(1, num_patches)).bool()

        >>> outputs = model(pixel_values, bool_masked_pos=bool_masked_pos)
        >>> loss, reconstructed_pixel_values = outputs.loss, outputs.reconstruction
        >>> list(reconstructed_pixel_values.shape)
        [1, 3, 256, 256]
        ```"""
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.swinv2(
            pixel_values,
            bool_masked_pos=bool_masked_pos,
            head_mask=head_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            interpolate_pos_encoding=interpolate_pos_encoding,
            return_dict=return_dict,
        )

        sequence_output = outputs[0]
        # Reshape to (batch_size, num_channels, height, width)
        sequence_output = sequence_output.transpose(1, 2)
        batch_size, num_channels, sequence_length = sequence_output.shape
        height = width = math.floor(sequence_length**0.5)
        sequence_output = sequence_output.reshape(batch_size, num_channels, height, width)

        # Reconstruct pixel values
        reconstructed_pixel_values = self.decoder(sequence_output)

        masked_im_loss = None
        if bool_masked_pos is not None:
            size = self.config.image_size // self.config.patch_size
            bool_masked_pos = bool_masked_pos.reshape(-1, size, size)
            mask = (
                bool_masked_pos.repeat_interleave(self.config.patch_size, 1)
                .repeat_interleave(self.config.patch_size, 2)
                .unsqueeze(1)
                .contiguous()
            )
            reconstruction_loss = nn.functional.l1_loss(pixel_values, reconstructed_pixel_values, reduction="none")
            masked_im_loss = (reconstruction_loss * mask).sum() / (mask.sum() + 1e-5) / self.config.num_channels

        if not return_dict:
            output = (reconstructed_pixel_values,) + outputs[2:]
            return ((masked_im_loss,) + output) if masked_im_loss is not None else output

        return Swinv2MaskedImageModelingOutput(
            loss=masked_im_loss,
            reconstruction=reconstructed_pixel_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            reshaped_hidden_states=outputs.reshaped_hidden_states,
        )


@add_start_docstrings(
    """
    Swinv2 Model transformer with an image classification head on top (a linear layer on top of the final hidden state
    of the [CLS] token) e.g. for ImageNet.

    <Tip>

        Note that it's possible to fine-tune SwinV2 on higher resolution images than the ones it has been trained on, by
        setting `interpolate_pos_encoding` to `True` in the forward of the model. This will interpolate the pre-trained
        position embeddings to the higher resolution.

    </Tip>
    """,
    SWINV2_START_DOCSTRING,
)
# Copied from transformers.models.swin.modeling_swin.SwinForImageClassification with SWIN->SWINV2,Swin->Swinv2,swin->swinv2
class Swinv2ForImageClassification(Swinv2PreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.num_labels = config.num_labels
        self.swinv2 = Swinv2Model(config)

        # Classifier head
        self.classifier = (
            nn.Linear(self.swinv2.num_features, config.num_labels) if config.num_labels > 0 else nn.Identity()
        )

        # Initialize weights and apply final processing
        self.post_init()

    @add_start_docstrings_to_model_forward(SWINV2_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        checkpoint=_IMAGE_CLASS_CHECKPOINT,
        output_type=Swinv2ImageClassifierOutput,
        config_class=_CONFIG_FOR_DOC,
        expected_output=_IMAGE_CLASS_EXPECTED_OUTPUT,
    )
    def forward(
        self,
        pixel_values: Optional[torch.FloatTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        interpolate_pos_encoding: bool = False,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, Swinv2ImageClassifierOutput]:
        r"""
        labels (`torch.LongTensor` of shape `(batch_size,)`, *optional*):
            Labels for computing the image classification/regression loss. Indices should be in `[0, ...,
            config.num_labels - 1]`. If `config.num_labels == 1` a regression loss is computed (Mean-Square loss), If
            `config.num_labels > 1` a classification loss is computed (Cross-Entropy).
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.swinv2(
            pixel_values,
            head_mask=head_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            interpolate_pos_encoding=interpolate_pos_encoding,
            return_dict=return_dict,
        )

        pooled_output = outputs[1]

        logits = self.classifier(pooled_output)

        loss = None
        if labels is not None:
            if self.config.problem_type is None:
                if self.num_labels == 1:
                    self.config.problem_type = "regression"
                elif self.num_labels > 1 and (labels.dtype == torch.long or labels.dtype == torch.int):
                    self.config.problem_type = "single_label_classification"
                else:
                    self.config.problem_type = "multi_label_classification"

            if self.config.problem_type == "regression":
                loss_fct = MSELoss()
                if self.num_labels == 1:
                    loss = loss_fct(logits.squeeze(), labels.squeeze())
                else:
                    loss = loss_fct(logits, labels)
            elif self.config.problem_type == "single_label_classification":
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            elif self.config.problem_type == "multi_label_classification":
                loss_fct = BCEWithLogitsLoss()
                loss = loss_fct(logits, labels)

        if not return_dict:
            output = (logits,) + outputs[2:]
            return ((loss,) + output) if loss is not None else output

        return Swinv2ImageClassifierOutput(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            reshaped_hidden_states=outputs.reshaped_hidden_states,
        )


@add_start_docstrings(
    """
    Swinv2 backbone, to be used with frameworks like DETR and MaskFormer.
    """,
    SWINV2_START_DOCSTRING,
)
class Swinv2Backbone(Swinv2PreTrainedModel, BackboneMixin):
    def __init__(self, config):
        super().__init__(config)
        super()._init_backbone(config)

        self.num_features = [config.embed_dim] + [int(config.embed_dim * 2**i) for i in range(len(config.depths))]
        self.embeddings = Swinv2Embeddings(config)
        self.encoder = Swinv2Encoder(config, self.embeddings.patch_grid)

        # initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.embeddings.patch_embeddings

    @add_start_docstrings_to_model_forward(SWINV2_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=BackboneOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        pixel_values: Tensor,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> BackboneOutput:
        """
        Returns:

        Examples:

        ```python
        >>> from transformers import AutoImageProcessor, AutoBackbone
        >>> import torch
        >>> from PIL import Image
        >>> import requests

        >>> url = "http://images.cocodataset.org/val2017/000000039769.jpg"
        >>> image = Image.open(requests.get(url, stream=True).raw)

        >>> processor = AutoImageProcessor.from_pretrained("microsoft/swinv2-tiny-patch4-window8-256")
        >>> model = AutoBackbone.from_pretrained(
        ...     "microsoft/swinv2-tiny-patch4-window8-256", out_features=["stage1", "stage2", "stage3", "stage4"]
        ... )

        >>> inputs = processor(image, return_tensors="pt")

        >>> outputs = model(**inputs)
        >>> feature_maps = outputs.feature_maps
        >>> list(feature_maps[-1].shape)
        [1, 2048, 7, 7]
        ```"""
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions

        embedding_output, input_dimensions = self.embeddings(pixel_values)

        outputs = self.encoder(
            embedding_output,
            input_dimensions,
            head_mask=None,
            output_attentions=output_attentions,
            output_hidden_states=True,
            output_hidden_states_before_downsampling=True,
            return_dict=return_dict,
        )

        hidden_states = outputs.reshaped_hidden_states if return_dict else outputs[-1]

        feature_maps = ()
        for stage, hidden_state in zip(self.stage_names, hidden_states):
            if stage in self.out_features:
                feature_maps += (hidden_state,)

        if not return_dict:
            output = (feature_maps,)
            if output_hidden_states:
                output += (outputs[1],)
            if output_attentions:
                output += (outputs[2],)
            return output

        return BackboneOutput(
            feature_maps=feature_maps,
            hidden_states=outputs.hidden_states if output_hidden_states else None,
            attentions=outputs.attentions,
        )

__all__ = [
    "Swinv2ForImageClassification",
    "Swinv2ForMaskedImageModeling",
    "Swinv2Model",
    "Swinv2PreTrainedModel",
    "Swinv2Backbone",
    "Swinv2MixMAE",
]