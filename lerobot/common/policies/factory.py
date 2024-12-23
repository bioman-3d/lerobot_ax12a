#!/usr/bin/env python

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
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

from torch import nn

from lerobot.common.datasets.lerobot_dataset import LeRobotDatasetMetadata
from lerobot.common.policies.policy_protocol import Policy
from lerobot.configs.default import MainConfig


def get_policy_class(name: str) -> Policy:
    """Get the policy's class and config class given a name (matching the policy class' `name` attribute)."""
    if name == "tdmpc":
        from lerobot.common.policies.tdmpc.modeling_tdmpc import TDMPCPolicy

        return TDMPCPolicy
    elif name == "diffusion":
        from lerobot.common.policies.diffusion.modeling_diffusion import DiffusionPolicy

        return DiffusionPolicy
    elif name == "act":
        from lerobot.common.policies.act.modeling_act import ACTPolicy

        return ACTPolicy
    elif name == "vqbet":
        from lerobot.common.policies.vqbet.modeling_vqbet import VQBeTPolicy

        return VQBeTPolicy
    else:
        raise NotImplementedError(f"Policy with name {name} is not implemented.")


def make_policy(
    cfg: MainConfig, ds_meta: LeRobotDatasetMetadata, pretrained_policy_name_or_path: str | None = None
) -> Policy:
    """Make an instance of a policy class.

    Args:
        cfg (MainConfig): A MainConfig instance (see scripts). If `pretrained_policy_name_or_path` is
            provided, only `cfg.policy.type` is used while everything else is ignored.
        ds_meta (LeRobotDatasetMetadata): Dataset metadata to take input/output shapes and statistics to use
            for (un)normalization of inputs/outputs in the policy.
        pretrained_policy_name_or_path: Either the repo ID of a model hosted on the Hub or a path to a
            directory containing weights saved using `Policy.save_pretrained`. Note that providing this
            argument overrides everything in `hydra_cfg.policy` apart from `hydra_cfg.policy.type`.
    """
    # Note: Currently, if you try to run vqbet with mps backend, you'll get this error.
    # NotImplementedError: The operator 'aten::unique_dim' is not currently implemented for the MPS device. If
    # you want this op to be added in priority during the prototype phase of this feature, please comment on
    # https://github.com/pytorch/pytorch/issues/77764. As a temporary fix, you can set the environment
    # variable `PYTORCH_ENABLE_MPS_FALLBACK=1` to use the CPU as a fallback for this op. WARNING: this will be
    # slower than running natively on MPS.
    if cfg.policy.type == "vqbet" and cfg.device == "mps":
        raise NotImplementedError(
            "Current implementation of VQBeT does not support `mps` backend. "
            "Please use `cpu` or `cuda` backend."
        )

    policy_cls = get_policy_class(cfg.policy.type)
    cfg.policy.parse_features_from_dataset(ds_meta)

    if pretrained_policy_name_or_path is None:
        # Make a fresh policy.
        policy = policy_cls(cfg.policy, ds_meta.stats)
    else:
        # Load a pretrained policy and override the config if needed (for example, if there are inference-time
        # hyperparameters that we want to vary).
        # TODO(alexander-soare): This hack makes use of huggingface_hub's tooling to load the policy with,
        # pretrained weights which are then loaded into a fresh policy with the desired config. This PR in
        # huggingface_hub should make it possible to avoid the hack:
        # https://github.com/huggingface/huggingface_hub/pull/2274.
        policy = policy_cls(cfg.policy)
        policy.load_state_dict(policy_cls.from_pretrained(pretrained_policy_name_or_path).state_dict())

    policy.to(cfg.device)
    assert isinstance(policy, nn.Module)

    return policy
