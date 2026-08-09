import pytest

from utils.political_archive import select_archive_candidates
from utils.political_occurrence_ledger import attach_authoritative_occurrences


def test_closed_politics_candidates_preserve_occurrence_blocker_and_choose_one_binary_market():
    events = [{
        "id": "42", "title": "Will the Senate vote pass?", "endDate": "2026-08-01T00:00:00Z",
        "markets": [
            {"id": "thin", "closed": True, "outcomes": '["Yes", "No"]', "clobTokenIds": '["yes-a", "no-a"]', "volumeNum": 1},
            {"id": "deep", "closed": True, "outcomes": '["Yes", "No"]', "clobTokenIds": '["yes-b", "no-b"]', "volumeNum": 2, "closedTime": "2026-08-02T00:00:00Z"},
        ],
    }]

    candidates = select_archive_candidates(events, max_events=20)

    assert len(candidates) == 1
    assert candidates[0]["market_id"] == "deep"
    assert candidates[0]["family"] == "vote"
    assert candidates[0]["occurrence_at"] is None
    assert candidates[0]["occurrence_status"] == "requires_authoritative_event_source"
    assert candidates[0]["archive_provenance"]["event_end_date"] == "2026-08-01T00:00:00Z"


def test_candidates_reject_nonbinary_and_unrelated_archive_events():
    events = [
        {"id": "bad", "title": "Will Bitcoin close above $100,000?", "markets": []},
        {"id": "multi", "title": "Election vote", "markets": [{"closed": True, "outcomes": '["A", "B"]', "clobTokenIds": '["a", "b"]'}]},
    ]

    assert select_archive_candidates(events, max_events=5) == []


def test_external_occurrence_ledger_requires_exact_candidate_and_preserves_unmatched_blocker():
    candidates = [{
        "event_id": "polymarket-event-speech", "occurrence_at": None,
        "occurrence_status": "requires_authoritative_event_source",
        "archive_provenance": {"event_end_date": "2026-08-01T00:00:00Z"},
    }, {
        "event_id": "polymarket-event-vote", "occurrence_at": None,
        "occurrence_status": "requires_authoritative_event_source",
    }]

    enriched = attach_authoritative_occurrences(candidates, [{
        "event_id": "polymarket-event-speech",
        "occurrence_at": "2026-07-31T18:00:00Z",
        "source_name": "official schedule",
        "source_url": "https://example.gov/schedule",
    }])

    assert enriched[0]["occurrence_at"] == "2026-07-31T18:00:00Z"
    assert enriched[0]["occurrence_status"] == "verified_external_authority"
    assert enriched[0]["occurrence_provenance"]["evidence_type"] == "exact_event_id_ledger"
    assert enriched[1]["occurrence_at"] is None
    assert enriched[1]["occurrence_status"] == "requires_authoritative_event_source"
    assert candidates[0]["occurrence_at"] is None


def test_external_occurrence_ledger_rejects_duplicate_or_non_https_evidence():
    candidate = {"event_id": "polymarket-event-speech"}
    duplicate = [{
        "event_id": "polymarket-event-speech", "occurrence_at": "2026-07-31T18:00:00Z",
        "source_name": "official", "source_url": "https://example.gov/a",
    }, {
        "event_id": "polymarket-event-speech", "occurrence_at": "2026-07-31T19:00:00Z",
        "source_name": "official", "source_url": "https://example.gov/b",
    }]
    with pytest.raises(ValueError, match="duplicate"):
        attach_authoritative_occurrences([candidate], duplicate)
    with pytest.raises(ValueError, match="HTTPS"):
        attach_authoritative_occurrences([candidate], [{
            "event_id": "polymarket-event-speech", "occurrence_at": "2026-07-31T18:00:00Z",
            "source_name": "official", "source_url": "http://example.gov/a",
        }])
