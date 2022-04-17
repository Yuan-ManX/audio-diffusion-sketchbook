import torch
from torch import nn
import math

# Define the model (a residual U-Net)

class ResidualBlock(nn.Module):
    def __init__(self, main, skip=None):
        super().__init__()
        self.main = nn.Sequential(*main)
        self.skip = skip if skip else nn.Identity()

    def forward(self, input):
        return self.main(input) + self.skip(input)


class ResConvBlock(ResidualBlock):
    def __init__(self, c_in, c_mid, c_out, is_last=False):
        skip = None if c_in == c_out else nn.Conv1d(c_in, c_out, 1, bias=False)
        super().__init__([
            nn.Conv1d(c_in, c_mid, 5, padding=2),
            nn.ReLU(inplace=True),
            nn.Conv1d(c_mid, c_out, 5, padding=2),
            nn.ReLU(inplace=True) if not is_last else nn.Identity(),
        ], skip)


class SelfAttention1d(nn.Module):
    def __init__(self, c_in, n_head=1):
        super().__init__()
        assert c_in % n_head == 0
        self.norm = nn.GroupNorm(1, c_in)
        self.n_head = n_head
        self.qkv_proj = nn.Conv1d(c_in, c_in * 3, 1)
        self.out_proj = nn.Conv1d(c_in, c_in, 1)

    def forward(self, input):
        n, c, s = input.shape
        qkv = self.qkv_proj(self.norm(input))
        qkv = qkv.view([n, self.n_head * 3, c // self.n_head, s]).transpose(2, 3)
        q, k, v = qkv.chunk(3, dim=1)
        scale = k.shape[3]**-0.25
        att = ((q * scale) @ (k.transpose(2, 3) * scale)).softmax(3)
        y = (att @ v).transpose(2, 3).contiguous().view([n, c, s])
        return input + self.out_proj(y)


class SkipBlock(nn.Module):
    def __init__(self, *main):
        super().__init__()
        self.main = nn.Sequential(*main)

    def forward(self, input):
        return torch.cat([self.main(input), input], dim=1)


class FourierFeatures(nn.Module):
    def __init__(self, in_features, out_features, std=1.):
        super().__init__()
        assert out_features % 2 == 0
        self.weight = nn.Parameter(torch.randn([out_features // 2, in_features]) * std)

    def forward(self, input):
        f = 2 * math.pi * input @ self.weight.T
        return torch.cat([f.cos(), f.sin()], dim=-1)


def expand_to_planes(input, shape):
    return input[..., None].repeat([1, 1, shape[2]])


class AudioDiffusion(nn.Module):
    def __init__(self):
        super().__init__()
        c_mults = [128, 128, 256, 256] + [512] * 12
        depth = len(c_mults)

        self.timestep_embed = FourierFeatures(1, 16)

        block = nn.Identity()
        for i in range(depth, 0, -1):
            c = c_mults[i - 1]
            if i > 1:
                c_prev = c_mults[i - 2]
                block = SkipBlock(
                    nn.AvgPool1d(2),
                    ResConvBlock(c_prev, c, c),
                    SelfAttention1d(c, c // 32) if i >= 9 else nn.Identity(),
                    ResConvBlock(c, c, c),
                    SelfAttention1d(c, c // 32) if i >= 9 else nn.Identity(),
                    ResConvBlock(c, c, c),
                    SelfAttention1d(c, c // 32) if i >= 9 else nn.Identity(),
                    block,
                    ResConvBlock(c * 2 if i != depth else c, c, c),
                    SelfAttention1d(c, c // 32) if i >= 9 else nn.Identity(),
                    ResConvBlock(c, c, c),
                    SelfAttention1d(c, c // 32) if i >= 9 else nn.Identity(),
                    ResConvBlock(c, c, c_prev),
                    SelfAttention1d(c_prev, c_prev // 32) if i >= 9 else nn.Identity(),
                    nn.Upsample(scale_factor=2, mode='linear', align_corners=False),
                )
            else:
                block = nn.Sequential(
                    ResConvBlock(2 + 16, c, c),
                    ResConvBlock(c, c, c),
                    ResConvBlock(c, c, c),
                    block,
                    ResConvBlock(c * 2, c, c),
                    ResConvBlock(c, c, c),
                    ResConvBlock(c, c, 2, is_last=True),
                )
        self.net = block

    def forward(self, input, t):
        timestep_embed = expand_to_planes(self.timestep_embed(t[:, None]), input.shape)
        return self.net(torch.cat([input, timestep_embed], dim=1))
