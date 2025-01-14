# coding=utf-8
# Copyright 2022 Google LLC.
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

"""Module to manage checkpoint metadata and automatic checkpoint deletion."""

import dataclasses
import datetime
import os
import typing
from typing import Any, Optional, List, Union, Mapping

from etils import epath
import jax
import orbax.checkpoint
from paxml import checkpoint_pb2
from paxml import checkpoints
from praxis import py_utils
import tensorflow.compat.v2 as tf

from paxml import preemption  # mapped to internal


CheckpointType = checkpoint_pb2.CheckpointType

CHECKPOINT_PREFIX = 'checkpoint_'
DEFAULT_ITEM_NAME = orbax.checkpoint.checkpoint_manager.DEFAULT_ITEM_NAME
METRIC_ITEM_NAME = orbax.checkpoint.checkpoint_manager.METRIC_ITEM_NAME


@dataclasses.dataclass
class CheckpointManagerOptions(orbax.checkpoint.CheckpointManagerOptions):
  """Options for constructing OrbaxCheckpointManager.

  See superclass.

  Attributes:
    todelete_subdir: If set, checkpoints to be deleted will be only renamed into
      a subdirectory with the provided string. Otherwise, they will be directly
      deleted from the file system. Useful if checkpoint deletion is time
      consuming. By default, delete the checkpoint assets.
  """
  todelete_subdir: Optional[str] = None


class OrbaxCheckpointManager(orbax.checkpoint.CheckpointManager):
  """Provides Pax-specific logic for orbax.checkpoint.CheckpointManager.

  Pax only supports a single checkpointable item (TrainState) and checkpoints
  are saved under a different folder name in a flat manner (no per-item
  sub-directories).

  Additionally, Pax supports extra options provided via CheckpointManagerOptions
  (see above).

  An instance of this class can be created on several JAX processes.
  All public APIs may be called by all processes.
  """

  def __init__(
      self,
      *args,
      checkpoint_type: CheckpointType = CheckpointType.CHECKPOINT_UNSPECIFIED,
      **kwargs):
    if checkpoint_type == CheckpointType.CHECKPOINT_UNSPECIFIED:
      raise ValueError('Must specify checkpoint type.')
    self._checkpoint_type = checkpoint_type
    super().__init__(*args, **kwargs)
    # Set to 1 if not provided or set to 0.
    self._options.save_interval_steps = self._options.save_interval_steps or 1

  def _checkpoint_name(self, step: Union[int, str]) -> str:
    if self._checkpoint_type == CheckpointType.CHECKPOINT_FLAX:
      return f'{CHECKPOINT_PREFIX}{step}'
    else:
      return checkpoints.checkpoint_name(step)

  def should_save(self, step: int) -> bool:
    """Indicates whether there is a need to save a checkpoint."""
    # Whether to save an on-demand checkpoint due to preemption
    if preemption.reached_preemption_sync_point(step):
      return True
    last_checkpoint_step = (
        self._last_checkpoint.step if self._last_checkpoint else None)
    # Ensure current step is between the last step and next step (accounting for
    # save interval). The `last_checkpoint_step` may not be initialized, in
    # which case we should save. Otherwise, step must fall on the specified
    # save interval. This condition accounts for the possibility of saving
    # on preemption, in which case we want to maintain the same save period as
    # if preemption had not happened.
    return last_checkpoint_step is None or (
        last_checkpoint_step < step and
        step % self._options.save_interval_steps == 0)

  # TODO(b/262389151) Rely on superclass logic when possible.
  def _create_checkpoints(
      self,
  ) -> List[orbax.checkpoint.checkpoint_manager.CheckpointInfo]:
    """Create a list of CheckpointInfo for existing checkpoints.

    If none are present, returns empty list.

    This method is copied from the superclass, except for the logic reading
    existing checkpoint steps.

    Returns:
      a list of CheckpointInfo, sorted by increasing step.
    """
    checkpoint_dirnames = tf.io.gfile.listdir(self.directory)
    dirnames = [
        x for x in checkpoint_dirnames if checkpoints.is_checkpoint_asset(x)
    ]
    steps = sorted([
        int(os.path.basename(x).replace(checkpoints.CHECKPOINT_PREFIX, ''))
        for x in dirnames
    ])
    if not steps:
      return []

    tz = datetime.timezone.utc
    times = [
        datetime.datetime.fromtimestamp(
            (self.directory / self._checkpoint_name(step)).stat().mtime, tz=tz)
        for step in steps
    ]

    def get_metrics(step):
      if self._track_best:
        restored = self._restore_impl(step, {METRIC_ITEM_NAME: None}, {})
        if METRIC_ITEM_NAME in restored:
          return restored[METRIC_ITEM_NAME]
      return None

    metrics = [get_metrics(step) for step in steps]

    return [
        orbax.checkpoint.checkpoint_manager.CheckpointInfo(
            step=s, time=t, metrics=m)
        for s, t, m in zip(steps, times, metrics)
    ]

  def _get_save_directory(self,
                          step: int,
                          directory: epath.Path,
                          key_name: Optional[str] = None) -> epath.Path:
    """Returns the standardized path to a save directory for a single item."""
    if key_name is None or key_name == DEFAULT_ITEM_NAME:
      return checkpoints._make_checkpoint_step_dir(  # pylint: disable=protected-access
          directory, step, checkpoint_type=self._checkpoint_type
      )
    else:
      raise ValueError(
          f'Unrecognized item {key_name} is not currently supported.')

  def _cleanup_tmp_directories(self):
    if py_utils.is_mock_tpu_backend():
      return

    tmp_dirs = [
        f
        for f in self.directory.glob(CHECKPOINT_PREFIX + '*')
        if not orbax.checkpoint.utils.is_checkpoint_item_finalized(f)
    ]
    if jax.process_index() == 0:
      for tmp_dir in tmp_dirs:
        assert tmp_dir.is_dir()
        tmp_dir.rmtree()
    py_utils.sync_global_devices('cleanup_tmp_dirs')

  def _delete_directory(self, step: int):
    if jax.process_index() != 0:
      return
    options = typing.cast(CheckpointManagerOptions, self._options)
    todelete_subdir = options.todelete_subdir
    checkpoint_name = self._checkpoint_name(step)

    if todelete_subdir:
      rename_dir = self.directory / todelete_subdir
      if not rename_dir.exists():
        rename_dir.mkdir(parents=True)
      src = self.directory / checkpoint_name
      dst = rename_dir / checkpoint_name
      # TODO(pax-team): Check if dst already exists?
      tf.io.gfile.rename(src, dst)
    else:
      super()._delete_directory(step)

  def structure(self) -> Union[Any, Mapping[str, Any]]:
    if self._checkpoint_type == CheckpointType.CHECKPOINT_FLAX:
      raise ValueError('`structure` not supported for Flax format checkpoints.')
    return super().structure()
