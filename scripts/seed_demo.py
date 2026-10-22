"""Seed a fresh demo database with rules-only synthetic analyses."""
from dataclasses import replace
import hashlib
import json

from sdr.config import ROOT
from sdr.engine import Engine
from sdr.models import AnalyzeRequest
from sdr.store import Store, dumps

SEED_IDS = ("assignment", "verified", "competitor", "conditional", "ambiguous", "casual")


def seed(settings, owner, store=None):
    store = store or Store(settings.db_path)
    with store.connection() as db:
        if db.execute("SELECT count(*) FROM analyses").fetchone()[0]:
            return 0
    samples = {item["id"]: item for item in json.loads((ROOT / "data/samples.json").read_text())}
    engine = Engine(store, replace(settings, entity_provider="off"))
    count = 0
    try:
        for sample_id in SEED_IDS:
            sample = samples.get(sample_id)
            if not sample:
                continue
            request = AnalyzeRequest(transcript=sample["transcript"], title=sample["title"], mode="rules",
                                     data_mode=sample["data_mode"], idempotency_key="seed-" + sample_id).model_dump()
            fingerprint = hashlib.sha256(dumps({key: value for key, value in request.items()
                                                if key != "idempotency_key"}).encode()).hexdigest()
            record, created = store.create(request, fingerprint, owner)
            if created:
                engine.run(record["id"])
                count += 1
    finally:
        engine.pool.shutdown(wait=True)
    return count
