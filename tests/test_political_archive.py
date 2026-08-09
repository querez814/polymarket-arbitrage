from utils.political_archive import select_archive_candidates


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
