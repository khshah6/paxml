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

"""Tuning loop for PAX."""

import enum
import math
import os
import re
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Union
from absl import logging
from clu import platform
import jax
from paxml import automl
from paxml import base_experiment
from paxml import metric_utils
from paxml import trainer_lib
from praxis import base_hyperparams
from praxis import base_input
from praxis import py_utils
from praxis import pytypes

import pyglove as pg  # mapped to internal
import tensorflow.compat.v2 as tf


instantiate = base_hyperparams.instantiate
TrialFn = Callable[[base_experiment.BaseExperiment,  # Experiment config.
                    platform.WorkUnit,               # Platform work unit.
                    str,                             # Job log dir
                    trainer_lib.EarlyStoppingFn],    # Early stopping fn.
                   None]


# Step interval for sub-experiments to report their metrics, which is necessary
# to map progresses of individual sub-experiments to the progress of the trial.
# This means that when there is 3 sub-experiments in a tuning experiment:
# the 1st sub-experiment starts its step 0 at step 0;
# the 2nd sub-experiment starts its step 0 at step 1000_000_000;
# the 3rd sub-experiment starts its step 0 at step 2000_000_000.
SUB_EXPERIMENT_STEP_INTERVAL = 1000_000_000


def get_search_space(
    experiment_config: base_experiment.BaseExperiment
    ) -> pg.hyper.DynamicEvaluationContext:
  """Gets the search space from experiment config."""
  # Inspect the search space by evaluating the hyperparameters.
  # We include tuning parameters from both the `task` and `datasets` in the
  # search space. A caveat is that when multiple datasets have tunable
  # parameters, even one of them is not evaluated, its tunable parameters will
  # be included. We can improve this in the future if this turns out to be an
  # issue.
  def inspect_search_space() -> None:
    _ = experiment_config.task()
    _ = experiment_config.datasets()
    _ = experiment_config.decoder_datasets()

  return pg.hyper.trace(inspect_search_space, require_hyper_name=True)


def tune(trial_fn: TrialFn,
         experiment_config: base_experiment.BaseExperiment,
         work_unit: platform.WorkUnit,
         job_log_dir: str,
         study: Optional[str] = None,
         pythia_port: Optional[int] = None,
         is_metric_reporting_role: bool = True,
         tuner_group: Optional[str] = None,
         max_num_trials: Optional[int] = None) -> None:
  """Tune an experiment.

  An experiment can be tuned by running a tuning loop, with each iteration
  calling `trial_fn` for evaluating a trial sampled by the controller.

  The tuning procedure is set up with the following steps:
  1) It calls the `search` method of the experiment class to get the
     hyperparameters for the search, which contains the definition for
     the search algorithm and reward function.
  2) It inspects the search space by calling the `task` and `datasets` methods
     of the experiment class, thus all PyGlove search primitives (e.g.
     `pg.oneof`) will be collected.
  3) Then it starts a loop with `pg.sample`, based on the search space and
     search algorithm obtained above.
  4) Within the tuning loop, the `example` is provided as a context manager
     to connect the controller decisions with the return value of each search
     primitive called under the context manager. Therefore, we delegate the
     trial evaluation logic to `trial_fn`, which is done by passing
     a per-trial early stopping function for reporting measurements, completing/
     early stopping the trial.

  Args:
    trial_fn: Trial function, which will be called for each trial in the loop.
    experiment_config: The experiment to run.
    work_unit: Work unit for adding experiment artifact and reporting status.
    job_log_dir: The directory used for storing logs and writing checkpoints.
    study: Vizier study name.
    pythia_port: Pythia port for hosting Vizier algorithms.
    is_metric_reporting_role: Whether current process is in the role for
      reporting metrics. Among train/eval/decoder, only one role can report
      metrics to the controller at the moment.
    tuner_group: The identifier for the tuner group that current process belongs
      to. If None, all processes will be working on different trials. When
      specified, paired training, eval and decoder processes should use the
      same tuner group, which will get the same trial during tuning. Only one
      process (with is_metric_reporting_role=True) should report the measurement
      and signal the completion or stopping of the training.
    max_num_trials: An optional max number of trials for current tuning.
      If not None, it will override the default max number of trials specified
      by the `search` method of the experiment.
  """
  # Google-internal tuning infra init.

  search_hparams = experiment_config.search()
  search_algorithm = instantiate(search_hparams.search_algorithm)()
  reward_fn = instantiate(search_hparams.search_reward)
  max_num_trials = max_num_trials or search_hparams.max_num_trials
  errors_to_skip = search_hparams.errors_to_skip or []
  cross_step_metric_aggregator = instantiate(
      search_hparams.cross_step_metric_aggregator
      or automl.LastReportedMetricValues.HParams())

  search_space = get_search_space(experiment_config)
  if search_space.dna_spec.is_constant:
    raise ValueError(f'Aborting tuning: there is no tunable parameters in'
                     f'experiment {experiment_config.__class__.__name__!r}.')

  tf.io.gfile.makedirs(job_log_dir)
  logging.info('Search space: %s', search_space.dna_spec)
  search_space_debug_file = os.path.join(job_log_dir, 'search_space.txt')
  _write_file_once(search_space_debug_file, str(search_space.dna_spec))
  work_unit.create_artifact(platform.ArtifactType.FILE, search_space_debug_file,
                            'search_space')

  logging.info('Search algorithm: %s', search_algorithm)
  algorithm_debug_file = os.path.join(job_log_dir, 'search_algorithm.txt')
  _write_file_once(algorithm_debug_file, str(search_algorithm))
  work_unit.create_artifact(platform.ArtifactType.FILE, algorithm_debug_file,
                            'search_algorithm')

  sub_experiments = experiment_config.sub_experiments()
  trial_dirname_generator = TrialDirectoryNameGenerator(
      job_log_dir, search_space.dna_spec)

  published_study_link = False
  for example, feedback in pg.sample(
      search_space, search_algorithm, max_num_trials,
      group=tuner_group,
      name=study if study and study.startswith('local') else None):

  # Google-internal tuning infra logging.

    logging.info(
        'Start working on trial %d (group=%r)...', feedback.id, tuner_group)
    # Context manager to deliver different program hyperparameters
    # in each trial.
    with example():
      trial_dirname = trial_dirname_generator.dirname(feedback.id, feedback.dna)

      # Mark trial as infeasible on NaN. PAX user can add more error
      # through `SearchHParams.errors_to_skip`.
      with feedback.skip_on_exceptions([FloatingPointError] + errors_to_skip):
        try:
          for i, (sub_experiment_id, sub_experiment_cls) in enumerate(
              sub_experiments.items()):
            trial_fn(
                sub_experiment_cls(),  # pytype: disable=not-instantiable
                work_unit,
                trial_dirname,
                _get_early_stopping_fn(
                    sub_experiment_id=sub_experiment_id,
                    feedback=feedback,
                    reward_fn=reward_fn,
                    cross_step_metric_aggregator=cross_step_metric_aggregator,
                    is_metric_reporting_role=is_metric_reporting_role,
                    is_last_experiment=(i == len(sub_experiments) - 1),
                    tuning_step_start=i * automl.SUB_EXPERIMENT_STEP_OFFSET))
        except automl.EarlyStoppingError as e:
          if jax.process_index() == 0:
            if e.skip:
              py_utils.sync_global_devices(
                  f'Sync on trial {feedback.id} early stopping started.')
              feedback.skip(e.skip_reason or 'Unknown.')
              py_utils.sync_global_devices(
                  f'Sync on trial {feedback.id} early stopping completed.')
              logging.info(
                  'Trial %d is early stopped at step %d and will be skipped '
                  'by controller. Reason: %s.',
                  feedback.id, e.step, e.skip_reason)
            else:
              reward = e.reward
              if reward is None:
                reward = reward_fn(e.metrics, e.step)
              feedback.add_measurement(
                  reward=reward,
                  step=e.step,
                  metrics=e.metrics,
                  checkpoint_path=e.checkpoint)
              py_utils.sync_global_devices(
                  f'Sync on trial {feedback.id} early stopping started.')
              feedback.done()
              py_utils.sync_global_devices(
                  f'Sync on trial {feedback.id} early stopping completed.')
              logging.info(
                  'Trial %d is early stopped at step %d with reward %f which '
                  'will be fed back to the controller. Metrics: %s.',
                  feedback.id, e.step, reward, e.metrics)
  logging.info('Completed with all trials for study %r', study)


def _get_early_stopping_fn(
    sub_experiment_id: str,
    feedback: pg.tuning.Feedback,
    reward_fn: automl.BaseReward,
    cross_step_metric_aggregator: automl.CrossStepMetricAggregator,
    is_metric_reporting_role: bool,
    is_last_experiment: bool,
    tuning_step_start: int) -> trainer_lib.EarlyStoppingFn:
  """Gets early stopping function based on a feedback object."""

  def should_stop_early(metrics: Dict[str, float],
                        running_mode: trainer_lib.RunningMode,
                        global_step: int, is_last_ckpt: bool) -> bool:
    """Early stopping function."""
    if is_metric_reporting_role:
      tuning_step = tuning_step_start + global_step

      # `metrics_by_dataset` could be None for interleaved train/eval
      # when evaluation is not performed at current global step.
      if (jax.process_index() == 0 and
          (running_mode & trainer_lib.RunningMode.EVAL or
           running_mode & trainer_lib.RunningMode.DECODE)):

        # Append sub_experiment_id as the suffix.
        if sub_experiment_id:
          metrics = {f'{k}:{sub_experiment_id}': v for k, v in metrics.items()}

        # Computing reward and report back to the tuning service.
        reward = reward_fn(metrics, tuning_step)
        if math.isnan(reward):
          raise FloatingPointError('Reward is NaN.')
        feedback.add_measurement(reward, metrics=metrics, step=tuning_step)
        logging.info(
            'Measurement is reported to trial %d (sub-experiment=%s) at step '
            '%d with reward value %f (mode=%s, is_last_checkpoint=%s): %s.',
            feedback.id, sub_experiment_id, global_step, reward, running_mode,
            is_last_ckpt, metrics)

      if is_last_ckpt:
        py_utils.sync_global_devices(
            f'Sync on trial (sub-experiment={sub_experiment_id!r}) '
            f'termination at step {global_step} started.')
        # `feedback.done` should be called just once per trial.
        if is_last_experiment and jax.process_index() == 0:
          _add_final_measurement(
              feedback, cross_step_metric_aggregator,
              reward_fn, tuning_step + 1)
          feedback.done()
        py_utils.sync_global_devices(
            f'Sync on trial (sub-experiment={sub_experiment_id!r}) '
            f'termination at step {global_step} completed.')
        logging.info('Sub-experiment %s (trial %d) is completed.',
                     sub_experiment_id, feedback.id)
        logging.info('Trial %d is now completed.', feedback.id)
    return feedback.should_stop_early()
  return should_stop_early


def _write_file_once(file_path, content):
  """Writes debug information to file only once."""
  if not tf.io.gfile.exists(file_path):
    try:
      with tf.io.gfile.GFile(file_path, 'w') as f:
        f.write(content)
    except tf.errors.NotFoundError:
      logging.warn(
          'Cannot write file %r as another process is writing to the same '
          'file. This is not an issue as the file is only created for '
          'debugging purpose and has the same content among all the workers. '
          'So any successful write will achieve this purpose.', file_path)


def _add_final_measurement(
    feedback: pg.tuning.Feedback,
    cross_step_metric_aggregator: automl.CrossStepMetricAggregator,
    reward_fn: automl.BaseReward,
    global_step: int):
  """Adds final measurement to trial based on metric aggregator."""
  # Poll the metrics across steps for aggregation.
  metrics_across_steps = []
  for m in feedback.get_trial().measurements:
    metrics = dict(m.metrics)
    metrics['reward'] = m.reward
    metrics_across_steps.append((m.step, metrics))

  final_metrics = cross_step_metric_aggregator(metrics_across_steps)
  final_metrics.pop('reward', None)
  final_reward = reward_fn(final_metrics, global_step)
  feedback.add_measurement(final_reward, final_metrics, step=global_step)
  logging.info(
      'Final measurement is reported to trial %d at step %d '
      'with reward value %f and metrics %s.',
      feedback.id, global_step, final_reward, final_metrics)


class EvalMetrics(NamedTuple):
  input_p: Optional[Sequence[base_input.BaseInput.HParams]] = None
  metrics_list: Optional[Sequence[Optional[Dict[str, float]]]] = None
  scoring_metrics_list: Optional[Sequence[Optional[Dict[str, float]]]] = None
  steps_per_sec: Optional[float] = None


class DecodeMetrics(NamedTuple):
  input_p: Optional[Sequence[base_input.BaseInput.HParams]] = None
  metrics_list: Optional[Sequence[Optional[Dict[str, float]]]] = None
  processed_metrics_list: Optional[Sequence[Optional[Dict[str, float]]]] = None
  seqio_metrics_list: Optional[Sequence[Optional[Dict[str, float]]]] = None
  steps_per_sec: Optional[float] = None


def should_early_stop(early_stop_fn: trainer_lib.EarlyStoppingFn,
                      global_step: int,
                      is_last_ckpt: bool,
                      train_weighted_scalars: Optional[
                          Union[pytypes.WeightedScalars,
                                pytypes.WeightedScalarsList]] = None,
                      eval_train_metrics: Optional[Dict[str, float]] = None,
                      eval_metrics: Optional[EvalMetrics] = None,
                      decode_metrics: Optional[DecodeMetrics] = None,
                      num_params: Optional[float] = None,
                      train_steps_per_sec: Optional[float] = None) -> bool:
  """Returns True if the training process should stop early."""
  if early_stop_fn is None:
    return False

  # Detect running mode.
  running_mode = trainer_lib.RunningMode.detect(
      has_train_metrics=train_steps_per_sec is not None,
      has_eval_metrics=bool(eval_metrics),
      has_decode_metrics=bool(decode_metrics))

  # Since train metrics will be produced at each step, for performance reasons,
  # we only aggregate the metrics at the last checkpoint or at the step when
  # evaluation or decoding takes place.
  train_metrics = None
  if train_weighted_scalars is not None:
    if is_last_ckpt or running_mode.has_eval or running_mode.has_decode:
      train_weighted_scalars = py_utils.maybe_unreplicate_for_fully_replicated(
          train_weighted_scalars)
      train_metrics = metric_utils.as_float_dict(train_weighted_scalars)
      logging.info(
          ('Aggregate train weighted scalars as tuning metrics. '
           'Metrics=%s, WeightedScalars=%s'),
          train_metrics, train_weighted_scalars)

  # Aggregate metrics for tuning.
  tuning_metrics = _aggregate_metrics(train_metrics, eval_train_metrics,
                                      eval_metrics, decode_metrics, num_params,
                                      train_steps_per_sec)
  return early_stop_fn(tuning_metrics, running_mode, global_step, is_last_ckpt)


def _aggregate_metrics(
    train_metrics: Optional[Dict[str, float]] = None,
    eval_train_metrics: Optional[Dict[str, float]] = None,
    eval_metrics: Optional[EvalMetrics] = None,
    decode_metrics: Optional[DecodeMetrics] = None,
    num_params: Optional[float] = None,
    train_steps_per_sec: Optional[float] = None) -> Dict[str, float]:
  """Aggregate metrics from training, evaluation and decoding for tuning."""
  metrics = {}
  if train_metrics is not None:
    metric_utils.update_float_dict(metrics, train_metrics, 'train')

  if eval_train_metrics is not None:
    metric_utils.update_float_dict(
        metrics, eval_train_metrics, 'eval_train/metrics')

  def _add_input_based_metrics(
      input_p: Optional[List[base_input.BaseInput.HParams]],
      metrics_list: Optional[List[Optional[Dict[str, float]]]],
      dataset_type: Optional[str] = None,
      category: Optional[str] = None):
    if input_p is None or metrics_list is None:
      return
    assert len(input_p) == len(metrics_list), (input_p, metrics_list)
    merged = {}
    for p, m in zip(input_p, metrics_list):
      if m is not None:
        prefix = p.name
        if dataset_type is not None:
          prefix = f'{dataset_type}_{prefix}'
        if category is not None:
          prefix = f'{prefix}/{category}'
        metric_utils.update_float_dict(merged, m, prefix)
    metric_utils.update_float_dict(metrics, merged)

  if eval_metrics:
    eval_input_p = eval_metrics.input_p
    _add_input_based_metrics(eval_input_p, eval_metrics.metrics_list,
                             'eval_test', 'metrics')
    _add_input_based_metrics(eval_input_p, eval_metrics.scoring_metrics_list,
                             'eval_test', 'scoring_eval')
  if decode_metrics:
    decode_input_p = decode_metrics.input_p
    _add_input_based_metrics(decode_input_p, decode_metrics.metrics_list,
                             'decode_test')
    _add_input_based_metrics(decode_input_p,
                             decode_metrics.processed_metrics_list,
                             'decode_test')
    _add_input_based_metrics(decode_input_p, decode_metrics.seqio_metrics_list,
                             'decode_test')

  # Add training metrics.
  def _add_metric_if_not_none(name: str, value: Optional[float]):
    if value is not None:
      metrics[name] = value

  _add_metric_if_not_none('train_steps_per_sec', train_steps_per_sec)
  if eval_metrics is not None:
    metrics['eval_steps_per_sec'] = eval_metrics.steps_per_sec
  if decode_metrics is not None:
    metrics['decode_steps_per_sec'] = decode_metrics.steps_per_sec
  _add_metric_if_not_none('num_params', num_params)
  return metrics


def is_last_checkpoint(
    running_mode: trainer_lib.RunningMode,
    global_step: int,
    num_train_steps: int,
    eval_interval_steps: int,
    decode_interval_steps: int,
    save_interval_steps: int) -> bool:
  """Returns True if current step should be treated as last evaluation."""
  remaining = num_train_steps - global_step
  is_last = remaining == 0
  if not is_last:
    last_eval = False
    if running_mode & trainer_lib.RunningMode.EVAL:
      last_eval = remaining < max(eval_interval_steps, save_interval_steps)
    last_decode = False
    if running_mode & trainer_lib.RunningMode.DECODE:
      last_decode = remaining < max(decode_interval_steps, save_interval_steps)
    is_last = last_eval or last_decode
  return is_last


class TrialDirectoryNameGenerator:
  """Trial directory name generator.

  Each trial will be creating a sub-directory under the root experiment
  directory. To make it easy to compare trials in TensorBoard, we include
  the search decision point names and values in the directory name. This class
  is introduced for deciding the directory name with human readability.

  By default, it will include both decision names and values with '|' delimited
  string. For example: 'my_experiment/123/x=1|y=abc|z=(0)', where 123 is the
  trial ID, which contains 3 decisions points named 'x', 'y', and 'z'. '(0)'
  indicates the choice index for 'z', which will be used when its literal value
  is not path friendly.

  When there are lots of decision points, usually for NAS, including decision
  names will make the directory name very long (determined by
  `total_name_length_threshold`). In such case, we only include the decision
  values in the path. For example: 'my_experiment/123/0|0.1|abc|(0)|(1)|(2)'.
  """

  class DecisionFormat(enum.Enum):
    EMPTY = 0
    VALUE = 1
    LITERAL = 2

  def __init__(self,
               root_dir: str,
               dna_spec: pg.DNASpec,
               total_name_length_threshold: int = 64):
    path_regex = re.compile(r'^[A-Za-z0-9\-_\.]+$')
    def path_friendly(v: str):
      return bool(path_regex.match(v))

    decision_formats: Dict[pg.geno.DecisionPoint,
                           TrialDirectoryNameGenerator.DecisionFormat] = {}

    self._formatted_categorical_literals = {}

    # Determine whether decisions can be included
    total_key_len = 0
    for dp in dna_spec.decision_points:
      if isinstance(dp, pg.geno.CustomDecisionPoint):
        decision_formats[dp] = TrialDirectoryNameGenerator.DecisionFormat.EMPTY
      elif isinstance(dp, pg.geno.Choices) and all(
          path_friendly(self.format_literal(v)) for v in dp.literal_values):
        decision_formats[dp] = (
            TrialDirectoryNameGenerator.DecisionFormat.LITERAL)
      else:
        decision_formats[dp] = TrialDirectoryNameGenerator.DecisionFormat.VALUE
      total_key_len += len(dp.name)
    include_decision_names = total_key_len < total_name_length_threshold
    self._root_dir = root_dir
    self._include_decision_names = include_decision_names
    self._decision_formats = decision_formats

  def format_literal(self, literal: Union[str, int, float]) -> str:
    """Formats literal values."""
    if isinstance(literal, int):
      return str(literal)
    if isinstance(literal, float):
      return f'{literal:.3e}'
    formatted = self._formatted_categorical_literals.get(literal, None)
    if formatted is None:
      formatted = self._format_categorical(literal)
      self._formatted_categorical_literals[literal] = formatted
    return formatted

  def _format_categorical(self, literal: str) -> str:
    """Formats common categorical values."""
    if literal.startswith('\'') and literal.endswith('\''):
      return literal[1:-1]
    if literal.startswith('<class'):
      # Try extract class name.
      r = re.match(r'^<class \'(.+)\'>$', literal)
      if r:
        qual_name = r.groups()[0]
        assert qual_name is not None
        return qual_name.split('.')[-1]
    # NOTE(daiyip): add formatting for more common categorical literals here.
    return literal

  def dirname(self, trial_id: int, dna: pg.DNA) -> str:
    """Gets the directory name for a trial."""
    kv_pairs = []
    for dp, fmt in self._decision_formats.items():
      decision = dna[dp]
      assert isinstance(decision, pg.DNA), decision
      if fmt == TrialDirectoryNameGenerator.DecisionFormat.EMPTY:
        v = '(CUSTOM)'
      elif fmt == TrialDirectoryNameGenerator.DecisionFormat.LITERAL:
        assert isinstance(dp, pg.geno.Choices), dp
        v = self.format_literal(decision.literal_value)
      else:
        assert fmt == TrialDirectoryNameGenerator.DecisionFormat.VALUE
        if isinstance(dp, pg.geno.Float):
          v = self.format_literal(decision.value)
        else:
          assert isinstance(dp, pg.geno.Choices)
          v = f'({decision.value})'
      kv_pairs.append((dp.name, v))

    if self._include_decision_names:
      items = [f'{k}={v}' for k, v in kv_pairs]
    else:
      items = [v for _, v in kv_pairs]
    return os.path.join(self._root_dir, str(trial_id), '|'.join(items))
