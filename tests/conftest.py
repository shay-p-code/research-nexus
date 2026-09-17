import pytest
from fastapi.testclient import TestClient

from nexus.app import create_app
from nexus.config import Settings
from nexus.schemas import AnswerPart, AnswerResult, LibraryNote, LibraryResult


class FakeProvider:
    def __init__(self):
        self.research_calls = 0
        self.answer_calls = 0
        self.contexts = []
        self.fail_research = False
        self.fail_library = False
        self.bad_citation = False
        self.bad_answer = False
        self.sufficient = False
        self.on_research = None

    def embed(self, texts):
        return [[0., 1.] + [0.]*1534 if "unrelated" in t else [1., 0.] + [0.]*1534 for t in texts]

    def research(self, question, focus, context):
        self.research_calls += 1
        self.contexts.append(context)
        if self.on_research:
            self.on_research()
        if self.fail_research:
            raise RuntimeError("provider failed with secret-should-not-leak")
        return {"report": "The primary study measured 42 units in the tested population.",
                "citations": [{"url": "https://example.org/study?utm_source=research", "title": "Primary study"}],
                "provider_response_id": "response-1", "usage": {"total_tokens": 123}}

    def librarian(self, question, report, citations, context):
        if self.fail_library:
            raise RuntimeError("library failure")
        return LibraryResult(summary="The study measured 42 units.", sufficient=self.sufficient,
            gaps=["Find independent replication"], notes=[LibraryNote(
                text="In the tested population, the primary study measured 42 units.", kind="finding",
                confidence="medium", existing_note_id=context[0]["id"] if context else None,
                contradicts=[], source_urls=["https://invented.example/" if self.bad_citation else citations[0]["url"]])])

    def answer(self, question, context):
        self.answer_calls += 1
        return AnswerResult(parts=[AnswerPart(text="The saved study measured 42 units.",
                           note_ids=["made-up" if self.bad_answer else context[0]["id"]])], gaps=[])


@pytest.fixture
def rig(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/test.db", api_token="a"*40,
                        worker_token="b"*40, owner_password="c"*40, oauth_client_secret="d"*40)
    provider = FakeProvider()
    app = create_app(settings, provider)
    with TestClient(app, base_url="http://localhost:8000") as client:
        yield app.state.service, provider, client, settings
