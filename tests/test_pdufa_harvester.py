from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.dataflows.pdufa_harvester import (
    extract_ticker,
    find_dates_near_keyword,
    parse_search_hit,
    pick_pdufa_date,
    strip_html,
)

SEARCH_HIT = {
    "_id": "0001579428-26-000112:ea0208765-8k_axsome.htm",
    "_source": {
        "display_names": ["Axsome Therapeutics, Inc.  (AXSM)  (CIK 0001579428)"],
        "ciks": ["0001579428"],
        "file_date": "2026-07-15",
    },
}

FILING_TEXT = """
<html><body>
<p>The Company announced that the U.S. Food and Drug Administration has accepted
for review the New Drug Application for AXS-05 and has assigned a
<b>PDUFA</b> target action date of&nbsp;March 15, 2027.</p>
</body></html>
"""

NUMERIC_TEXT = "The FDA set a PDUFA goal date of 03/15/2027 for this application."

NO_DATE_TEXT = "The Company discussed PDUFA procedures generally without giving a date."


def test_extract_ticker_from_display_name():
    assert extract_ticker(["Axsome Therapeutics, Inc.  (AXSM)  (CIK 0001579428)"]) == "AXSM"


def test_extract_ticker_returns_none_without_ticker():
    assert extract_ticker(["Some Private Filer  (CIK 0001234567)"]) is None


def test_extract_ticker_handles_dotted_symbols():
    assert extract_ticker(["Example Corp  (BRK.B)  (CIK 0000000001)"]) == "BRK.B"


def test_parse_search_hit_builds_record():
    hit = parse_search_hit(SEARCH_HIT)
    assert hit is not None
    assert hit.ticker == "AXSM"
    assert hit.company == "Axsome Therapeutics, Inc."
    assert hit.accession == "0001579428-26-000112"
    assert hit.document == "ea0208765-8k_axsome.htm"
    assert hit.filed_at == date(2026, 7, 15)


def test_document_url_strips_leading_zeros_from_cik():
    hit = parse_search_hit(SEARCH_HIT)
    assert hit.document_url == (
        "https://www.sec.gov/Archives/edgar/data/1579428/"
        "000157942826000112/ea0208765-8k_axsome.htm"
    )


def test_parse_search_hit_rejects_missing_id():
    assert parse_search_hit({"_id": "", "_source": SEARCH_HIT["_source"]}) is None


def test_parse_search_hit_rejects_bad_date():
    payload = {"_id": "a:b", "_source": dict(SEARCH_HIT["_source"], file_date="not-a-date")}
    assert parse_search_hit(payload) is None


def test_strip_html_removes_tags_and_entities():
    cleaned = strip_html("<p>PDUFA date of&nbsp;March 15, 2027.</p>")
    assert "<p>" not in cleaned
    assert "&nbsp;" not in cleaned
    assert "March 15, 2027" in cleaned


def test_find_dates_extracts_long_form_date():
    dates = find_dates_near_keyword(strip_html(FILING_TEXT), date(2026, 7, 15))
    assert date(2027, 3, 15) in dates


def test_find_dates_extracts_numeric_date():
    dates = find_dates_near_keyword(NUMERIC_TEXT, date(2026, 7, 15))
    assert date(2027, 3, 15) in dates


def test_find_dates_ignores_dates_before_filing():
    text = "PDUFA action date was January 5, 2020 previously."
    assert find_dates_near_keyword(text, date(2026, 7, 15)) == []


def test_find_dates_returns_empty_when_no_date_present():
    assert find_dates_near_keyword(NO_DATE_TEXT, date(2026, 7, 15)) == []


def test_find_dates_ignores_dates_far_from_keyword():
    filler = "x" * 2000
    text = f"PDUFA discussion here.{filler}March 15, 2027 unrelated mention."
    assert find_dates_near_keyword(text, date(2026, 7, 15)) == []


def test_find_dates_rejects_impossible_calendar_dates():
    assert find_dates_near_keyword("PDUFA date of February 30, 2027", date(2026, 7, 15)) == []


def test_pick_pdufa_date_takes_earliest_plausible():
    candidates = [date(2027, 3, 15), date(2027, 9, 1)]
    assert pick_pdufa_date(candidates, date(2026, 7, 15)) == date(2027, 3, 15)


def test_pick_pdufa_date_rejects_dates_beyond_horizon():
    assert pick_pdufa_date([date(2030, 1, 1)], date(2026, 7, 15)) is None


def test_pick_pdufa_date_rejects_the_filing_date_itself():
    filed = date(2026, 7, 15)
    assert pick_pdufa_date([filed], filed) is None


def test_pick_pdufa_date_rejects_dates_inside_announcement_gap():
    filed = date(2026, 7, 15)
    assert pick_pdufa_date([date(2026, 7, 18)], filed) is None


def test_pick_pdufa_date_accepts_date_just_past_the_gap():
    filed = date(2026, 7, 15)
    assert pick_pdufa_date([date(2026, 7, 25)], filed) == date(2026, 7, 25)


def test_pick_pdufa_date_skips_header_date_and_takes_the_real_one():
    filed = date(2026, 7, 15)
    candidates = [filed, date(2027, 3, 15)]
    assert pick_pdufa_date(candidates, filed) == date(2027, 3, 15)


def test_pick_pdufa_date_returns_none_without_candidates():
    assert pick_pdufa_date([], date(2026, 7, 15)) is None


def test_full_extraction_path_on_realistic_filing():
    hit = parse_search_hit(SEARCH_HIT)
    dates = find_dates_near_keyword(strip_html(FILING_TEXT), hit.filed_at)
    assert pick_pdufa_date(dates, hit.filed_at) == date(2027, 3, 15)
