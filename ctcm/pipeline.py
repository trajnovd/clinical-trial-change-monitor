"""v0.2 pipeline: for each trial, resolve timeline anchors, diff every consecutive
version pair with the T0 exact matcher, classify the changes, and write findings.
Delete+reinsert per trial -- idempotent, so re-running after load_corpus() picks
up more of the corpus is free.
"""

from datetime import date

from ctcm import db
from ctcm.classify import OutcomeRow, classify, diff_pair, t0_matcher, timeline_revised
from ctcm.timeline import anchors as compute_anchors


def _outcome_rows(conn, nct: str, version_no: int) -> list[OutcomeRow]:
    rows = conn.execute(
        "SELECT nct_id, version_no, outcome_type, ordinal, measure, description, time_frame, measure_norm "
        "FROM outcomes WHERE nct_id=? AND version_no=?",
        (nct, version_no),
    ).fetchall()
    return [OutcomeRow(**dict(r)) for r in rows]


def _write_findings(conn, nct: str, findings) -> None:
    conn.execute("DELETE FROM findings WHERE nct_id=?", (nct,))
    conn.executemany(
        "INSERT INTO findings(nct_id, from_version, to_version, change_type, severity, before_measure, "
        "after_measure, days_after_enrolment, days_after_primary_completion, confidence, resolved_by, rationale) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                f.nct_id, f.from_version, f.to_version, f.change_type, f.severity, f.before_measure,
                f.after_measure, f.days_after_enrolment, f.days_after_primary_completion, f.confidence,
                f.resolved_by, f.rationale,
            )
            for f in findings
        ],
    )


def run_pipeline() -> int:
    conn = db.connect()
    nct_ids = [r[0] for r in conn.execute("SELECT nct_id FROM trials")]
    total = 0

    for nct in nct_ids:
        versions = conn.execute(
            "SELECT version_no, version_date FROM versions WHERE nct_id=? ORDER BY version_no", (nct,)
        ).fetchall()

        anc = compute_anchors(nct, conn)
        findings = list(timeline_revised(nct, anc))

        for vfrom, vto in zip(versions, versions[1:]):
            if not vto["version_date"]:
                continue  # can't compute a position without a date for the "to" version
            vdate = date.fromisoformat(vto["version_date"])
            before = _outcome_rows(conn, nct, vfrom["version_no"])
            after = _outcome_rows(conn, nct, vto["version_no"])
            changes = diff_pair(before, after, t0_matcher)
            findings.extend(classify(nct, vfrom["version_no"], vto["version_no"], vdate, changes, anc))

        _write_findings(conn, nct, findings)
        total += len(findings)
        conn.commit()

    conn.close()
    return total
