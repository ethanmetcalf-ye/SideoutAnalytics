import sentry_sdk

from .settings import get_settings


class UnknownSentryError(Exception):
    pass


def configure_observability(service: str) -> bool:
    # pick that service's DSN and do nothing when it is unset, then call sentry_sdk.init
    # with the DSN, settings.environment, a trace sample rate that depends on the
    # environment, and send_default_pii=False.
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
    if service_dsn:
        myenvironment = get_settings().environment
        # Only want to trace errors on the free tier. Other logging done
        # via JSON output of logging lib with structlog stdlib ProcessorFormatter
        try:
            sentry_sdk.init(
                dsn=service_dsn,
                send_default_pii=False,
                environment=myenvironment,
            )
            return True
        except sentry_sdk.utils.BadDsn as e:
            raise ValueError(
                f"The provided DSN for service {service} is invalid. "
                f"Please review the SENTRY_{service.upper()}_DSN variable."
            ) from e
        except Exception as e:
            raise UnknownSentryError("Sentry could not be started due to an unknown error...") from e
    else:
        return False
