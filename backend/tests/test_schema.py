"""Checks the target schema against a real PostgreSQL: it applies, constraints reject bad data,
and deleting a candidate removes all of their data (N19). Uses a throw-away schema.
Run: TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/db python -m pytest tests/test_schema.py"""
import os
import pathlib

import psycopg
import pytest

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
SQL = (pathlib.Path(__file__).parent.parent / "app" / "db" / "schema.sql").read_text()
VEC = "[" + ",".join(["0.1"] * 768) + "]"


@pytest.fixture()
def db():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS v2test CASCADE")
        c.execute("CREATE SCHEMA v2test")
        c.execute("SET search_path = v2test, public")
        c.execute(SQL)
        c.execute(SQL)          # idempotent: a second run must not fail
        yield c
        c.execute("DROP SCHEMA v2test CASCADE")


def bad(db, sql, args=()):
    """The statement must be rejected by the database, and the connection stay usable."""
    with pytest.raises(psycopg.errors.IntegrityError):
        with db.transaction():
            db.execute(sql, args)


def seed(db):
    db.execute("INSERT INTO job (id, weights) VALUES ('j1', '{\"technical_skills\":1}'), ('j2', '{\"technical_skills\":1}')")
    db.execute("INSERT INTO job_requirement VALUES ('j1','R1',1,'Python','required','skill'), ('j2','R1',1,'SQL','required','skill')")
    db.execute("INSERT INTO candidate (id, job_id, name, email, status, source, aggregate_score) VALUES ('c1','j1','Test One','a@example.com','assessed','email',80)")
    db.execute("INSERT INTO cv_parsed VALUES ('c1', '{\"skills\":[\"Python\"]}', 'cv-v1', now())")
    db.execute("INSERT INTO screening_result VALUES ('c1','j1','R1','met','Shown in CV','[\"cv:c1:c001\"]','cv-v1')")
    db.execute("INSERT INTO score (candidate_id, dimension, value, weight, justification, prompt_version) VALUES ('c1','technical_skills',80,1,'ok','cv-v1')")
    db.execute("INSERT INTO interview_session (id, job_id, candidate_id, join_token_hash) VALUES ('s1','j1','c1','hash1')")
    db.execute("INSERT INTO transcript_turn VALUES ('s1','t1','interviewer',0,1000,'Question?',0.9), ('s1','t2','candidate',1100,5000,'Answer.',0.9)")
    db.execute("INSERT INTO qa_pair VALUES ('s1','p1','{t1}','{t2}',false,NULL,8,8,7,8,'Good','[\"tr:s1:t2\"]','qa-v1')")
    db.execute("INSERT INTO qa_pair_requirement VALUES ('s1','p1','j1','R1')")
    db.execute("INSERT INTO assessment VALUES ('a1','c1','s1','completed',true,'strong',80,75,77.5,'{}','as-v1',now())")
    db.execute("INSERT INTO interview_note (session_id, candidate_id, author_id, author_name, body) VALUES ('s1','c1','ivy','Ivy','Ask about testing')")
    db.execute("INSERT INTO vector_item (collection,item_id,candidate_id,text,embedding) VALUES ('cv:c1','c001','c1','Python dev', %s::vector)", (VEC,))
    db.execute("INSERT INTO policy_document VALUES ('pol-1','Standards','1.0',now(),'active',1)")
    db.execute("INSERT INTO vector_item (collection,item_id,policy_id,text,embedding) VALUES ('policy','pol-1:c001','pol-1','Use titles', %s::vector)", (VEC,))


def test_constraints_reject_bad_data(db):
    seed(db)
    bad(db, "INSERT INTO score (candidate_id,dimension,value,weight,justification,prompt_version) VALUES ('c1','x',101,1,'j','v')")
    bad(db, "INSERT INTO transcript_turn VALUES ('s1','t9','candidate',2000,1000,'backwards',NULL)")
    bad(db, "INSERT INTO transcript_turn VALUES ('s1','t9','robot',0,1,'x',NULL)")
    bad(db, "INSERT INTO screening_result VALUES ('c1','j1','R1','met','x','[]','v')")                    # PK clash
    db.execute("DELETE FROM screening_result")
    bad(db, "INSERT INTO screening_result VALUES ('c1','j1','R1','met','no evidence','[]','v')")          # met needs evidence
    bad(db, "INSERT INTO screening_result VALUES ('c1','j2','R1','unmet','wrong job','[]','v')")          # candidate belongs to j1
    bad(db, "INSERT INTO qa_pair VALUES ('s1','p2','{t1}','{t2}',false,NULL,8,8,7,8,'no cites','[]','qa-v1')")
    bad(db, "INSERT INTO qa_pair VALUES ('s1','p3','{t1}','{t2}',false,NULL,11,8,7,8,'too high','[\"tr:s1:t2\"]','qa-v1')")
    bad(db, "INSERT INTO qa_pair_requirement VALUES ('s1','p1','j2','R1')")                              # session is for j1
    bad(db, "INSERT INTO assessment VALUES ('a2','c1',NULL,'partial',true,'weak',10,NULL,10,'{}','v',now())")  # has_interview without session
    bad(db, "INSERT INTO vector_item (collection,item_id,text,embedding) VALUES ('x','1','orphan',%s::vector)", (VEC,))  # no owner
    bad(db, "INSERT INTO vector_item (collection,item_id,candidate_id,job_id,text,embedding) VALUES ('x','2','c1','j1','two owners',%s::vector)", (VEC,))
    bad(db, "INSERT INTO interview_session (id,job_id,candidate_id,join_token_hash) VALUES ('s2','j2','c1','hash2')")   # candidate not in j2
    bad(db, "INSERT INTO policy_document VALUES ('pol-2','Standards','2.0',now(),'active',1)")             # second active version
    bad(db, "INSERT INTO application (job_id,candidate_id,message_id_hash,status) VALUES ('j1','c1','m1','accepted'), ('j1','c1','m1','accepted')")  # duplicate email
    bad(db, "INSERT INTO application (job_id,message_id_hash,status) VALUES ('j1','m2','accepted')")            # accepted needs a candidate
    db.execute("INSERT INTO application (message_id_hash,status,reject_reason) VALUES ('m3','rejected','no_job_code')")  # rejected emails may have no job
    bad(db, "INSERT INTO job (id, weights) VALUES ('j3','[1,2]')")                                          # weights must be an object


def test_deleting_a_candidate_removes_all_their_data_but_keeps_the_audit_record(db):
    seed(db)
    db.execute("INSERT INTO audit_event (actor_role, action, entity, entity_id, outcome) VALUES ('admin','candidate_deleted','candidate','c1','ok')")
    db.execute("DELETE FROM candidate WHERE id = 'c1'")
    for table in ["cv_parsed", "screening_result", "score", "interview_session", "transcript_turn", "qa_pair",
                  "qa_pair_requirement", "assessment", "interview_note"]:
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    assert db.execute("SELECT count(*) FROM vector_item WHERE collection = 'cv:c1'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM vector_item WHERE collection = 'policy'").fetchone()[0] == 1   # policy untouched
    assert db.execute("SELECT count(*) FROM job WHERE id = 'j1'").fetchone()[0] == 1                       # job untouched
    assert db.execute("SELECT count(*) FROM audit_event WHERE entity_id = 'c1'").fetchone()[0] == 1       # audit survives


def test_deleting_a_job_and_a_policy_cascades(db):
    seed(db)
    db.execute("DELETE FROM policy_document WHERE id = 'pol-1'")
    assert db.execute("SELECT count(*) FROM vector_item WHERE collection = 'policy'").fetchone()[0] == 0
    db.execute("DELETE FROM job WHERE id = 'j1'")
    for table in ["candidate", "job_requirement", "interview_session"]:
        assert db.execute(f"SELECT count(*) FROM {table} WHERE job_id = 'j1'").fetchone()[0] == 0, table
    for table in ["assessment", "vector_item"]:
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    assert db.execute("SELECT count(*) FROM job_requirement WHERE job_id = 'j2'").fetchone()[0] == 1   # other jobs untouched


def test_similarity_search_and_ranking_queries_work(db):
    seed(db)
    row = db.execute("SELECT item_id FROM vector_item WHERE collection='policy' ORDER BY embedding <=> %s::vector LIMIT 1", (VEC,)).fetchone()
    assert row[0] == "pol-1:c001"
    db.execute("INSERT INTO candidate (id,job_id,status,source,aggregate_score) VALUES ('c2','j1','screened','upload',90), ('c3','j1','failed','upload',NULL)")
    ranked = [r[0] for r in db.execute("SELECT id FROM candidate WHERE job_id='j1' ORDER BY aggregate_score DESC NULLS LAST").fetchall()]
    assert ranked == ["c2", "c1", "c3"]
