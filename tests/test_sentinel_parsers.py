from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.dataflows.edgar import parse_current_feed, parse_items
from src.sentinel.models import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    DIRECTION_UNCLEAR,
    PRIORITY_BACKGROUND,
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    classify_edgar_items,
    classify_halt,
)
from src.sentinel.sources.halts import parse_halt_feed

ATOM_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>8-K - ACME BIOSCIENCES INC (0001234567) (Filer)</title>
    <link rel="alternate" type="text/html"
          href="/Archives/edgar/data/1234567/000123456726000123/0001234567-26-000123-index.htm"/>
    <updated>2026-07-27T14:31:00-04:00</updated>
  </entry>
  <entry>
    <title>S-3 - NORTHWIND ENERGY CORP (0007654321) (Filer)</title>
    <link rel="alternate" type="text/html"
          href="/Archives/edgar/data/7654321/000765432126000045/0007654321-26-000045-index.htm"/>
    <updated>2026-07-27T14:35:00-04:00</updated>
  </entry>
</feed>
"""

MALFORMED_ENTRY_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>8-K - NO LINK CORP (0009999999) (Filer)</title>
    <updated>2026-07-27T14:31:00-04:00</updated>
  </entry>
  <entry>
    <title>8-K - GOOD CORP (0001111111) (Filer)</title>
    <link rel="alternate" type="text/html"
          href="/Archives/edgar/data/1111111/000111111126000001/0001111111-26-000001-index.htm"/>
    <updated>2026-07-27T15:00:00-04:00</updated>
  </entry>
</feed>
"""

INDEX_HTML = """
<html><body>
<div class="infoHead">Items</div>
<div class="info">Item 2.02 Results of Operations and Financial Condition</div>
<div class="info">Item 9.01 Financial Statements and Exhibits</div>
<div class="info">Item 7.01 Regulation FD Disclosure</div>
</body></html>
"""

HALT_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/">
  <channel>
    <item>
      <ndaq:IssueSymbol>ACME</ndaq:IssueSymbol>
      <ndaq:ReasonCode>T1</ndaq:ReasonCode>
      <ndaq:HaltDate>07/27/2026</ndaq:HaltDate>
      <ndaq:HaltTime>14:32:11</ndaq:HaltTime>
      <ndaq:ResumptionDate></ndaq:ResumptionDate>
      <ndaq:ResumptionTradeTime></ndaq:ResumptionTradeTime>
    </item>
    <item>
      <ndaq:IssueSymbol>NWND</ndaq:IssueSymbol>
      <ndaq:ReasonCode>LUDP</ndaq:ReasonCode>
      <ndaq:HaltDate>07/27/2026</ndaq:HaltDate>
      <ndaq:HaltTime>10:05:00</ndaq:HaltTime>
      <ndaq:ResumptionDate>07/27/2026</ndaq:ResumptionDate>
      <ndaq:ResumptionTradeTime>10:10:00</ndaq:ResumptionTradeTime>
    </item>
  </channel>
</rss>
"""


def test_parse_current_feed_extracts_filings():
    filings = parse_current_feed(ATOM_FEED)
    assert len(filings) == 2

    first = filings[0]
    assert first.form_type == "8-K"
    assert first.company == "ACME BIOSCIENCES INC"
    assert first.cik == "1234567"
    assert first.accession == "0001234567-26-000123"
    assert first.index_url.startswith("https://www.sec.gov/Archives")
    assert first.dedup_key == "edgar:0001234567-26-000123"


def test_parse_current_feed_parses_timestamp():
    filing = parse_current_feed(ATOM_FEED)[0]
    assert filing.filed_at is not None
    assert filing.filed_at.year == 2026
    assert filing.filed_at.tzinfo is None


def test_parse_current_feed_skips_entries_without_link():
    filings = parse_current_feed(MALFORMED_ENTRY_FEED)
    assert len(filings) == 1
    assert filings[0].company == "GOOD CORP"


def test_parse_current_feed_survives_broken_xml():
    assert parse_current_feed("<feed><entry>") == []


def test_parse_items_extracts_and_sorts():
    assert parse_items(INDEX_HTML) == ["2.02", "7.01", "9.01"]


def test_parse_items_deduplicates_repeats():
    html = "Item 2.02 ... later Item 2.02 again"
    assert parse_items(html) == ["2.02"]


def test_parse_items_returns_empty_when_absent():
    assert parse_items("<html>nothing here</html>") == []


def test_classify_items_picks_most_severe_priority():
    priority, direction, ranked = classify_edgar_items(["8.01", "4.02"])
    assert priority == PRIORITY_CRITICAL
    assert direction == DIRECTION_SHORT
    assert ranked[0] == "4.02"


def test_classify_items_earnings_is_directionally_unclear():
    priority, direction, _ = classify_edgar_items(["2.02"])
    assert priority == PRIORITY_HIGH
    assert direction == DIRECTION_UNCLEAR


def test_classify_items_conflicting_directions_resolve_to_unclear():
    _, direction, _ = classify_edgar_items(["1.01", "4.01"])
    assert direction == DIRECTION_UNCLEAR


def test_classify_items_agreeing_directions_are_kept():
    _, direction, _ = classify_edgar_items(["1.03", "4.01"])
    assert direction == DIRECTION_SHORT


def test_classify_items_ignores_unknown_items():
    priority, direction, ranked = classify_edgar_items(["9.01"])
    assert priority == PRIORITY_BACKGROUND
    assert direction == DIRECTION_UNCLEAR
    assert ranked == []


def test_classify_items_material_agreement_is_long():
    _, direction, _ = classify_edgar_items(["1.01"])
    assert direction == DIRECTION_LONG


def test_parse_halt_feed_reads_records():
    records = parse_halt_feed(HALT_FEED)
    assert len(records) == 2

    halted, resumed = records
    assert halted.symbol == "ACME"
    assert halted.reason_code == "T1"
    assert halted.resumed is False
    assert resumed.resumed is True


def test_halt_dedup_key_is_unique_per_halt():
    records = parse_halt_feed(HALT_FEED)
    assert records[0].dedup_key != records[1].dedup_key
    assert "ACME" in records[0].dedup_key


def test_parse_halt_feed_survives_broken_xml():
    assert parse_halt_feed("<rss><channel>") == []


@pytest.mark.parametrize(
    "code,expected_priority",
    [("T1", PRIORITY_CRITICAL), ("T12", PRIORITY_CRITICAL), ("LUDP", PRIORITY_HIGH)],
)
def test_classify_halt_known_codes(code, expected_priority):
    result = classify_halt(code)
    assert result is not None
    assert result[0] == expected_priority


def test_classify_halt_unknown_code_returns_none():
    assert classify_halt("T3") is None


def test_classify_halt_is_case_insensitive():
    assert classify_halt("t1") is not None
