from typing import Literal
import os, sys
import logging
import structlog #type: ignore

LogMode = Literal['development', 'production']

from dotenv import load_dotenv #type: ignore
load_dotenv()

def configure_logging(
        mode: LogMode | None,
        level: str = "INFO",
) -> None :
    mode = mode or os.environ.get("CRAWLER_LOG_MODE", "development")
    level = os.environ.get("CRAWLER_LOG_LEVEL", level).upper()
    numeric_level = getattr(logging, level, logging.INFO)

    shared_processors = [       #run on every log event regardless of mode
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        structlog.processors.StackInfoRenderer(),
    ]

    if mode == "production":
        processors = shared_processors + [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
        renderer = None 
    else:
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(
                colors=sys.stdout.isatty(),
                exception_formatter=structlog.dev.plain_traceback,
            )
        ]
        renderer = None
    
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
    )

def get_logger(name:str | None = None, **initital_context):
    log = structlog.get_logger(name)
    if initital_context:
        log = log.bind(**initital_context)
    return log

_log = get_logger("logger")