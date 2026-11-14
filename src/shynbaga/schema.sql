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


-- The catalog tree. Popularity is the marketplace's own figure, as given in the
-- answer for the node itself or its parent (the root's answer understates it).
-- is_leaf is null until a check showed whether the node has children.
CREATE TABLE IF NOT EXISTS categories (
    marketplace    text NOT NULL,
    code           text NOT NULL,
    parent_code    text,
    title          text NOT NULL,
    depth          integer NOT NULL,
    products_count integer NOT NULL DEFAULT 0,
    popularity     bigint NOT NULL DEFAULT 0,
    is_leaf        boolean,
    checked_at     timestamptz,
    -- No longer listed under its parent.
    gone           boolean NOT NULL DEFAULT false,
    swept_at       timestamptz,
    swept_products integer,
    PRIMARY KEY (marketplace, code)
);

-- The lowest price a listing card showed, one row per stretch of an unchanged value,
-- like price_intervals but without a seller. This covers the whole catalog.
CREATE TABLE IF NOT EXISTS card_prices (
    id            bigserial PRIMARY KEY,
    product_id    bigint NOT NULL REFERENCES products (id),
    region        text NOT NULL,
    price         numeric(14, 2) NOT NULL CHECK (price > 0),
    list_price    numeric(14, 2),
    first_seen_at timestamptz NOT NULL,
    last_seen_at  timestamptz NOT NULL,
    is_current    boolean NOT NULL DEFAULT true
);
CREATE UNIQUE INDEX IF NOT EXISTS card_prices_one_current
    ON card_prices (product_id, region) WHERE is_current;
CREATE INDEX IF NOT EXISTS card_prices_history
    ON card_prices (product_id, region, first_seen_at);

-- The latest listing card, so the seller layer can snapshot it without a re-read.
ALTER TABLE products ADD COLUMN IF NOT EXISTS category_code text;
ALTER TABLE products ADD COLUMN IF NOT EXISTS position integer;
ALTER TABLE products ADD COLUMN IF NOT EXISTS card jsonb;
ALTER TABLE products ADD COLUMN IF NOT EXISTS card_price numeric(14, 2);
ALTER TABLE products ADD COLUMN IF NOT EXISTS card_seen_at timestamptz;
-- When sellers' prices were last fetched; last_seen_at now also moves with listings.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM information_schema.columns
        WHERE table_name = 'products' AND column_name = 'offers_checked_at'
    ) THEN
        ALTER TABLE products ADD COLUMN offers_checked_at timestamptz;
        UPDATE products SET offers_checked_at = last_seen_at;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS products_category ON products (category_code);

-- What a catalog round did beyond the products whose sellers it checked.
ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS cards_seen integer NOT NULL DEFAULT 0;
ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS categories_checked integer NOT NULL DEFAULT 0;
ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS leaves_swept integer NOT NULL DEFAULT 0;

-- Per-product summaries, kept up by the collector, so admin lists stay fast as the
-- catalog grows: sellers' current prices, how many price stretches there were, how
-- many times the card price changed, and the popularity estimate for sorting.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM information_schema.columns
        WHERE table_name = 'products' AND column_name = 'sellers_now'
    ) THEN
        ALTER TABLE products
            ADD COLUMN offers_min_price numeric(14, 2),
            ADD COLUMN offers_max_price numeric(14, 2),
            ADD COLUMN sellers_now integer NOT NULL DEFAULT 0,
            ADD COLUMN offer_intervals integer NOT NULL DEFAULT 0,
            ADD COLUMN card_changes integer NOT NULL DEFAULT 0,
            ADD COLUMN score double precision;
        UPDATE products p SET
            offers_min_price = s.min_price, offers_max_price = s.max_price,
            sellers_now = s.sellers_now, offer_intervals = s.intervals
        FROM (
            SELECT product_id, min(price) FILTER (WHERE is_current) AS min_price,
                   max(price) FILTER (WHERE is_current) AS max_price,
                   count(*) FILTER (WHERE is_current) AS sellers_now, count(*) AS intervals
            FROM price_intervals GROUP BY product_id
        ) s
        WHERE s.product_id = p.id;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS products_score ON products (score DESC NULLS LAST, id);

-- Parts of a leaf too big to list whole (Kaspi serves ~3600 cards per listing).
-- A part is the leaf's listing with filter terms appended, such as a brand and a
-- price range; a part still too big is split again. Popularity is an estimate: the
-- parent's share by product count. Only unsplit parts are swept.
CREATE TABLE IF NOT EXISTS category_segments (
    id             bigserial PRIMARY KEY,
    marketplace    text NOT NULL,
    category_code  text NOT NULL,
    filters        text NOT NULL,
    -- Filter ids used in `filters`: a split never uses one twice.
    facet_ids      text[] NOT NULL,
    parent_id      bigint REFERENCES category_segments (id),
    title          text NOT NULL,
    products_count integer NOT NULL,
    popularity     bigint NOT NULL,
    -- Null until checked; true when split into parts, false when it cannot be.
    is_split       boolean,
    checked_at     timestamptz,
    gone           boolean NOT NULL DEFAULT false,
    swept_at       timestamptz,
    swept_products integer,
    UNIQUE (marketplace, category_code, filters)
);
ALTER TABLE categories ADD COLUMN IF NOT EXISTS split_checked_at timestamptz;
-- A part waits for its first sweep from when it was made, not from "30 days ago":
-- a split makes up to 255 parts at once, which would otherwise jump the queue.
ALTER TABLE category_segments ADD COLUMN IF NOT EXISTS created_at timestamptz;
CREATE INDEX IF NOT EXISTS category_segments_parent ON category_segments (parent_id);
-- When `position` was set: cards from parts carry no place in the whole leaf.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM information_schema.columns
        WHERE table_name = 'products' AND column_name = 'ranked_at'
    ) THEN
        ALTER TABLE products ADD COLUMN ranked_at timestamptz;
        UPDATE products SET ranked_at = card_seen_at WHERE position IS NOT NULL;
    END IF;
END $$;

-- The admin site reads through its own role, created once by hand with a password.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'shynbaga_web') THEN
        GRANT USAGE ON SCHEMA public TO shynbaga_web;
        GRANT SELECT ON ALL TABLES IN SCHEMA public TO shynbaga_web;
    END IF;
END $$;
