import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, update

from nexus.db import Evidence, Inspection, Job, Note, ResearchRound, Source
from nexus.schemas import Start
from nexus.service import Problem, canonical_url


def start(service, question="What does the primary study establish?", rounds=3, refresh=False):
    inspection = service.inspect(question)
    return service.start(Start(inspection_id=inspection["inspection_id"], focus="Find primary evidence and gaps",
                               max_rounds=rounds, refresh=refresh))["job"]


def step(service):
    claim = service.claim()
    assert claim["available"]
    return service.step(claim["job_id"], claim["lease_token"], claim["stage"])


def test_end_to_end_compounds_and_stops_when_nothing_new(rig):
    s, p, _, _ = rig
    job = start(s)
    assert job["status"] == "queued"
    assert step(s)["stage"] == "librarian"
    first = step(s)
    assert first["iteration"] == 1 and first["stage"] == "research"
    step(s)
    assert len(p.contexts[1]) == 1
    final = step(s)
    assert final["status"] == "completed" and final["stop_reason"] == "no_new_evidence"
    with s.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Note)) == 1
        assert db.scalar(select(func.count()).select_from(Source)) == 1
        assert db.scalar(select(func.count()).select_from(Evidence)) == 2  # separate observation history
    answer = s.answer("What did the study measure?")
    assert answer["database_only"]
    assert answer["sources"][0]["sources"][0]["url"] == "https://example.org/study"
    assert p.research_calls == 2  # querying the library does not trigger web research
    inspection = s.inspect("What does the primary study establish?")
    assert inspection["recommendation"] == "review_existing"
    assert inspection["notes"]


def test_exact_duplicate_and_idempotent_submission(rig):
    s, _, _, _ = rig
    inspection = s.inspect("What does the study establish?")
    request = Start(inspection_id=inspection["inspection_id"], focus="Find the missing evidence")
    first = s.start(request)
    assert s.start(request)["job"]["id"] == first["job"]["id"]
    assert start(s, "  WHAT does the study establish?  ")["id"] == first["job"]["id"]


def test_completed_question_requires_explicit_refresh(rig):
    s, p, _, _ = rig
    p.sufficient = True
    original = start(s)
    step(s)
    step(s)
    assert start(s)["id"] == original["id"]
    assert start(s, refresh=True)["id"] != original["id"]


def test_round_budget_stops_even_with_gaps(rig):
    s, _, _, _ = rig
    start(s, rounds=1)
    step(s)
    result = step(s)
    assert result["stop_reason"] == "round_limit"
    assert result["rounds"][0]["library_result"]["gaps"]


def test_duplicate_claim_and_duplicate_stage_cannot_execute_twice(rig):
    s, p, _, _ = rig
    start(s)
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda _: s.claim(), range(2)))
    assert sum(c["available"] for c in claims) == 1
    claim = next(c for c in claims if c["available"])
    s.step(claim["job_id"], claim["lease_token"], claim["stage"])
    with pytest.raises(Problem, match="Stale lease"):
        s.step(claim["job_id"], claim["lease_token"], claim["stage"])
    assert p.research_calls == 1


def test_racing_submissions_share_one_job(rig):
    s, _, _, _ = rig
    ids = [s.inspect("Does this identical question have evidence?")["inspection_id"] for _ in range(2)]
    with ThreadPoolExecutor(2) as pool:
        jobs = list(pool.map(lambda i: s.start(Start(inspection_id=i, focus="Find the missing evidence")), ids))
    assert jobs[0]["job"]["id"] == jobs[1]["job"]["id"]


def test_cancellation_during_provider_call_rejects_late_write(rig):
    s, p, _, _ = rig
    job = start(s)
    p.on_research = lambda: s.cancel(job["id"])
    with pytest.raises(Problem, match="stopped job"):
        step(s)
    assert s.get_job(job["id"])["status"] == "cancelled"
    with s.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ResearchRound)) == 0


def test_librarian_retry_uses_saved_report(rig):
    s, p, _, _ = rig
    job = start(s)
    step(s)
    p.fail_library = True
    with pytest.raises(Problem):
        step(s)
    assert s.get_job(job["id"])["status"] == "failed"
    p.fail_library = False
    s.retry(job["id"])
    step(s)
    assert p.research_calls == 1


def test_provider_failure_is_sanitized_and_not_automatically_replayed(rig):
    s, p, _, _ = rig
    job = start(s)
    p.fail_research = True
    with pytest.raises(Problem) as error:
        step(s)
    assert "secret-should-not-leak" not in str(error.value)
    assert "secret-should-not-leak" not in s.get_job(job["id"])["stop_reason"]
    assert s.claim() == {"available": False}


def test_expired_lease_is_failed_and_fenced(rig):
    s, _, _, _ = rig
    job = start(s)
    claim = s.claim()
    with s.sessions.begin() as db:
        db.execute(update(Job).where(Job.id == job["id"]).values(lease_expires=time.time()-1))
    assert not s.claim()["available"]
    assert s.get_job(job["id"])["status"] == "failed"
    with pytest.raises(Problem):
        s.step(job["id"], claim["lease_token"], "research")


def test_uncited_librarian_claim_rolls_back(rig):
    s, p, _, _ = rig
    start(s)
    step(s)
    p.bad_citation = True
    with pytest.raises(Problem):
        step(s)
    with s.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Note)) == 0


def test_empty_database_abstains_without_model_answer(rig):
    s, p, _, _ = rig
    result = s.answer("What is in the database?")
    assert not result["parts"] and result["gaps"]
    assert p.answer_calls == p.research_calls == 0


def test_unknown_answer_citation_is_not_returned(rig):
    s, p, _, _ = rig
    start(s, rounds=1)
    step(s)
    step(s)
    p.bad_answer = True
    with pytest.raises(Problem, match="unknown note"):
        s.answer("What did the primary study measure?")


def test_expired_inspection_cannot_start_research(rig):
    s, _, _, _ = rig
    result = s.inspect("What do we know about the study?")
    with s.sessions.begin() as db:
        db.execute(update(Inspection).where(Inspection.id == result["inspection_id"]).values(created_at=time.time()-1900))
    with pytest.raises(Problem, match="expired"):
        s.start(Start(inspection_id=result["inspection_id"], focus="Gather missing evidence"))


def test_canonical_urls_preserve_meaningful_parameters():
    assert canonical_url("https://EXAMPLE.org:443/p?a=1&utm_source=x#section") == "https://example.org/p?a=1"
    assert canonical_url("https://example.org/p?a=2") != canonical_url("https://example.org/p?a=1")
    with pytest.raises(ValueError):
        canonical_url("javascript:alert(1)")


def test_worker_token_cannot_read_library_and_owner_cannot_run_agents(rig):
    _, _, client, settings = rig
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs", headers={"Authorization": "Bearer "+settings.worker_token}).status_code == 401
    assert client.post("/internal/claim", headers={"Authorization": "Bearer "+settings.api_token}).status_code == 401
    assert client.get("/api/jobs", headers={"Authorization": "Bearer "+settings.api_token}).status_code == 200


def test_api_validation_rejects_unbounded_research(rig):
    s, _, client, settings = rig
    inspection = s.inspect("What do we know about the study?")
    response = client.post("/api/jobs", headers={"Authorization": "Bearer "+settings.api_token},
        json={"inspection_id": inspection["inspection_id"], "focus": "Find evidence", "max_rounds": 100})
    assert response.status_code == 422


def test_conflicting_findings_preserve_both_notes_and_links(rig):
    from nexus.schemas import LibraryNote, LibraryResult
    s, p, _, _ = rig
    start(s, rounds=1)
    step(s)
    step(s)
    with s.sessions() as db:
        old = db.scalar(select(Note))
        old_id, old_text = old.id, old.text
    p.librarian = lambda *args: LibraryResult(summary="A different result needs reconciliation.",
        sufficient=False, gaps=["Check population differences"], notes=[LibraryNote(
            text="A follow-up study in a different population measured 21 units, conflicting with the earlier result.",
            kind="contradiction", confidence="low", source_urls=["https://example.org/study"],
            existing_note_id=None, contradicts=[old_id])])
    start(s, rounds=1, refresh=True)
    step(s)
    step(s)
    context = s.inspect("What did the studies measure?")["notes"]
    assert len(context) == 2
    assert next(n for n in context if n["id"] == old_id)["text"] == old_text
    assert all(n["contradicts"] for n in context)
