"""Process-local argument binding only. This module never launches anything."""
from pathlib import Path
import inspect
import shlex

FIXED_ARGS = ('-machinelist', 'list=n116:1:4', '-batchoptions',
              "'Maxwell 2D/NumCoresPerDistributedTask'=4")
CANONICAL_STRING = '-machinelist list=n116:1:4 -batchoptions "\'Maxwell 2D/NumCoresPerDistributedTask\'=4"'

def bind_private_settings(settings, launch_string, *, private_root, settings_module_path):
    """The reviewed Root bootstrap must additionally bind actual authority/node.

    Does not create authority, configure the environment or enable a fallback.
    One job, one private-import process; call before stock Desktop creation.
    """
    if type(launch_string) is not str or tuple(shlex.split(launch_string)) != FIXED_ARGS:
        raise ValueError('only exact reviewed node/task/core batchoptions are permitted')
    root = Path(private_root).resolve(strict=True)
    origin = Path(settings_module_path).resolve(strict=True)
    if origin != root/'ansys/aedt/core/generic/settings.py':
        raise ValueError('settings must originate from complete pinned private SDK')
    try:
        actual_class_origin = Path(inspect.getfile(type(settings))).resolve(strict=True)
    except (TypeError, OSError) as exc:
        raise ValueError('actual private Settings class required') from exc
    if actual_class_origin != origin:
        raise ValueError('actual Settings class does not match pinned module origin')
    if getattr(settings, 'custom_desktop_launch_args', None) is not None:
        raise ValueError('fresh process-local launch binding required')
    settings.custom_desktop_launch_args = launch_string
    return FIXED_ARGS
