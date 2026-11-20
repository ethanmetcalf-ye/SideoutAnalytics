import sentry_sdk

from .settings import get_settings


def configure_observability(service: str) -> bool:
    # starts Sentry for one service ("api" or "pipeline") and returns whether
    # it is on. An unset DSN leaves it off. An unknown service or a malformed
    # DSN raises ValueError, so a typo can't silently turn Sentry off.
    observability_enabled_services = {
        "api": get_settings().sentry_api_dsn,
        "pipeline": get_settings().sentry_pipeline_dsn,
    }
    if service.lower() not in observability_enabled_services:
        raise ValueError(
            f"Service {service} is not valid. Observability is only configured for "
            f"{sorted(observability_enabled_services)}"
        )

    service_dsn = observability_enabled_services[service.lower()]
    if not service_dsn:
        return False

    try:
        # Errors only: no traces_sample_rate, so tracing stays off (free plan).
        # Everything else is logged through structlog.
        sentry_sdk.init(
            dsn=service_dsn,
            send_default_pii=False,
            environment=get_settings().environment,
        )
    except sentry_sdk.utils.BadDsn as e:
        raise ValueError(
            f"The provided DSN for service {service} is invalid. "
            f"Please review the SENTRY_{service.upper()}_DSN variable."
        ) from e
    return True
