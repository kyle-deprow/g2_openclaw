import pkgutil
import subprocess
import sys

import gateway.autoresearch


def test_autoresearch_modules_do_not_import_runner() -> None:
    package = gateway.autoresearch
    module_names = sorted(
        module.name for module in pkgutil.walk_packages(package.__path__, f"{package.__name__}.")
    )
    probe = (
        "import importlib, sys\n"
        "try:\n"
        "    importlib.import_module(sys.argv[1])\n"
        "except BaseException as exc:\n"
        "    print(f'IMPORT_ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "if 'gateway.autoresearch_runner' in sys.modules:\n"
        "    print('RUNNER_IMPORTED', file=sys.stderr)\n"
        "    raise SystemExit(2)\n"
    )
    for module_name in module_names:
        result = subprocess.run(
            [sys.executable, "-c", probe, module_name],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 1:
            message = f"{module_name} import error"
        elif result.returncode == 2:
            message = f"{module_name} imported gateway.autoresearch_runner"
        elif result.returncode != 0:
            message = f"{module_name} import probe failed"
        else:
            continue
        raise AssertionError(f"{message}\nstdout={result.stdout}\nstderr={result.stderr}")
