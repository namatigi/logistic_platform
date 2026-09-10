# HYPAX Cargo Logistic and Transportation System

A Django web platform for managing cargo logistics:

- Email-based **user sign up / sign in** with three roles: **Administrator**, **Agent** and **User**
- **Company registration** with full company details
- **Tender submission** to an external system via `POST {base_url}/api/v1/tenders`, including **payment terms**
- **Orders inbox** fed by a **webhook** from the external system, linked back to the originating tender
- **Invoices paid online** through the Selcom payment gateway (mobile money &amp; bank transfer)
- **Escrow accounts** per tender, tracking what was deposited against what was invoiced
- **Administrator tools**: live user overview, escrow monitoring, and API/payment/file configuration pages

## Tech stack

- Python 3.14
- Django 6.0.8 (pinned: supports PostgreSQL 14; Django 6.1+ requires PG 15+)
- PostgreSQL
- Channels / WebSockets for live order updates
- `requests` for outgoing API calls
- Optional S3-compatible object storage for uploaded files (profile pictures)

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
  "cargo_date": "2026-09-10",
  "payment_terms": {
    "name": "On confirmation",
    "description": "Pay via Selcom before loading.",
    "items": [
      { "text": "50% advance on confirmation" },
      { "text": "Balance on delivery" }
    ]
  }
}
```

The optional `payment_terms` object repeats what the user picked from their **Pay Term** library on the tender form:
- `name` — the term label (e.g. "Net 30", "On confirmation")
- `description` — the full-term description
- `items` — the individual term details added one at a time on the Pay Term page

When no payment term is selected the field is sent as `null`.

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

### Payment terms (Pay Term)
Every user has a personal **Pay Term** library (`/payment-terms/`):
- Create a term with a name, optional description, and **multiple individual term details** added one after the other (e.g. "50% advance on confirmation", then "Balance on delivery").
- Toggle terms on/off, edit, delete, and add/remove individual details at any time.
- Selecting a Pay Term on the tender form includes it in the tender payload sent to the external system (see above).

### Escrow accounts
An escrow account is created automatically for each tender once its cargo is awarded (invoice issued):
- Virtual account number `EA-00001`, customer, transporter(s), amount, payment terms and status (**open** → **pending** → **paid**).
- A cargo reference can have **multiple invoices** — one per transporter/order. All invoices and all their transporters are listed on the escrow account.
- `deposited_amount` is summed from all invoices of the tender; confirming a Selcom payment marks the invoice paid and refreshes the escrow. The account is **paid** once every invoice is paid.
- Administrators monitor everything on the **Escrow** page (`/escrow/`).

### Administrator tools
- **Users** (`/users/`) — number of users **online now** (derived from active sessions) and the full account list with role, phone, last activity and status, auto-refreshing every 30 seconds.
- **Escrow** — see *Escrow accounts* above.
- **Configuration** — API settings, Selcom gateway, incoming/outgoing email, and file storage (server volume vs S3).

### Configuration &gt; API Settings
Per-user configuration for the outgoing tender endpoint:
- `base_url` — root URL; tenders are posted to `{base_url}/api/v1/tenders`
- `auth_type` — `none`, `bearer` (Bearer token) or `basic`
- `api_token` / `username` / `password`

The page also shows the **incoming webhook URL** (with a copy button) to share with the external system.

### Configuration &gt; Selcom payment gateway
The Setting page (administrator only) also configures **invoice payments** through [Selcom](https://selcom.net) APGW:

- `selcom_enabled` — master switch for online invoice payments
- `selcom_sandbox` — use the sandbox API (`https://apigwdev.selcommobile.com/v1`) instead of production
- `selcom_base_url` / `selcom_paylink_base` — override the APIGW / hosted-checkout base (normally left blank)
- `selcom_client_id` / `selcom_client_secret` — Selcom vendor credentials
- `selcom_sales_channel` — the vendor sales channel (e.g. `PURCHASE`) issued by Selcom
- `selcom_currency` — invoice currency, default `TZS`
- `selcom_payment_methods` — comma-separated wallets/banks allowed on checkout, e.g. `MPESA,TIGOPESA,AIRTELMONEY,HALOPESA,CRDB,NMB`
- `selcom_webhook_secret` — optional secret used to verify payment callbacks (HMAC-SHA256 / confirm-hash)

The page shows the **Selcom payment callback URL** (with a copy button) to register as the callback/webhook URL when creating Selcom checkout orders.

### Invoices &amp; Selcom checkout
- Invoices are created automatically for awarded orders and can be paid online from the **Invoices** page (and the agent invoices page).
- **Pay now** creates a Selcom checkout order (`POST /checkout/create-order`) and opens the hosted checkout where the customer pays by mobile money (M-Pesa, Tigo, Airtel, Halopesa) or bank transfer (CRDB, NMB, …).
- **Refresh / Check status** queries the payment status (`POST /checkout/get-order-status`); a successful payment marks the invoice as paid.
- Selcom calls `POST /webhook/selcom/` (CSRF-exempt) with the payment result; the callback is signature-verified when a webhook secret is configured, then the invoice is marked paid.

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
├── users/                  # CustomUser (email auth), roles, profiles, admin dashboard
├── companies/              # Company model + CRUD
├── tenders/                # Tender, Pay Term, Escrow, ApiSetting, Order, OrderLine, Selcom, webhook
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
| `/tracker/`            | Cargo tracker                    |
| `/invoices/`           | Invoices + pay online (Selcom)   |
| `/settings/`           | API settings (URL + auth + webhook URL) |
| `/payment-terms/`      | Pay Term library                 |
| `/escrow/`             | Escrow accounts (admin)          |
| `/users/`              | Online users overview (admin)    |
| `/configuration/*`     | Odoo / Selcom / Email / Files (admin) |
| `/webhook/orders/`     | Incoming order webhook (POST)    |
| `/webhook/selcom/`     | Selcom payment callback (POST)   |
| `/admin/`              | Django admin                     |