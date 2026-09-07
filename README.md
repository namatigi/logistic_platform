# HYPAX Cargo Logistic and Transportation System

A Django web platform for managing cargo logistics:

- Email-based **user sign up / sign in**
- **Company registration** with full company details
- **Tender submission** to an external system via `POST {base_url}/api/v1/tenders`
- **Orders inbox** fed by a **webhook** from the external system, linked back to the originating tender

## Tech stack

- Python 3.14
- Django 6.0.8 (pinned: supports PostgreSQL 14; Django 6.1+ requires PG 15+)
- PostgreSQL
- `requests` for outgoing API calls

## Features

### Authentication
- Custom `User` model keyed on **email** (`USERNAME_FIELD = "email"`).
- Sign up, sign in, sign out pages under `/accounts/`.
- User registrations are scoped to their own companies and tenders.

### Companies
- Register / edit / delete companies (name, registration number, contact person, phone, email, address, city, country).
- Scoped per authenticated user.

### Tenders
- Submit cargo tenders with:
  `route_loading`, `route_delivery`, `customer`, `cargo_type`, `truck_type`, `weight`, `number_of_trucks`, `distance_km`, `cargo_date`.
- `route_loading` / `route_delivery` are selectable towns for **Tanzania, Zambia, Congo, Burundi, Rwanda, Kenya, South Sudan and Uganda** (`tenders/towns.py`).
- On submit the form `POST`s the JSON payload to `{base_url}/api/v1/tenders` and records the response (HTTP status, body, and the returned `data.id` / `data.name` / `data.status`).

#### Tender request payload
```json
{
  "route_loading": "Dar es Salaam",
  "route_delivery": "Mwanza",
  "customer": "HYPAX",
  "cargo_type": "container_20",
  "truck_type": "trailer",
  "weight": 25.0,
  "number_of_trucks": 2,
  "distance_km": 850.0,
  "cargo_date": "2026-09-10"
}
```

#### Tender API response (used to link webhook orders)
```json
{
  "status": "success",
  "message": "Tender created successfully.",
  "data": {
    "id": 18,
    "name": "CAR00014",
    "route_loading": "Dar es Salaam",
    "route_delivery": "Mwanza",
    "customer": "HYPAX",
    "cargo_type": "container_20",
    "truck_type": "trailer",
    "weight": 25.0,
    "number_of_trucks": 2,
    "distance_km": 850.0,
    "cargo_date": "2026-09-10",
    "status": "open"
  }
}
```
The returned `data.name` (`CAR00014`) is stored as the tender's **cargo reference** and used to match incoming webhook orders.

### Configuration &gt; API Settings
Per-user configuration for the outgoing tender endpoint:
- `base_url` — root URL; tenders are posted to `{base_url}/api/v1/tenders`
- `auth_type` — `none`, `bearer` (Bearer token) or `basic`
- `api_token` / `username` / `password`

The page also shows the **incoming webhook URL** (with a copy button) to share with the external system.

### Orders (webhook)
- The external system `POST`s order data to `/webhook/orders/` (CSRF-exempt, JSON).
- Orders are upserted by `order_id`, storing every detail and the order lines.
- If `cargo_reference` matches a tender reference, the order is linked to that tender and its owner.
- Orders appear on the **Orders** page (list + detail) with all details and how much it totals.

#### Webhook payload
```json
{
  "order_id": 42,
  "order_name": "S00042",
  "state": "draft",
  "company_id": 1,
  "company_name": "My Company",
  "date_order": "2026-09-03 18:00:00",
  "amount_total": 250.0,
  "customer": "HYPAX",
  "currency": "USD",
  "cargo_reference": "CAR00014",
  "cargo_id": 18,
  "order_lines": [
    {
      "line_id": 99,
      "product_id": 7,
      "product_name": "Freight Service",
      "quantity": 2.0,
      "price_unit": 100.0,
      "commission": 0.0,
      "price_subtotal": 200.0,
      "price_total": 200.0
    }
  ]
}
```

## Getting started

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create the PostgreSQL database and role
createdb django_project

# 4. Apply migrations
python manage.py migrate

# 5. Create a superuser (optional, for the admin)
python manage.py createsuperuser

# 6. Run the development server
python manage.py runserver
```

Open http://127.0.0.1:8000/ and sign up.

## Configuration

Database connection, in priority order:

1. If `DATABASE_URL` is set (e.g. `postgresql://user:pass@host:5432/db`), it is used as-is — this is how hosted environments such as **Railway's managed PostgreSQL** should be configured.
2. Otherwise, if `DB_HOST` is set, a TCP connection is used with `DB_NAME` and `DB_USER` (both required, `DB_PASSWORD`/`DB_PORT` optional).
3. Otherwise, a local PostgreSQL **unix socket** (`/var/run/postgresql/` or `/tmp/`) is used — intended only for local development.
4. If none of the above apply (no `DATABASE_URL`, no `DB_HOST`, and no local socket — e.g. a container with no database env vars), the app **fails at startup** with a clear `ImproperlyConfigured` message instead of crash-looping against a missing socket.

| Variable      | Used when            | Description                 |
|---------------|----------------------|-----------------------------|
| `DATABASE_URL`| Always               | Single connection string    |
| `DB_NAME`     | TCP (item 2)         | Database name               |
| `DB_USER`     | TCP (item 2)         | Database user               |
| `DB_PASSWORD` | TCP (item 2)         | Database password           |
| `DB_HOST`     | TCP (item 2)         | Database host               |
| `DB_PORT`     | TCP (item 2)         | Database port               |

On Railway, set `DATABASE_URL` to the connection string provided by the PostgreSQL plugin (or map the plugin's `POSTGRESQL_*`/`PG*` variables into a `DATABASE_URL`).

Emails (sign-up) use the console backend by default.

## Project structure

```
DjangoProject/
├── manage.py
├── DjangoProject/          # Project config (settings, urls, wsgi/asgi)
├── users/                  # CustomUser (email auth), sign up / sign in
├── companies/              # Company model + CRUD
├── tenders/                # Tender, ApiSetting, Order, OrderLine, webhook
├── templates/              # Shared templates (base, auth, apps)
└── static/                 # stylesheets
```

## Routes

| URL                    | Purpose                          |
|------------------------|----------------------------------|
| `/accounts/signup/`    | Sign up with email               |
| `/accounts/login/`     | Sign in                          |
| `/`                    | Dashboard                        |
| `/companies/`          | Company list / CRUD              |
| `/tenders/`            | Tender list                      |
| `/tenders/new/`        | New tender form                  |
| `/orders/`             | Orders received via webhook      |
| `/settings/`           | API settings (URL + auth + webhook URL) |
| `/webhook/orders/`     | Incoming order webhook (POST)    |
| `/admin/`              | Django admin                     |