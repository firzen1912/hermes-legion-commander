"""Hermes Legion Commander package."""

__version__ = "2.0.0"


def _install_managed_account_loader() -> None:
    # GUI-managed account metadata is composed with hand-authored Legion TOML at
    # the package boundary. Raw API keys are never stored in the companion file.
    from . import legion_config as _legion_config
    from .account_registry import install_legion_config_loader

    install_legion_config_loader(_legion_config)


def _install_repo_data_safeguard() -> None:
    # Install at the package boundary so every import path that reaches
    # executor_runtime.invoke_executor receives the same provider-neutral guard.
    from . import executor_runtime as _executor_runtime
    from .repo_data_safeguard import install_executor_runtime_guard

    install_executor_runtime_guard(_executor_runtime)


_install_managed_account_loader()
_install_repo_data_safeguard()
del _install_managed_account_loader
del _install_repo_data_safeguard
