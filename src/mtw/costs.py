"""Cost arithmetic. The prices below are ASSUMPTIONS to check against the AWS pricing page for eu-west-2 before quoting."""

from dataclasses import dataclass

HOURS_PER_MONTH = 730


@dataclass(frozen=True)
class Prices:
    rpu_hour_usd: float = 0.375  # Redshift Serverless, per RPU-hour (US East price on the AWS page; London may be higher)
    managed_storage_gb_month_usd: float = 0.024  # Redshift managed storage, approximate
    s3_gb_month_usd: float = 0.023  # S3 Standard, approximate
    spectrum_tb_scanned_usd: float = 5.0  # Spectrum, per TB scanned


def rpu_hours(charged_seconds, rpus):
    return charged_seconds * rpus / 3600.0


def compute_cost(rpu_hours_used, prices=Prices()):
    return rpu_hours_used * prices.rpu_hour_usd


def storage_monthly(hot_gb, cold_gb, prices=Prices()):
    return hot_gb * prices.managed_storage_gb_month_usd + cold_gb * prices.s3_gb_month_usd


def offload_saving_monthly(moved_gb, prices=Prices()):
    """What moving `moved_gb` from warehouse storage to S3 saves per month (storage only; Spectrum scans cost extra when queried)."""
    return moved_gb * (prices.managed_storage_gb_month_usd - prices.s3_gb_month_usd)


def scale(measured, from_gb, to_gb):
    """Straight-line projection, clearly a projection and not a measurement."""
    return measured * (to_gb / from_gb)
