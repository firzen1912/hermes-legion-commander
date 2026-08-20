"""Hermes Legion Commander package."""

__version__ = "1.7.4"


def _install_repo_data_safeguard() -> None:
    # Install at the package boundary so every import path that reaches
    # executor_runtime.invoke_executor receives the same provider-neutral guard.
    from . import executor_runtime as _executor_runtime
    from .repo_data_safeguard import install_executor_runtime_guard

    install_executor_runtime_guard(_executor_runtime)


_install_repo_data_safeguard()
del _install_repo_data_safeguard
