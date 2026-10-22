"""Run the whole pipeline in order: load -> extract -> classify (docs, then emails) -> chasers -> accuracy report.

Every step resumes: re-running only processes what has no result yet, so nothing is paid twice.
The model step costs about $5 for the full task inbox (Claude Haiku 5.5).

Run: python -m ap_agent.run_all            (everything)
     python -m ap_agent.run_all --review   (also the ~$2 accuracy sample with Claude Sonnet 5.5)
"""
import sys

from . import chasers, classify, evaluate, extract, load
from .db import connect


def main():
    print("1/5 load: emails + attachment links (free)")
    load.main()
    print("\n2/5 extract: read each unique file once (free)")
    extract.main()
    print("\n3/5 classify: documents, then emails (Claude Haiku 5.5, ~$5)")
    con = connect()
    classify.classify_docs(con)
    classify.classify_emails(con)
    print("\n4/5 chasers: vendors chasing payment, invoices found / not found (free)")
    chasers.main()
    if "--review" in sys.argv:
        print("\n5/5 accuracy sample: second opinion from Claude Sonnet 5.5 (~$2)")
        evaluate.review(connect(), per_class=12)
    print("\n5/5 accuracy report:")
    import json
    print(json.dumps(evaluate.report(connect()), indent=1)[:3000])


if __name__ == "__main__":
    main()
