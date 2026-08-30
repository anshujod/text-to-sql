"""Deterministic synthetic seed data generator.

Generates ~18 months of e-commerce history with the ambiguity traps:

- customer "archetypes" (whale / frequent_bargain / internal / normal) drive
  order count and order value independently, so top-10-by-revenue and
  top-10-by-order-count are different customer pools
- session engagement is generated from an independent trait, so
  top-10-by-revenue and top-10-by-session-count also diverge
- a Black Friday spike sits in the calendar month before SEED_END_DATE but
  outside the trailing-30-day window, so "last month" is genuinely ambiguous
- per-product price drift means order_items.unit_price and products.price
  (the current price) diverge for older orders
- a refund rate tuned to remove ~5-15% of gross revenue

Fully deterministic: every source of randomness is drawn from `RNG`, a single
numpy Generator seeded with SEED. Re-running produces byte-identical output.
Safe to re-run: TRUNCATEs and RESTARTs identities before inserting.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import psycopg
from faker import Faker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from t2sql.db.connection import get_connection  # noqa: E402

SEED = 42
RNG = np.random.default_rng(SEED)
FAKE = Faker()
Faker.seed(SEED)

# 18 months of order history. Fixed (not wall-clock) so the data -- and the
# "last month" boundary trap below -- is reproducible forever. This doubles as
# the intended anchor for "last month" in the semantic layer:
# anchor to max(orders.created_at), not now().
SEED_END_DATE = date(2025, 12, 31)
START_DATE = date(2024, 7, 1)
TOTAL_DAYS = (SEED_END_DATE - START_DATE).days + 1

# Black Friday weekend sits in November -- the calendar month before
# SEED_END_DATE -- but outside the trailing-30-day window (which is all of
# December). That's what makes "last month" materially ambiguous.
BLACK_FRIDAY_START = date(2025, 11, 27)
BLACK_FRIDAY_END = date(2025, 11, 30)

N_CUSTOMERS = 5000
N_PRODUCTS = 600
N_CATEGORIES_TARGET = 40000  # orders target, kept as a named budget below

CATEGORIES: list[tuple[str, float, float]] = [
    ("Electronics", 15, 1200),
    ("Home & Kitchen", 8, 400),
    ("Apparel", 10, 150),
    ("Books", 5, 45),
    ("Beauty", 5, 90),
    ("Sports & Outdoors", 10, 350),
    ("Toys & Games", 5, 120),
    ("Grocery", 2, 60),
    ("Office Supplies", 3, 150),
    ("Pet Supplies", 5, 100),
    ("Automotive", 10, 500),
    ("Health & Wellness", 5, 120),
    ("Garden & Outdoor", 8, 300),
    ("Furniture", 30, 1500),
]

ADJECTIVES = [
    "Sleek", "Compact", "Premium", "Classic", "Eco", "Portable", "Deluxe",
    "Rustic", "Modern", "Essential", "Pro", "Ultra", "Everyday", "Signature",
    "Heavy-Duty", "Foldable", "Wireless", "Reusable",
]

COUNTRIES = [
    # (country, currency, weight)
    ("US", "USD", 0.85),
    ("CA", "CAD", 0.05),
    ("GB", "GBP", 0.04),
    ("DE", "EUR", 0.03),
    ("FR", "EUR", 0.02),
    ("AU", "AUD", 0.01),
]

DOW_MULTIPLIER = [0.85, 0.85, 0.90, 1.0, 1.05, 1.35, 1.25]  # Mon..Sun


def day_weights(start: date, end: date, spike_start: date | None, spike_end: date | None, spike_mult: float) -> tuple[list[date], np.ndarray]:
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    weights = np.array([DOW_MULTIPLIER[d.weekday()] for d in days], dtype=float)
    if spike_start is not None:
        for i, d in enumerate(days):
            if spike_start <= d <= spike_end:
                weights[i] *= spike_mult
    # mild linear growth trend: the business is growing over the window
    growth = np.linspace(0.8, 1.2, num=len(days))
    weights *= growth
    weights /= weights.sum()
    return days, weights


ALL_DAYS, ALL_DAY_WEIGHTS = day_weights(START_DATE, SEED_END_DATE, BLACK_FRIDAY_START, BLACK_FRIDAY_END, spike_mult=6.0)


def random_datetime_on(d: date) -> datetime:
    hour = int(RNG.choice(range(7, 23), p=_hour_weights()))
    minute = int(RNG.integers(0, 60))
    second = int(RNG.integers(0, 60))
    return datetime.combine(d, time(hour, minute, second))


_HOUR_WEIGHTS_CACHE: np.ndarray | None = None


def _hour_weights() -> np.ndarray:
    global _HOUR_WEIGHTS_CACHE
    if _HOUR_WEIGHTS_CACHE is None:
        hours = np.arange(7, 23)
        # peak around lunch and evening
        w = 1.0 + 0.8 * np.exp(-((hours - 12) ** 2) / 8) + 1.2 * np.exp(-((hours - 20) ** 2) / 10)
        _HOUR_WEIGHTS_CACHE = w / w.sum()
    return _HOUR_WEIGHTS_CACHE


def sample_days(n: int) -> list[date]:
    idx = RNG.choice(len(ALL_DAYS), size=n, p=ALL_DAY_WEIGHTS)
    return [ALL_DAYS[i] for i in idx]


# ---------------------------------------------------------------------------
# Categories & products
# ---------------------------------------------------------------------------


@dataclass
class Product:
    id: int
    category_id: int
    name: str
    price: float
    price_growth: float  # total fractional growth over the window
    created_at: datetime
    deleted_at: datetime | None
    tier: str  # "cheap" | "mid" | "expensive"


def build_categories() -> list[tuple[int, str]]:
    return [(i + 1, name) for i, (name, _, _) in enumerate(CATEGORIES)]


def build_products(categories: list[tuple[int, str]]) -> list[Product]:
    products: list[Product] = []
    cat_price_range = {cid: (lo, hi) for (cid, _), (_, lo, hi) in zip(categories, CATEGORIES)}
    counter = 1
    per_cat = N_PRODUCTS // len(categories)
    remainder = N_PRODUCTS - per_cat * len(categories)
    for i, (cid, cname) in enumerate(categories):
        n = per_cat + (1 if i < remainder else 0)
        for _ in range(n):
            lo, hi = cat_price_range[cid]
            price = round(float(np.exp(RNG.uniform(np.log(lo), np.log(hi)))), 2)
            adjective = ADJECTIVES[RNG.integers(0, len(ADJECTIVES))]
            name = f"{adjective} {cname.split(' & ')[0]} #{counter}"
            created_at = datetime.combine(
                START_DATE - timedelta(days=int(RNG.integers(30, 400))), time(9, 0)
            )
            deleted_at = None
            if RNG.random() < 0.05:
                deleted_at = random_datetime_on(
                    START_DATE + timedelta(days=int(RNG.integers(60, TOTAL_DAYS)))
                )
            price_growth = float(RNG.uniform(0.05, 0.35))
            products.append(
                Product(counter, cid, name, price, price_growth, created_at, deleted_at, "")
            )
            counter += 1
    prices_sorted = sorted(p.price for p in products)
    lo_cut = prices_sorted[len(prices_sorted) // 3]
    hi_cut = prices_sorted[2 * len(prices_sorted) // 3]
    for p in products:
        p.tier = "cheap" if p.price <= lo_cut else ("expensive" if p.price >= hi_cut else "mid")
    return products


def unit_price_at(product: Product, order_date: date) -> float:
    price_at_start = product.price / (1 + product.price_growth)
    frac = (order_date - START_DATE).days / TOTAL_DAYS
    base = price_at_start + (product.price - price_at_start) * frac
    noise = RNG.uniform(-0.03, 0.03)
    return round(max(base * (1 + noise), 0.5), 2)


# ---------------------------------------------------------------------------
# Customers, users, addresses
# ---------------------------------------------------------------------------


@dataclass
class Customer:
    id: int
    name: str
    created_at: datetime
    archetype: str
    country: str
    currency: str


@dataclass
class User:
    id: int
    customer_id: int
    email: str
    is_internal: bool
    created_at: datetime
    deleted_at: datetime | None


@dataclass
class Address:
    id: int
    customer_id: int
    line1: str
    line2: str | None
    city: str
    region: str | None
    postal_code: str | None
    country: str
    is_default: bool
    created_at: datetime


N_INTERNAL = 6
N_WHALE = int(N_CUSTOMERS * 0.02)
N_BARGAIN = int(N_CUSTOMERS * 0.05)


def pick_country() -> tuple[str, str]:
    countries = [c for c, _, _ in COUNTRIES]
    weights = np.array([w for _, _, w in COUNTRIES])
    idx = RNG.choice(len(countries), p=weights / weights.sum())
    country, currency, _ = COUNTRIES[idx]
    return country, currency


def build_customers() -> list[Customer]:
    archetypes = (
        ["internal"] * N_INTERNAL
        + ["whale"] * N_WHALE
        + ["frequent_bargain"] * N_BARGAIN
    )
    archetypes += ["normal"] * (N_CUSTOMERS - len(archetypes))
    RNG.shuffle(archetypes)

    customers = []
    for i in range(N_CUSTOMERS):
        country, currency = pick_country()
        signup_offset = int(RNG.integers(0, 180))
        if RNG.random() < 0.3:
            created_at = datetime.combine(
                START_DATE + timedelta(days=int(RNG.integers(0, TOTAL_DAYS - 30))), time(10, 0)
            )
        else:
            created_at = datetime.combine(START_DATE - timedelta(days=signup_offset), time(10, 0))
        name = FAKE.company() if RNG.random() < 0.1 else FAKE.name()
        customers.append(Customer(i + 1, name, created_at, archetypes[i], country, currency))
    return customers


def build_users_and_addresses(customers: list[Customer]) -> tuple[list[User], list[Address], dict[int, list[int]]]:
    users: list[User] = []
    addresses: list[Address] = []
    users_by_customer: dict[int, list[int]] = {}
    next_user_id = 1
    next_addr_id = 1

    for c in customers:
        if c.archetype == "internal":
            n_users = 1
        else:
            n_users = 1 + int(RNG.poisson(0.55))
            n_users = min(n_users, 4)

        my_user_ids = []
        for j in range(n_users):
            created_at = c.created_at + timedelta(days=int(RNG.integers(0, 30)) * j)
            deleted_at = None
            if c.archetype != "internal" and RNG.random() < 0.03:
                deleted_at = created_at + timedelta(days=int(RNG.integers(60, 500)))
            email = FAKE.unique.email()
            users.append(
                User(
                    next_user_id,
                    c.id,
                    email,
                    is_internal=(c.archetype == "internal"),
                    created_at=created_at,
                    deleted_at=deleted_at,
                )
            )
            my_user_ids.append(next_user_id)
            next_user_id += 1
        users_by_customer[c.id] = my_user_ids

        n_addr = 1 if RNG.random() < 0.8 else 2
        for k in range(n_addr):
            addresses.append(
                Address(
                    next_addr_id,
                    c.id,
                    FAKE.street_address(),
                    FAKE.secondary_address() if RNG.random() < 0.15 else None,
                    FAKE.city(),
                    FAKE.state_abbr() if c.country == "US" else None,
                    FAKE.postcode(),
                    c.country,
                    is_default=(k == 0),
                    created_at=c.created_at,
                )
            )
            next_addr_id += 1

    return users, addresses, users_by_customer


# ---------------------------------------------------------------------------
# Orders, order items, payments, refunds
# ---------------------------------------------------------------------------


@dataclass
class Order:
    id: int
    customer_id: int
    user_id: int
    shipping_address_id: int | None
    status: str
    created_at: datetime
    archetype: str


ARCHETYPE_ORDER_COUNT = {
    "internal": (200, 350),
    "whale": (4, 16),
    "frequent_bargain": (20, 45),
    "normal": (0, 14),
}

ARCHETYPE_ITEM_COUNT = {
    "internal": (1, 1),
    "whale": (3, 8),
    "frequent_bargain": (1, 3),
    "normal": (1, 5),
}

ARCHETYPE_TIER_WEIGHTS = {
    "internal": {"cheap": 1.0, "mid": 0.0, "expensive": 0.0},
    "whale": {"cheap": 0.05, "mid": 0.25, "expensive": 0.70},
    "frequent_bargain": {"cheap": 0.85, "mid": 0.15, "expensive": 0.0},
    "normal": {"cheap": 0.4, "mid": 0.4, "expensive": 0.2},
}

STATUS_WEIGHTS = [
    ("delivered", 0.62),
    ("paid", 0.10),
    ("shipped", 0.08),
    ("pending", 0.05),
    ("cancelled", 0.07),
    ("returned", 0.08),
]


def pick_status() -> str:
    statuses = [s for s, _ in STATUS_WEIGHTS]
    weights = np.array([w for _, w in STATUS_WEIGHTS])
    return statuses[RNG.choice(len(statuses), p=weights / weights.sum())]


def build_orders(customers: list[Customer], users_by_customer: dict[int, list[int]], addresses_by_customer: dict[int, list[int]]) -> list[Order]:
    plan: list[tuple[Customer, int]] = []
    for c in customers:
        lo, hi = ARCHETYPE_ORDER_COUNT[c.archetype]
        if c.archetype == "normal":
            # negative_binomial(r, p) has mean r*(1-p)/p; parameterizing
            # p = r/(r+target_mean) makes `target_mean` the actual mean,
            # independent of r (r only shapes the dispersion/overdispersion).
            r, target_mean = 2.2, 6.3
            n = int(RNG.negative_binomial(r, r / (r + target_mean)))
            n = min(n, 45)
        else:
            n = int(RNG.integers(lo, hi + 1))
        plan.append((c, n))

    total = sum(n for _, n in plan)
    dates = sample_days(total)
    RNG.shuffle(dates)

    orders: list[Order] = []
    next_id = 1
    date_cursor = 0
    for c, n in plan:
        my_users = users_by_customer[c.id]
        my_addrs = addresses_by_customer.get(c.id, [])
        for _ in range(n):
            d = dates[date_cursor]
            date_cursor += 1
            created_at = random_datetime_on(d)
            status = pick_status()
            # very recent orders can't already be "delivered"
            if (SEED_END_DATE - d).days < 3 and status in ("delivered", "returned"):
                status = "shipped"
            user_id = int(RNG.choice(my_users))
            addr_id = int(RNG.choice(my_addrs)) if my_addrs else None
            orders.append(
                Order(next_id, c.id, user_id, addr_id, status, created_at, c.archetype)
            )
            next_id += 1
    return orders


def pick_product_for_archetype(products_by_tier: dict[str, list[Product]], archetype: str) -> Product:
    weights = ARCHETYPE_TIER_WEIGHTS[archetype]
    tiers = list(weights.keys())
    w = np.array([weights[t] for t in tiers])
    tier = tiers[RNG.choice(len(tiers), p=w / w.sum())]
    pool = products_by_tier[tier]
    return pool[RNG.integers(0, len(pool))]


@dataclass
class OrderItem:
    id: int
    order_id: int
    product_id: int
    quantity: int
    unit_price: float


@dataclass
class Payment:
    id: int
    order_id: int
    amount: float
    currency: str
    status: str
    paid_at: datetime


@dataclass
class Refund:
    id: int
    order_id: int
    order_item_id: int | None
    amount: float
    currency: str
    reason: str | None
    refunded_at: datetime


PAID_STATUSES = {"paid", "shipped", "delivered", "returned"}


def build_order_items_payments_refunds(
    orders: list[Order], products: list[Product], customers_by_id: dict[int, Customer]
) -> tuple[list[OrderItem], list[Payment], list[Refund]]:
    products_by_tier: dict[str, list[Product]] = {"cheap": [], "mid": [], "expensive": []}
    for p in products:
        products_by_tier[p.tier].append(p)

    order_items: list[OrderItem] = []
    payments: list[Payment] = []
    refunds: list[Refund] = []
    next_item_id = 1
    next_payment_id = 1
    next_refund_id = 1

    for order in orders:
        lo, hi = ARCHETYPE_ITEM_COUNT[order.archetype]
        n_items = int(RNG.integers(lo, hi + 1))
        order_date = order.created_at.date()
        my_items: list[OrderItem] = []
        order_total = 0.0
        for _ in range(n_items):
            product = pick_product_for_archetype(products_by_tier, order.archetype)
            qty = 1
            if order.archetype == "whale" and RNG.random() < 0.4:
                qty = int(RNG.integers(2, 7))
            elif order.archetype == "normal" and RNG.random() < 0.15:
                qty = int(RNG.integers(2, 4))
            unit_price = unit_price_at(product, order_date)
            item = OrderItem(next_item_id, order.id, product.id, qty, unit_price)
            order_items.append(item)
            my_items.append(item)
            order_total += unit_price * qty
            next_item_id += 1

        if order.status not in PAID_STATUSES:
            if order.status == "cancelled" and RNG.random() < 0.3:
                payments.append(
                    Payment(
                        next_payment_id,
                        order.id,
                        round(order_total, 2),
                        customers_by_id[order.customer_id].currency,
                        "failed",
                        order.created_at,
                    )
                )
                next_payment_id += 1
            continue

        currency = customers_by_id[order.customer_id].currency
        paid_at = order.created_at + timedelta(hours=float(RNG.uniform(0, 2)))
        payments.append(
            Payment(next_payment_id, order.id, round(order_total, 2), currency, "succeeded", paid_at)
        )
        next_payment_id += 1

        if order.status == "returned":
            refund_frac = float(RNG.uniform(0.65, 1.0))
            refunds.append(
                Refund(
                    next_refund_id,
                    order.id,
                    None,
                    round(order_total * refund_frac, 2),
                    currency,
                    "customer return",
                    paid_at + timedelta(days=float(RNG.uniform(2, 20))),
                )
            )
            next_refund_id += 1
        elif RNG.random() < 0.08:
            item = my_items[int(RNG.integers(0, len(my_items)))]
            item_value = item.unit_price * item.quantity
            refund_frac = float(RNG.uniform(0.3, 1.0))
            refunds.append(
                Refund(
                    next_refund_id,
                    order.id,
                    item.id,
                    round(item_value * refund_frac, 2),
                    currency,
                    "partial refund",
                    paid_at + timedelta(days=float(RNG.uniform(1, 15))),
                )
            )
            next_refund_id += 1

    return order_items, payments, refunds


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@dataclass
class Session:
    id: int
    user_id: int
    started_at: datetime
    ended_at: datetime | None


def build_sessions(users: list[User], customers_by_id: dict[int, Customer]) -> list[Session]:
    # engagement is independent of spend archetype -- deliberately so that
    # top-10-by-session-count diverges from top-10-by-revenue.
    n_users = len(users)
    engagement = RNG.exponential(1.0, size=n_users)
    weights = engagement / engagement.sum()
    total_sessions = 60000
    user_idx = RNG.choice(n_users, size=total_sessions, p=weights)
    days = sample_days(total_sessions)

    sessions: list[Session] = []
    for i in range(total_sessions):
        user = users[user_idx[i]]
        started_at = random_datetime_on(days[i])
        if started_at < user.created_at:
            started_at = user.created_at + timedelta(hours=float(RNG.uniform(0, 48)))
        ended_at = None
        if RNG.random() > 0.05:
            ended_at = started_at + timedelta(minutes=float(RNG.uniform(1, 90)))
        sessions.append(Session(i + 1, user.id, started_at, ended_at))
    return sessions


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

TRUNCATE_SQL = """
TRUNCATE TABLE sessions, refunds, payments, order_items, orders, addresses,
    users, products, categories, customers
RESTART IDENTITY CASCADE;
"""


def load(conn: psycopg.Connection) -> None:
    print("Truncating existing data...")
    with conn.cursor() as cur:
        cur.execute(TRUNCATE_SQL)
    conn.commit()

    print("Building categories & products...")
    categories = build_categories()
    products = build_products(categories)

    print("Building customers, users, addresses...")
    customers = build_customers()
    users, addresses, users_by_customer = build_users_and_addresses(customers)
    addresses_by_customer: dict[int, list[int]] = {}
    for a in addresses:
        addresses_by_customer.setdefault(a.customer_id, []).append(a.id)
    customers_by_id = {c.id: c for c in customers}

    print("Building orders...")
    orders = build_orders(customers, users_by_customer, addresses_by_customer)

    print("Building order_items, payments, refunds...")
    order_items, payments, refunds = build_order_items_payments_refunds(orders, products, customers_by_id)

    print("Building sessions...")
    sessions = build_sessions(users, customers_by_id)

    print(
        f"Rows: categories={len(categories)} products={len(products)} "
        f"customers={len(customers)} users={len(users)} addresses={len(addresses)} "
        f"orders={len(orders)} order_items={len(order_items)} payments={len(payments)} "
        f"refunds={len(refunds)} sessions={len(sessions)}"
    )

    with conn.cursor() as cur:
        with cur.copy("COPY categories (name, created_at) FROM STDIN") as copy:
            for _, name in categories:
                copy.write_row((name, START_DATE - timedelta(days=400)))

        with cur.copy(
            "COPY products (category_id, name, price, created_at, deleted_at) FROM STDIN"
        ) as copy:
            for p in products:
                copy.write_row((p.category_id, p.name, p.price, p.created_at, p.deleted_at))

        with cur.copy("COPY customers (name, created_at) FROM STDIN") as copy:
            for c in customers:
                copy.write_row((c.name, c.created_at))

        with cur.copy(
            "COPY users (customer_id, email, is_internal, created_at, deleted_at) FROM STDIN"
        ) as copy:
            for u in users:
                copy.write_row((u.customer_id, u.email, u.is_internal, u.created_at, u.deleted_at))

        with cur.copy(
            "COPY addresses (customer_id, line1, line2, city, region, postal_code, "
            "country, is_default, created_at) FROM STDIN"
        ) as copy:
            for a in addresses:
                copy.write_row(
                    (a.customer_id, a.line1, a.line2, a.city, a.region, a.postal_code,
                     a.country, a.is_default, a.created_at)
                )

        with cur.copy(
            "COPY orders (customer_id, user_id, shipping_address_id, status, created_at) FROM STDIN"
        ) as copy:
            for o in orders:
                copy.write_row((o.customer_id, o.user_id, o.shipping_address_id, o.status, o.created_at))

        with cur.copy(
            "COPY order_items (order_id, product_id, quantity, unit_price) FROM STDIN"
        ) as copy:
            for oi in order_items:
                copy.write_row((oi.order_id, oi.product_id, oi.quantity, oi.unit_price))

        with cur.copy(
            "COPY payments (order_id, amount, currency, status, paid_at) FROM STDIN"
        ) as copy:
            for pay in payments:
                copy.write_row((pay.order_id, pay.amount, pay.currency, pay.status, pay.paid_at))

        with cur.copy(
            "COPY refunds (order_id, order_item_id, amount, currency, reason, refunded_at) FROM STDIN"
        ) as copy:
            for r in refunds:
                copy.write_row((r.order_id, r.order_item_id, r.amount, r.currency, r.reason, r.refunded_at))

        with cur.copy(
            "COPY sessions (user_id, started_at, ended_at) FROM STDIN"
        ) as copy:
            for s in sessions:
                copy.write_row((s.user_id, s.started_at, s.ended_at))

    conn.commit()
    print("Done.")


def main() -> None:
    with get_connection(role="owner") as conn:
        load(conn)


if __name__ == "__main__":
    main()
