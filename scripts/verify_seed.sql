-- Verification queries for the Phase 0.3 seed data. Each block proves one
-- ambiguity trap actually fires on the generated data, per PLAN.md 0.3.
-- SEED_END_DATE below must match SEED_END_DATE in scripts/generate_seed.py.
-- Raises an exception (nonzero exit) if any check fails.

DO $$
DECLARE
    overlap_revenue_orders   int;
    overlap_revenue_sessions int;
    gross                    numeric;
    net                      numeric;
    net_diff_pct             numeric;
    internal_in_top20        int;
    last_calendar_month      numeric;
    trailing_30_days         numeric;
    month_diff_pct           numeric;
    failures                 text[] := '{}';
BEGIN
    CREATE TEMP TABLE _customer_revenue AS
    SELECT c.id AS customer_id, COALESCE(SUM(p.amount), 0) AS revenue
    FROM customers c
    LEFT JOIN orders o ON o.customer_id = c.id
    LEFT JOIN payments p ON p.order_id = o.id AND p.status = 'succeeded'
    GROUP BY c.id;

    CREATE TEMP TABLE _customer_orders AS
    SELECT customer_id, count(*) AS order_count
    FROM orders
    GROUP BY customer_id;

    CREATE TEMP TABLE _customer_sessions AS
    SELECT u.customer_id, count(*) AS session_count
    FROM sessions s JOIN users u ON u.id = s.user_id
    GROUP BY u.customer_id;

    -- 1. top-10 by revenue vs top-10 by order count must overlap by < 50%
    SELECT count(*) INTO overlap_revenue_orders FROM (
        (SELECT customer_id FROM _customer_revenue ORDER BY revenue DESC LIMIT 10)
        INTERSECT
        (SELECT customer_id FROM _customer_orders ORDER BY order_count DESC LIMIT 10)
    ) x;
    RAISE NOTICE 'top-10 revenue vs top-10 order-count overlap: %/10', overlap_revenue_orders;
    IF overlap_revenue_orders >= 5 THEN
        failures := failures || format('revenue/order-count top-10 overlap too high: %s/10', overlap_revenue_orders);
    END IF;

    -- 2. top-10 by revenue vs top-10 by session count must overlap by < 50%
    SELECT count(*) INTO overlap_revenue_sessions FROM (
        (SELECT customer_id FROM _customer_revenue ORDER BY revenue DESC LIMIT 10)
        INTERSECT
        (SELECT customer_id FROM _customer_sessions ORDER BY session_count DESC LIMIT 10)
    ) x;
    RAISE NOTICE 'top-10 revenue vs top-10 session-count overlap: %/10', overlap_revenue_sessions;
    IF overlap_revenue_sessions >= 5 THEN
        failures := failures || format('revenue/session-count top-10 overlap too high: %s/10', overlap_revenue_sessions);
    END IF;

    -- 3. gross revenue vs net-of-refunds revenue must differ by 5-15%
    SELECT COALESCE(SUM(amount), 0) INTO gross FROM payments WHERE status = 'succeeded';
    SELECT gross - COALESCE(SUM(amount), 0) INTO net FROM refunds;
    net_diff_pct := round(100 * (gross - net) / NULLIF(gross, 0), 2);
    RAISE NOTICE 'gross revenue: %, net-of-refunds revenue: %, diff: %%%', gross, net, net_diff_pct;
    IF net_diff_pct < 5 OR net_diff_pct > 15 THEN
        failures := failures || format('gross/net revenue diff out of 5-15%% band: %s%%', net_diff_pct);
    END IF;

    -- 4. internal accounts must appear in the naive top-20 by order count
    SELECT count(*) INTO internal_in_top20 FROM (
        SELECT co.customer_id FROM _customer_orders co ORDER BY co.order_count DESC LIMIT 20
    ) top20
    JOIN customers c ON c.id = top20.customer_id
    JOIN users u ON u.customer_id = c.id AND u.is_internal;
    RAISE NOTICE 'internal accounts in naive top-20 by order count: %', internal_in_top20;
    IF internal_in_top20 < 1 THEN
        failures := failures || 'no internal accounts in naive top-20 by order count';
    END IF;

    -- 5. "last month": calendar month vs trailing 30 days must diverge materially.
    -- SEED_END_DATE = 2025-12-31 -> calendar last month = November 2025 (includes
    -- the Black Friday spike), trailing 30 days = all of December (does not).
    SELECT COALESCE(SUM(p.amount), 0) INTO last_calendar_month
    FROM payments p
    WHERE p.status = 'succeeded'
      AND p.paid_at >= date_trunc('month', DATE '2025-12-31' - INTERVAL '1 month')
      AND p.paid_at <  date_trunc('month', DATE '2025-12-31');

    SELECT COALESCE(SUM(p.amount), 0) INTO trailing_30_days
    FROM payments p
    WHERE p.status = 'succeeded'
      AND p.paid_at >= DATE '2025-12-31' - INTERVAL '30 days'
      AND p.paid_at <  DATE '2025-12-31' + INTERVAL '1 day';

    month_diff_pct := round(
        100 * abs(last_calendar_month - trailing_30_days) /
        NULLIF(GREATEST(last_calendar_month, trailing_30_days), 0), 2
    );
    RAISE NOTICE 'last calendar month revenue: %, trailing-30-days revenue: %, diff: %%%',
        last_calendar_month, trailing_30_days, month_diff_pct;
    IF month_diff_pct < 15 THEN
        failures := failures || format('calendar-month vs trailing-30-days diff too small: %s%%', month_diff_pct);
    END IF;

    IF array_length(failures, 1) > 0 THEN
        RAISE EXCEPTION 'SEED VERIFICATION FAILED: %', array_to_string(failures, ' | ');
    ELSE
        RAISE NOTICE 'ALL SEED VERIFICATION CHECKS PASSED';
    END IF;
END $$;
