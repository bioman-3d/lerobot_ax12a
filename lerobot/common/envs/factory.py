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
import importlib

import gymnasium as gym

from lerobot.common.envs.configs import AlohaEnv, EnvConfig, PushtEnv, RealEnv, XarmEnv


def make_env_config(env_type: str, **kwargs) -> EnvConfig:
    if env_type == "real_world":
        return RealEnv(**kwargs)
    elif env_type == "aloha":
        return AlohaEnv(**kwargs)
    elif env_type == "pusht":
        return PushtEnv(**kwargs)
    elif env_type == "xarm":
        return XarmEnv(**kwargs)
    else:
        raise ValueError(f"Policy type '{env_type}' is not available.")


def make_env(
    cfg: EnvConfig, n_envs: int | None = None, use_async_envs: bool = False
) -> gym.vector.VectorEnv | None:
    """Makes a gym vector environment according to the evaluation config.

    n_envs can be used to override eval.batch_size in the configuration. Must be at least 1.
    """
    if n_envs is not None and n_envs < 1:
        raise ValueError("`n_envs must be at least 1")

    if cfg.type == "real_world":
        return

    package_name = f"gym_{cfg.type}"

    try:
        importlib.import_module(package_name)
    except ModuleNotFoundError as e:
        print(f"{package_name} is not installed. Please install it with `pip install 'lerobot[{cfg.type}]'`")
        raise e

    gym_handle = f"{package_name}/{cfg.task}"
    gym_kwgs = getattr(cfg, "gym", {})

    if getattr(cfg, "episode_length", None):
        gym_kwgs["max_episode_steps"] = cfg.episode_length

    # batched version of the env that returns an observation of shape (b, c)
    env_cls = gym.vector.AsyncVectorEnv if use_async_envs else gym.vector.SyncVectorEnv
    env = env_cls(
        [
            lambda: gym.make(gym_handle, disable_env_checker=True, **gym_kwgs)
            # TODO(rcadene, aliberts): fix cfg.eval.batch_size not exist
            for _ in range(n_envs if n_envs is not None else cfg.eval.batch_size)
        ]
    )

    return env
