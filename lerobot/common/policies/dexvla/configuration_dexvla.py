# coding=utf-8
# Copyright 2024 The Qwen team, Alibaba Group and the HuggingFace Inc. team. All rights reserved.
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
"""Qwen2VL model configuration"""

from typing import Tuple

from dataclasses import dataclass, field

from transformers import AutoConfig

from lerobot.common.optim.optimizers import AdamWConfig
from lerobot.common.optim.schedulers import (
    CosineDecayWithWarmupSchedulerConfig,
)
from transformers.utils import logging
from lerobot.configs.policies import PreTrainedConfig
from lerobot.common.policies.dexvla.policy_heads.configuration_scaledp import ScaleDPPolicyConfig
from lerobot.common.policies.dexvla.policy_heads.configuration_unet_diffusion import UnetDiffusionPolicyConfig
from lerobot.common.policies.dexvla.qwe2_vla.configuration_qwen2_vla import Qwen2VLAConfig
from lerobot.configs.types import NormalizationMode

logger = logging.get_logger(__name__)
@PreTrainedConfig.register_subclass("dexvla")
@dataclass
class DexVLAConfig(PreTrainedConfig):
    # For loading policy head
    policy_head_type: str = 'scale_dp_policy'
    policy_head_size: str = 'ScaleDP_L'
    action_dim: int = 14
    state_dim: int = 14
    chunk_size: int = 50
    n_action_steps: int = 50
    n_obs_steps: int = 1

    hidden_size: int = 1536
    qwen2_vla_path: str = '/media/rl/HDD/data/weights/Qwen2-VL-2B-Instruct'

    pretrained_path: str = None # pretrained dexvla
    using_film: bool = True
    llm_loss_weight: float = 1.0
    with_llm_head: bool = True
    using_reasoning: bool = True
    resize_size: tuple = (240, 320)
    # Training presets
    optimizer_lr: float = 2e-5
    optimizer_betas: Tuple[float, float] = (0.9, 0.95)
    optimizer_eps: float = 1e-8
    optimizer_weight_decay: float = 1e-10

    scheduler_warmup_steps: int = 1_000
    scheduler_decay_steps: int = 30_000
    scheduler_decay_lr: float = 2.5e-6

    normalization_mapping: dict[str, NormalizationMode] = field(
        default_factory=lambda: {
            # "VISUAL": NormalizationMode.MEAN_STD,
            "STATE": NormalizationMode.MEAN_STD,
            "ACTION": NormalizationMode.MIN_MAX,
        }
    )

    def __post_init__(self):
        if self.n_action_steps > self.chunk_size:
            raise ValueError(
                f"The chunk size is the upper bound for the number of action steps per model invocation. Got "
                f"{self.n_action_steps} for `n_action_steps` and {self.chunk_size} for `chunk_size`."
            )
        if self.n_obs_steps != 1:
            raise ValueError(
                f"Multiple observation steps not handled yet. Got `nobs_steps={self.n_obs_steps}`"
            )
        if self.using_reasoning:
            assert self.using_film, f"using_reasoning requires `using_film=True`"
            assert self.with_llm_head, f"using_reasoning requires `with_llm_head=True`"
            print("You have set using_reasoning=True, please make sure your data has key 'reasoning'.")
        else:
            print(f"Warning:DexVLA recommend to use reasoning data which can better handle long-horizon and dexterous tasks. You can set 'using_reaasoning=True'.")

        if self.policy_head_type == 'scale_dp_policy':
            self.policy_head_config = AutoConfig.for_model(
                model_type=self.policy_head_type,
                model_size=self.policy_head_size,
                cond_dim=self.hidden_size,
                action_dim=self.action_dim,
                prediction_horizon=self.chunk_size,
                state_dim=self.state_dim
            )
        elif self.policy_head_type == 'unet_diffusion':
            self.policy_head_config = AutoConfig.for_model(
                model_type=self.policy_head_type,
                global_cond_dim=self.hidden_size,
                action_dim=self.action_dim,
                state_dim=self.state_dim
            )
        else:
            raise ValueError(f'Policy head type {self.policy_head_type} not supported')

        self.qwen2_vla_config = AutoConfig.from_pretrained(self.qwen2_vla_path)

    def validate_features(self) -> None:
        # TODO: implement value error
        if not self.image_features and not self.env_state_feature:
            raise ValueError("You must provide at least one image or the environment state among the inputs.")

        # for i in range(self.empty_cameras):
        #     key = f"observation.images.empty_camera_{i}"
        #     empty_camera = PolicyFeature(
        #         type=FeatureType.VISUAL,
        #         shape=(3, 480, 640),
        #     )
        #     self.input_features[key] = empty_camera

    def get_optimizer_preset(self) -> AdamWConfig:
        return AdamWConfig(
            lr=self.optimizer_lr,
            betas=self.optimizer_betas,
            eps=self.optimizer_eps,
            weight_decay=self.optimizer_weight_decay,
        )

    def get_scheduler_preset(self):
        return CosineDecayWithWarmupSchedulerConfig(
            peak_lr=self.optimizer_lr,
            decay_lr=self.scheduler_decay_lr,
            num_warmup_steps=self.scheduler_warmup_steps,
            num_decay_steps=self.scheduler_decay_steps,
        )

    @property
    def observation_delta_indices(self) -> None:
        return None

    @property
    def action_delta_indices(self) -> list:
        return list(range(self.chunk_size))

    @property
    def reward_delta_indices(self) -> None:
        return None



