from django.test import TestCase
from django.urls import reverse

from users.models import CustomUser


class EscrowAccountsTest(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            email='admin@example.com', password='pass1234', role=CustomUser.Role.ADMINISTRATOR,
        )
        self.user = CustomUser.objects.create_user(
            email='user@example.com', password='pass1234', role=CustomUser.Role.USER,
        )

    def test_payment_term_crud(self):
        self.client.login(email='user@example.com', password='pass1234')
        res = self.client.get(reverse('tenders:api_payment_terms'))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['payment_terms'], [])
        res = self.client.post(
            reverse('tenders:api_payment_term_create'),
            {'name': 'Net 30', 'description': 'Pay within 30 days', 'items': [{'text': '50% on confirmation'}, {'text': 'Balance on delivery'}]},
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        pid = res.json()['payment_term']['id']
        items = res.json()['payment_term']['items']
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['text'], '50% on confirmation')
        res = self.client.get(reverse('tenders:api_payment_terms'))
        self.assertEqual(len(res.json()['payment_terms']), 1)
        self.assertEqual(len(res.json()['payment_terms'][0]['items']), 2)
        res = self.client.post(reverse('tenders:api_payment_term_toggle', args=[pid]), {}, content_type='application/json')
        self.assertEqual(res.json()['payment_term']['is_active'], False)
        res = self.client.post(reverse('tenders:api_payment_term_add_item', args=[pid]), {'text': 'Net 30 days'}, content_type='application/json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(len(res.json()['payment_term']['items']), 3)
        item_pk = res.json()['item']['id']
        res = self.client.post(reverse('tenders:api_payment_term_item_delete', args=[pid, item_pk]), {}, content_type='application/json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()['payment_term']['items']), 2)
        res = self.client.post(reverse('tenders:api_payment_term_delete', args=[pid]), {}, content_type='application/json')
        self.assertEqual(res.status_code, 200)
        res = self.client.get(reverse('tenders:api_payment_terms'))
        self.assertEqual(res.json()['payment_terms'], [])

    def test_admin_users_endpoint(self):
        self.client.login(email='admin@example.com', password='pass1234')
        res = self.client.get(reverse('tenders:api_admin_users'))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        users = {u['email']: u for u in data['users']}
        self.assertEqual(data['total_count'], len(users))
        self.assertIn('user@example.com', users)
        self.assertEqual(users['admin@example.com']['role_label'], 'Administrator')
        self.assertTrue(users['admin@example.com']['is_online'])
        self.assertEqual(users['user@example.com']['role_label'], 'User')
        self.assertIn('first_name', users['user@example.com'])
        self.assertIn('last_name', users['user@example.com'])
        self.assertIn('phone', users['user@example.com'])

    def test_admin_users_requires_admin(self):
        res = self.client.get(reverse('tenders:admin_users'))
        self.assertEqual(res.status_code, 302)
        self.client.login(email='user@example.com', password='pass1234')
        res = self.client.get(reverse('tenders:api_admin_users'))
        self.assertEqual(res.status_code, 302)

    def test_admin_change_password(self):
        self.client.login(email='admin@example.com', password='pass1234')
        url = reverse('tenders:api_admin_user_password', args=[self.user.pk])
        res = self.client.post(url, {'password': 'Br4ndNew!Pass'}, content_type='application/json')
        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['user']['email'], 'user@example.com')
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Br4ndNew!Pass'))

    def test_admin_change_password_rejects_missing(self):
        self.client.login(email='admin@example.com', password='pass1234')
        url = reverse('tenders:api_admin_user_password', args=[self.user.pk])
        res = self.client.post(url, {}, content_type='application/json')
        self.assertEqual(res.status_code, 400)
        self.assertFalse(res.json()['ok'])

    def test_admin_change_password_unknown_user(self):
        self.client.login(email='admin@example.com', password='pass1234')
        res = self.client.post(reverse('tenders:api_admin_user_password', args=[99999]), {'password': 'x'})
        self.assertEqual(res.status_code, 404)

    def test_admin_change_password_requires_admin(self):
        self.client.login(email='user@example.com', password='pass1234')
        res = self.client.post(reverse('tenders:api_admin_user_password', args=[self.admin.pk]), {'password': 'x'})
        self.assertEqual(res.status_code, 302)

    def test_payment_term_not_owned(self):
        other = CustomUser.objects.create_user(email='other@example.com', password='pass1234')
        from tenders.models import PaymentTerm
        term = PaymentTerm.objects.create(user=other, name='Mine')
        self.client.login(email='user@example.com', password='pass1234')
        res = self.client.post(reverse('tenders:api_payment_term_delete', args=[term.pk]), {}, content_type='application/json')
        self.assertEqual(res.status_code, 404)
        res = self.client.post(reverse('tenders:api_payment_term_add_item', args=[term.pk]), {'text': 'x'}, content_type='application/json')
        self.assertEqual(res.status_code, 404)

    def test_escrow_admin_requires_admin(self):
        self.client.login(email='user@example.com', password='pass1234')
        res = self.client.get(reverse('tenders:api_admin_escrow'))
        self.assertEqual(res.status_code, 302)
        self.client.login(email='admin@example.com', password='pass1234')
        res = self.client.get(reverse('tenders:api_admin_escrow'))
        self.assertEqual(res.status_code, 200)
        self.assertIn('escrow_accounts', res.json())

    def test_build_payload_includes_payment_terms(self):
        from tenders.models import PaymentTerm, Tender
        from tenders.views import build_payload
        from django.utils import timezone
        term = PaymentTerm.objects.create(user=self.user, name='On confirmation', description='Pay via Selcom before loading.')
        term.items.create(text='50% advance on confirmation', sort_order=0)
        term.items.create(text='Balance on delivery', sort_order=1)
        tender = Tender.objects.create(
            user=self.user, route_loading='Dar es Salaam', route_delivery='Mombasa',
            customer='HYPAX', cargo_type='container_20', truck_type='trailer',
            weight=10, number_of_trucks=1, distance_km=100, cargo_date=timezone.localdate(),
            payment_terms=term,
        )
        payload = build_payload(tender)
        self.assertEqual(payload['payment_terms']['name'], 'On confirmation')
        self.assertEqual(payload['payment_terms']['description'], 'Pay via Selcom before loading.')
        self.assertEqual([i['text'] for i in payload['payment_terms']['items']], ['50% advance on confirmation', 'Balance on delivery'])

    def test_build_payload_payment_terms_nullable(self):
        from tenders.models import Tender
        from django.utils import timezone
        from tenders.views import build_payload
        tender = Tender.objects.create(
            user=self.user, route_loading='Dar es Salaam', route_delivery='Mombasa',
            customer='HYPAX', cargo_type='container_20', truck_type='trailer',
            weight=10, number_of_trucks=1, distance_km=100, cargo_date=timezone.localdate(),
        )
        payload = build_payload(tender)
        self.assertIsNone(payload['payment_terms'])

    def test_form_meta_includes_payment_terms(self):
        from tenders.models import PaymentTerm
        PaymentTerm.objects.create(user=self.user, name='On confirmation')
        self.client.login(email='user@example.com', password='pass1234')
        res = self.client.get(reverse('tenders:api_form_meta'))
        meta = res.json()['meta']
        self.assertEqual(len(meta['payment_terms']), 1)
        self.assertEqual(meta['payment_terms'][0]['name'], 'On confirmation')

    def test_escrow_created_with_invoice(self):
        from tenders.views import get_or_create_invoice
        from tenders.models import Order, Tender, EscrowAccount, OrderLine
        tender = Tender.objects.create(
            user=self.user, route_loading='Dar es Salaam', route_delivery='Mombasa',
            customer='HYPAX', cargo_type='container_20', truck_type='trailer',
            weight=10, number_of_trucks=1, distance_km=100, cargo_date='2026-09-01',
        )
        order = Order.objects.create(
            order_id=9001, order_name='ORD-9001', company_name='Transit Ltd',
            amount_total=1000, currency='TZS', cargo_reference='CAR9001', tender=tender,
        )
        OrderLine.objects.create(order=order, line_id=1, product_name='Freight', quantity=1, price_unit=1000, price_total=1000, awarded=True)
        invoice = get_or_create_invoice(order)
        escrow = EscrowAccount.objects.filter(tender=tender).first()
        self.assertIsNotNone(escrow)
        self.assertEqual(escrow.virtual_account, f'EA-{escrow.pk:05d}')
        self.assertTrue(escrow.invoices.filter(pk=invoice.pk).exists())
        self.assertEqual(escrow.user_id, self.user.pk)
        self.assertTrue(escrow.virtual_account.startswith('EA-'))

    def test_escrow_multiple_invoices(self):
        from tenders.views import get_or_create_invoice
        from tenders.models import Order, Tender, EscrowAccount, OrderLine, Invoice
        tender = Tender.objects.create(
            user=self.user, route_loading='Dar es Salaam', route_delivery='Mombasa',
            customer='HYPAX', cargo_type='container_20', truck_type='trailer',
            weight=10, number_of_trucks=1, distance_km=100, cargo_date='2026-09-01',
        )
        invoices = []
        for offset, amount, company in ((9001, 500, 'Transit Ltd'), (9002, 700, 'Haulmax Ltd')):
            order = Order.objects.create(
                order_id=offset, order_name=f'ORD-{offset}', company_name=company,
                amount_total=amount, currency='TZS', cargo_reference='CAR9001', tender=tender,
            )
            OrderLine.objects.create(order=order, line_id=offset, product_name='Freight', quantity=1, price_unit=amount, price_total=amount, awarded=True)
            invoices.append(get_or_create_invoice(order))
        escrow = EscrowAccount.objects.filter(tender=tender).first()
        self.assertIsNotNone(escrow)
        self.assertEqual(set(escrow.invoices.values_list('pk', flat=True)), {i.pk for i in invoices})
        self.assertEqual(set(escrow.transporters.values_list('company_name', flat=True)), {'Transit Ltd', 'Haulmax Ltd'})
        invoice_a, invoice_b = invoices
        invoice_a.deposited_amount = 300
        invoice_a.save(update_fields=('deposited_amount',))
        invoice_b.deposited_amount = 400
        invoice_b.save(update_fields=('deposited_amount',))
        from tenders.views import _refresh_escrow
        _refresh_escrow(escrow)
        escrow.refresh_from_db()
        self.assertEqual(escrow.deposited_amount, 700)

        self.client.login(email='admin@example.com', password='pass1234')
        res = self.client.get(reverse('tenders:api_admin_escrow'))
        self.assertEqual(res.json()['escrow_accounts'], [])
        self.assertEqual(escrow.status, 'open')

        invoice_a.status = Invoice.Status.PAID
        invoice_a.save(update_fields=('status',))
        _refresh_escrow(escrow)
        escrow.refresh_from_db()
        self.assertEqual(escrow.status, EscrowAccount.Status.PENDING)
        invoice_b.status = Invoice.Status.PAID
        invoice_b.save(update_fields=('status',))
        _refresh_escrow(escrow)
        escrow.refresh_from_db()
        self.assertEqual(escrow.status, EscrowAccount.Status.PAID)

        res = self.client.get(reverse('tenders:api_admin_escrow'))
        data = res.json()['escrow_accounts'][0]
        self.assertEqual(set(data['invoice_numbers']), {i.number for i in invoices})
        self.assertEqual(set(data['transporter_names']), {'Transit Ltd', 'Haulmax Ltd'})
        self.assertIn(',', data['transporter'])
        self.assertEqual(data['cargo_reference'], 'CAR9001')
        self.assertEqual(data['status'], 'paid')
        self.assertEqual(data['trucks'], ['Freight'])