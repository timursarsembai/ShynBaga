CREATE TABLE IF NOT EXISTS products (
    id            bigserial PRIMARY KEY,
    marketplace   text NOT NULL,
    external_id   text NOT NULL,
    title         text NOT NULL,
    brand         text,
    category_path text[] NOT NULL DEFAULT '{}',
    url           text,
    image_url     text,
    first_seen_at timestamptz NOT NULL,
    last_seen_at  timestamptz NOT NULL,
    UNIQUE (marketplace, external_id)
);

CREATE TABLE IF NOT EXISTS sellers (
    id            bigserial PRIMARY KEY,
    marketplace   text NOT NULL,
    external_id   text NOT NULL,
    name          text NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at  timestamptz NOT NULL,
    UNIQUE (marketplace, external_id)
);

-- One row per stretch of an unchanged price. A price was observed at both ends;
-- between them it is assumed unchanged, so a long collection gap ends the row.
CREATE TABLE IF NOT EXISTS price_intervals (
    id            bigserial PRIMARY KEY,
    product_id    bigint NOT NULL REFERENCES products (id),
    seller_id     bigint NOT NULL REFERENCES sellers (id),
    region        text NOT NULL,
    price         numeric(14, 2) NOT NULL CHECK (price > 0),
    currency      char(3) NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at  timestamptz NOT NULL,
    is_current    boolean NOT NULL DEFAULT true
);

CREATE UNIQUE INDEX IF NOT EXISTS price_intervals_one_current
    ON price_intervals (product_id, seller_id, region) WHERE is_current;
CREATE INDEX IF NOT EXISTS price_intervals_history
    ON price_intervals (product_id, region, first_seen_at);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id              bigserial PRIMARY KEY,
    marketplace     text NOT NULL,
    category        text NOT NULL,
    region          text NOT NULL,
    started_at      timestamptz NOT NULL,
    finished_at     timestamptz,
    status          text NOT NULL DEFAULT 'running',
    products_ok     integer NOT NULL DEFAULT 0,
    products_failed integer NOT NULL DEFAULT 0,
    bytes_received  bigint NOT NULL DEFAULT 0,
    error           text
);

ALTER TABLE sellers ADD COLUMN IF NOT EXISTS url text;
ALTER TABLE products ADD COLUMN IF NOT EXISTS offers_ref text;
-- Set when someone looked the product up: it stays tracked outside the crawled top.
ALTER TABLE products ADD COLUMN IF NOT EXISTS requested_at timestamptz;

-- Analytics: what a listing card showed at each crawl. Card fields are null for
-- products fetched outside a crawl, such as a lookup.
ALTER TABLE products ADD COLUMN IF NOT EXISTS listed_at timestamptz;
CREATE TABLE IF NOT EXISTS product_snapshots (
    id               bigserial PRIMARY KEY,
    product_id       bigint NOT NULL REFERENCES products (id),
    run_id           bigint REFERENCES crawl_runs (id),
    region           text NOT NULL,
    observed_at      timestamptz NOT NULL,
    position         integer,
    rating           numeric(3, 2),
    reviews_count    integer,
    price            numeric(14, 2),
    list_price       numeric(14, 2),
    price_with_bonus numeric(14, 2),
    delivery         text,
    promos           jsonb NOT NULL DEFAULT '[]',
    offers_total     integer,
    offers_seen      integer NOT NULL
);
CREATE INDEX IF NOT EXISTS product_snapshots_history
    ON product_snapshots (product_id, region, observed_at);

-- A seller's rating and review count, the latest value of each day.
CREATE TABLE IF NOT EXISTS seller_snapshots (
    seller_id     bigint NOT NULL REFERENCES sellers (id),
    observed_on   date NOT NULL,
    rating        numeric(3, 2),
    reviews_count integer,
    PRIMARY KEY (seller_id, observed_on)
);

-- Seller pages, refreshed about weekly. No phone numbers: personal data for sole traders.
ALTER TABLE sellers ADD COLUMN IF NOT EXISTS joined_on date;
ALTER TABLE sellers ADD COLUMN IF NOT EXISTS logo_url text;
ALTER TABLE sellers ADD COLUMN IF NOT EXISTS profile_checked_at timestamptz;
CREATE TABLE IF NOT EXISTS seller_profiles (
    seller_id         bigint NOT NULL REFERENCES sellers (id),
    observed_on       date NOT NULL,
    state             text,
    score             numeric(3, 2),
    cancelled_pct     numeric(6, 3),
    returned_pct      numeric(6, 3),
    late_delivery_pct numeric(6, 3),
    orders_segment    integer,
    PRIMARY KEY (seller_id, observed_on)
);

-- The admin site reads through its own role, created once by hand with a password.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'shynbaga_web') THEN
        GRANT USAGE ON SCHEMA public TO shynbaga_web;
        GRANT SELECT ON ALL TABLES IN SCHEMA public TO shynbaga_web;
    END IF;
END $$;
