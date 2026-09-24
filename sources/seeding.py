"""Reference data — the source register, instruments, release groups and known
dislocations.

planning.md §5 (source register), locked decision 6 (majors only), §4.6 (what
a 2007 start actually contains).

Lives here rather than in the management command so the console button and the
CLI run the same code.  Idempotent: re-running updates descriptive fields and
leaves health, history and hand-edited mappings alone.
"""

from datetime import datetime, timezone
from decimal import Decimal

from calendar_data.models import ReleaseGroup
from prices.models import MAJORS, Instrument, MarketEvent, MarketEventKind
from quality.enums import Regime
from sources.models import RobotsStatus, Source, SourceKind

SOURCES = [
    dict(
        key="mt5_calendar",
        name="MetaTrader 5 calendar",
        kind=SourceKind.CALENDAR,
        base_url="",
        gives="forecast, actual, previous, revised-previous, revision no., impact, period, event time",
        depth_note="2007+ or 2017+ — terminal-dependent, measure it (P0.5 Q1)",
        role="Primary. The only free source with all four value fields at depth.",
        timezone_rule="MT5 trade server time (TimeTradeServer); broker-dependent, "
        "usually EET/EEST, and server DST rules have changed over the years. "
        "Needs TimeServerDST-style correction over a 2007 start (§4.1).",
        robots_status=RobotsStatus.NOT_APPLICABLE,
        terms_note="Local terminal. Bridge is an MQL5 script writing UTF-8 CSV to "
        "MQL5\\Files — the Python MetaTrader5 package has no calendar function (§4.2).",
        fetch_policy_json={"transport": "mql5_csv", "encoding": "utf-8"},
        supports_date_range=True,
    ),
    dict(
        key="forexfactory_weekly",
        name="ForexFactory weekly feed",
        kind=SourceKind.CALENDAR,
        base_url="https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        gives="forecast, previous, impact, currency",
        depth_note="current week only",
        role="Forward point-in-time capture (§7.3). No actuals in the feed.",
        timezone_rule="Explicit UTC offset in the payload (US Eastern). Verify "
        "against a known release before trusting (§4.1).",
        robots_status=RobotsStatus.RESTRICTED,
        terms_note="Verified: polling hard gets you blocked quickly. Community "
        "guidance is once a week, cached (§4.2 Route B).",
        fetch_policy_json={"cadence": "weekly", "min_interval_hours": 24},
        supports_date_range=False,
    ),
    dict(
        key="forexfactory_pages",
        name="ForexFactory calendar pages",
        kind=SourceKind.CALENDAR,
        base_url="https://www.forexfactory.com/calendar",
        gives="release time (Unix, DST-correct), actual, forecast, previous, "
        "revised previous, impact, stable series id",
        depth_note="Verified back to Jan 2007: 326 events that month, 273 with "
        "actuals, 220 with forecasts, 100% with a stable series id",
        role="Primary historical calendar. Covers 2007→now with timestamps and "
        "all four value fields, which closes the 2007–2017 gap §4.2 called the "
        "plan's biggest open risk.",
        timezone_rule="Unix seconds, verified DST-correct: 08:30 New York reads "
        "13:30 UTC in January and 12:30 UTC in June. No conversion assumption "
        "needed for this source.",
        robots_status=RobotsStatus.RESTRICTED,
        terms_note="Unofficial interface behind Cloudflare; data is embedded as a "
        "JS object rather than only rendered, so the parser is sturdier than a "
        "DOM scrape but can still change without notice. Forecasts are what the "
        "site displays today, so they are vendor-stored and never "
        "point-in-time (§4.3). One request per month; paced.",
        fetch_policy_json={"cadence": "manual", "delay_seconds": 2.5, "granularity": "month"},
    ),
    dict(
        key="alfred",
        name="ALFRED (FRED archival)",
        kind=SourceKind.ACTUALS,
        base_url="https://api.stlouisfed.org/fred/",
        gives="actual vintages (first prints)",
        depth_note="very deep",
        role="Ground truth for actual_first_print, US.",
        timezone_rule="Reference periods, not release instants.",
        robots_status=RobotsStatus.ALLOWED,
        terms_note="Free API key required.",
    ),
    dict(
        key="dbnomics",
        name="DBnomics",
        kind=SourceKind.ACTUALS,
        base_url="https://api.db.nomics.world/v22/",
        gives="actuals across agencies, one unified API, values unmodified",
        depth_note="deep",
        role="Actuals backbone for all 8 economies (§4.2 Route D).",
        timezone_rule="Indexed by reference period, not release event — validates "
        "and fills `actual`, does not replace the calendar.",
        robots_status=RobotsStatus.ALLOWED,
    ),
    dict(
        key="agency_schedule",
        name="Official agencies",
        kind=SourceKind.CALENDAR,
        base_url="",
        gives="actuals + official release schedule",
        depth_note="deep",
        role="Authoritative release timestamps for tier-1 (§4.2 Route C).",
        timezone_rule="Local time with local DST — use IANA zones, never a fixed offset.",
        robots_status=RobotsStatus.ALLOWED,
        terms_note="BLS, BEA, Census, Eurostat, ECB, ONS, BoE, Destatis, BoJ, RBA, BoC, SNB, RBNZ.",
    ),
    dict(
        key="philly_fed_spf",
        name="Philadelphia Fed — Survey of Professional Forecasters",
        kind=SourceKind.REFERENCE,
        base_url="https://www.philadelphiafed.org/surveys-and-data/",
        gives="true survey consensus, quarterly",
        depth_note="1968+",
        role="Independent cross-check on MT5's forecast values.",
        robots_status=RobotsStatus.ALLOWED,
    ),
    dict(
        key="histdata",
        name="HistData.com",
        kind=SourceKind.PRICE,
        base_url="https://www.histdata.com/",
        gives="M1 OHLC, bid only",
        depth_note="2000+ — ~1,600 monthly zips for 7 pairs × 19 years",
        role="Bulk M1 backbone. Import-only: the site declines automated "
        "downloads, so upload the zips or point import_dir at a folder (§14 Q4).",
        timezone_rule="Eastern Standard Time with NO DST adjustment. Fixed UTC-5 "
        "year-round. Do NOT use America/New_York — it would shift half the "
        "history by an hour (§4.1).",
        robots_status=RobotsStatus.RESTRICTED,
        terms_note="M1 bars are bid-only; ask appears in tick data only, so "
        "HistData alone cannot give spread (§4.7). Verified: the download form "
        "posts an empty token and returns HTTP 200 with zero bytes to anything "
        "that is not a browser — hence the import path.",
        fetch_policy_json={"transport": "import", "import_glob": "*.zip"},
    ),
    dict(
        key="dukascopy",
        name="Dukascopy",
        kind=SourceKind.PRICE,
        base_url="https://datafeed.dukascopy.com/",
        gives="tick bid/ask + volumes",
        depth_note="~2003+",
        role="Event-window ticks, spread, cross-validation. Bulk tick is off the "
        "table — 19y × 7 pairs is roughly 800 GB (§4.5).",
        timezone_rule="UTC / GMT — verify against a known release before trusting.",
        robots_status=RobotsStatus.RESTRICTED,
    ),
    dict(
        key="mt5_prices",
        name="MetaTrader 5 terminal (prices)",
        kind=SourceKind.PRICE,
        base_url="",
        gives="M1 via the Python API",
        depth_note="broker-dependent",
        role="Third opinion; not for depth.",
        timezone_rule="Trade server time.",
        robots_status=RobotsStatus.NOT_APPLICABLE,
    ),
]

#: §4.6.  Instants are best-known, not verified — every one carries a note
#: saying so, because a confidently wrong timestamp is worse than a flagged one.
MARKET_EVENTS = [
    dict(
        ts_utc=datetime(2008, 9, 15, 0, 0, tzinfo=timezone.utc),
        end_ts_utc=datetime(2009, 6, 30, 0, 0, tzinfo=timezone.utc),
        label="Global financial crisis",
        kind=MarketEventKind.CRISIS,
        regime=Regime.CRISIS,
        note="Volatility regime unlike anything since. Period boundaries are a "
        "convention, not a measurement.",
    ),
    dict(
        ts_utc=datetime(2015, 1, 15, 9, 30, tzinfo=timezone.utc),
        label="SNB removes the EUR/CHF floor",
        kind=MarketEventKind.POLICY_SHOCK,
        regime=Regime.NORMALISATION,
        note="Not an economic release, but it lands inside event windows and will "
        "dominate any USDCHF variance calculation it is included in. "
        "Instant approximate — verify against the price series (§4.1 vol-check).",
        instruments=["USDCHF"],
    ),
    dict(
        ts_utc=datetime(2016, 10, 6, 23, 7, tzinfo=timezone.utc),
        label="GBP flash crash",
        kind=MarketEventKind.DISLOCATION,
        regime=Regime.NORMALISATION,
        note="Provider-dependent: different feeds show different extremes because "
        "there was no consolidated price. A concrete reason to cross-validate "
        "HistData against Dukascopy (§4.6). Instant approximate.",
        instruments=["GBPUSD"],
    ),
    dict(
        ts_utc=datetime(2019, 1, 2, 22, 30, tzinfo=timezone.utc),
        label="JPY flash crash",
        kind=MarketEventKind.DISLOCATION,
        regime=Regime.NORMALISATION,
        note="Provider-dependent, as above. Instant approximate — verify. The crash "
        "ran through AUDJPY, so AUDUSD took it as well as USDJPY.",
        instruments=["USDJPY", "AUDUSD"],
    ),
    dict(
        ts_utc=datetime(2011, 9, 6, 8, 0, tzinfo=timezone.utc),
        label="SNB sets the EUR/CHF floor at 1.20",
        kind=MarketEventKind.POLICY_SHOCK,
        regime=Regime.ZIRP_QE,
        note="Announced 10:00 Zurich. Found by the outlier flag, not looked up: it "
        "lands inside the +1h window of that morning's Swiss CPI (07:15 UTC), which "
        "read as a 34-sigma CPI reaction until the two were told apart.",
        instruments=["USDCHF"],
    ),
    dict(
        ts_utc=datetime(2016, 6, 23, 23, 0, tzinfo=timezone.utc),
        label="Brexit referendum result",
        kind=MarketEventKind.POLICY_SHOCK,
        regime=Regime.NORMALISATION,
        note="Counts ran overnight; the first decisive results came around "
        "23:00–01:00 UTC. Instant approximate. Any GBPUSD window spanning that "
        "night measures the vote, not the release it is anchored to.",
        instruments=["GBPUSD"],
    ),
    dict(
        ts_utc=datetime(2020, 3, 1, 0, 0, tzinfo=timezone.utc),
        end_ts_utc=datetime(2021, 12, 31, 0, 0, tzinfo=timezone.utc),
        label="COVID",
        kind=MarketEventKind.CRISIS,
        regime=Regime.COVID,
        note="Period, not an instant.",
    ),
    dict(
        ts_utc=datetime(2022, 1, 1, 0, 0, tzinfo=timezone.utc),
        end_ts_utc=datetime(2023, 12, 31, 0, 0, tzinfo=timezone.utc),
        label="Inflation shock and hiking cycle",
        kind=MarketEventKind.CRISIS,
        regime=Regime.INFLATION,
        note="Period, not an instant.",
    ),
]

RELEASE_GROUPS = [
    dict(
        key="us_employment_situation",
        name="US Employment Situation",
        country="United States",
        currency="USD",
        description="Nonfarm payrolls + unemployment rate + average hourly earnings, "
        "published at one instant (08:30 New York). §3.3 Case B: three numbers, one "
        "timestamp, perfectly collinear regressors. The move is attributed to the "
        "report, never to the unemployment rate alone.",
    ),
]


def seed_reference(log=lambda msg: None) -> dict:
    created = updated = 0
    for spec in SOURCES:
        _, was_created = Source.objects.update_or_create(
            key=spec["key"], defaults={k: v for k, v in spec.items() if k != "key"}
        )
        created += was_created
        updated += not was_created
        log(f"{'created' if was_created else 'updated'} source {spec['key']}")

    for symbol, base, quote, pip in MAJORS:
        Instrument.objects.update_or_create(
            symbol=symbol,
            defaults={"base_ccy": base, "quote_ccy": quote, "pip_size": Decimal(pip)},
        )

    for spec in RELEASE_GROUPS:
        ReleaseGroup.objects.update_or_create(
            key=spec["key"], defaults={k: v for k, v in spec.items() if k != "key"}
        )

    for spec in MARKET_EVENTS:
        event, _ = MarketEvent.objects.update_or_create(
            label=spec["label"],
            ts_utc=spec["ts_utc"],
            defaults={
                k: v for k, v in spec.items() if k not in ("label", "ts_utc", "instruments")
            },
        )
        # No instruments means every pair: a crisis period is market-wide.
        event.instruments.set(
            Instrument.objects.filter(symbol__in=spec.get("instruments", []))
        )

    summary = (
        f"{len(SOURCES)} sources ({created} created, {updated} updated), "
        f"{len(MAJORS)} instruments, {len(RELEASE_GROUPS)} release groups, "
        f"{len(MARKET_EVENTS)} market events"
    )
    log(summary)
    return {
        "sources": len(SOURCES),
        "sources_created": created,
        "instruments": len(MAJORS),
        "release_groups": len(RELEASE_GROUPS),
        "market_events": len(MARKET_EVENTS),
        "summary": summary,
    }
