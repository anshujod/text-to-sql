# Ambiguity taxonomy: worked examples

One example per type from `t2sql.clarify.taxonomy`, run against the live
seed data (docker db, `app_readonly`). Every number below was executed
directly, not estimated. Full SQL for each variant is included so the
divergence is checkable, not asserted.

Schema and semantic-layer references: `docker/init/sql/schema.sql`,
`src/t2sql/semantic/{entities,metrics,defaults,joins}.yaml`.

---

## METRIC

**Question:** "Who is our best customer?"

`metrics.yaml` lists `revenue_net`, `order_count`, and `session_count` as all
matching the synonym "best" -- deliberately, so this collision is real, not
contrived.

```sql
-- interpretation A: best = highest net revenue
SELECT o.customer_id,
       COALESCE(SUM(p.amount) FILTER (WHERE p.status = 'succeeded'), 0)
         - COALESCE((SELECT SUM(r.amount) FROM refunds r
                      JOIN orders o2 ON r.order_id = o2.id
                      WHERE o2.customer_id = o.customer_id), 0) AS revenue_net
FROM orders o JOIN payments p ON p.order_id = o.id
GROUP BY o.customer_id ORDER BY revenue_net DESC LIMIT 1;
-- -> customer_id 1333, $38,722.31

-- interpretation B: best = most orders placed
SELECT customer_id, COUNT(DISTINCT id) AS order_count
FROM orders WHERE status != 'cancelled'
GROUP BY customer_id ORDER BY order_count DESC LIMIT 1;
-- -> customer_id 3000, 319 orders
```

**Divergence:** different customer entirely (1333 vs. 3000). Default policy: **ASK**.

---

## TEMPORAL

**Question:** "How many orders did we get last month?"

Seed data is anchored to `max(orders.created_at) = 2025-12-31`, with a
Black Friday spike deliberately placed inside November (see
`defaults.yaml`'s anchor comment).

```sql
-- interpretation A: calendar last month (November 2025)
SELECT COUNT(*) FROM orders
WHERE created_at >= '2025-11-01' AND created_at < '2025-12-01';
-- -> 4361

-- interpretation B: trailing 30 days from the data anchor
SELECT COUNT(*) FROM orders
WHERE created_at >= '2025-12-01 22:27:15' AND created_at <= '2025-12-31 22:27:15';
-- -> 2503
```

**Divergence:** 4361 vs. 2503 -- the calendar-month reading captures the
Black Friday spike, the trailing-30-day reading mostly doesn't. Default
policy: **DEFAULT_AND_DISCLOSE** (default = calendar month, per
`defaults.yaml`).

---

## ENTITY

**Question:** "How many customers do we have?"

`entities.yaml` flags `customers` (one row per real-world entity) vs.
`users` (one row per login account) as different grain.

```sql
-- interpretation A: customer entities
SELECT COUNT(*) FROM customers;
-- -> 5000

-- interpretation B: active, non-internal login accounts
SELECT COUNT(*) FROM users WHERE deleted_at IS NULL AND is_internal = false;
-- -> 7589
```

**Divergence:** 5000 vs. 7589 -- customers can have more than one login.
Default policy: **ASK** (grains genuinely differ).

---

## SCOPE

**Question:** "How many orders have we had?"

```sql
-- interpretation A: all orders, any status
SELECT COUNT(*) FROM orders;
-- -> 40341

-- interpretation B: excluding cancelled (house default; a cancelled order
-- never became a sale)
SELECT COUNT(*) FROM orders WHERE status != 'cancelled';
-- -> 37531
```

**Divergence:** 40341 vs. 37531. Default policy: **DEFAULT_AND_DISCLOSE**
(default = exclude cancelled, per `defaults.yaml` / `metrics.yaml`'s
`order_count`).

---

## GRAIN

**Question:** "What's our average order value?"

```sql
-- interpretation A: per-order grain (total revenue / order count)
SELECT COALESCE(SUM(p.amount) FILTER (WHERE p.status = 'succeeded'), 0)
       / NULLIF(COUNT(DISTINCT o.id), 0)
FROM orders o JOIN payments p ON p.order_id = o.id
WHERE o.status != 'cancelled' AND p.currency = 'USD';
-- -> $192.31

-- interpretation B: per-customer grain (average of each customer's own
-- average spend)
SELECT AVG(customer_total) FROM (
  SELECT o.customer_id,
         SUM(p.amount) FILTER (WHERE p.status = 'succeeded') AS customer_total
  FROM orders o JOIN payments p ON p.order_id = o.id
  WHERE o.status != 'cancelled' AND p.currency = 'USD'
  GROUP BY o.customer_id
) x;
-- -> $1452.35
```

**Divergence:** $192.31 vs. $1452.35 -- not a rounding difference, a
different quantity (repeat customers pull the per-customer figure up).
Default policy: **ASK** (`metrics.yaml`'s `aov` documents per-order as the
system default, but the gap is large enough that guessing silently is
risky).

---

## COMPARISON

**Question:** "Which product category is growing the fastest?"

```sql
-- interpretation A: month-over-month (December vs. November 2025)
-- top category by unit_count growth: Books, -32.0% (looks like decline --
-- it's coming down off the November Black Friday spike)

-- interpretation B: last month vs. trailing 6-month average (Jun-Nov 2025)
-- same top category, Books, +3.5% (looks like growth)
```

Full query (both interpretations share this shape, differing only in the
baseline CTE):

```sql
WITH cat_month AS (
  SELECT c.name AS category, date_trunc('month', o.created_at) AS mon,
         SUM(oi.quantity) AS units
  FROM order_items oi
  JOIN orders o ON o.id = oi.order_id
  JOIN products p ON p.id = oi.product_id
  JOIN categories c ON c.id = p.category_id
  WHERE o.status != 'cancelled'
  GROUP BY c.name, date_trunc('month', o.created_at)
)
-- A: baseline = cat_month WHERE mon = '2025-11-01'
-- B: baseline = AVG(units) over cat_month WHERE mon BETWEEN '2025-06-01' AND '2025-11-30'
```

**Divergence:** the top category is the same (Books) under both readings,
but the sign flips -- month-over-month says it's shrinking (-32.0%), the
6-month baseline says it's growing (+3.5%). "Is it growing" gets opposite
answers depending on the unstated baseline. Default policy: **ASK**.

---

## RESULT_SHAPE

**Question:** "Show me our top customers by revenue."

```sql
WITH rev AS (
  SELECT o.customer_id,
         COALESCE(SUM(p.amount) FILTER (WHERE p.status = 'succeeded'), 0) AS revenue
  FROM orders o JOIN payments p ON p.order_id = o.id
  WHERE p.currency = 'USD'
  GROUP BY o.customer_id
)
SELECT customer_id, revenue FROM rev ORDER BY revenue DESC LIMIT 5;   -- interpretation A
SELECT customer_id, revenue FROM rev ORDER BY revenue DESC LIMIT 10;  -- interpretation B
```

Top 5 (interpretation A): customers 1333 ($40,310.70), 2181 ($38,624.66),
3732 ($33,369.90), 397 ($32,023.61), 3628 ($30,955.96).

**Divergence:** interpretation B is a strict superset of A plus 5 more
rows -- literally a different result set, even though the ranking logic
is uncontested. Default policy: **DEFAULT_AND_DISCLOSE** (default = 10,
per `defaults.yaml`).
