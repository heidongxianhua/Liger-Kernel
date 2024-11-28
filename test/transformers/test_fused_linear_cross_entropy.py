from test.transformers.test_cross_entropy import CrossEntropyWithZLoss
from test.utils import assert_verbose_allclose, set_seed
from typing import Optional

import pytest
import torch

from liger_kernel.ops.fused_linear_cross_entropy import (
    LigerFusedLinearCrossEntropyFunction,
)
from liger_kernel.transformers.functional import liger_fused_linear_cross_entropy
from liger_kernel.transformers.fused_linear_cross_entropy import (
    LigerFusedLinearCrossEntropyLoss,
)
from liger_kernel.ops.fused_linear_cross_entropy import fused_linear_cross_entropy_forward
from liger_kernel.utils import infer_device

device = infer_device()

# set random seed globally
set_seed()


class TorchLMHeadCE(torch.nn.Module):
    """Ground truth implementation of the linear fused with torch based cross entropy loss.

    :param H: hidden size
    :param V: vocab size
    :param ignore_index: index to ignore
    :param reduction: reduction method
    :param label_smoothing: label_smoothing to apply on target
    :param lse_square_scale: scaler of lse ^ 2 to compute z loss

    # TODO: if we bump CI env's `transformers` version to >= 4.46, we should just directly
    # call https://github.com/huggingface/transformers/blob/main/src/transformers/loss/loss_utils.py#L32
    # to be consistent with Hugging Face model implementation.
    """

    def __init__(
        self,
        H: int,
        V: int,
        dtype: torch.dtype,
        bias: bool = False,
        ignore_index: int = -100,
        lse_square_scale: float = 0.0,
        label_smoothing: float = 0.0,
        reduction: str = "mean",
        softcap: Optional[float] = None,
    ):
        super().__init__()
        self.lin = torch.nn.Linear(
            in_features=H, out_features=V, bias=bias, dtype=dtype
        )
        self.ce_loss = CrossEntropyWithZLoss(
            ignore_index=ignore_index,
            lse_square_scale=lse_square_scale,
            label_smoothing=label_smoothing,
            reduction=reduction,
        )
        self.softcap = softcap

    def forward(self, x, y):
        logits = self.lin(x).to(torch.float32)
        if self.softcap is not None and self.softcap != 0.0:
            logits = self.softcap * torch.tanh(logits / self.softcap)
        return self.ce_loss(logits, y)


import torch.nn.functional as F
class TorchCE(torch.nn.Module):
    def __init__(
        self,
        lse_square_scale=0.0,
        reduction="mean",
        ignore_index=-100,
        label_smoothing=0.0,
        return_z_loss=False,
        dtype=torch.float32,
        softcap: Optional[float] = None,
    ):
        super().__init__()
        self.lse_square_scale = lse_square_scale
        self.reduction = reduction
        self.ignore_index = ignore_index
        self.return_z_loss = return_z_loss
        self.label_smoothing = label_smoothing
        self.dtype = dtype
        self.softcap = softcap

    def forward(self, input, targets, weight, bias):
        
        logits = torch.matmul(input, weight)
        if bias:
            logits = logits + bias
        logits = logits.to(torch.float32)
        if self.softcap is not None and self.softcap != 0.0:
            logits = self.softcap * torch.tanh(logits / self.softcap)
        
        ## CrossEntropyWithZLoss implement
        # Loss calculations are all in float32
        logits = logits.to(torch.float32)
        # Standard cross entropy loss
        ce_loss = F.cross_entropy(
            logits,
            targets,
            reduction=self.reduction,
            label_smoothing=self.label_smoothing,
            ignore_index=self.ignore_index,
        )

        # Compute log-sum-exp term
        lse = torch.logsumexp(logits, dim=-1)

        # Z-loss term
        z_loss = torch.where(
            targets != self.ignore_index, self.lse_square_scale * (lse**2), 0.0
        )
        z_loss = z_loss.to(logits.dtype)
        if self.reduction == "mean":
            z_loss = z_loss.sum() / (targets != self.ignore_index).sum()
        elif self.reduction == "sum":
            z_loss = z_loss.sum()
        else:
            z_loss = z_loss
        ce_loss = ce_loss.to(self.dtype)
        z_loss = z_loss.to(self.dtype)

        # Final loss: cross-entropy loss + Z-loss
        total_loss = ce_loss + z_loss
        if self.return_z_loss:
            return total_loss, z_loss
        else:
            return total_loss


class LigerCE(torch.nn.Module):
    def __init__(
        self,
        H: int,
        V: int,
        dtype: torch.dtype,
        bias: bool = False,
        ignore_index: int = -100,
        lse_square_scale: float = 0.0,
        label_smoothing: float = 0.0,
        reduction: str = "mean",
        softcap: Optional[float] = None,
    ):
        super().__init__()
        self.ignore_index=ignore_index
        self.lse_square_scale=lse_square_scale
        self.label_smoothing=label_smoothing
        self.reduction=reduction
        self.softcap=softcap

    def forward(self, input, target, weight, bias):
        # return self.ce_loss(weight, x, y, bias)
        return fused_linear_cross_entropy_forward(input, weight, target, bias, self.ignore_index, 
                    self.lse_square_scale, self.label_smoothing, self.reduction, self.softcap)

class LigerLMHeadCE(torch.nn.Module):
    def __init__(
        self,
        H: int,
        V: int,
        dtype: torch.dtype,
        bias: bool = False,
        ignore_index: int = -100,
        lse_square_scale: float = 0.0,
        label_smoothing: float = 0.0,
        reduction: str = "mean",
        softcap: Optional[float] = None,
    ):
        super().__init__()
        self.lin = torch.nn.Linear(
            in_features=H, out_features=V, bias=bias, dtype=dtype
        )
        self.ce_loss = LigerFusedLinearCrossEntropyLoss(
            ignore_index=ignore_index,
            lse_square_scale=lse_square_scale,
            label_smoothing=label_smoothing,
            reduction=reduction,
            softcap=softcap,
        )

    def forward(self, x, y):
        return self.ce_loss(self.lin.weight, x, y, self.lin.bias)


#############################################################################
# Test the correctness of the fused linear cross entropy loss
#############################################################################



@pytest.mark.parametrize(
    "B, T, H, V",
    [
        (8, 128, 1024, 4096),
        (4, 47, 31, 123),  # random shape
    ],
)
@pytest.mark.parametrize(
    "reduction, scalar, dtype, atol, rtol",
    [
        ("mean", 1.0, torch.bfloat16, 5e-3, 5e-2),
        ("mean", 1.0, torch.float32, 1e-5, 5e-4),
        ("sum", 1.0, torch.bfloat16, 5e-0, 5e1),
        ("sum", 1.0, torch.float32, 1e-3, 5e-2),
    ],
)
@pytest.mark.parametrize("bias", [True, False])
@pytest.mark.parametrize(
    "label_smoothing, ignore_index, lse_square_scale, softcap",
    [
        (0, -100, 0, None),
        (
            0.1,
            42,
            1e-4,
            30.0,
        ),  # Pass non-default values once to ensure all params work along
    ],
)
def test_torch_correctness(
    B,
    T,
    H,
    V,
    scalar,
    dtype,
    bias,
    lse_square_scale,
    label_smoothing,
    ignore_index,
    reduction,
    softcap,
    atol,
    rtol,
):
    torch_lm_head_ce = TorchCE(
        H=H,
        V=V,
        bias=bias,
        lse_square_scale=lse_square_scale,
        label_smoothing=label_smoothing,
        ignore_index=ignore_index,
        reduction=reduction,
        softcap=softcap,
        dtype=dtype,
    ).to(device)
    liger_lm_head_ce = LigerCE(
        H=H,
        V=V,
        bias=bias,
        lse_square_scale=lse_square_scale,
        label_smoothing=label_smoothing,
        ignore_index=ignore_index,
        reduction=reduction,
        softcap=softcap,
        dtype=dtype,
    ).to(device)

    # init the linear in all CEs with the same weights
    weight = torch.rand(
        V, H, device=device, dtype=dtype
    )

    if bias:
        bias = torch.rand(
            V, device=device, dtype=dtype
        )
        _bias1 = bias.detach().clone().requires_grad_(True)
        _bias2 = bias.detach().clone().requires_grad_(True)
    else:
        _bias1 = None
        _bias2 = None

    _tensor = torch.randn(B * T, H, device=device, dtype=dtype) * scalar
    _input1 = _tensor.detach().clone().requires_grad_(True)
    _input2 = _tensor.detach().clone().requires_grad_(True)
    _weight1 = weight.detach().clone().requires_grad_(True)
    _weight2 = weight.detach().clone().requires_grad_(True)

    target = torch.randint(0, V, (B * T,), device=device, dtype=torch.long)
    # Assign some random number of elements as ignore_index
    num_elements_to_assign = torch.randint(
        1, B * T // 2, (1,)
    ).item()  # Random number of elements to set to ignore_index
    indices_to_assign = torch.randperm(B * T)[
        :num_elements_to_assign
    ]  # Randomly select indices
    target[indices_to_assign] = ignore_index

    output1 = torch_lm_head_ce(_input1, target, _weight1, _bias1)
    output2 = liger_lm_head_ce(_input2, target, _weight2, _bias2)

    assert_verbose_allclose(output1, output2, atol=atol, rtol=rtol)

    output1.backward()
    output2.backward()

    assert_verbose_allclose(_input1.grad, _input2.grad, atol=atol, rtol=rtol)

    assert_verbose_allclose(
        _weight1.grad,
        _weight2.grad,
        atol=atol,
        rtol=rtol,
    )

    if bias:
        assert_verbose_allclose(
            _bias1.grad,
            _bias2.grad,
            atol=atol,
            rtol=rtol,
        )

@pytest.mark.parametrize(
    "B, T, H, V",
    [
        (8, 128, 1024, 4096),
        (4, 47, 31, 123),  # random shape
    ],
)
@pytest.mark.parametrize(
    "reduction, scalar, dtype, atol, rtol",
    [
        ("mean", 1.0, torch.bfloat16, 5e-3, 5e-2),
        ("mean", 1.0, torch.float32, 1e-5, 5e-4),
        ("sum", 1.0, torch.bfloat16, 5e-0, 5e1),
        ("sum", 1.0, torch.float32, 1e-3, 5e-2),
    ],
)
@pytest.mark.parametrize("bias", [True, False])
@pytest.mark.parametrize(
    "label_smoothing, ignore_index, lse_square_scale, softcap",
    [
        (0, -100, 0, None),
        (
            0.1,
            42,
            1e-4,
            30.0,
        ),  # Pass non-default values once to ensure all params work along
    ],
)
def test_correctness(
    B,
    T,
    H,
    V,
    scalar,
    dtype,
    bias,
    lse_square_scale,
    label_smoothing,
    ignore_index,
    reduction,
    softcap,
    atol,
    rtol,
):
    torch_lm_head_ce = TorchLMHeadCE(
        H=H,
        V=V,
        bias=bias,
        lse_square_scale=lse_square_scale,
        label_smoothing=label_smoothing,
        ignore_index=ignore_index,
        reduction=reduction,
        softcap=softcap,
        dtype=dtype,
    ).to(device)
    liger_lm_head_ce = LigerLMHeadCE(
        H=H,
        V=V,
        bias=bias,
        lse_square_scale=lse_square_scale,
        label_smoothing=label_smoothing,
        ignore_index=ignore_index,
        reduction=reduction,
        softcap=softcap,
        dtype=dtype,
    ).to(device)

    # init the linear in all CEs with the same weights
    torch_lm_head_ce.lin.weight.data = liger_lm_head_ce.lin.weight.data = torch.rand(
        V, H, device=device, dtype=dtype
    )

    if bias:
        torch_lm_head_ce.lin.bias.data = liger_lm_head_ce.lin.bias.data = torch.rand(
            V, device=device, dtype=dtype
        )

    _tensor = torch.randn(B * T, H, device=device, dtype=dtype) * scalar
    _input1 = _tensor.detach().clone().requires_grad_(True)
    _input2 = _tensor.detach().clone().requires_grad_(True)

    target = torch.randint(0, V, (B * T,), device=device, dtype=torch.long)
    # Assign some random number of elements as ignore_index
    num_elements_to_assign = torch.randint(
        1, B * T // 2, (1,)
    ).item()  # Random number of elements to set to ignore_index
    indices_to_assign = torch.randperm(B * T)[
        :num_elements_to_assign
    ]  # Randomly select indices
    target[indices_to_assign] = ignore_index

    output1 = torch_lm_head_ce(_input1, target)
    output2 = liger_lm_head_ce(_input2, target)

    assert_verbose_allclose(output1, output2, atol=atol, rtol=rtol)

    output1.backward()
    output2.backward()

    assert_verbose_allclose(_input1.grad, _input2.grad, atol=atol, rtol=rtol)

    assert_verbose_allclose(
        torch_lm_head_ce.lin.weight.grad,
        liger_lm_head_ce.lin.weight.grad,
        atol=atol,
        rtol=rtol,
    )

    if bias:
        assert_verbose_allclose(
            torch_lm_head_ce.lin.bias.grad,
            liger_lm_head_ce.lin.bias.grad,
            atol=atol,
            rtol=rtol,
        )


@pytest.mark.parametrize(
    "B, T, H, V",
    [
        (2, 2, 8, 8),
        # weird shapes
        (9, 7, 41, 41),
    ],
)
@pytest.mark.parametrize(
    "scalar, dtype, atol, rtol",
    [
        (1.0, torch.bfloat16, 5e-3, 5e-2),
        (1.0, torch.float32, 1e-5, 5e-4),
    ],
)
@pytest.mark.parametrize("bias", [True, False])
def test_correctness_functional(B, T, H, V, scalar, dtype, bias, atol, rtol):
    _input = torch.randn(B * T, H, device=device, dtype=dtype) * scalar
    x1 = _input.detach().clone().requires_grad_(True)
    x2 = _input.detach().clone().requires_grad_(True)

    target = torch.randint(0, V, (B * T,), device=device, dtype=torch.long)

    weight = torch.randn(V, H, device=device, dtype=dtype)
    bias = torch.randn(V, device=device, dtype=dtype) if bias else None

    y1 = liger_fused_linear_cross_entropy(
        input=x1,
        weight=weight,
        target=target,
        bias=bias,
    )
    y2 = LigerFusedLinearCrossEntropyFunction.apply(x2, weight, target, bias)

    assert torch.allclose(y1, y2, atol=atol, rtol=rtol)

    grad_output = torch.randn_like(y1)

    y1.backward(grad_output)
    y2.backward(grad_output)

    assert torch.allclose(x1.grad, x2.grad, atol=atol, rtol=rtol)


@pytest.mark.parametrize(
    "B, T, H, V",
    [
        (8, 128, 1024, 4096),
        (4, 47, 31, 123),  # random shape
    ],
)
@pytest.mark.parametrize(
    "cast_dtype, atol, rtol",
    [
        (torch.bfloat16, 5e-3, 5e-2),
        (torch.float16, 5e-3, 5e-2),
    ],
)
def test_amp(B, T, H, V, cast_dtype, atol, rtol):
    dtype = torch.float32
    torch_lm_head_ce = TorchLMHeadCE(
        H=H,
        V=V,
        bias=True,
        label_smoothing=0.0,
        reduction="mean",
        dtype=dtype,
    ).to(device)
    liger_lm_head_ce = LigerLMHeadCE(
        H=H,
        V=V,
        bias=True,
        label_smoothing=0.0,
        reduction="mean",
        dtype=dtype,
    ).to(device)

    # init the linear in all CEs with the same weights
    torch_lm_head_ce.lin.weight.data = liger_lm_head_ce.lin.weight.data = torch.rand(
        V, H, device=device, dtype=dtype
    )

    _tensor = torch.randn(B * T, H, device=device, dtype=dtype)
    _input1 = _tensor.detach().clone().requires_grad_(True)
    _input2 = _tensor.detach().clone().requires_grad_(True)

    target = torch.randint(0, V, (B * T,), device=device, dtype=torch.long)

    with torch.autocast(device_type=device, dtype=cast_dtype):
        output1 = torch_lm_head_ce(_input1, target)
        output2 = liger_lm_head_ce(_input2, target)

    assert_verbose_allclose(output1, output2, atol=atol, rtol=rtol)

    with torch.autocast(device_type=device, dtype=cast_dtype):
        output1.backward()
        output2.backward()

    assert_verbose_allclose(_input1.grad, _input2.grad, atol=atol, rtol=rtol)

    assert_verbose_allclose(
        torch_lm_head_ce.lin.weight.grad,
        liger_lm_head_ce.lin.weight.grad,
        atol=atol,
        rtol=rtol,
    )
