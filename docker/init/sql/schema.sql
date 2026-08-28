-- Core e-commerce schema. Deliberately messy: each design choice below
-- creates a specific, defensible ambiguity that Phase 2/3 exploit. See
-- PLAN.md Phase 0.2 for the full rationale table.

CREATE TABLE categories (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE categories IS 'Product categories. Flat, no hierarchy.';

CREATE TABLE customers (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE customers IS
    'A real-world customer entity (one person or company). Distinct from users: '
    'a single customer can have several login accounts in users. "Customer" in a '
    'question is ambiguous between this table and users.';

CREATE TABLE users (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id  bigint NOT NULL REFERENCES customers(id),
    email        text NOT NULL UNIQUE,
    is_internal  boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now(),
    deleted_at   timestamptz
);
COMMENT ON TABLE users IS 'Login accounts. Several can belong to one customer.';
COMMENT ON COLUMN users.customer_id IS 'The customer entity this login belongs to.';
COMMENT ON COLUMN users.is_internal IS
    'True for staff/test accounts. Excluded from customer-facing analytics by '
    'default (see semantic/defaults.yaml.';
COMMENT ON COLUMN users.deleted_at IS 'Soft-delete marker. NULL means the account is active.';

CREATE INDEX idx_users_customer_id ON users(customer_id);

CREATE TABLE addresses (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES customers(id),
    line1       text NOT NULL,
    line2       text,
    city        text NOT NULL,
    region      text,
    postal_code text,
    country     text NOT NULL,
    is_default  boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_addresses_customer_id ON addresses(customer_id);

CREATE TABLE products (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_id  bigint NOT NULL REFERENCES categories(id),
    name         text NOT NULL,
    price        numeric(10,2) NOT NULL CHECK (price >= 0),
    created_at   timestamptz NOT NULL DEFAULT now(),
    deleted_at   timestamptz
);
COMMENT ON COLUMN products.price IS
    'Current list price. Prices change over time; do not use this for historical '
    'revenue -- use order_items.unit_price, the price actually charged.';
COMMENT ON COLUMN products.deleted_at IS 'Soft-delete marker for discontinued products. NULL means active.';
CREATE INDEX idx_products_category_id ON products(category_id);

CREATE TABLE orders (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id         bigint NOT NULL REFERENCES customers(id),
    user_id             bigint NOT NULL REFERENCES users(id),
    shipping_address_id bigint REFERENCES addresses(id),
    status              text NOT NULL CHECK (status IN ('pending','paid','shipped','delivered','cancelled','returned')),
    created_at          timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE orders IS 'Order header grain: one row per order. See order_items for line grain.';
COMMENT ON COLUMN orders.customer_id IS 'The owning customer entity, denormalized from users.customer_id for convenience.';
COMMENT ON COLUMN orders.user_id IS 'The login account that placed the order.';
COMMENT ON COLUMN orders.status IS
    'pending, paid, shipped, delivered, cancelled, returned. Whether cancelled/returned '
    'orders count as sales is a policy decision, not implicit in the schema.';

CREATE INDEX idx_orders_customer_id ON orders(customer_id);
CREATE INDEX idx_orders_user_id ON orders(user_id);
CREATE INDEX idx_orders_created_at ON orders(created_at);
CREATE INDEX idx_orders_status ON orders(status);

CREATE TABLE order_items (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES orders(id),
    product_id  bigint NOT NULL REFERENCES products(id),
    quantity    integer NOT NULL CHECK (quantity > 0),
    unit_price  numeric(10,2) NOT NULL CHECK (unit_price >= 0)
);
COMMENT ON TABLE order_items IS 'Order line grain: one row per product per order.';
COMMENT ON COLUMN order_items.unit_price IS
    'Price actually charged at time of purchase, in the base currency. May differ '
    'from the current products.price.';

CREATE INDEX idx_order_items_order_id ON order_items(order_id);
CREATE INDEX idx_order_items_product_id ON order_items(product_id);

CREATE TABLE payments (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES orders(id),
    amount      numeric(10,2) NOT NULL CHECK (amount >= 0),
    currency    char(3) NOT NULL DEFAULT 'USD',
    status      text NOT NULL DEFAULT 'succeeded' CHECK (status IN ('succeeded','failed','pending')),
    paid_at     timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN payments.currency IS
    'ISO 4217 currency code. amount is denominated in this currency -- do not sum '
    'payments.amount across rows without converting to a common currency first.';
COMMENT ON COLUMN payments.status IS 'Only succeeded payments represent realized revenue.';

CREATE INDEX idx_payments_order_id ON payments(order_id);
CREATE INDEX idx_payments_paid_at ON payments(paid_at);

CREATE TABLE refunds (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id      bigint NOT NULL REFERENCES orders(id),
    order_item_id bigint REFERENCES order_items(id),
    amount        numeric(10,2) NOT NULL CHECK (amount >= 0),
    currency      char(3) NOT NULL DEFAULT 'USD',
    reason        text,
    refunded_at   timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE refunds IS 'Refunds against an order. Partial refunds allowed -- amount may be less than the order total.';
COMMENT ON COLUMN refunds.order_item_id IS 'NULL for order-level refunds; set for refunds against a specific line item.';

CREATE INDEX idx_refunds_order_id ON refunds(order_id);

CREATE TABLE sessions (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users(id),
    started_at  timestamptz NOT NULL,
    ended_at    timestamptz
);
COMMENT ON TABLE sessions IS
    'Browsing/login sessions, independent of purchases. Basis for engagement '
    'metrics like session_count and repeat-visit rate, distinct from order-based metrics.';

CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_sessions_started_at ON sessions(started_at);
