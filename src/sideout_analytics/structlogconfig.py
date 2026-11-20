import logging
import sys
from typing import Self

import structlog

from .settings import get_settings

# the name of the handler configure_logging adds, so a second call can tell it is already there
HANDLER_NAME = "sideout-structlog"
# uvicorn gives these their own handlers and stops them reaching the root logger
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class StructLogConfig:
    def __init__(self, environment: str, show_timestamps: bool = True) -> None:
        self.environment = environment
        self.show_timestamps = show_timestamps

    @classmethod
    def from_settings(cls) -> Self:
        settings = get_settings()
        return cls(environment=settings.environment)

    def get_shared_processors(self) -> list[structlog.typing.Processor]:
        processors = [structlog.stdlib.add_log_level, structlog.stdlib.add_logger_name]
        if self.show_timestamps:
            # ISO 8601 in UTC with a Z ("2026-11-20T02:15:48.123456Z"), so
            # whatever parses the JSON doesn't have to guess the time zone
            processors.append(structlog.processors.TimeStamper(fmt="iso"))
        return processors

    def get_formatter(self) -> structlog.stdlib.ProcessorFormatter:
        if self.environment.lower() == "production":
            # format_exc_info turns an exception into a traceback string the JSON can hold
            renderers = [structlog.processors.format_exc_info, structlog.processors.JSONRenderer()]
        else:
            renderers = [structlog.dev.ConsoleRenderer()]

        return structlog.stdlib.ProcessorFormatter(
            # These run ONLY on `logging` entries that do NOT originate within
            # structlog.
            foreign_pre_chain=self.get_shared_processors(),
            # These run on ALL entries after the pre_chain is done.
            processors=[
                # Remove _record & _from_structlog.
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                *renderers,
            ],
        )


def configure_logging() -> None:
    root_logger = logging.getLogger()
    if any(handler.get_name() == HANDLER_NAME for handler in root_logger.handlers):
        return
    structlog_config = StructLogConfig.from_settings()

    structlog.configure(
        processors=[
            *structlog_config.get_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(structlog_config.get_formatter())
    root_logger.addHandler(handler)
    root_logger.setLevel(get_settings().log_level)

    # Send uvicorn's own logs (startup, access lines) through the root handler
    # too, or the api's output mixes JSON lines with uvicorn's plain text.
    for name in UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
