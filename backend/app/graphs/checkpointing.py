"""Checkpointers for graphs that pause (Graph 0: human approval and chat).

Use the Postgres saver in Docker so paused jobs survive container restarts.
Needs: langgraph-checkpoint-postgres and psycopg[binary] (pin both, N11).
"""
from contextlib import contextmanager

from langgraph.checkpoint.memory import MemorySaver


def memory_checkpointer() -> MemorySaver:
    """Development and tests only. State is lost when the process stops."""
    return MemorySaver()


@contextmanager
def postgres_checkpointer(conn_string: str):
    """Usage:
        with postgres_checkpointer(os.environ["DATABASE_URL"]) as saver:
            graph = build_job_post_graph(deps, saver)
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(conn_string) as saver:
        saver.setup()  # creates the checkpoint tables if they do not exist
        yield saver
