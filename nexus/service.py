import hashlib
import math
import re
import secrets
import time
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from nexus.db import AuthRecord, Contradiction, Evidence, Inspection, Job, Note, ResearchRound, Source


class Problem(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def fingerprint(value):
    return digest(" ".join(unicodedata.normalize("NFKC", value).casefold().split()).rstrip(".?!"))


def canonical_url(value):
    u = urlsplit(value)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise ValueError("Invalid citation URL")
    host = u.hostname.lower().encode("idna").decode()
    if ":" in host:
        host = f"[{host}]"
    if u.port and (u.scheme, u.port) not in (("https", 443), ("http", 80)):
        host += f":{u.port}"
    query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    # Keep meaningful query order, path case, and trailing slashes.
    return urlunsplit((u.scheme, host, u.path or "/", urlencode(query), ""))


def similarity(a, b):
    denom = math.sqrt(sum(x*x for x in a) * sum(y*y for y in b))
    return sum(x*y for x, y in zip(a, b)) / denom if denom else 0


def job_view(job):
    return {k: getattr(job, k) for k in ("id", "question", "focus", "status", "stage", "iteration",
                                       "max_rounds", "stop_reason", "created_at", "updated_at")}


class Service:
    def __init__(self, sessions, provider, settings):
        self.sessions, self.provider, self.settings = sessions, provider, settings

    def related(self, session, model, question, embedding, limit=20):
        field = model.text if model is Note else model.question
        if session.bind.dialect.name == "postgresql":
            distance = model.embedding.cosine_distance(embedding)
            semantic = list(session.scalars(select(model).where(distance <= .45).order_by(distance).limit(limit)))
            lexical = list(session.scalars(select(model).where(
                func.to_tsvector("english", field).op("@@")(func.plainto_tsquery("english", question))
            ).limit(limit)))
        else:
            # SQLite is for local development; production uses indexed pgvector/full text.
            all_rows = list(session.scalars(select(model)))
            semantic = sorted((r for r in all_rows if similarity(r.embedding, embedding) >= .55),
                              key=lambda r: similarity(r.embedding, embedding), reverse=True)[:limit]
            words = set(re.findall(r"\w{3,}", question.lower())) - {"the", "and", "what", "are", "how", "for"}
            lexical = [r for r in all_rows if words.intersection(re.findall(r"\w{3,}", getattr(r, field.key).lower()))][:limit]
        return list({r.id: r for r in semantic + lexical}.values())[:limit]

    def notes_view(self, session, notes):
        result = []
        for n in notes:
            sources = session.execute(select(Source, ResearchRound.created_at, ResearchRound.job_id)
                .join(Evidence, Evidence.source_id == Source.id)
                .join(ResearchRound, Evidence.round_id == ResearchRound.id)
                .where(Evidence.note_id == n.id)).all()
            links = session.scalars(select(Contradiction).where(or_(
                Contradiction.note_id == n.id, Contradiction.other_id == n.id))).all()
            result.append({"id": n.id, "text": n.text, "kind": n.kind, "confidence": n.confidence,
                           "created_at": n.created_at,
                           "sources": [{"url": src.url, "title": src.title, "observed_at": at, "job_id": jid}
                                       for src, at, jid in sources],
                           "contradicts": [c.other_id if c.note_id == n.id else c.note_id for c in links]})
        return result

    def context(self, session, question, embedding, job_id=None):
        notes = self.related(session, Note, question, embedding)
        if job_id:
            own = session.scalars(select(Note).join(Evidence).join(ResearchRound)
                .where(ResearchRound.job_id == job_id)).all()
            notes = list({n.id: n for n in notes + list(own)}.values())
        # Include linked contradictory claims even if semantic retrieval missed them.
        linked = session.scalars(select(Contradiction).where(or_(
            Contradiction.note_id.in_([n.id for n in notes]),
            Contradiction.other_id.in_([n.id for n in notes])))).all()
        extra_ids = {c.note_id for c in linked} | {c.other_id for c in linked}
        if extra_ids:
            notes = list({n.id: n for n in notes + list(session.scalars(select(Note).where(Note.id.in_(extra_ids))))}.values())
        return self.notes_view(session, notes)

    def inspect(self, question):
        embedding = self.provider.embed([question])[0]
        with self.sessions.begin() as s:
            context = self.context(s, question, embedding)
            jobs = self.related(s, Job, question, embedding)
            exact = s.scalars(select(Job).where(Job.fingerprint == fingerprint(question))
                              .order_by(Job.created_at.desc())).all()
            jobs = list({j.id: j for j in list(exact) + jobs}.values())[:20]
            inspection = Inspection(question=question, fingerprint=fingerprint(question), embedding=embedding)
            s.add(inspection)
            s.flush()
            active = next((j for j in exact if j.active_key), None)
            completed = next((j for j in exact if j.status == "completed"), None)
            recommendation = "wait" if active else "review_existing" if context or completed else "research"
            return {"inspection_id": inspection.id, "question": question,
                    "recommendation": recommendation, "notes": context,
                    "related_jobs": [job_view(j) for j in jobs],
                    "instructions": "Assess coverage, age, contradictions and remaining gaps. Matching research does not prove completeness. Start only if new work is warranted."}

    def start(self, request):
        try:
            with self.sessions.begin() as s:
                inspection = s.get(Inspection, request.inspection_id)
                if not inspection:
                    raise Problem(404, "Inspection not found; inspect the question first")
                existing = s.scalar(select(Job).where(or_(Job.inspection_id == inspection.id,
                                                         Job.active_key == inspection.fingerprint)))
                if existing:
                    return {"reused": True, "job": job_view(existing)}
                if time.time() - inspection.created_at > 1800:
                    raise Problem(409, "Inspection expired; inspect the question again")
                completed = s.scalar(select(Job).where(Job.fingerprint == inspection.fingerprint,
                    Job.status == "completed").order_by(Job.created_at.desc()).limit(1))
                if completed and not request.refresh:
                    return {"reused": True, "job": job_view(completed),
                            "message": "Already researched. Review its result; use refresh only for an intentional update."}
                job = Job(inspection_id=inspection.id, question=inspection.question,
                          fingerprint=inspection.fingerprint, active_key=inspection.fingerprint,
                          embedding=inspection.embedding, focus=request.focus, max_rounds=request.max_rounds)
                s.add(job)
                s.flush()
                return {"reused": False, "job": job_view(job)}
        except IntegrityError:
            # The unique active fingerprint is the final guard against racing starts.
            with self.sessions() as s:
                inspection = s.get(Inspection, request.inspection_id)
                job = s.scalar(select(Job).where(or_(Job.active_key == inspection.fingerprint,
                                                     Job.inspection_id == inspection.id)))
                if job:
                    return {"reused": True, "job": job_view(job)}
            raise

    def get_job(self, job_id):
        with self.sessions() as s:
            job = s.get(Job, job_id)
            if not job:
                raise Problem(404, "Job not found")
            rounds = s.scalars(select(ResearchRound).where(ResearchRound.job_id == job_id)
                              .order_by(ResearchRound.iteration)).all()
            return {**job_view(job), "rounds": [
                {"id": r.id, "iteration": r.iteration, "report": r.report, "citations": r.citations,
                 "usage": r.usage, "library_result": r.library_result, "created_at": r.created_at}
                for r in rounds]}

    def list_jobs(self, limit=20, offset=0):
        with self.sessions() as s:
            return [job_view(j) for j in s.scalars(select(Job).order_by(Job.created_at.desc()).limit(limit).offset(offset))]

    def cancel(self, job_id):
        with self.sessions.begin() as s:
            job = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if not job:
                raise Problem(404, "Job not found")
            if job.status in ("queued", "running"):
                job.status, job.stop_reason = "cancelled", "Cancelled by user"
                job.active_key = job.lease_hash = job.lease_expires = None
                job.updated_at = time.time()
            return job_view(job)

    def retry(self, job_id):
        try:
            with self.sessions.begin() as s:
                job = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
                if not job:
                    raise Problem(404, "Job not found")
                if job.status != "failed":
                    raise Problem(409, "Only failed jobs can be retried")
                job.active_key, job.status, job.stop_reason = job.fingerprint, "queued", None
                job.lease_hash = job.lease_expires = None
                job.executing = False
                job.updated_at = time.time()
                s.flush()
                return job_view(job)
        except IntegrityError:
            raise Problem(409, "Equivalent research is already active")

    def claim(self):
        now = time.time()
        with self.sessions.begin() as s:
            # Unknown outcomes are NOT silently replayed: that could repeat billable calls.
            s.execute(update(Job).where(Job.status == "running", Job.lease_expires < now).values(
                status="failed", stop_reason="Worker lease expired; inspect and retry explicitly",
                active_key=None, lease_hash=None, lease_expires=None, executing=False, updated_at=now))
            s.execute(delete(AuthRecord).where(AuthRecord.expires_at < now))
            s.execute(delete(Inspection).where(Inspection.created_at < now-86400,
                ~Inspection.id.in_(select(Job.inspection_id))))
            candidates = s.scalars(select(Job.id).where(Job.status == "queued").order_by(Job.updated_at).limit(20)).all()
            for job_id in candidates:
                token = secrets.token_urlsafe(32)
                claimed = s.execute(update(Job).where(Job.id == job_id, Job.status == "queued").values(
                    status="running", lease_hash=digest(token), lease_expires=now+self.settings.lease_seconds,
                    executing=False, updated_at=now))
                if claimed.rowcount:
                    job = s.get(Job, job_id)
                    return {"available": True, "job_id": job.id, "stage": job.stage, "lease_token": token}
        return {"available": False}

    def locked_job(self, s, job_id, token):
        job = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if (not job or job.status != "running" or not job.lease_hash
                or not secrets.compare_digest(job.lease_hash, digest(token)) or job.lease_expires < time.time()):
            raise Problem(409, "Stale lease or stopped job")
        return job

    def step(self, job_id, token, stage):
        with self.sessions.begin() as s:
            claimed = s.execute(update(Job).where(Job.id == job_id, Job.status == "running",
                Job.stage == stage, Job.lease_hash == digest(token), Job.lease_expires > time.time(),
                Job.executing.is_(False)).values(executing=True))
            if not claimed.rowcount:
                raise Problem(409, "Stale lease, wrong stage or step already executing")
            job = s.get(Job, job_id)
            context = self.context(s, job.question, job.embedding, job.id)
            round_ = s.scalar(select(ResearchRound).where(ResearchRound.job_id == job.id,
                                                         ResearchRound.iteration == job.iteration))
        try:
            if stage == "research":
                data = self.provider.research(job.question, job.focus, context)
                for citation in data["citations"]:
                    canonical_url(citation["url"])
                with self.sessions.begin() as s:
                    current = self.locked_job(s, job_id, token)
                    s.add(ResearchRound(job_id=job_id, iteration=job.iteration, **data))
                    current.stage = "librarian"
                    self.release(current)
            else:
                if not round_:
                    raise ValueError("Missing saved research round")
                result = self.provider.librarian(job.question, round_.report, round_.citations, context)
                allowed = {canonical_url(c["url"]): c for c in round_.citations}
                known_ids = {n["id"] for n in context}
                for note in result.notes:
                    if any(canonical_url(u) not in allowed for u in note.source_urls):
                        raise ValueError("Librarian cited a source not present in the research")
                    if note.existing_note_id and note.existing_note_id not in known_ids:
                        raise ValueError("Librarian invented an existing note")
                    if not set(note.contradicts) <= known_ids or note.existing_note_id in note.contradicts:
                        raise ValueError("Invalid contradiction link")
                embeddings = self.provider.embed([n.text for n in result.notes]) if result.notes else []
                with self.sessions.begin() as s:
                    # Serialize librarian writes across jobs to make shared note/source upserts atomic.
                    if s.bind.dialect.name == "postgresql":
                        s.execute(text("SELECT pg_advisory_xact_lock(74001)"))
                    else:
                        s.execute(text("BEGIN IMMEDIATE"))
                    current = self.locked_job(s, job_id, token)
                    novelty = self.catalog(s, round_.id, result.notes, embeddings, allowed)
                    saved = s.get(ResearchRound, round_.id)
                    saved.library_result = {**result.model_dump(), "new_evidence_count": novelty}
                    current.iteration += 1
                    reason = ("answered" if result.sufficient else "no_new_evidence" if not novelty
                              else "round_limit" if current.iteration >= current.max_rounds
                              else "no_further_gaps" if not result.gaps else None)
                    if reason:
                        current.status, current.stop_reason, current.active_key = "completed", reason, None
                    else:
                        current.focus = "\n".join(result.gaps)[:3000]
                        current.stage = "research"
                    self.release(current)
        except Problem:
            raise
        except Exception as exc:
            with self.sessions.begin() as s:
                s.execute(update(Job).where(Job.id == job_id, Job.status == "running",
                    Job.lease_hash == digest(token)).values(status="failed", active_key=None,
                    lease_hash=None, lease_expires=None, executing=False, updated_at=time.time(),
                    stop_reason=f"{stage} failed ({type(exc).__name__}); inspect configuration and retry explicitly"))
            raise Problem(502, f"{stage} failed ({type(exc).__name__}); the job was saved for review") from exc
        return self.get_job(job_id)

    @staticmethod
    def release(job):
        if job.status == "running":
            job.status = "queued"
        job.lease_hash = job.lease_expires = None
        job.executing = False
        job.updated_at = time.time()

    def catalog(self, s, round_id, entries, embeddings, allowed):
        novelty = 0
        for entry, embedding in zip(entries, embeddings, strict=True):
            note = s.get(Note, entry.existing_note_id) if entry.existing_note_id else s.scalar(
                select(Note).where(Note.fingerprint == fingerprint(entry.text)))
            if not note:
                note = Note(text=entry.text, fingerprint=fingerprint(entry.text), kind=entry.kind,
                            confidence=entry.confidence, embedding=embedding)
                s.add(note)
                s.flush()
            for url in dict.fromkeys(canonical_url(u) for u in entry.source_urls):
                source = s.scalar(select(Source).where(Source.url == url))
                if not source:
                    source = Source(url=url, title=allowed[url]["title"])
                    s.add(source)
                    s.flush()
                prior = s.scalar(select(Evidence).where(Evidence.note_id == note.id, Evidence.source_id == source.id))
                if not prior:
                    novelty += 1
                duplicate = s.scalar(select(Evidence).where(Evidence.note_id == note.id,
                    Evidence.source_id == source.id, Evidence.round_id == round_id))
                if not duplicate:
                    s.add(Evidence(note_id=note.id, source_id=source.id, round_id=round_id))
                    s.flush()
            for other_id in entry.contradicts:
                if other_id == note.id:
                    raise ValueError("A note cannot contradict itself")
                a, b = sorted((note.id, other_id))
                if not s.scalar(select(Contradiction).where(Contradiction.note_id == a, Contradiction.other_id == b)):
                    s.add(Contradiction(note_id=a, other_id=b))
                    s.flush()
        return novelty

    def answer(self, question):
        embedding = self.provider.embed([question])[0]
        with self.sessions() as s:
            context = self.context(s, question, embedding)
        if not context:
            return {"parts": [], "gaps": ["No relevant research is saved yet."], "sources": [], "database_only": True}
        result = self.provider.answer(question, context)
        known = {n["id"]: n for n in context}
        if any(i not in known for part in result.parts for i in part.note_ids):
            raise Problem(502, "Answer cited an unknown note; no answer released")
        cited = {i for part in result.parts for i in part.note_ids}
        return {**result.model_dump(), "sources": [known[i] for i in cited], "database_only": True}
