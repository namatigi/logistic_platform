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
- Every registered company is a **transporter**, and every transporter is its own **Odoo company/instance**. The company form links the company to the `OdooCompany` whose base URL its tenders are submitted to.
- Scoped per authenticated user.

### Tenders
- Submit cargo tenders with:
  `route_loading`, `route_delivery`, `customer`, `cargo_type`, `truck_type`, `weight`, `number_of_trucks`, `distance_km`, `cargo_date`.
- `route_loading` / `route_delivery` are selectable towns for **Tanzania, Zambia, Congo, Burundi, Rwanda, Kenya, South Sudan and Uganda** (`tenders/towns.py`).
- On submit the form `POST`s the JSON payload to the **Odoo base URL configured for the transporter** and records the response (HTTP status, body, and the returned `data.id` / `data.name` / `data.status`).

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

### Configuration &gt; API Settings
Global platform setting (shared by every user, administrator-only) for the **outgoing tender endpoint**:
- `base_url` — root URL; tenders are posted to `{base_url}{tenders_path}` (default `/api/v1/tenders`)
- `auth_type` — `none`, `bearer` (Bearer token) or `basic`
- `api_token` / `username` / `password`
- `tenders_path`, `order_confirmation_path`, `partial_order_confirmation_path`, `order_invoice_path` — override the four outgoing API paths (leave blank to use the defaults below)

The page also shows the **incoming webhook URL** (with a copy button) to share with the external system.

The same **Odoo** configuration page includes a **Shared settings** box (administrator only) for the shared API
`base_url` (preview: `&lt;base_url&gt;{{ tenders_path|default:'/api/v1/tenders' }}`), its auth, and the four outgoing
API paths. Tenders submitted by companies **not** linked to an Odoo company, and confirmations for orders received on
the shared (legacy) webhook, use these shared settings.

### Configuration &gt; Odoo companies
Multiple Odoo instances/companies can be registered on the **Odoo** configuration page. Each company has:
- `name` and auto-generated `slug` (customisable)
- `base_url`, `auth_type`, `api_token`, `username`, `password`
- its own configurable **four API endpoint paths** (default: `/api/v1/...`)
- `is_active` — inactive companies reject webhooks with HTTP 403

Every company gets its own incoming order webhook URL: `…/webhook/orders/<slug>/`. Orders posted to it are attributed to that company, and the **order/invoice confirmations are sent back to that company** (its `base_url` + configured paths).

An **Odoo company is a transporter**: a registered company (see *Companies*) linked to an Odoo company submits its **tenders to that instance's `base_url` + `tenders_path`** (with that instance's auth). Unlinked companies fall back to the shared API setting. Each company's webhook URL is shown with a copy button.

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
- Invoices are created **lazily when the user pays or checks out** an awarded order (from the **Invoices** page or the agent invoices page); they do not exist until then. The invoices list shows awarded orders with a pending payment state even before an invoice row exists.
- **Pay now** creates the invoice (and its escrow account) and a Selcom checkout order (`POST /checkout/create-order`), then opens the hosted checkout where the customer pays by mobile money (M-Pesa, Tigo, Airtel, Halopesa) or bank transfer (CRDB, NMB, …).
- **Refresh / Check status** queries the payment status (`POST /checkout/get-order-status`); a successful payment marks the invoice as paid.
- Selcom calls `POST /webhook/selcom/` (CSRF-exempt) with the payment result; the callback is signature-verified when a webhook secret is configured, then the invoice is marked paid.

### Orders (webhook)
Orders are pushed by the Odoo instances to one of two (CSRF-exempt, JSON) webhook URLs:

| URL | Behaviour |
|-----|-----------|
| `POST /webhook/orders/<slug>/` | Per-instance URL. The order is attributed to the Odoo company with that `slug`. Unknown slug → **404**, inactive company → **403**. |
| `POST /webhook/orders/` | Legacy shared URL. Orders are **not** attributed to any company; their confirmations fall back to the shared API setting. |

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

### Outgoing order confirmations
When an order is awarded, HYPAX confirms it back to the **order's own Odoo company** (shared settings when the order has no company), using that company's `base_url` + configured paths, with the same auth type (Bearer/basic).

- **Full confirmation** → `POST {base_url}/api/v1/order-confirmation`
  ```json
  {
    "order_id": 42,
    "message": "Confirmed",
    "cargo_name": "CAR00014"
  }
  ```
- **Partial confirmation** (only some order lines are awarded) → `POST {base_url}/api/v1/partial-order-confirmation`
  ```json
  {
    "order_id": 42,
    "message": "Confirmed",
    "cargo_name": "CAR00014",
    "order_lines": [
      { "line_id": 99 }
    ]
  }
  ```

A confirmation is treated as successful when the HTTP status is `2xx`, or the response body contains `"status": "success"`. A successful confirmation stores the award response and marks the selected lines as awarded.

### Invoice confirmations
When an invoice is confirmed as paid, HYPAX notifies the Odoo instance the order came from → `POST {base_url}/api/v1/order-invoice` (same auth as above):

```json
{
  "order_id": 42,
  "cargo_name": "CAR00014",
  "customer_name": "HYPAX",
  "tax_id": "TIN-123",
  "country": "TZ"
}
```

`tax_id` / `country` come from the posting user's registered company. The response's `data.name` (if present, e.g. an external invoice number) is stored as the invoice number.

### API endpoint paths (configuration-backed)
All four outgoing paths default to `/api/v1/...` and can be overridden **per Odoo company** or **globally in the shared settings**:

| Purpose | Default path |
|---------|--------------|
| Tender submission | `/api/v1/tenders` |
| Order confirmation | `/api/v1/order-confirmation` |
| Partial order confirmation | `/api/v1/partial-order-confirmation` |
| Invoice confirmation | `/api/v1/order-invoice` |

Both `ApiSetting` and `OdooCompany` store these four path fields; leaving them blank uses the defaults. The settings API (`/api/settings/`) also exposes the computed full endpoint URLs.

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
| `/settings/`           | Shared API settings (base URL, auth, endpoint paths, webhook URL) |
| `/payment-terms/`      | Pay Term library                 |
| `/escrow/`             | Escrow accounts (admin)          |
| `/users/`              | Online users overview (admin)          |
| `/diagnostics/`        | API error diagnostics (admin)          |
| `/api/admin/diagnostics/` | Diagnostics data (admin, JSON)      |
| `/configuration/*`     | Odoo companies / Selcom / Email / Files (admin) |
| `/webhook/orders/<slug>/` | Per-instance order webhook (POST, CSRF-exempt) |
| `/webhook/orders/`     | Shared order webhook (POST, CSRF-exempt) |
| `/webhook/selcom/`     | Selcom payment callback (POST)   |
| `/admin/`              | Django admin                     |