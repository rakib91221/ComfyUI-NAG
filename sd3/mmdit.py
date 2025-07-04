from typing import Optional
from types import MethodType
from functools import partial

import torch
from einops import repeat
import comfy
from comfy.ldm.modules.diffusionmodules.mmdit import (
    OpenAISignatureMMDITWrapper,
    JointBlock,
    optimized_attention,
    default,
)

from ..utils import nag, cat_context, check_nag_activation


def _nag_block_mixing(
        context,
        x,
        context_block,
        x_block,
        c,
        nag_scale: float = 1.0,
        nag_tau: float = 2.5,
        nag_alpha: float = 0.5,
):
    origin_bsz = len(context) - len(x)
    assert origin_bsz != 0

    context_qkv, context_intermediates = context_block.pre_attention(context, c)

    if x_block.x_block_self_attn:
        x_qkv, x_qkv2, x_intermediates = x_block.pre_attention_x(x, c[:-origin_bsz])
    else:
        x_qkv, x_intermediates = x_block.pre_attention(x, c[:-origin_bsz])

    o = []
    for t in range(3):
        o.append(torch.cat((
            context_qkv[t],
            torch.cat([x_qkv[t], x_qkv[t][-origin_bsz:]], dim=0),
        ),dim=1))
    qkv = tuple(o)

    attn = optimized_attention(
        qkv[0], qkv[1], qkv[2],
        heads=x_block.attn.num_heads,
    )
    context_attn, x_attn = (
        attn[:, : context_qkv[0].shape[1]],
        attn[:, context_qkv[0].shape[1] :],
    )

    # NAG
    x_attn_negative, x_attn_positive = x_attn[-origin_bsz:], x_attn[-origin_bsz * 2:-origin_bsz]
    x_attn_guidance = nag(x_attn_positive, x_attn_negative, nag_scale, nag_tau, nag_alpha)

    x_attn = torch.cat([x_attn[:-origin_bsz * 2], x_attn_guidance], dim=0)

    if not context_block.pre_only:
        context = context_block.post_attention(context_attn, *context_intermediates)

    else:
        context = None
    if x_block.x_block_self_attn:
        attn2 = optimized_attention(
                x_qkv2[0], x_qkv2[1], x_qkv2[2],
                heads=x_block.attn2.num_heads,
            )
        x = x_block.post_attention_x(x_attn, attn2, *x_intermediates)
    else:
        x = x_block.post_attention(x_attn, *x_intermediates)
    return context, x


def nag_block_mixing(*args, use_checkpoint=True, **kwargs):
    if use_checkpoint:
        return torch.utils.checkpoint.checkpoint(
            _nag_block_mixing, *args, use_reentrant=False, **kwargs
        )
    else:
        return _nag_block_mixing(*args, **kwargs)


class NAGJointBlock(JointBlock):
    def forward(self, *args, **kwargs):
        return nag_block_mixing(
            *args, context_block=self.context_block, x_block=self.x_block, **kwargs
        )


class NAGOpenAISignatureMMDITWrapper(OpenAISignatureMMDITWrapper):
    def __init__(
            self,
            *args,
            nag_scale: float = 1,
            nag_tau: float = 2.5,
            nag_alpha: float = 0.25,
            **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.nag_scale = nag_scale
        self.nag_tau = nag_tau
        self.nag_alpha = nag_alpha

    def forward_core_with_concat(
        self,
        x: torch.Tensor,
        c_mod: torch.Tensor,
        context: Optional[torch.Tensor] = None,
        control = None,
        transformer_options = {},
    ) -> torch.Tensor:
        patches_replace = transformer_options.get("patches_replace", {})
        if self.register_length > 0:
            context = torch.cat(
                (
                    repeat(self.register, "1 ... -> b ...", b=x.shape[0]),
                    default(context, torch.Tensor([]).type_as(x)),
                ),
                1,
            )

        # context is B, L', D
        # x is B, L, D
        blocks_replace = patches_replace.get("dit", {})
        blocks = len(self.joint_blocks)
        for i in range(blocks):
            if ("double_block", i) in blocks_replace:
                def block_wrap(args):
                    out = {}
                    out["txt"], out["img"] = self.joint_blocks[i](args["txt"], args["img"], c=args["vec"])
                    return out

                out = blocks_replace[("double_block", i)]({"img": x, "txt": context, "vec": c_mod}, {"original_block": block_wrap})
                context = out["txt"]
                x = out["img"]
            else:
                context, x = self.joint_blocks[i](
                    context,
                    x,
                    c=c_mod,
                    use_checkpoint=self.use_checkpoint,
                )
            if control is not None:
                control_o = control.get("output")
                if i < len(control_o):
                    add = control_o[i]
                    if add is not None:
                        x += add

        x = self.final_layer(x, c_mod[:len(x)])  # (N, T, patch_size ** 2 * out_channels)
        return x

    def forward(
            self,
            x: torch.Tensor,
            timesteps: torch.Tensor,
            context: Optional[torch.Tensor] = None,
            y: Optional[torch.Tensor] = None,
            control=None,
            transformer_options={},

            positive_context=None,
            nag_negative_context=None,
            nag_negative_y=None,
            nag_sigma_end=0.,

            **kwargs,
    ) -> torch.Tensor:
        apply_nag = check_nag_activation(context, transformer_options, positive_context, nag_negative_context, nag_sigma_end)
        if apply_nag:
            context = cat_context(context, nag_negative_context)
            y = torch.cat((y, nag_negative_y.to(y)), dim=0)

            self.forward_core_with_concat = MethodType(NAGOpenAISignatureMMDITWrapper.forward_core_with_concat, self)
            for block in self.joint_blocks:
                block.forward = MethodType(
                    partial(
                        NAGJointBlock.forward,
                        nag_scale=self.nag_scale,
                        nag_tau=self.nag_tau,
                        nag_alpha=self.nag_alpha,
                    ),
                    block,
                )
        else:
            self.forward_core_with_concat = MethodType(OpenAISignatureMMDITWrapper.forward_core_with_concat, self)
            for block in self.joint_blocks:
                block.forward = MethodType(JointBlock.forward, block)

        if self.context_processor is not None:
            context = self.context_processor(context)

        hw = x.shape[-2:]
        x = self.x_embedder(x) + comfy.ops.cast_to_input(self.cropped_pos_embed(hw, device=x.device), x)
        c = self.t_embedder(timesteps, dtype=x.dtype)  # (N, D)

        if apply_nag:
            origin_bsz = len(context) - len(x)
            c = torch.cat((c, c[-origin_bsz:]), dim=0)

        if y is not None and self.y_embedder is not None:
            y = self.y_embedder(y)  # (N, D)
            c = c + y  # (N, D)

        if context is not None:
            context = self.context_embedder(context)

        x = self.forward_core_with_concat(x, c, context, control, transformer_options)

        x = self.unpatchify(x, hw=hw)  # (N, out_channels, H, W)
        return x[:, :, :hw[-2], :hw[-1]]


def set_nag_sd3(
        model: OpenAISignatureMMDITWrapper,
        positive_context,
        nag_negative_cond,
        nag_scale, nag_tau, nag_alpha, nag_sigma_end,
):
    model.nag_scale = nag_scale
    model.nag_tau = nag_tau
    model.nag_alpha = nag_alpha
    model.forward = MethodType(
        partial(
            NAGOpenAISignatureMMDITWrapper.forward,
            positive_context=positive_context,
            nag_negative_context=nag_negative_cond[0][0],
            nag_negative_y=nag_negative_cond[0][1]["pooled_output"],
            nag_sigma_end=nag_sigma_end,
        ),
        model
    )


def set_origin_sd3(model: NAGOpenAISignatureMMDITWrapper):
    model.forward = MethodType(OpenAISignatureMMDITWrapper.forward, model)
    model.forward_core_with_concat = MethodType(OpenAISignatureMMDITWrapper.forward_core_with_concat, model)
    for block in model.joint_blocks:
        block.forward = MethodType(JointBlock.forward, block)
