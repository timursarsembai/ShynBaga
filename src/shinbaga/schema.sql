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
