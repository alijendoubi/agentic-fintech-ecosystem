-- 002_verify_chain.sql  --  run as afe_audit_owner. Chain verification function.
--
-- audit.verify_chain(from_seq, to_seq): walks rows in seq order and returns ONE row.
--   first_break_seq IS NULL  => the range is valid. Otherwise first_break_seq/defect/expected/actual describe the FIRST break.
--   head_seq/head_hash describe the last row examined (publish these to an external anchor sink).
-- Range semantics: the row immediately before from_seq (if any) supplies the expected prev_hash; when there is no
-- predecessor the first row must have seq = 1 and prev_hash = genesis (64 zeros).
-- NOTE: this detects tampering *inside* the table. Truncation of the tail or a consistent rewrite of the whole
-- suffix is only detectable against externally anchored head hashes (see afe_audit.anchor).
\set ON_ERROR_STOP on
SELECT (current_user = 'afe_audit_owner') AS is_owner \gset
\if :is_owner
\else
  \echo ERROR: run this migration as afe_audit_owner
  SELECT 1 / 0;
\endif

CREATE OR REPLACE FUNCTION audit.verify_chain(from_seq bigint DEFAULT NULL, to_seq bigint DEFAULT NULL)
RETURNS TABLE (
    rows_checked    bigint,
    first_break_seq bigint,
    defect          text,
    expected        text,
    actual          text,
    head_seq        bigint,
    head_hash       text
)
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, audit
AS $$
DECLARE
    rec    audit.audit_events;
    p_seq  bigint := 0;
    p_hash text := audit.genesis_hash();
    n      bigint := 0;
    d      text;
    l_seq  bigint;
    l_hash text;
BEGIN
    IF from_seq IS NOT NULL THEN
        SELECT e.seq, e.hash INTO l_seq, l_hash FROM audit.audit_events e
         WHERE e.seq < from_seq ORDER BY e.seq DESC LIMIT 1;
        IF FOUND THEN
            p_seq := l_seq;
            p_hash := l_hash;
        END IF;
    END IF;

    FOR rec IN
        SELECT * FROM audit.audit_events e
         WHERE (from_seq IS NULL OR e.seq >= from_seq) AND (to_seq IS NULL OR e.seq <= to_seq)
         ORDER BY e.seq
    LOOP
        n := n + 1;
        IF rec.seq <> p_seq + 1 THEN
            RETURN QUERY SELECT n, rec.seq, 'seq_gap'::text, (p_seq + 1)::text, rec.seq::text, p_seq, p_hash;
            RETURN;
        END IF;
        d := audit.row_defect(rec, p_hash);
        IF d IS NOT NULL THEN
            RETURN QUERY SELECT n, rec.seq, d,
                CASE WHEN d = 'prev_hash_link_broken' THEN p_hash END,
                CASE WHEN d = 'prev_hash_link_broken' THEN rec.prev_hash::text END,
                p_seq, p_hash;
            RETURN;
        END IF;
        p_seq := rec.seq;
        p_hash := rec.hash;
    END LOOP;

    RETURN QUERY SELECT n, NULL::bigint, NULL::text, NULL::text, NULL::text, p_seq, p_hash;
END;
$$;

GRANT EXECUTE ON FUNCTION audit.verify_chain(bigint, bigint) TO afe_audit_app;
