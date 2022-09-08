import importlib
from paxml import base_experiment
from paxml import experiment_registry

def get_experiment(experiment_name: str) -> base_experiment.BaseExperimentT:
  """Retrieves an experiment config from the global registry."""
  experiment_class = experiment_registry.get(experiment_name)
  if experiment_class is not None:
    return experiment_class
  # Try to import the module that registers the experiment, assuming the
  # experiment name contains the full path.
  module_name = experiment_name.rsplit('.', 1)[0]
  # internal experiment module import code
  try:
    importlib.import_module(module_name)
  except ModuleNotFoundError as e:
    raise ValueError(f'Could not find experiment `{experiment_name}`.') from e
  experiment_class = experiment_registry.get(experiment_name)
  if experiment_class is not None:
    return experiment_class
  raise ValueError(f'Could not find experiment `{experiment_name}`.')
  