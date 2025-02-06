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
import shutil
from pathlib import Path

import torch
from safetensors.torch import save_file

from lerobot.common.datasets.factory import make_dataset
from lerobot.common.optim.factory import make_optimizer_and_scheduler
from lerobot.common.policies.factory import make_policy, make_policy_config
from lerobot.common.utils.utils import set_global_seed
from lerobot.configs.default import DatasetConfig
from lerobot.configs.train import TrainPipelineConfig


def get_policy_stats(ds_repo_id, env_name, policy_name, policy_kwargs, train_kwargs):
    # TODO(rcadene, aliberts): env_name?
    set_global_seed(1337)

    train_cfg = TrainPipelineConfig(
        # TODO(rcadene, aliberts): remove dataset download
        dataset=DatasetConfig(repo_id=ds_repo_id, episodes=[0]),
        policy=make_policy_config(policy_name, **policy_kwargs),
        device="cpu",
        **train_kwargs,
    )
    train_cfg.validate()  # Needed for auto-setting some parameters

    dataset = make_dataset(train_cfg)
    policy = make_policy(train_cfg.policy, ds_meta=dataset.meta, device=train_cfg.device)
    policy.train()

    optimizer, _ = make_optimizer_and_scheduler(train_cfg, policy)
    dataloader = torch.utils.data.DataLoader(
        dataset,
        num_workers=0,
        batch_size=train_cfg.batch_size,
        shuffle=False,
    )

    batch = next(iter(dataloader))
    output_dict = policy.forward(batch)
    output_dict = {k: v for k, v in output_dict.items() if isinstance(v, torch.Tensor)}
    loss = output_dict["loss"]

    loss.backward()
    grad_stats = {}
    for key, param in policy.named_parameters():
        if param.requires_grad:
            grad_stats[f"{key}_mean"] = param.grad.mean()
            grad_stats[f"{key}_std"] = (
                param.grad.std() if param.grad.numel() > 1 else torch.tensor(float(0.0))
            )

    optimizer.step()
    param_stats = {}
    for key, param in policy.named_parameters():
        param_stats[f"{key}_mean"] = param.mean()
        param_stats[f"{key}_std"] = param.std() if param.numel() > 1 else torch.tensor(float(0.0))

    optimizer.zero_grad()
    policy.reset()

    # HACK: We reload a batch with no delta_indices as `select_action` won't expect a timestamps dimension
    # We simulate having an environment using a dataset by setting delta_indices to None and dropping tensors
    # indicating padding (those ending with "_is_pad")
    dataset.delta_indices = None
    batch = next(iter(dataloader))
    obs = {}
    for k in batch:
        # TODO: regenerate the safetensors
        # for backward compatibility
        if k.endswith("_is_pad"):
            continue
        # for backward compatibility
        if k == "task":
            continue
        if k.startswith("observation"):
            obs[k] = batch[k]

    if hasattr(train_cfg.policy, "n_action_steps"):
        actions_queue = train_cfg.policy.n_action_steps
    else:
        actions_queue = train_cfg.policy.n_action_repeats

    actions = {str(i): policy.select_action(obs).contiguous() for i in range(actions_queue)}
    return output_dict, grad_stats, param_stats, actions


def save_policy_to_safetensors(output_dir, env_name, policy_name, policy_kwargs, file_name_extra):
    env_policy_dir = Path(output_dir) / f"{env_name}_{policy_name}{file_name_extra}"

    if env_policy_dir.exists():
        print(f"Overwrite existing safetensors in '{env_policy_dir}':")
        print(f" - Validate with: `git add {env_policy_dir}`")
        print(f" - Revert with: `git checkout -- {env_policy_dir}`")
        shutil.rmtree(env_policy_dir)

    env_policy_dir.mkdir(parents=True, exist_ok=True)
    output_dict, grad_stats, param_stats, actions = get_policy_stats(env_name, policy_name, policy_kwargs)
    save_file(output_dict, env_policy_dir / "output_dict.safetensors")
    save_file(grad_stats, env_policy_dir / "grad_stats.safetensors")
    save_file(param_stats, env_policy_dir / "param_stats.safetensors")
    save_file(actions, env_policy_dir / "actions.safetensors")


if __name__ == "__main__":
    env_policies = [
        ("lerobot/xarm_lift_medium", "xarm", "tdmpc", {"use_mpc": False}, "use_policy"),
        ("lerobot/xarm_lift_medium", "xarm", "tdmpc", {"use_mpc": True}, "use_mpc"),
        (
            "lerobot/pusht",
            "pusht",
            "diffusion",
            {
                "n_action_steps": 8,
                "num_inference_steps": 10,
                "down_dims": [128, 256, 512],
            },
            "",
        ),
        ("lerobot/aloha_sim_insertion_human", "aloha", "act", {"n_action_steps": 10}, ""),
        (
            "lerobot/aloha_sim_insertion_human",
            "aloha",
            "act",
            {"n_action_steps": 1000, "chunk_size": 1000},
            "_1000_steps",
        ),
    ]
    if len(env_policies) == 0:
        raise RuntimeError("No policies were provided!")
    for ds_repo_id, env, policy, policy_kwargs, file_name_extra in env_policies:
        save_policy_to_safetensors(
            "tests/data/save_policy_to_safetensors", ds_repo_id, env, policy, policy_kwargs, file_name_extra
        )
