-- ══════════════════════════════════════════════════════════════════════════════
-- RLS ISOLATION TEST HARNESS
-- ══════════════════════════════════════════════════════════════════════════════
--
-- Tests that firm A CANNOT see firm B's data, including historical corpus.
-- All test data is wrapped in BEGIN/ROLLBACK — nothing persists.
--
-- Run AFTER migrations 001-005 have been applied.
-- Run with service role (to bypass RLS for data setup), then switch role
-- to 'authenticated' with crafted JWT claims to simulate per-firm access.
--
-- Expected output: 15 PASS notices, 0 failures.
-- ══════════════════════════════════════════════════════════════════════════════

BEGIN;

-- ══════════════════════════════════════════════════════════════
-- TEST DATA SETUP (runs as service role, bypasses RLS)
-- ══════════════════════════════════════════════════════════════

-- Two test firms
INSERT INTO firms (id, name) VALUES
    ('aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa', 'Firm Alpha'),
    ('bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb', 'Firm Beta');

-- Five corpora: 2 private per firm + 1 shared
INSERT INTO corpus (id, name, type, firm_id, is_shared_across_firms) VALUES
    ('c1111111-1111-4111-8111-111111111111', 'Alpha Active',    'active_case', 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa', false),
    ('c2222222-2222-4222-8222-222222222222', 'Alpha Historical', 'historical',  'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa', false),
    ('c3333333-3333-4333-8333-333333333333', 'Beta Active',     'active_case', 'bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb', false),
    ('c4444444-4444-4444-8444-444444444444', 'Beta Historical',  'historical',  'bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb', false),
    ('c5555555-5555-4555-8555-555555555555', 'Shared Statutes',  'legislation', 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa', true);

-- Cases — one per firm
INSERT INTO cases (id, case_name, firm_id) VALUES
    ('ca111111-1111-4111-8111-111111111111', 'Alpha v. Defendant', 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa'),
    ('ca222222-2222-4222-8222-222222222222', 'Beta v. Plaintiff',  'bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb');

-- Six documents across all corpora + orphan
INSERT INTO documents (id, file_name, case_id, corpus_id) VALUES
    ('d1111111-1111-4111-8111-111111111111', 'alpha_complaint',  'ca111111-1111-4111-8111-111111111111', 'c1111111-1111-4111-8111-111111111111'),
    ('d2222222-2222-4222-8222-222222222222', 'alpha_old_case',    NULL,                                  'c2222222-2222-4222-8222-222222222222'),
    ('d3333333-3333-4333-8333-333333333333', 'beta_contract',    'ca222222-2222-4222-8222-222222222222', 'c3333333-3333-4333-8333-333333333333'),
    ('d4444444-4444-4444-8444-444444444444', 'beta_old_case',     NULL,                                  'c4444444-4444-4444-8444-444444444444'),
    ('d5555555-5555-4555-8555-555555555555', 'shared_statute',    NULL,                                  'c5555555-5555-4555-8555-555555555555'),
    ('d6666666-6666-4666-8666-666666666666', 'orphan_doc',        NULL,                                  NULL);

-- Sections
INSERT INTO sections (id, document_id, section_title) VALUES
    ('a1111111-1111-4111-8111-111111111111', 'd1111111-1111-4111-8111-111111111111', 'Alpha Sec 1'),
    ('a2222222-2222-4222-8222-222222222222', 'd2222222-2222-4222-8222-222222222222', 'Alpha Hist Sec'),
    ('a3333333-3333-4333-8333-333333333333', 'd3333333-3333-4333-8333-333333333333', 'Beta Sec 1'),
    ('a4444444-4444-4444-8444-444444444444', 'd4444444-4444-4444-8444-444444444444', 'Beta Hist Sec'),
    ('a5555555-5555-4555-8555-555555555555', 'd5555555-5555-4555-8555-555555555555', 'Shared Sec'),
    ('a6666666-6666-4666-8666-666666666666', 'd6666666-6666-4666-8666-666666666666', 'Orphan Sec');

-- Intake queue items
INSERT INTO intake_queue (id, firm_id, source_channel, file_name, status) VALUES
    ('iq111111-1111-4111-8111-111111111111', 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa', 'upload', 'alpha.pdf', 'pending'),
    ('iq222222-2222-4222-8222-222222222222', 'bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb', 'email',  'beta.pdf',  'pending');

-- Notifications
INSERT INTO notifications (id, firm_id, event_type, payload) VALUES
    ('n1111111-1111-4111-8111-111111111111', 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa', 'intake_completed', '{"msg":"alpha done"}'),
    ('n2222222-2222-4222-8222-222222222222', 'bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb', 'intake_completed', '{"msg":"beta done"}');

-- Agent responses
INSERT INTO agent_responses (id, case_id, session_id, query, agent_name, answer, confidence, firm_id) VALUES
    ('ar111111-1111-4111-8111-111111111111', 'ca111111-1111-4111-8111-111111111111', 'sess-a', 'test', 'general', 'answer', 0.9, 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa'),
    ('ar222222-2222-4222-8222-222222222222', 'ca222222-2222-4222-8222-222222222222', 'sess-b', 'test', 'general', 'answer', 0.9, 'bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb');


-- ══════════════════════════════════════════════════════════════════════════════
-- TESTS AS FIRM ALPHA
-- ══════════════════════════════════════════════════════════════════════════════

SET LOCAL request.jwt.claims = '{"firm_id":"aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"}';
SET LOCAL role = 'authenticated';

-- T1: Alpha sees 3 corpora (own 2 + shared 1)
DO $$ BEGIN
    ASSERT (SELECT count(*) FROM corpus) = 3,
        'FAIL T1: Alpha should see 3 corpora, got ' || (SELECT count(*) FROM corpus);
    RAISE NOTICE 'PASS T1: Alpha sees 3 corpora (2 own + 1 shared)';
END $$;

-- T2: Alpha sees 3 documents (own 2 + shared statute)
DO $$ BEGIN
    ASSERT (SELECT count(*) FROM documents) = 3,
        'FAIL T2: Alpha should see 3 docs, got ' || (SELECT count(*) FROM documents);
    RAISE NOTICE 'PASS T2: Alpha sees 3 documents';
END $$;

-- T3: CRITICAL — Alpha CANNOT see beta_old_case (historical corpus leak)
DO $$ BEGIN
    ASSERT NOT EXISTS (SELECT 1 FROM documents WHERE file_name = 'beta_old_case'),
        'FAIL T3: Alpha can see Beta historical doc — CRITICAL DATA LEAK';
    RAISE NOTICE 'PASS T3: Beta historical corpus INVISIBLE to Alpha';
END $$;

-- T4: Alpha CANNOT see beta_contract (active case leak)
DO $$ BEGIN
    ASSERT NOT EXISTS (SELECT 1 FROM documents WHERE file_name = 'beta_contract'),
        'FAIL T4: Alpha can see Beta active doc — DATA LEAK';
    RAISE NOTICE 'PASS T4: Beta active docs INVISIBLE to Alpha';
END $$;

-- T5: Orphan document invisible (default deny on NULL corpus + NULL case)
DO $$ BEGIN
    ASSERT NOT EXISTS (SELECT 1 FROM documents WHERE file_name = 'orphan_doc'),
        'FAIL T5: Orphan visible — default deny broken';
    RAISE NOTICE 'PASS T5: Orphan document correctly invisible (default deny)';
END $$;

-- T6: Alpha sees 3 sections (own 2 + shared 1; NOT beta's 2, NOT orphan)
DO $$ BEGIN
    ASSERT (SELECT count(*) FROM sections) = 3,
        'FAIL T6: Alpha should see 3 sections, got ' || (SELECT count(*) FROM sections);
    RAISE NOTICE 'PASS T6: Alpha section isolation correct';
END $$;

-- T7: Alpha sees 1 case
DO $$ BEGIN
    ASSERT (SELECT count(*) FROM cases) = 1,
        'FAIL T7: Alpha should see 1 case, got ' || (SELECT count(*) FROM cases);
    RAISE NOTICE 'PASS T7: Alpha case isolation correct';
END $$;

-- T8: Alpha intake queue isolated
DO $$ BEGIN
    ASSERT (SELECT count(*) FROM intake_queue) = 1,
        'FAIL T8: Alpha should see 1 intake item, got ' || (SELECT count(*) FROM intake_queue);
    RAISE NOTICE 'PASS T8: Alpha intake queue isolated';
END $$;

-- T9: Alpha notifications isolated
DO $$ BEGIN
    ASSERT (SELECT count(*) FROM notifications) = 1,
        'FAIL T9: Alpha should see 1 notification, got ' || (SELECT count(*) FROM notifications);
    RAISE NOTICE 'PASS T9: Alpha notifications isolated';
END $$;

-- T10: Alpha CAN see shared statute (legislation corpus)
DO $$ BEGIN
    ASSERT EXISTS (SELECT 1 FROM documents WHERE file_name = 'shared_statute'),
        'FAIL T10: Shared statute should be visible to Alpha';
    RAISE NOTICE 'PASS T10: Shared legislation accessible to Alpha';
END $$;


-- ══════════════════════════════════════════════════════════════════════════════
-- TESTS AS FIRM BETA
-- ══════════════════════════════════════════════════════════════════════════════

SET LOCAL request.jwt.claims = '{"firm_id":"bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"}';

-- T11: Beta sees 3 corpora (own 2 + shared 1)
DO $$ BEGIN
    ASSERT (SELECT count(*) FROM corpus) = 3,
        'FAIL T11: Beta should see 3 corpora, got ' || (SELECT count(*) FROM corpus);
    RAISE NOTICE 'PASS T11: Beta sees 3 corpora';
END $$;

-- T12: CRITICAL — Beta CANNOT see alpha_old_case (historical corpus leak)
DO $$ BEGIN
    ASSERT NOT EXISTS (SELECT 1 FROM documents WHERE file_name = 'alpha_old_case'),
        'FAIL T12: Beta can see Alpha historical doc — CRITICAL DATA LEAK';
    RAISE NOTICE 'PASS T12: Alpha historical corpus INVISIBLE to Beta';
END $$;

-- T13: Beta CANNOT see alpha_complaint
DO $$ BEGIN
    ASSERT NOT EXISTS (SELECT 1 FROM documents WHERE file_name = 'alpha_complaint'),
        'FAIL T13: Beta can see Alpha active doc — DATA LEAK';
    RAISE NOTICE 'PASS T13: Alpha active docs INVISIBLE to Beta';
END $$;

-- T14: Beta sees 3 docs (own 2 + shared)
DO $$ BEGIN
    ASSERT (SELECT count(*) FROM documents) = 3,
        'FAIL T14: Beta should see 3 docs, got ' || (SELECT count(*) FROM documents);
    RAISE NOTICE 'PASS T14: Beta document isolation correct';
END $$;


-- ══════════════════════════════════════════════════════════════════════════════
-- TESTS WITH NO JWT / EMPTY CLAIMS (default deny)
-- ══════════════════════════════════════════════════════════════════════════════

SET LOCAL request.jwt.claims = '{}';

-- T15: Empty JWT sees NOTHING at all
DO $$ BEGIN
    ASSERT (SELECT count(*) FROM corpus) = 0,
        'FAIL T15a: empty JWT sees corpora';
    ASSERT (SELECT count(*) FROM documents) = 0,
        'FAIL T15b: empty JWT sees documents';
    ASSERT (SELECT count(*) FROM sections) = 0,
        'FAIL T15c: empty JWT sees sections';
    ASSERT (SELECT count(*) FROM cases) = 0,
        'FAIL T15d: empty JWT sees cases';
    ASSERT (SELECT count(*) FROM intake_queue) = 0,
        'FAIL T15e: empty JWT sees intake queue';
    ASSERT (SELECT count(*) FROM notifications) = 0,
        'FAIL T15f: empty JWT sees notifications';
    ASSERT (SELECT count(*) FROM agent_responses) = 0,
        'FAIL T15g: empty JWT sees agent responses';
    RAISE NOTICE 'PASS T15: Empty JWT = zero rows everywhere (default deny works)';
END $$;


-- ══════════════════════════════════════════════════════════════════════════════
-- CLEANUP — rollback all test data
-- ══════════════════════════════════════════════════════════════════════════════

ROLLBACK;
