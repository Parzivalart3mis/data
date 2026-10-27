\set ON_ERROR_STOP on
\if :{?expected_requests}
\else
  \set expected_requests 500
\endif

BEGIN;
SET search_path TO virginia_dev_saayam_rdbms, public;
CREATE TEMP TABLE mock_checks (name text, passed boolean NOT NULL);

INSERT INTO mock_checks VALUES
  ('request_count', (SELECT count(*) = :expected_requests FROM requests)),
  ('nine_validated_request_foreign_keys', (
    SELECT count(*) = 9 AND bool_and(convalidated)
    FROM pg_constraint WHERE conrelid = 'requests'::regclass AND contype = 'f'
  )),
  ('case_1_skewed_lead_nulls', (
    SELECT count(*) FILTER (WHERE lead_volunteer_id IS NULL) = :expected_requests - greatest(2, round(:expected_requests * 3 / 221.0)) FROM requests
  )),
  ('case_1_skewed_beneficiary_nulls', (
    SELECT count(*) FILTER (WHERE beneficiary_id IS NULL) = round(:expected_requests * 8 / 221.0) FROM requests
  )),
  ('case_2_shared_microsecond', (
    SELECT count(*) >= 3 FROM requests WHERE last_updated_at = '2026-10-24 12:00:00.123456'
  )),
  ('case_2_adjacent_half_open_windows', (
    SELECT
      count(*) FILTER (WHERE last_updated_at >= '2026-10-24 12:00:00.123456' AND last_updated_at < '2026-10-24 12:00:00.123457') >= 3
      AND count(*) FILTER (WHERE last_updated_at >= '2026-10-24 12:00:00.123457' AND last_updated_at < '2026-10-24 12:00:00.123458') = 1
    FROM requests
  )),
  ('case_3_update_before_submission', (SELECT bool_or(last_updated_at < submission_date) FROM requests)),
  ('case_4_csv_special_characters', (
    SELECT strpos(req_desc, E'\n') > 0 AND strpos(req_desc, ',') > 0 AND strpos(req_desc, '"') > 0 FROM requests WHERE req_id = 'REQ-MOCK-0007'
  )),
  ('case_5_unicode', (SELECT strpos(req_desc, 'mañana') > 0 FROM requests WHERE req_id = 'REQ-MOCK-0008')),
  ('case_7_all_nullable_text_null', (
    SELECT beneficiary_id IS NULL AND lead_volunteer_id IS NULL AND req_doc_link IS NULL AND audio_req_desc IS NULL AND deletion_reason IS NULL
    FROM requests WHERE req_id = 'REQ-MOCK-0001'
  )),
  ('case_7_all_nullable_text_empty', (
    SELECT beneficiary_id = '' AND lead_volunteer_id = '' AND req_doc_link = '' AND audio_req_desc = '' AND deletion_reason = ''
    FROM requests WHERE req_id = 'REQ-MOCK-0009'
  )),
  ('case_7_empty_fk_target_exists', (SELECT count(*) = 1 FROM users WHERE user_id = '')),
  ('case_10_contradictory_lead', (SELECT req_islead_id = 1 AND lead_volunteer_id IS NULL FROM requests WHERE req_id = 'REQ-MOCK-0011'));

CREATE TEMP TABLE mock_pii (req_id text, fields text, pii_types text, synthetic text);
\copy mock_pii FROM '/fixtures/pii_manifest.csv' WITH (FORMAT csv, HEADER true)
INSERT INTO mock_checks VALUES ('case_6_marked_synthetic_pii', (
  SELECT count(*) = 6 AND bool_and(
    p.synthetic = 'true' AND r.req_desc LIKE 'Synthetic person:%'
    AND r.req_desc LIKE '%Phone:%' AND r.req_desc LIKE '%@example.%'
    AND r.req_desc LIKE '%Address:%Fictional City.%'
  ) FROM mock_pii p JOIN requests r USING (req_id)
));
INSERT INTO mock_checks VALUES ('case_6_phone_as_words', (
  SELECT req_desc LIKE '%four eight zero%' FROM requests WHERE req_id = 'REQ-MOCK-0022'
));

-- Check every bounded request text column against its actual PostgreSQL limit.
DO $$
DECLARE col record; maximum_matches boolean;
BEGIN
  FOR col IN SELECT column_name, character_maximum_length FROM information_schema.columns
    WHERE table_schema = 'virginia_dev_saayam_rdbms' AND table_name = 'requests'
      AND character_maximum_length IS NOT NULL
  LOOP
    EXECUTE format('SELECT max(char_length(%I)) = %s FROM requests', col.column_name, col.character_maximum_length) INTO maximum_matches;
    INSERT INTO mock_checks VALUES ('case_8_maximum_' || col.column_name, maximum_matches);
  END LOOP;
END $$;

INSERT INTO mock_checks VALUES
  ('case_9_categories', (SELECT NOT EXISTS (SELECT cat_id FROM help_categories EXCEPT SELECT req_cat_id FROM requests))),
  ('case_9_for', (SELECT NOT EXISTS (SELECT req_for_id FROM request_for EXCEPT SELECT req_for_id FROM requests))),
  ('case_9_islead', (SELECT NOT EXISTS (SELECT req_islead_id FROM request_isleadvol EXCEPT SELECT req_islead_id FROM requests))),
  ('case_9_types', (SELECT NOT EXISTS (SELECT req_type_id FROM request_types EXCEPT SELECT req_type_id FROM requests))),
  ('case_9_priorities', (SELECT NOT EXISTS (SELECT req_priority_id FROM request_priorities EXCEPT SELECT req_priority_id FROM requests))),
  ('case_9_statuses', (SELECT NOT EXISTS (SELECT req_status_id FROM request_statuses EXCEPT SELECT req_status_id FROM requests))),
  ('postgis_coordinates', (SELECT bool_and(ST_SRID(req_loc) = 4326 AND GeometryType(req_loc) = 'POINT') FROM requests WHERE req_loc IS NOT NULL));

-- Stage the invalid CSV without foreign keys, then require SQLSTATE 23503 on insert.
CREATE TEMP TABLE mock_broken (LIKE requests INCLUDING DEFAULTS);
\copy mock_broken FROM '/fixtures/fixtures/requests_broken_reference.csv' WITH (FORMAT csv, HEADER true, NULL '\N', ENCODING 'UTF8')
DO $$
DECLARE failure_constraint text;
BEGIN
  BEGIN
    INSERT INTO requests SELECT * FROM mock_broken;
    RAISE EXCEPTION 'Broken fixture unexpectedly loaded';
  EXCEPTION WHEN foreign_key_violation THEN
    GET STACKED DIAGNOSTICS failure_constraint = CONSTRAINT_NAME;
    INSERT INTO mock_checks VALUES ('case_11_broken_fixture_rejected', failure_constraint = 'requests_creator_id_fkey');
  END;
END $$;
INSERT INTO mock_checks VALUES ('main_data_unchanged', (SELECT count(*) = :expected_requests FROM requests));

DO $$
DECLARE failures text;
BEGIN
  SELECT string_agg(name, ', ' ORDER BY name) INTO failures FROM mock_checks WHERE NOT passed;
  IF failures IS NOT NULL THEN RAISE EXCEPTION 'Mock data validation failed: %', failures; END IF;
END $$;
SELECT name, passed FROM mock_checks ORDER BY name;
SELECT count(*) AS checks_passed FROM mock_checks WHERE passed;
ROLLBACK;
