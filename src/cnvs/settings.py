import logging
import os
from dataclasses import dataclass
from pathlib import Path


VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


@dataclass(frozen=True)
class Settings:
    config_dir: Path
    log_level: str

    @classmethod
    def from_environment(
        cls,
        *,
        config_dir_override: str | Path | None = None,
        log_level_override: str | None = None,
    ) -> "Settings":
        config_dir = config_dir_override
        if config_dir is None:
            config_dir = os.environ.get("CNVS_CONFIG_DIR")
        if config_dir is None:
            config_dir = Path.cwd() / "config"

        log_level = log_level_override
        if log_level is None:
            log_level = os.environ.get("CNVS_LOG_LEVEL")
        if log_level is None:
            log_level = "WARNING"
        log_level = log_level.upper()
        if log_level not in VALID_LOG_LEVELS:
            valid_levels = ", ".join(sorted(VALID_LOG_LEVELS))
            raise ValueError(
                f"Invalid CNVS log level {log_level!r}; choose one of: {valid_levels}."
            )
        logging_level = getattr(logging, log_level)
        logging.basicConfig(level=logging_level)
        logging.getLogger().setLevel(logging_level)
        return cls(config_dir=Path(config_dir).expanduser().resolve(), log_level=log_level)
