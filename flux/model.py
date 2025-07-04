from functools import partial
from types import MethodType

import torch
from torch import Tensor
from einops import rearrange, repeat
import comfy.ldm.common_dit

from comfy.ldm.flux.layers import (
    DoubleStreamBlock,
    SingleStreamBlock,
    timestep_embedding,
)
from comfy.ldm.flux.model import Flux

from .layers import NAGDoubleStreamBlock, NAGSingleStreamBlock
from ..utils import cat_context, check_nag_activation


class NAGFlux(Flux):
    def forward_orig(
        self,
        img: Tensor,
        img_ids: Tensor,
        txt: Tensor,
        txt_ids: Tensor,
        timesteps: Tensor,
        y: Tensor,
        guidance: Tensor = None,
        control = None,
        transformer_options={},
        attn_mask: Tensor = None,
    ) -> Tensor:
        if y is None:
            y = torch.zeros((img.shape[0], self.params.vec_in_dim), device=img.device, dtype=img.dtype)

        patches_replace = transformer_options.get("patches_replace", {})
        if img.ndim != 3 or txt.ndim != 3:
            raise ValueError("Input img and txt tensors must have 3 dimensions.")

        # running on sequences img
        img = self.img_in(img)
        vec = self.time_in(timestep_embedding(timesteps, 256).to(img.dtype))
        if self.params.guidance_embed:
            if guidance is not None:
                vec = vec + self.guidance_in(timestep_embedding(guidance, 256).to(img.dtype))

        origin_bsz = len(txt) - len(img)
        vec = torch.cat((vec, vec[-origin_bsz:]), dim=0)

        vec = vec + self.vector_in(y[:,:self.params.vec_in_dim])
        txt = self.txt_in(txt)

        if img_ids is not None:
            ids = torch.cat((txt_ids, img_ids), dim=1)
            pe = self.pe_embedder(ids)
        else:
            pe = None

        blocks_replace = patches_replace.get("dit", {})
        for i, block in enumerate(self.double_blocks):
            if ("double_block", i) in blocks_replace:
                def block_wrap(args):
                    out = {}
                    out["img"], out["txt"] = block(img=args["img"],
                                                   txt=args["txt"],
                                                   vec=args["vec"],
                                                   pe=args["pe"],
                                                   attn_mask=args.get("attn_mask"))
                    return out

                out = blocks_replace[("double_block", i)]({"img": img,
                                                           "txt": txt,
                                                           "vec": vec,
                                                           "pe": pe,
                                                           "attn_mask": attn_mask},
                                                          {"original_block": block_wrap})
                txt = out["txt"]
                img = out["img"]
            else:
                img, txt = block(img=img,
                                 txt=txt,
                                 vec=vec,
                                 pe=pe,
                                 attn_mask=attn_mask)

            if control is not None: # Controlnet
                control_i = control.get("input")
                if i < len(control_i):
                    add = control_i[i]
                    if add is not None:
                        img += add

        if img.dtype == torch.float16:
            img = torch.nan_to_num(img, nan=0.0, posinf=65504, neginf=-65504)

        pe = torch.cat((pe, pe[-origin_bsz:]), dim=0)
        img = torch.cat((img, img[-origin_bsz:]), dim=0)
        img = torch.cat((txt, img), 1)

        for i, block in enumerate(self.single_blocks):
            if ("single_block", i) in blocks_replace:
                def block_wrap(args):
                    out = {}
                    out["img"] = block(args["img"],
                                       vec=args["vec"],
                                       pe=args["pe"],
                                       attn_mask=args.get("attn_mask"))
                    return out

                out = blocks_replace[("single_block", i)]({"img": img,
                                                           "vec": vec,
                                                           "pe": pe,
                                                           "attn_mask": attn_mask},
                                                          {"original_block": block_wrap})
                img = out["img"]
            else:
                img = block(img, vec=vec, pe=pe, attn_mask=attn_mask)

            if control is not None: # Controlnet
                control_o = control.get("output")
                if i < len(control_o):
                    add = control_o[i]
                    if add is not None:
                        img[:, txt.shape[1] :, ...] += add

        img = img[:-origin_bsz]
        img = img[:, txt.shape[1] :, ...]

        img = self.final_layer(img, vec[:-origin_bsz])  # (N, T, patch_size ** 2 * out_channels)
        return img

    def forward(
            self,
            x,
            timestep,
            context,
            y=None,
            guidance=None,
            ref_latents=None,
            control=None,
            transformer_options={},

            positive_context=None,
            nag_negative_context=None,
            nag_negative_y=None,
            nag_sigma_end=0.,

            **kwargs,
    ):
        apply_nag = check_nag_activation(context, transformer_options, positive_context, nag_negative_context, nag_sigma_end)
        if apply_nag:
            origin_context_len = context.shape[1]
            context = cat_context(context, nag_negative_context, trim_context=True)
            y = torch.cat((y, nag_negative_y.to(y)), dim=0)
            context_pad_len = context.shape[1] - origin_context_len
            nag_pad_len = context.shape[1] - nag_negative_context.shape[1]

            self.forward_orig = MethodType(NAGFlux.forward_orig, self)
            for block in self.double_blocks:
                block.forward = MethodType(
                    partial(
                        NAGDoubleStreamBlock.forward,
                        context_pad_len=context_pad_len,
                        nag_pad_len=nag_pad_len,
                    ),
                    block,
                )
            for block in self.single_blocks:
                block.forward = MethodType(
                    partial(
                        NAGSingleStreamBlock.forward,
                        txt_length=context.shape[1],
                        origin_bsz=nag_negative_context.shape[0],
                        context_pad_len=context_pad_len,
                        nag_pad_len=nag_pad_len,
                    ),
                    block,
                )
        else:
            self.forward_orig = MethodType(Flux.forward_orig, self)
            for block in self.double_blocks:
                block.forward = MethodType(DoubleStreamBlock.forward, block)
            for block in self.single_blocks:
                block.forward = MethodType(SingleStreamBlock.forward, block)

        bs, c, h_orig, w_orig = x.shape
        patch_size = self.patch_size

        h_len = ((h_orig + (patch_size // 2)) // patch_size)
        w_len = ((w_orig + (patch_size // 2)) // patch_size)
        img, img_ids = self.process_img(x)
        img_tokens = img.shape[1]
        if ref_latents is not None:
            h = 0
            w = 0
            for ref in ref_latents:
                h_offset = 0
                w_offset = 0
                if ref.shape[-2] + h > ref.shape[-1] + w:
                    w_offset = w
                else:
                    h_offset = h

                kontext, kontext_ids = self.process_img(ref, index=1, h_offset=h_offset, w_offset=w_offset)
                img = torch.cat([img, kontext], dim=1)
                img_ids = torch.cat([img_ids, kontext_ids], dim=1)
                h = max(h, ref.shape[-2] + h_offset)
                w = max(w, ref.shape[-1] + w_offset)

        txt_ids = torch.zeros((bs, context.shape[1], 3), device=x.device, dtype=x.dtype)
        out = self.forward_orig(img, img_ids, context, txt_ids, timestep, y, guidance, control, transformer_options,
                                attn_mask=kwargs.get("attention_mask", None))
        out = out[:, :img_tokens]
        return rearrange(out, "b (h w) (c ph pw) -> b c (h ph) (w pw)", h=h_len, w=w_len, ph=2, pw=2)[:, :, :h_orig, :w_orig]


def set_nag_flux(
        model: Flux,
        positive_context,
        nag_negative_cond,
        nag_scale, nag_tau, nag_alpha, nag_sigma_end,
):
    model.forward = MethodType(
        partial(
            NAGFlux.forward,
            positive_context=positive_context,
            nag_negative_context=nag_negative_cond[0][0],
            nag_negative_y=nag_negative_cond[0][1]["pooled_output"],
            nag_sigma_end=nag_sigma_end,
        ),
        model,
    )
    for block in model.double_blocks:
        block.nag_scale = nag_scale
        block.nag_tau = nag_tau
        block.nag_alpha = nag_alpha
    for block in model.single_blocks:
        block.nag_scale = nag_scale
        block.nag_tau = nag_tau
        block.nag_alpha = nag_alpha


def set_origin_flux(model: NAGFlux):
    model.forward_orig = MethodType(Flux.forward_orig, model)
    model.forward = MethodType(Flux.forward, model)
    for block in model.double_blocks:
        block.forward = MethodType(DoubleStreamBlock.forward, block)
    for block in model.single_blocks:
        block.forward = MethodType(SingleStreamBlock.forward, block)
