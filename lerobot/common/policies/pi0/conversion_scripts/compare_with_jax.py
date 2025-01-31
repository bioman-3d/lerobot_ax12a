import json
import pickle
from pathlib import Path

import torch

from lerobot.common.datasets.lerobot_dataset import LeRobotDatasetMetadata
from lerobot.common.policies.factory import make_policy
from lerobot.common.policies.pi0.configuration_pi0 import PI0Config


def display(tensor: torch.Tensor):
    if tensor.dtype == torch.bool:
        tensor = tensor.float()
    print(f"Shape: {tensor.shape}")
    print(f"Mean: {tensor.mean().item()}")
    print(f"Std: {tensor.std().item()}")
    print(f"Min: {tensor.min().item()}")
    print(f"Max: {tensor.max().item()}")


def main():
    # obs_path = "/raid/pablo/alohasim/obs.pkl"
    # action_path = "/raid/pablo/alohasim/action.pkl"
    # checkpoint_dir = Path("/raid/pablo/.cache/openpi/openpi-assets/checkpoints/pi0_aloha_sim")
    # noise_bsize_2_path = "/raid/pablo/alohasim/noise_bsize_2.pkl"
    # noise_path = "/raid/pablo/alohasim/noise_2.pkl"
    # save_pretrained_path = "outputs/exported/2025-01-27/12-17-01_aloha_pi0/last/pretrained_model"

    device = "cuda"
    num_motors = 14

    obs_path = "/home/remi_cadene/code/openpi/data/aloha_sim/obs.pkl"
    action_path = "/home/remi_cadene/code/openpi/data/aloha_sim/action.pkl"
    ckpt_jax_dir = Path("/home/remi_cadene/.cache/openpi/openpi-assets/checkpoints/pi0_aloha_sim")
    ckpt_torch_dir = Path("/home/remi_cadene/.cache/openpi/openpi-assets/checkpoints/pi0_aloha_sim_pytorch")
    noise_bsize_2_path = "/home/remi_cadene/code/openpi/data/aloha_sim/noise_bsize_2.pth"
    noise_path = "/home/remi_cadene/code/openpi/data/aloha_sim/noise_2.pth"
    # save_pretrained_path = "outputs/exported/2025-01-27/12-17-01_aloha_pi0/last/pretrained_model"

    with open(obs_path, "rb") as f:
        obs = pickle.load(f)

    with open(action_path, "rb") as f:
        pi_actions = torch.from_numpy(pickle.load(f)["actions"])

    with open(ckpt_jax_dir / "assets/norm_stats.json") as f:
        norm_stats = json.load(f)

    time_beta = torch.load("/home/remi_cadene/code/openpi/data/aloha_sim/beta_time.pth")
    time_beta = torch.from_numpy(time_beta).to(dtype=torch.float32, device=device)
    pi_losses = torch.from_numpy(torch.load("/home/remi_cadene/code/openpi/data/aloha_sim/loss.pth"))

    dataset_stats = {
        "observation.images.top": {
            "mean": torch.zeros(3, 1, 1),
            "std": torch.ones(3, 1, 1),
            "min": torch.zeros(3, 1, 1),
            "max": torch.ones(3, 1, 1),
        },
        "observation.state": {
            "mean": torch.tensor(norm_stats["norm_stats"]["state"]["mean"][:num_motors]),
            "std": torch.tensor(norm_stats["norm_stats"]["state"]["std"][:num_motors]),
            "min": torch.zeros(num_motors),
            "max": torch.ones(num_motors),
        },
        "action": {
            "mean": torch.tensor(norm_stats["norm_stats"]["actions"]["mean"][:num_motors]),
            "std": torch.tensor(norm_stats["norm_stats"]["actions"]["std"][:num_motors]),
            "min": torch.zeros(num_motors),
            "max": torch.ones(num_motors),
        },
    }

    cam_top = torch.from_numpy(obs["images"]["cam_high"]).unsqueeze(0) / 255.0
    cam_top = cam_top.to(dtype=torch.float32)

    state = torch.from_numpy(obs["state"]).unsqueeze(0)
    state = state.to(dtype=torch.float32)

    gt_action = pi_actions.to(dtype=torch.float32)

    # Add bsize=2
    make_double_bsize = False
    if make_double_bsize:
        cam_top = torch.cat([cam_top, cam_top], dim=0)
        state = torch.cat([state, state], dim=0)
        noise = torch.load(noise_bsize_2_path)
        noise[1] = noise[0]
    else:
        noise = torch.load(noise_path)

    if not isinstance(noise, torch.Tensor):
        noise = torch.from_numpy(noise)

    noise = noise.to(dtype=torch.float32, device=device)

    batch = {
        "observation.images.top": cam_top,
        "observation.state": state,
        "action": gt_action.unsqueeze(0),
        "task": ["Transfer cube"],
    }

    for k in batch:
        if isinstance(batch[k], torch.Tensor):
            batch[k] = batch[k].to(device=device)

    ds_meta = LeRobotDatasetMetadata("lerobot/aloha_sim_transfer_cube_human")

    ds_meta.stats = dataset_stats

    cfg = PI0Config.from_pretrained(ckpt_torch_dir)
    cfg.pretrained_path = ckpt_torch_dir
    policy = make_policy(cfg, device, ds_meta)

    loss_dict = policy.forward(batch, noise=noise, time=time_beta)
    loss_dict["loss"].backward()
    print("losses")
    display(loss_dict["losses_after_forward"])
    print("pi_losses")
    display(pi_losses)

    actions = []
    for _ in range(50):
        action = policy.select_action(batch, noise=noise)
        actions.append(action)

    actions = torch.stack(actions, dim=1)
    pi_actions = pi_actions.to(dtype=actions.dtype, device=actions.device)
    pi_actions = pi_actions.unsqueeze(0)
    print("actions")
    display(actions)
    print()
    print("pi_actions")
    display(pi_actions)
    print("atol=3e-2", torch.allclose(actions, pi_actions, atol=3e-2))
    print("atol=2e-2", torch.allclose(actions, pi_actions, atol=2e-2))
    print("atol=1e-2", torch.allclose(actions, pi_actions, atol=1e-2))


if __name__ == "__main__":
    main()
