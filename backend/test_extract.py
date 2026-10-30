"""Local check of job-field parsing. This does not call Gemini."""
from app.graphs.job_post_graph import parse_job_fields

samples = [
    "backend developer",
    "Python and Docker, 3 years, BSc in CSE",
    "hiring@example.com",
    "We need a backend developer. Python, 3 years, BSc in CSE. hiring@example.com",
]
for text in samples:
    print(text)
    print(" ", parse_job_fields(text))
