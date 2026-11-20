import logging
import sys
from typing import Self

import structlog

from .settings import get_settings


class StructLogConfig:
    def __init__(self, environment: str, show_timestamps: bool = True) -> None:
        self.environment = environment
        self.show_timestamps = show_timestamps

    @classmethod
    def from_settings(cls) -> Self:
        settings = get_settings()
        return cls(environment=settings.environment)

    def get_formatter(self) -> structlog.stdlib.ProcessorFormatter:
        if self.show_timestamps:
            timestamper = structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S")
            shared_processors = [
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                timestamper,
            ]
        else:
            shared_processors = [structlog.stdlib.add_log_level, structlog.stdlib.add_logger_name]

        structlog.configure(
            processors=shared_processors
            + [
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )

        formatter = structlog.stdlib.ProcessorFormatter(
            # These run ONLY on `logging` entries that do NOT originate within
            # structlog.
            foreign_pre_chain=shared_processors,
            # These run on ALL entries after the pre_chain is done.
            processors=[
                # Remove _record & _from_structlog.
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer()
                if self.environment.lower() == "production"
                else structlog.dev.ConsoleRenderer(),
            ],
        )
        return formatter


def configure_logging() -> None:
    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        return
    structlog_config = StructLogConfig.from_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(structlog_config.get_formatter())
    root_logger.addHandler(handler)
    root_logger.setLevel(get_settings().log_level)
