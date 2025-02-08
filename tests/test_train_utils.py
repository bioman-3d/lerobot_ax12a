from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from lerobot.common.constants import (
    CHECKPOINTS_DIR,
    LAST_CHECKPOINT_LINK,
    OPTIMIZER_PARAM_GROUPS,
    OPTIMIZER_STATE,
    RNG_STATE,
    SCHEDULER_STATE,
    TRAINING_STATE_DIR,
    TRAINING_STEP,
)
from lerobot.common.utils.train_utils import (
    get_step_checkpoint_dir,
    get_step_identifier,
    load_training_state,
    load_training_step,
    save_checkpoint,
    save_training_state,
    save_training_step,
    update_last_checkpoint,
)


@pytest.fixture
def mock_optimizer():
    optimizer = Mock()
    optimizer.state_dict.return_value = {"param_groups": [{"lr": 0.001}], "state": {}}
    return optimizer


@pytest.fixture
def mock_scheduler():
    scheduler = Mock()
    scheduler.state_dict.return_value = {"last_epoch": 5}
    return scheduler


def test_get_step_identifier():
    assert get_step_identifier(5, 1000) == "000005"
    assert get_step_identifier(123, 100_000) == "000123"
    assert get_step_identifier(456789, 1_000_000) == "0456789"


def test_get_step_checkpoint_dir():
    output_dir = Path("/checkpoints")
    step_dir = get_step_checkpoint_dir(output_dir, 1000, 5)
    assert step_dir == output_dir / CHECKPOINTS_DIR / "000005"


def test_save_load_training_step(tmp_path):
    save_training_step(5000, tmp_path)
    assert (tmp_path / TRAINING_STEP).is_file()


def test_load_training_step(tmp_path):
    step = 5000
    save_training_step(step, tmp_path)
    loaded_step = load_training_step(tmp_path)
    assert loaded_step == step


def test_update_last_checkpoint(tmp_path):
    checkpoint = tmp_path / "0005"
    checkpoint.mkdir()
    update_last_checkpoint(checkpoint)
    last_checkpoint = tmp_path / LAST_CHECKPOINT_LINK
    assert last_checkpoint.is_symlink()
    assert last_checkpoint.resolve() == checkpoint


@patch("lerobot.common.utils.train_utils.save_training_state")
def test_save_checkpoint(mock_save_training_state, tmp_path, mock_optimizer):
    policy = Mock()
    cfg = Mock()
    save_checkpoint(tmp_path, 10, cfg, policy, mock_optimizer)
    policy.save_pretrained.assert_called_once()
    cfg.save_pretrained.assert_called_once()
    mock_save_training_state.assert_called_once()


def test_save_training_state(tmp_path, mock_optimizer, mock_scheduler):
    save_training_state(tmp_path, 10, mock_optimizer, mock_scheduler)
    assert (tmp_path / TRAINING_STATE_DIR).is_dir()
    assert (tmp_path / TRAINING_STATE_DIR / TRAINING_STEP).is_file()
    assert (tmp_path / TRAINING_STATE_DIR / RNG_STATE).is_file()
    assert (tmp_path / TRAINING_STATE_DIR / OPTIMIZER_STATE).is_file()
    assert (tmp_path / TRAINING_STATE_DIR / OPTIMIZER_PARAM_GROUPS).is_file()
    assert (tmp_path / TRAINING_STATE_DIR / SCHEDULER_STATE).is_file()


def test_save_load_training_state(tmp_path, mock_optimizer, mock_scheduler):
    save_training_state(tmp_path, 10, mock_optimizer, mock_scheduler)
    loaded_step, loaded_optimizer, loaded_scheduler = load_training_state(
        tmp_path, mock_optimizer, mock_scheduler
    )
    assert loaded_step == 10
    assert loaded_optimizer is mock_optimizer
    assert loaded_scheduler is mock_scheduler
