"""Quality vocabulary — planning.md §5.3.

These are *visible fields*, not internal notes.  They live here rather than on
any one model because both the calendar and price sides grade themselves with
the same words.  Nothing in this module imports models, so it is safe to
import from anywhere.
"""

from django.db import models


class TimestampConfidence(models.TextChoices):
    EXACT = "exact", "exact — second-level release time from an authoritative source"
    MINUTE = "minute", "minute — rounded to the minute"
    DATE_ONLY = "date_only", "date only — no usable time of day"
    INFERRED = "inferred", "inferred — reconstructed from schedule or convention"


class ForecastProvenance(models.TextChoices):
    POINT_IN_TIME = "point_in_time", "point in time — captured before the release"
    VENDOR_STORED = "vendor_stored", "vendor stored — may have been updated after the fact"
    UNVERIFIED = "unverified", "unverified — scraped dataset of unknown provenance"
    MODELLED = "modelled", "modelled — §6.7 baseline, not a survey consensus"
    NONE = "none", "none"


class ActualProvenance(models.TextChoices):
    FIRST_PRINT = "first_print", "first print — the number as originally published"
    CURRENT = "current_may_be_revised", "current — may include later revisions"
    UNKNOWN = "unknown", "unknown"


class CrossSource(models.TextChoices):
    AGREE = "agree", "agree — sources within tolerance"
    DISAGREE = "disagree", "disagree — outside tolerance, both values retained"
    SINGLE = "single_source", "single source — nothing to compare against"


class VolCheck(models.TextChoices):
    """§4.1 validation rule: does a volatility spike sit where the stored
    timestamp claims?  This is how we find our own bugs."""

    CONFIRMED = "confirmed", "confirmed — spike within ±2 minutes"
    OFFSET = "offset", "offset — spike found, but not where the timestamp says"
    NO_SPIKE = "no_spike", "no spike — nothing measurable happened here"
    NOT_CHECKED = "not_checked", "not checked"


class MappingStatus(models.TextChoices):
    """§9 note: canonical_code will be the buggiest artifact in the project."""

    AUTO = "auto", "auto — created by an ingest, not yet reviewed"
    MAPPED = "mapped", "mapped — canonical code assigned by hand"
    IGNORED = "ignored", "ignored — deliberately out of scope"


class EpistemicStatus(models.TextChoices):
    """§6.10.  Used in code and UI, not only in prose."""

    ESTABLISHED = "established", "ESTABLISHED — peer-reviewed and replicated"
    ESTIMATE = "estimate", "ESTIMATE — measured from our own sample"
    HYPOTHESIS = "hypothesis", "HYPOTHESIS — not established by this tool"
    UNKNOWN = "unknown", "UNKNOWN — undetermined, and we say so"


class Importance(models.IntegerChoices):
    """The calendar sites' traffic-light rating, kept as *their* claim.
    §6.4 measures our own ranking and compares the two."""

    UNKNOWN = 0, "unknown"
    LOW = 1, "low"
    MEDIUM = 2, "medium"
    HIGH = 3, "high"


class Regime(models.TextChoices):
    """§4.6 regime tags for the split control."""

    PRE_CRISIS = "pre_crisis", "pre-crisis (2007–08)"
    CRISIS = "crisis", "crisis (2008–09)"
    ZIRP_QE = "zirp_qe", "ZIRP/QE (2010–15)"
    NORMALISATION = "normalisation", "normalisation (2015–19)"
    COVID = "covid", "COVID (2020–21)"
    INFLATION = "inflation_hiking", "inflation/hiking (2022–23)"
    POST_HIKING = "post_hiking", "post-hiking (2024–)"
