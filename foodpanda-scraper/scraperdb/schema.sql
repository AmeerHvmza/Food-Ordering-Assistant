CREATE TABLE IF NOT EXISTS restaurants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT UNIQUE NOT NULL,
    rating REAL,
    cuisine TEXT,
    address TEXT,
    delivery_time TEXT,
    image_url TEXT,
    review_count INTEGER,
    delivery_areas TEXT,
    scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS menu_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL,
    category_name TEXT NOT NULL,
    FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
);

CREATE TABLE IF NOT EXISTS menu_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    price TEXT,
    original_price TEXT,
    description TEXT,
    image_url TEXT,
    updated_at TEXT,
    FOREIGN KEY (category_id) REFERENCES menu_categories(id)
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,
    restaurants_ok INTEGER DEFAULT 0,
    restaurants_failed INTEGER DEFAULT 0,
    restaurants_skipped INTEGER DEFAULT 0,
    listing_only INTEGER DEFAULT 0,
    log_path TEXT,
    note TEXT
);

-- Manual review text sample (not scraped). See plans/REVIEWS_IMPORT_PLAN.md.
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL,
    review_text TEXT NOT NULL,
    liked_dishes TEXT,
    owner_response TEXT,
    source TEXT NOT NULL DEFAULT 'manual_sample',
    imported_at TEXT NOT NULL,
    FOREIGN KEY (restaurant_id) REFERENCES restaurants(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_dedupe
    ON reviews (restaurant_id, review_text);
