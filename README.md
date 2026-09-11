# HYPAX Cargo Logistic and Transportation System

A Django web platform for managing cargo logistics:

- Email-based **user sign up / sign in** with three roles: **Administrator**, **Agent** and **User**
- **Company registration** with full company details
- **Odoo integration** with one or more external Odoo instances/companies, each with its own base URL, auth and webhook URL
- **Tender submission** to the transporter's Odoo instance via `POST {base_url}/api/v1/tenders`, including **payment terms**
- **Orders inbox** fed by a **webhook** from the external Odoo instances, linked back to the originating tender
- **Award confirmations** (`order-confirmation` / `partial-order-confirmation`) and **invoice confirmations** (`order-invoice`) sent back to the Odoo instance the order came from
- **Configurable API endpoint paths** for every outgoing Odoo endpoint (defaults to `/api/v1/...`)
- **Invoices paid online** through the Selcom payment gateway (mobile money &amp; bank transfer)
- **Escrow accounts** per tender, tracking what was deposited against what was invoiced
- **Administrator tools**: live user overview, escrow monitoring, API diagnostics, and Odoo/payment/file configuration pages

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
- Register / edit / delete companies (name, registration number, TIN/VAT, contact person, phone, email, address, city, country).
- A registered company is a **customer company** — the company whose cargo we ship/transport. It carries no transporter link; the tender form picks the customer from these.
- Scoped per authenticated user.

### Tenders
- Submit cargo tenders with:
  `route_loading`, `route_delivery`, `customer`, `cargo_type`, `truck_type`, `weight`, `number_of_trucks`, `distance_km`, `cargo_date`.
- `route_loading` / `route_delivery` are selectable towns for **Tanzania, Zambia, Congo, Burundi, Rwanda, Kenya, South Sudan and Uganda** (`tenders/towns.py`).
- On submit the form `POST`s the JSON payload to **every configured transport company's Odoo base URL** (each active `OdooCompany` with a base URL) and records a per-company result (HTTP status, body, and the returned `data.id` / `data.name` / `data.status`).

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
  "tender_reference": "HX-2609-000042",
  "cargo_reference": "HX-2609-000042",
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

`tender_reference` is the **canonical tender number** — the unique `HX-YYMM-<id>` reference assigned when the tender is created. It is always present in the payload (required). `cargo_reference` carries the same value and is kept for backward compatibility with earlier integrations.

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
The returned `data.name` (e.g. `CAR00014`) is stored on the tender as its **cargo reference**. The **canonical** tender number is the `tender_reference` (HX reference) sent in the payload. Incoming webhook orders are matched by `cargo_reference` against the tender's HX reference, its cargo reference, or a recorded submission, so both values link orders back to the originating tender.

### Payment terms (Pay Term)
Every user has a personal **Pay Term** library (`/payment-terms/`):
- Create a term with a name, optional description, and **multiple individual term details** added one after the other (e.g. "50% advance on confirmation", then "Balance on delivery").
- Toggle terms on/off, edit, delete, and add/remove individual details at any time.
- Selecting a Pay Term on the tender form includes it in the tender payload sent to the external system (see above).

### Escrow accounts
An escrow account is created automatically for a tender **when its first invoice is confirmed** (i.e. at payment/checkout time — not when the order is awarded):
- Virtual account number `EA-00001`, customer, transporter(s), amount, payment terms and status (**open** → **pending** → **paid**).
- A cargo reference can have **multiple invoices** — one per transporter/order. All invoices and all their transporters are listed on the escrow account.
- `deposited_amount` is summed from all invoices of the tender; confirming a Selcom payment marks the invoice paid and refreshes the escrow. The account is **paid** once every invoice is paid.
- Administrators monitor everything on the **Escrow** page (`/escrow/`).

### Administrator tools
- **Users** (`/users/`) — number of users **online now** (derived from active sessions) and the full account list with role, phone, last activity and status, auto-refreshing every 30 seconds.
- **Escrow** — see *Escrow accounts* above.
- **Diagnostics** (`/diagnostics/`) — every **error response from the platform's API points** is recorded in the database and shown here: the **account** that triggered it, the **timestamp**, the API point, method, path, HTTP status and the **error message** (with expandable response details). Errors are captured automatically for any `/api/*` or `/webhook/*` response that is an HTTP error or returns `"ok": false`, plus explicit logging of **outgoing** Odoo failures (tender submission, order confirmations). Filter by API point, search by account email, paginated, auto-refreshing.
- **Pending tender submissions** — a **tender whose first submission failed because the Odoo instance was unreachable** (connection error or HTTP 5xx) is **queued automatically** and pushed through once the API is back. It is re-sent on the next **successful** tender submission, by the management command `manage.py flush_pending_pushes` (safe to run on a cron schedule), or via the **Retry now** button on the Diagnostics page. Queued items are shown there with the account, target and last error; submissions permanently rejected with a 4xx response are marked failed and stop retrying.
- **Configuration** — API settings, Selcom gateway, incoming/outgoing email, and file storage (server volume vs S3).

### Configuration &gt; Odoo companies
Multiple Odoo instances/companies can be registered on the **Odoo** configuration page. Each company has:
- `name` and auto-generated `slug` (customisable)
- `base_url`, `auth_type`, `api_token`, `username`, `password`
- `is_active` — inactive companies reject webhooks with HTTP 403

Each company uses the default four API paths (`/api/v1/tenders`, `/api/v1/order-confirmation`,
`/api/v1/partial-order-confirmation`, `/api/v1/order-invoice`), overridable per company in the Django admin.

Every company gets its own incoming order webhook URL: `…/webhook/orders/<slug>/`. Orders posted to it are attributed to that company, and the **order/invoice confirmations are sent back to that company** (its `base_url` + configured paths).

An **Odoo company is a transport company** — the recipient of tenders. Every tender is submitted to **all configured transport companies** (each with its own `base_url` + `tenders_path` and auth), and each company's result is recorded on the tender. Each Odoo company's webhook URL is shown with a copy button.

### Configuration &gt; Selcom payment gateway
The Selcom page (administrator only) configures **invoice payments** through [Selcom](https://selcom.net) APGW:

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
- Invoices are created **lazily when the user pays or checks out** an awarded order (from the **Invoices** page or the agent invoices page); they do not exist until then. The invoices list shows awarded orders with a pending payment state even before an invoice row exists.
- **Pay now** creates the invoice (and its escrow account) and a Selcom checkout order (`POST /checkout/create-order`), then opens the hosted checkout where the customer pays by mobile money (M-Pesa, Tigo, Airtel, Halopesa) or bank transfer (CRDB, NMB, …).
- **Refresh / Check status** queries the payment status (`POST /checkout/get-order-status`); a successful payment marks the invoice as paid.
- Selcom calls `POST /webhook/selcom/` (CSRF-exempt) with the payment result; the callback is signature-verified when a webhook secret is configured, then the invoice is marked paid.

### Orders (webhook)
Orders are pushed by the Odoo instances to the (CSRF-exempt, JSON) webhook URL:

| URL | Behaviour |
|-----|-----------|
| `POST /webhook/orders/<slug>/` | Per-instance URL. The order is attributed to the Odoo company with that `slug`. Unknown slug → **404**, inactive company → **403**. |

- Orders are upserted by `order_id`, storing every detail and the order lines.
- If `cargo_reference` matches a tender's HX reference, its cargo reference, or a recorded submission, the order is linked to that tender and its owner.
- Orders appear on the **Orders** page (list + detail) with all details and how much it totals.

The webhook responds with:
```json
{
  "status": "ok",
  "created": true,
  "order_id": 42,
  "cargo_reference": "HX-2609-000042",
  "linked_tender": "HX-2609-000042",
  "company_slug": "lake-trans"
}
```
`linked_tender` reports the **canonical tender number** of the matched tender (`HX-YYMM-<id>`, preferring the HX reference over the external cargo reference), or `null` when no tender matched.

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

### Outgoing order confirmations
When an order is awarded, HYPAX confirms it back to the **order's own Odoo company** (the transporter it was posted by), using that company's `base_url` + configured paths, with the same auth type (Bearer/basic). An order with no linked company cannot be confirmed until an administrator fixes its transporter.

- **Full confirmation** → `POST {base_url}/api/v1/order-confirmation`
  ```json
  {
    "order_id": 42,
    "message": "Confirmed",
    "cargo_name": "HX-2609-000042",
    "tender_reference": "HX-2609-000042"
  }
  ```
- **Partial confirmation** (only some order lines are awarded) → `POST {base_url}/api/v1/partial-order-confirmation`
  ```json
  {
    "order_id": 42,
    "message": "Confirmed",
    "cargo_name": "HX-2609-000042",
    "tender_reference": "HX-2609-000042",
    "order_lines": [
      { "line_id": 99 }
    ]
  }
  ```

`cargo_name` and `tender_reference` both carry the order's **canonical tender number** (`HX-YYMM-<id>`); `cargo_name` falls back to the order's `cargo_reference` when the tender has no HX number yet.

A confirmation is treated as successful when the HTTP status is `2xx`, or the response body contains `"status": "success"`. A successful confirmation stores the award response and marks the selected lines as awarded.

### Invoice confirmations
When an invoice is confirmed as paid, HYPAX notifies the Odoo instance the order came from → `POST {base_url}/api/v1/order-invoice` (same auth as above):

```json
{
  "order_id": 42,
  "cargo_name": "HX-2609-000042",
  "tender_reference": "HX-2609-000042",
  "customer_name": "HYPAX",
  "tax_id": "TIN-123",
  "country": "TZ"
}
```

`tax_id` / `country` come from the posting user's registered company. The response's `data.name` (if present, e.g. an external invoice number) is stored as the invoice number.

### API endpoint paths (configuration-backed)
All four outgoing paths default to `/api/v1/...` and can be overridden **per Odoo company** (in the Django admin):

| Purpose | Default path |
|---------|--------------|
| Tender submission | `/api/v1/tenders` |
| Order confirmation | `/api/v1/order-confirmation` |
| Partial order confirmation | `/api/v1/partial-order-confirmation` |
| Invoice confirmation | `/api/v1/order-invoice` |

`OdooCompany` stores these four path fields; leaving them blank uses the defaults.

### Platform API endpoints (internal JSON)
These endpoints power the platform's own UI and use the logged-in user's session:

| Endpoint | Returns |
|----------|---------|
| `GET /api/tenders/` | Paginated tender list for the logged-in user |
| `GET /api/orders/` | Order list grouped by **tender reference** (`groups[]`, each with a `grouper` = the canonical HX tender number when linked); every order carries `tender_ref` |
| `GET /api/orders/<pk>/` | Order detail, including `tender_ref` (canonical tender number) and its lines |
| `POST /api/orders/<pk>/award/` | Confirms an order (`order-confirmation` or `partial-order-confirmation`) — shipment includes `tender_reference` (see above) |
| `GET /api/invoices/` | Awarded orders and their paid invoices |
| `GET /api/tracker/` | Trucks grouped by **tender reference** as `{ "ok": true, "groups": [...], "now": "..." }` |
| `GET /api/agents/tracker/` | Agent-scoped version of `/api/tracker/`, restricted to the agent's linked transporters |

The tracker `groups[]` entries are keyed by the canonical tender reference and contain **all** of that tender's transporter orders and trucks together:

```json
{
  "key": "HX-2609-000042",
  "tender_ref": "HX-2609-000042",
  "customer": "HYPAX",
  "route": "Dar es Salaam \u2192 Mwanza",
  "orders": [
    {
      "id": 12,
      "order_id": 42,
      "order_name": "S00042",
      "company_name": "My Company",
      "cargo_reference": "HX-2609-000042",
      "state": "confirmed",
      "awarded_amount": "200.00",
      "awarded_lines_count": 1,
      "total_lines_count": 1,
      "fully_confirmed": true,
      "partially_confirmed": false
    }
  ],
  "origin":     { "name": "Dar es Salaam", "lat": -6.8, "lng": 39.2 },
  "destination": { "name": "Mwanza", "lat": -2.5, "lng": 32.9 },
  "distance_km": 850.0,
  "trucks": [
    { "label": "HX-2609-000042 \u00b7 T1", "lat": -6.2, "lng": 34.3,
      "status": "En route", "progress": 0.42 }
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
├── tenders/                # Tender, Pay Term, Escrow, ApiSetting, OdooCompany, Order, OrderLine, Selcom, webhook
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
| `/payment-terms/`      | Pay Term library                 |
| `/escrow/`             | Escrow accounts (admin)          |
| `/users/`              | Online users overview (admin)          |
| `/diagnostics/`        | API error diagnostics (admin)          |
| `/api/admin/diagnostics/` | Diagnostics data (admin, JSON)      |
| `/configuration/*`     | Odoo companies / Selcom / Email / Files (admin) |
| `/webhook/orders/<slug>/` | Per-instance order webhook (POST, CSRF-exempt) |
| `/webhook/selcom/`     | Selcom payment callback (POST)   |
| `/admin/`              | Django admin                     |