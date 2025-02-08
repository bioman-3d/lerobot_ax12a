import pytest
import torch
from torch.optim import Adam
from torch.optim.lr_scheduler import LambdaLR

from lerobot.common.constants import SCHEDULER_STATE
from lerobot.common.optim.schedulers import (
    CosineDecayWithWarmupSchedulerConfig,
    DiffuserSchedulerConfig,
    VQBeTSchedulerConfig,
    load_scheduler_state,
    save_scheduler_state,
)


@pytest.fixture
def optimizer():
    return Adam([torch.nn.Parameter(torch.randn(2, 2, requires_grad=True))], lr=0.01)


def test_diffuser_scheduler(optimizer):
    config = DiffuserSchedulerConfig(name="cosine", num_warmup_steps=5)
    scheduler = config.build(optimizer, num_training_steps=100)
    assert isinstance(scheduler, LambdaLR)

    optimizer.step()
    scheduler.step()
    expected_state_dict = {
        "_get_lr_called_within_step": False,
        "_last_lr": [0.002],
        "_step_count": 2,
        "base_lrs": [0.01],
        "last_epoch": 1,
        "lr_lambdas": [None],
        "verbose": False,
    }
    assert scheduler.state_dict() == expected_state_dict


def test_vqbet_scheduler(optimizer):
    config = VQBeTSchedulerConfig(num_warmup_steps=10, num_vqvae_training_steps=20, num_cycles=0.5)
    scheduler = config.build(optimizer, num_training_steps=100)
    assert isinstance(scheduler, LambdaLR)

    optimizer.step()
    scheduler.step()
    expected_state_dict = {
        "_get_lr_called_within_step": False,
        "_last_lr": [0.01],
        "_step_count": 2,
        "base_lrs": [0.01],
        "last_epoch": 1,
        "lr_lambdas": [None],
        "verbose": False,
    }
    assert scheduler.state_dict() == expected_state_dict


def test_cosine_decay_with_warmup_scheduler(optimizer):
    config = CosineDecayWithWarmupSchedulerConfig(
        num_warmup_steps=10, num_decay_steps=90, peak_lr=0.01, decay_lr=0.001
    )
    scheduler = config.build(optimizer, num_training_steps=100)
    assert isinstance(scheduler, LambdaLR)

    optimizer.step()
    scheduler.step()
    expected_state_dict = {
        "_get_lr_called_within_step": False,
        "_last_lr": [0.0018181818181818188],
        "_step_count": 2,
        "base_lrs": [0.01],
        "last_epoch": 1,
        "lr_lambdas": [None],
        "verbose": False,
    }
    assert scheduler.state_dict() == expected_state_dict


def test_save_scheduler_state(optimizer, tmp_path):
    config = VQBeTSchedulerConfig(num_warmup_steps=10, num_vqvae_training_steps=20, num_cycles=0.5)
    scheduler = config.build(optimizer, num_training_steps=100)
    save_scheduler_state(scheduler, tmp_path)
    assert (tmp_path / SCHEDULER_STATE).is_file()


def test_save_load_scheduler_state(optimizer, tmp_path):
    config = VQBeTSchedulerConfig(num_warmup_steps=10, num_vqvae_training_steps=20, num_cycles=0.5)
    scheduler = config.build(optimizer, num_training_steps=100)

    save_scheduler_state(scheduler, tmp_path)
    loaded_scheduler = load_scheduler_state(scheduler, tmp_path)

    assert scheduler.state_dict() == loaded_scheduler.state_dict()
