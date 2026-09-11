from io import BytesIO
import json
from unittest.mock import patch

from PIL import Image
from django.contrib.auth.models import Group
from django.contrib.gis.geos import Point
from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tenders.models import Invoice, Order, OrderLine, Tender, Town, Transporter, Truck, TruckModel
from tenders.views import _online_user_ids
from .models import Address, CustomUser, Profile


def _make_image(size=2000, fmt='PNG'):
    buffer = BytesIO()
    Image.new('RGB', (size, size), (120, 60, 200)).save(buffer, format=fmt)
    buffer.seek(0)
    return SimpleUploadedFile('avatar.png', buffer.read(), content_type='image/png')


class ProfileViewsTest(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(email='alice@example.com', password='pass1234')
        self.client.login(email='alice@example.com', password='pass1234')

    def test_profile_page_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('users:profile'))
        self.assertEqual(response.status_code, 302)

    def test_profile_page_renders(self):
        response = self.client.get(reverse('users:profile'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'profile-app')

    def test_navbar_shows_avatar_placeholder_not_email(self):
        response = self.client.get(reverse('users:profile'))
        self.assertNotContains(response, 'alice@example.com')
        self.assertContains(response, 'nav-avatar')
        self.assertContains(response, 'A')

    def test_navbar_avatar_initials(self):
        self.assertEqual(self.user.avatar_initials(), 'A')
        self.user.email = 'leon.mangu@gmail.com'
        self.assertEqual(self.user.avatar_initials(), 'LM')
        self.assertEqual(self.user.avatar_picture(), '')

    def test_api_profile_returns_defaults(self):
        response = self.client.get(reverse('users:api_profile'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['profile']['email'], 'alice@example.com')
        self.assertEqual(data['addresses'], [])
        self.assertEqual(data['profile']['street'], '')
        self.assertEqual(data['profile']['city'], '')
        self.assertEqual(data['profile']['country'], '')
        self.assertEqual(data['profile']['theme'], 'system')
        self.assertEqual(data['profile']['notification_pref'], 'email')

    def test_profile_save_preferences(self):
        from users.models import Profile
        response = self.client.post(reverse('users:api_profile_save'), {
            'email': 'alice@example.com', 'theme': 'dark', 'notification_pref': 'hypax',
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['profile']['theme'], 'dark')
        self.assertEqual(data['profile']['notification_pref'], 'hypax')
        profile = Profile.objects.get(user=self.user)
        self.assertEqual(profile.theme, 'dark')
        self.assertEqual(profile.notification_pref, 'hypax')
        response = self.client.post(reverse('users:api_profile_save'), {
            'email': 'alice@example.com', 'theme': 'not-a-theme', 'notification_pref': 'spam',
        })
        self.assertEqual(response.json()['profile']['theme'], 'dark')
        self.assertEqual(response.json()['profile']['notification_pref'], 'hypax')

    def test_profile_page_sets_theme_attribute(self):
        from users.models import Profile
        Profile.objects.update_or_create(user=self.user, defaults={'theme': 'dark'})
        response = self.client.get(reverse('users:profile'))
        self.assertContains(response, 'data-user-theme="dark"')

    def test_profile_location_comes_from_physical_address(self):
        Address.objects.create(
            user=self.user, label='Office', street='1 Market St',
            city='Nairobi', postal_code='00100', country='Kenya', is_primary=True,
        )
        data = self.client.get(reverse('users:api_profile')).json()
        self.assertEqual(data['profile']['street'], '1 Market St')
        self.assertEqual(data['profile']['city'], 'Nairobi')
        self.assertEqual(data['profile']['country'], 'Kenya')

    def test_profile_save_keeps_address_location(self):
        Address.objects.create(
            user=self.user, label='Office', street='1 Market St',
            city='Nairobi', country='Kenya', is_primary=True,
        )
        response = self.client.post(reverse('users:api_profile_save'), {
            'email': 'alice@example.com', 'phone': '+1 555', 'bio': 'Updated',
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['profile']['street'], '1 Market St')
        self.assertEqual(data['profile']['city'], 'Nairobi')
        self.assertEqual(data['profile']['country'], 'Kenya')

    def test_profile_picture_is_resized(self):
        response = self.client.post(reverse('users:api_profile_save'), {
            'email': 'alice@example.com',
            'bio': 'Freight coordinator',
            'phone': '',
            'profile_picture': _make_image(),
        })
        self.assertEqual(response.status_code, 200)
        profile = Profile.objects.get(user=self.user)
        with profile.profile_picture.open('rb') as f:
            with Image.open(f) as img:
                self.assertLessEqual(img.width, 512)
                self.assertLessEqual(img.height, 512)
                self.assertEqual(img.format, 'JPEG')
        self.assertLess(profile.profile_picture.size, 200000)

    def test_profile_picture_can_be_removed(self):
        profile = Profile.objects.create(user=self.user)
        profile.profile_picture.save('avatar.jpg', SimpleUploadedFile('avatar.jpg', _make_image().read(), content_type='image/jpeg'))
        profile.refresh_from_db()
        self.assertTrue(profile.profile_picture)
        response = self.client.post(reverse('users:api_profile_save'), {
            'email': 'alice@example.com',
            'bio': 'Freight coordinator',
            'phone': '',
            'remove_picture': 'true',
        })
        self.assertEqual(response.status_code, 200)
        profile.refresh_from_db()
        self.assertFalse(profile.profile_picture)

    def test_api_profile_save_bio_and_email(self):
        response = self.client.post(reverse('users:api_profile_save'), {
            'email': 'alice@example.com',
            'phone': '+1 555 0100',
            'bio': 'Freight coordinator',
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        profile = Profile.objects.get(user=self.user)
        self.assertEqual(profile.bio, 'Freight coordinator')
        self.assertEqual(profile.phone, '+1 555 0100')

    def test_api_profile_save_first_and_last_name(self):
        response = self.client.post(reverse('users:api_profile_save'), {
            'email': 'alice@example.com',
            'phone': '',
            'bio': '',
            'first_name': 'Alice',
            'last_name': 'Mangu',
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['profile']['first_name'], 'Alice')
        self.assertEqual(data['profile']['last_name'], 'Mangu')
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Alice')
        self.assertEqual(self.user.last_name, 'Mangu')
        get_data = self.client.get(reverse('users:api_profile')).json()
        self.assertEqual(get_data['profile']['first_name'], 'Alice')
        self.assertEqual(get_data['profile']['last_name'], 'Mangu')

    def test_api_profile_save_email_change(self):
        response = self.client.post(reverse('users:api_profile_save'), {
            'email': 'alice.new@example.com',
            'phone': '',
            'bio': '',
        })
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'alice.new@example.com')

    def test_api_profile_email_taken(self):
        CustomUser.objects.create_user(email='bob@example.com', password='pass1234')
        response = self.client.post(reverse('users:api_profile_save'), {
            'email': 'bob@example.com',
            'phone': '',
            'bio': '',
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['ok'])

    def test_address_crud(self):
        create_url = reverse('users:api_address_create')
        response = self.client.post(create_url, {
            'label': 'Office', 'street': '1 Market St', 'city': 'Nairobi',
            'postal_code': '00100', 'country': 'Kenya', 'is_primary': True,
        })
        self.assertEqual(response.status_code, 200)
        addr = Address.objects.get(user=self.user)
        self.assertEqual(addr.label, 'Office')
        self.assertEqual(addr.city, 'Nairobi')
        self.assertTrue(addr.is_primary)

        update_url = reverse('users:api_address_update', args=[addr.pk])
        response = self.client.post(update_url, {
            'label': 'Head Office', 'street': '1 Market St', 'city': 'Nairobi',
            'postal_code': '00100', 'country': 'Kenya', 'is_primary': False,
        })
        self.assertEqual(response.status_code, 200)
        addr.refresh_from_db()
        self.assertEqual(addr.label, 'Head Office')
        self.assertFalse(addr.is_primary)

        profile = self.client.get(reverse('users:api_profile')).json()
        self.assertEqual(len(profile['addresses']), 1)

        delete_url = reverse('users:api_address_delete', args=[addr.pk])
        response = self.client.post(delete_url, {})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Address.objects.filter(pk=addr.pk).exists())

    def test_address_primary_exclusive(self):
        url = reverse('users:api_address_create')
        self.client.post(url, {'label': 'Home', 'street': 'A', 'is_primary': True})
        self.client.post(url, {'label': 'Office', 'street': 'B', 'is_primary': True})
        self.assertEqual(Address.objects.filter(user=self.user, is_primary=True).count(), 1)


class RolesGroupsTest(TestCase):
    def test_migration_creates_role_groups(self):
        self.assertEqual(
            set(Group.objects.filter(name__in=('Administrator', 'Agents', 'Users')).values_list('name', flat=True)),
            {'Administrator', 'Agents', 'Users'},
        )

    def test_user_save_syncs_role_group(self):
        user = CustomUser.objects.create_user(email='agent@example.com', password='pass1234', role=CustomUser.Role.AGENT)
        self.assertEqual(list(user.groups.values_list('name', flat=True)), ['Agents'])
        user.role = CustomUser.Role.USER
        user.save(update_fields=('role',))
        self.assertEqual(list(user.groups.values_list('name', flat=True)), ['Users'])


class AdminDashboardTest(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            email='admin@example.com', password='pass1234', role=CustomUser.Role.ADMINISTRATOR,
        )
        self.client.login(email='admin@example.com', password='pass1234')

    def test_non_admin_cannot_access_admin_panel(self):
        user = CustomUser.objects.create_user(email='user@example.com', password='pass1234')
        self.client.logout()
        self.client.login(email='user@example.com', password='pass1234')
        response = self.client.get(reverse('users:admin_dashboard'))
        self.assertRedirects(response, reverse('users:profile'))
        response = self.client.get(reverse('users:api_admin_dashboard'))
        self.assertEqual(response.status_code, 403)

    def test_admin_page_renders(self):
        response = self.client.get(reverse('users:admin_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'admin-app')

    def test_api_admin_dashboard_lists_transporters_and_agents(self):
        safari = Transporter.objects.create(company_id=9001, company_name='Safari Freight', alias='SF')
        Transporter.objects.create(company_id=9002, company_name='Arusha Movers', alias='AM')
        safari.agents.add(
            CustomUser.objects.create_user(
                email='agent2@example.com', password='pass1234',
                first_name='Grace', last_name='Njeri', role=CustomUser.Role.AGENT,
            )
        )
        response = self.client.get(reverse('users:api_admin_dashboard'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        names = sorted(t['company_name'] for t in data['transporters'])
        self.assertEqual(names, ['Arusha Movers', 'Safari Freight'])
        self.assertEqual(data['agents'][0]['email'], 'agent2@example.com')
        self.assertIn('online_count', data)
        self.assertIn('total_users', data)
        self.assertGreaterEqual(data['total_users'], 2)
        self.assertIn('online_users', data)
        self.assertIn('offline_users', data)
        online_emails = [u['email'] for u in data['online_users']]
        offline_emails = [u['email'] for u in data['offline_users']]
        self.assertIn('admin@example.com', online_emails)
        self.assertIn('agent2@example.com', offline_emails)
        self.assertEqual(len(online_emails) + len(offline_emails), data['total_users'])

    def test_admin_page_shows_users_presence(self):
        response = self.client.get(reverse('users:admin_dashboard'))
        self.assertContains(response, 'users-presence')
        self.assertContains(response, 'Online')
        self.assertContains(response, 'Offline')

    def test_admin_page_shows_users_summary_badge(self):
        response = self.client.get(reverse('users:admin_dashboard'))
        self.assertContains(response, 'users-summary')
        self.assertContains(response, reverse('tenders:admin_users'))

    def test_api_admin_agent_create_with_picture(self):
        response = self.client.post(reverse('users:api_admin_agent_create'), {
            'first_name': 'James',
            'last_name': 'Otieno',
            'email': 'james@example.com',
            'phone': '+254 700 000 000',
            'bio': 'Field agent',
            'profile_picture': _make_image(),
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        agent = CustomUser.objects.get(email='james@example.com')
        self.assertEqual(agent.role, CustomUser.Role.AGENT)
        self.assertEqual(agent.first_name, 'James')
        self.assertEqual(agent.last_name, 'Otieno')
        self.assertEqual(list(agent.groups.values_list('name', flat=True)), ['Agents'])
        profile = agent.profile
        self.assertEqual(profile.phone, '+254 700 000 000')
        self.assertTrue(profile.profile_picture)
        self.assertTrue(data['password'])

    def test_api_admin_agent_create_duplicate_email(self):
        CustomUser.objects.create_user(email='dup@example.com', password='pass1234')
        response = self.client.post(reverse('users:api_admin_agent_create'), {
            'first_name': 'Dup', 'email': 'dup@example.com',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['ok'])

    def test_api_admin_agent_create_with_password_sends_email(self):
        response = self.client.post(reverse('users:api_admin_agent_create'), {
            'first_name': 'Pam',
            'last_name': 'Njeri',
            'email': 'pam@example.com',
            'password': 'Secret123!',
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['password'], 'Secret123!')
        self.assertTrue(agent := CustomUser.objects.get(email='pam@example.com'))
        self.assertTrue(agent.check_password('Secret123!'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['pam@example.com'])
        self.assertIn('pam@example.com', mail.outbox[0].body)
        self.assertIn('Secret123!', mail.outbox[0].body)
        self.assertIn(('Agents',), list(agent.groups.values_list('name')))

    def test_api_admin_agent_create_rejects_short_password(self):
        response = self.client.post(reverse('users:api_admin_agent_create'), {
            'first_name': 'Pam', 'email': 'pam2@example.com', 'password': 'short',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['ok'])
        self.assertFalse(CustomUser.objects.filter(email='pam2@example.com').exists())

    def test_admin_links_agent_to_multiple_transporters(self):
        c1 = Transporter.objects.create(company_id=101, company_name='Trans A')
        c2 = Transporter.objects.create(company_id=102, company_name='Trans B')
        agent = CustomUser.objects.create_user(
            email='agent3@example.com', password='pass1234', role=CustomUser.Role.AGENT,
        )
        url = reverse('users:api_admin_agent_transporters', args=[agent.pk])
        response = self.client.post(url, {'transporter_ids': [c1.pk, c2.pk]}, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(list(agent.linked_transporters.order_by('pk').values_list('pk', flat=True)), [c1.pk, c2.pk])
        response = self.client.post(url, {'transporter_ids': [c2.pk]}, content_type='application/json')
        self.assertEqual(list(agent.linked_transporters.values_list('pk', flat=True)), [c2.pk])


class AgentPortalTest(TestCase):
    def setUp(self):
        self.osrm_patcher = patch('tenders.views._osrm_fetch', side_effect=OSError('no network'))
        self.addCleanup(self.osrm_patcher.stop)
        self.osrm_patcher.start()
        import tenders.views as tviews
        tviews._route_cache.clear()
        Town.objects.get_or_create(name='Nairobi', defaults={'country': 'Kenya', 'point': Point(36.8219, -1.2921, srid=4326)})
        Town.objects.get_or_create(name='Mombasa', defaults={'country': 'Kenya', 'point': Point(39.6682, -4.0435, srid=4326)})
        self.admin = CustomUser.objects.create_user(
            email='admin@example.com', password='pass1234', role=CustomUser.Role.ADMINISTRATOR,
        )
        self.owner = CustomUser.objects.create_user(email='owner@example.com', password='pass1234')
        self.trans_a = Transporter.objects.create(company_id=1, company_name='TransFleet A', alias='TA')
        self.trans_b = Transporter.objects.create(company_id=2, company_name='TransFleet B', alias='TB')
        self.agent_a = CustomUser.objects.create_user(
            email='agent.a@example.com', password='pass1234', role=CustomUser.Role.AGENT,
        )
        self.agent_b = CustomUser.objects.create_user(
            email='agent.b@example.com', password='pass1234', role=CustomUser.Role.AGENT,
        )
        self.trans_a.agents.add(self.agent_a)
        self.trans_b.agents.add(self.agent_b)

        def make_tender(name, customer):
            return Tender.objects.create(
                user=self.owner, route_loading='Nairobi', route_delivery='Mombasa',
                customer=customer, cargo_type=Tender.CargoType.DRY_VAN,
                truck_type=Tender.TruckType.TRUCK, weight=20.0, number_of_trucks=2,
                distance_km=480, cargo_date=timezone.localdate(), status=Tender.Status.SUCCESS,
            )

        self.tender_a = make_tender('CA', 'TransFleet A')
        self.tender_b = make_tender('CB', 'TransFleet B')

        def award_order(order_id, tender, company_name, company_id):
            order = Order.objects.create(
                order_id=order_id, order_name=f'ORD-{order_id}', customer=company_name,
                company_name=company_name, company_id=company_id, tender=tender,
            )
            OrderLine.objects.create(
                order=order, line_id=1, product_name='Sand', quantity=1, price_unit=100,
                commission=0, price_subtotal=100, price_total=100, awarded=True,
            )
            return order

        self.order_a = award_order(9100, self.tender_a, 'TransFleet A', 1)
        self.order_b = award_order(9101, self.tender_b, 'TransFleet B', 2)

    def _login(self, email):
        self.client.login(email=email, password='pass1234')

    def test_agent_sees_only_own_transporter_awarded_orders(self):
        partial = Order.objects.create(
            order_id=9102, order_name='ORD-9102', customer='TransFleet A',
            company_name='TransFleet A', company_id=1, tender=self.tender_a,
        )
        OrderLine.objects.create(
            order=partial, line_id=1, product_name='Sand', quantity=1, price_unit=100,
            commission=0, price_subtotal=100, price_total=100, awarded=True,
        )
        OrderLine.objects.create(
            order=partial, line_id=2, product_name='Gravel', quantity=1, price_unit=100,
            commission=0, price_subtotal=100, price_total=100, awarded=False,
        )
        self._login('agent.a@example.com')
        response = self.client.get(reverse('users:api_agent_awarded'))
        data = response.json()
        self.assertTrue(data['ok'])
        names = [o['company_name'] for o in data['orders']]
        self.assertEqual(names, ['TransFleet A', 'TransFleet A'])
        by_name = {o['order_name']: o for o in data['orders']}
        full = by_name['ORD-9100']
        self.assertEqual(full['awarded_amount'], '100.00')
        self.assertEqual(full['awarded_lines'], 1)
        self.assertEqual(full['lines_total'], 1)
        self.assertEqual(full['confirmation'], 'full')
        part = by_name['ORD-9102']
        self.assertEqual(part['awarded_lines'], 1)
        self.assertEqual(part['lines_total'], 2)
        self.assertEqual(part['confirmation'], 'partial')

    def test_agent_tracker_scoped_to_linked_transporters(self):
        self._login('agent.a@example.com')
        response = self.client.get(reverse('users:api_agent_tracker'))
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(len(data['groups']), 1)
        self.assertEqual([g['orders'][0]['company_name'] for g in data['groups']], ['TransFleet A'])

    def test_agent_invoices_scoped_and_mark_paid(self):
        self._login('agent.a@example.com')
        response = self.client.get(reverse('users:api_agent_invoices'))
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(len(data['invoices']), 1)
        self.assertIsNone(data['invoices'][0]['id'])
        self.assertEqual(data['invoices'][0]['number'], 'INV-9100')
        self.assertEqual(data['invoices'][0]['status'], 'pending')
        self.assertEqual(data['invoices'][0]['order_pk'], self.order_a.pk)
        self.assertFalse(Invoice.objects.filter(order=self.order_a).exists())

        paid_url = reverse('users:api_agent_invoice_paid', args=[self.order_a.pk])
        response = self.client.post(paid_url, {})
        self.assertTrue(response.json()['ok'])
        invoice = Invoice.objects.get(order=self.order_a)
        self.assertEqual(invoice.status, Invoice.Status.PAID)
        self.assertEqual(invoice.transporter, self.trans_a)

    def test_agent_cannot_mark_other_transporter_invoice_paid(self):
        self._login('agent.a@example.com')
        response = self.client.post(reverse('users:api_agent_invoice_paid', args=[self.order_b.pk]), {})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Invoice.objects.filter(order=self.order_b).exists())

    def test_agent_pages_render(self):
        self._login('agent.a@example.com')
        for name in ('agent_awarded', 'agent_tracker', 'agent_invoices'):
            response = self.client.get(reverse('users:' + name))
            self.assertEqual(response.status_code, 200, name)

    def test_agent_profile_api_lists_linked_transporter(self):
        self._login('agent.a@example.com')
        response = self.client.get(reverse('users:api_profile'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['linked_transporters']), 1)
        self.assertEqual(data['linked_transporters'][0]['company_name'], 'TransFleet A')
        self.assertEqual(data['linked_transporters'][0]['alias'], 'TA')

    def test_agent_profile_page_shows_linked_transporter(self):
        self._login('agent.a@example.com')
        response = self.client.get(reverse('users:profile'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Linked transporter')

    def test_non_agent_profile_has_no_transporter_section(self):
        self._login('owner@example.com')
        response = self.client.get(reverse('users:profile'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Linked transporter')
        data = self.client.get(reverse('users:api_profile')).json()
        self.assertEqual(data['linked_transporters'], [])


class AgentTransportersTest(TestCase):
    def setUp(self):
        self.owner = CustomUser.objects.create_user(email='owner.t@example.com', password='pass1234')
        self.agent = CustomUser.objects.create_user(
            email='agent.t@example.com', password='pass1234', role=CustomUser.Role.AGENT,
        )
        self.trans = Transporter.objects.create(company_id=77, company_name='TransFleet X', alias='TX')
        self.other_trans = Transporter.objects.create(company_id=78, company_name='TransFleet Y', alias='TY')
        self.trans.agents.add(self.agent)
        self.truck = Truck.objects.create(
            transporter=self.trans, model='Volvo FH16', license_plate='T 123 ABC',
            truck_type='truck', model_year=2021, tonnage_capacity=20,
        )

    def _login(self, email='agent.t@example.com'):
        self.client.login(email=email, password='pass1234')

    def _payload(self, **overrides):
        payload = {
            'model': 'Scania R450', 'license_plate': 'T 999 XYZ', 'tags': 'fuel',
            'chassis_number': 'CH-001', 'model_year': 2022, 'tonnage_capacity': 34.5,
            'number_of_axles': 4, 'volume_capacity': 12.75, 'truck_type': 'flatbed',
        }
        payload.update(overrides)
        return payload

    def test_agent_transporters_page_renders(self):
        self._login()
        response = self.client.get(reverse('users:agent_transporters'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '/accounts/api/agents/transporters/')

    def test_non_agent_page_redirects_to_profile(self):
        self._login('owner.t@example.com')
        response = self.client.get(reverse('users:agent_transporters'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('users:profile'), response.url)

    def test_api_lists_only_linked_transporters_with_trucks(self):
        self._login()
        response = self.client.get(reverse('users:api_agent_transporters'))
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual([t['company_name'] for t in data['transporters']], ['TransFleet X'])
        trucks = data['transporters'][0]['trucks']
        self.assertEqual(len(trucks), 1)
        self.assertEqual(trucks[0]['license_plate'], 'T 123 ABC')
        self.assertEqual(trucks[0]['truck_type_label'], 'Truck')

    def test_create_truck_for_linked_transporter(self):
        self._login()
        url = reverse('users:api_agent_transporter_trucks', args=[self.trans.pk])
        response = self.client.post(url, data=self._payload(), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['truck']['license_plate'], 'T 999 XYZ')
        self.assertEqual(data['truck']['tonnage_capacity'], 34.5)
        self.assertEqual(self.trans.trucks.count(), 2)

    def test_create_truck_denied_for_unlinked_transporter(self):
        self._login()
        url = reverse('users:api_agent_transporter_trucks', args=[self.other_trans.pk])
        response = self.client.post(url, data=self._payload(), content_type='application/json')
        self.assertEqual(response.status_code, 404)

    def test_create_truck_validation_errors(self):
        self._login()
        url = reverse('users:api_agent_transporter_trucks', args=[self.trans.pk])
        response = self.client.post(
            url,
            data=self._payload(model_year='abc', number_of_axles=-2, truck_type='hovercraft'),
            content_type='application/json',
        )
        data = response.json()
        self.assertFalse(data['ok'])
        self.assertIn('model_year', data['errors'])
        self.assertIn('number_of_axles', data['errors'])
        self.assertIn('truck_type', data['errors'])
        self.assertEqual(self.trans.trucks.count(), 1)

    def test_truck_models_search_and_create(self):
        self._login()
        TruckModel.objects.create(name='FH16', manufacturer='Volvo', vehicle_type='truck')

        response = self.client.get(reverse('users:api_agent_truck_models') + '?q=volvo')
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual([m['manufacturer'] for m in data['models']], ['Volvo'])

        create_url = reverse('users:api_agent_truck_models_create')
        response = self.client.post(create_url, data={
            'name': 'Actros', 'manufacturer': 'Mercedes', 'vehicle_type': 'trailer',
            'model_year': 2020, 'tonnage_capacity': 40, 'number_of_axles': 5,
            'fuel_type': 'diesel', 'transmission': 'automatic', 'drive_type': '6x4',
        }, content_type='application/json')
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['model']['name'], 'Actros')
        self.assertEqual(data['model']['fuel_type_label'], 'Diesel')
        self.assertEqual(TruckModel.objects.filter(manufacturer='Mercedes').count(), 1)

    def test_truck_model_create_validation(self):
        self._login()
        url = reverse('users:api_agent_truck_models_create')
        response = self.client.post(url, data={
            'name': '', 'fuel_type': 'nuclear', 'transmission': 'teleport', 'drive_type': '42x99',
        }, content_type='application/json')
        data = response.json()
        self.assertFalse(data['ok'])
        for key in ('name', 'fuel_type', 'transmission', 'drive_type'):
            self.assertIn(key, data['errors'])

    def test_create_truck_requires_fields_except_volume(self):
        self._login()
        url = reverse('users:api_agent_transporter_trucks', args=[self.trans.pk])
        response = self.client.post(url, data={}, content_type='application/json')
        data = response.json()
        self.assertFalse(data['ok'])
        for key in ('model', 'license_plate', 'tags', 'chassis_number',
                    'model_year', 'tonnage_capacity', 'number_of_axles', 'truck_type'):
            self.assertIn(key, data['errors'], key)
        self.assertNotIn('volume_capacity', data['errors'])
        self.assertEqual(self.trans.trucks.count(), 1)

    def test_create_truck_links_catalog_model(self):
        self._login()
        model = TruckModel.objects.create(
            name='FH16', manufacturer='Volvo', vehicle_type='truck', model_year=2021,
            tonnage_capacity=25, number_of_axles=3,
        )
        url = reverse('users:api_agent_transporter_trucks', args=[self.trans.pk])
        payload = self._payload(model_id=model.pk)
        payload['model'] = ''
        response = self.client.post(url, data=payload, content_type='application/json')
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['truck']['model_id'], model.pk)
        truck = Truck.objects.get(pk=data['truck']['id'])
        self.assertEqual(truck.truck_model, model)
        self.assertEqual(truck.model, 'FH16')

    def test_create_truck_rejects_invalid_model_id(self):
        self._login()
        url = reverse('users:api_agent_transporter_trucks', args=[self.trans.pk])
        response = self.client.post(
            url, data=self._payload(model_id=99999), content_type='application/json',
        )
        data = response.json()
        self.assertFalse(data['ok'])
        self.assertIn('model_id', data['errors'])
        self.assertEqual(self.trans.trucks.count(), 1)


class AgentTrucksTest(TestCase):
    def setUp(self):
        self.osrm_patcher = patch('tenders.views._osrm_fetch', side_effect=OSError('no network'))
        self.addCleanup(self.osrm_patcher.stop)
        self.osrm_patcher.start()
        import tenders.views as tviews
        tviews._route_cache.clear()
        Town.objects.get_or_create(name='Nairobi', defaults={'country': 'Kenya', 'point': Point(36.8219, -1.2921, srid=4326)})
        Town.objects.get_or_create(name='Mombasa', defaults={'country': 'Kenya', 'point': Point(39.6682, -4.0435, srid=4326)})
        self.owner = CustomUser.objects.create_user(email='owner.f@example.com', password='pass1234')
        self.agent = CustomUser.objects.create_user(
            email='agent.f@example.com', password='pass1234', role=CustomUser.Role.AGENT,
        )
        self.other_agent = CustomUser.objects.create_user(
            email='agent.other@example.com', password='pass1234', role=CustomUser.Role.AGENT,
        )
        self.trans = Transporter.objects.create(company_id=55, company_name='FastFleet', alias='FF')
        self.other_trans = Transporter.objects.create(company_id=56, company_name='SlowFleet', alias='SF')
        self.trans.agents.add(self.agent)
        self.other_trans.agents.add(self.other_agent)
        self.truck = Truck.objects.create(
            transporter=self.trans, model='Volvo FH16', license_plate='T 100 FF',
            truck_type='truck', model_year=2021, tonnage_capacity=22,
        )
        self.truck_flatbed = Truck.objects.create(
            transporter=self.trans, model='Scania R450', license_plate='T 200 FF',
            truck_type='flatbed', model_year=2020, tonnage_capacity=34,
        )
        self.other_agent_truck = Truck.objects.create(
            transporter=self.other_trans, model='MAN TGX', license_plate='T 100 SF',
            truck_type='truck',
        )
        self.tender = Tender.objects.create(
            user=self.owner, route_loading='Nairobi', route_delivery='Mombasa',
            customer='FastFleet', cargo_type=Tender.CargoType.DRY_VAN,
            truck_type=Tender.TruckType.TRUCK, weight=20.0, number_of_trucks=1,
            distance_km=480, cargo_date=timezone.localdate(), status=Tender.Status.SUCCESS,
        )
        self.awarded_order = Order.objects.create(
            order_id=4401, order_name='ORD-4401', customer='FastFleet',
            company_name='FastFleet', company_id=55, tender=self.tender,
        )
        OrderLine.objects.create(
            order=self.awarded_order, line_id=1, product_name='Sand', quantity=1,
            price_unit=100, commission=0, price_subtotal=100, price_total=100, awarded=True,
        )

    def _login(self, email='agent.f@example.com'):
        self.client.login(email=email, password='pass1234')

    def test_trucks_pages_render(self):
        self._login()
        self.assertEqual(self.client.get(reverse('users:agent_trucks')).status_code, 200)
        self.assertEqual(self.client.get(reverse('users:agent_truck_track', args=[self.truck.pk])).status_code, 200)

    def test_truck_track_page_404_for_foreign_truck(self):
        self._login()
        self.assertEqual(self.client.get(reverse('users:agent_truck_track', args=[self.other_agent_truck.pk])).status_code, 404)

    def test_truck_track_page_redirects_for_non_agent(self):
        self._login('owner.f@example.com')
        self.assertEqual(self.client.get(reverse('users:agent_trucks')).status_code, 302)

    def test_api_agent_trucks_lists_linked_with_tracking(self):
        self._login()
        response = self.client.get(reverse('users:api_agent_trucks'))
        data = response.json()
        self.assertTrue(data['ok'])
        plates = [t['license_plate'] for t in data['trucks']]
        self.assertEqual(plates, ['T 200 FF', 'T 100 FF'])
        active = [t['license_plate'] for t in data['trucks'] if t['tracking']['active']]
        self.assertEqual(active, ['T 100 FF'])
        truck = next(t for t in data['trucks'] if t['tracking']['active'])
        self.assertEqual(truck['tracking']['status'], 'En route')
        self.assertEqual(truck['tracking']['route_text'], 'Nairobi \u2192 Mombasa')
        self.assertEqual(truck['transporter'], 'FastFleet')
        idle = next(t for t in data['trucks'] if not t['tracking']['active'])
        self.assertEqual(idle['tracking']['status'], 'Idle')
        self.assertEqual(idle['license_plate'], 'T 200 FF')

    def test_api_agent_truck_track_position_along_route(self):
        self._login()
        response = self.client.get(reverse('users:api_agent_truck_track', args=[self.truck.pk]))
        data = response.json()
        self.assertTrue(data['ok'])
        tracking = data['tracking']
        self.assertTrue(tracking['active'])
        self.assertEqual(tracking['order_id'], 4401)
        self.assertEqual(tracking['origin']['name'], 'Nairobi')
        self.assertEqual(tracking['destination']['name'], 'Mombasa')
        self.assertGreaterEqual(tracking['progress'], 0.0)
        self.assertLessEqual(tracking['progress'], 1.0)
        self.assertIsInstance(tracking['lat'], float)
        self.assertIsInstance(tracking['lng'], float)
        self.assertEqual(data['truck']['transporter'], 'FastFleet')

    def test_api_agent_truck_track_idle_when_no_awarded_order(self):
        self._login()
        response = self.client.get(reverse('users:api_agent_truck_track', args=[self.truck_flatbed.pk]))
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertFalse(data['tracking']['active'])
        self.assertEqual(data['tracking']['status'], 'Idle')

    def test_api_agent_truck_track_denied_for_foreign_truck(self):
        self._login()
        response = self.client.get(reverse('users:api_agent_truck_track', args=[self.other_agent_truck.pk]))
        self.assertEqual(response.status_code, 404)

    def test_api_agent_truck_delete_linked_truck(self):
        self._login()
        response = self.client.post(reverse('users:api_agent_truck_delete', args=[self.truck.pk]), {})
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertFalse(Truck.objects.filter(pk=self.truck.pk).exists())

    def test_api_agent_truck_delete_denied_for_foreign_truck(self):
        self._login()
        response = self.client.post(reverse('users:api_agent_truck_delete', args=[self.other_agent_truck.pk]), {})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Truck.objects.filter(pk=self.other_agent_truck.pk).exists())

    def test_api_agent_truck_delete_requires_post(self):
        self._login()
        response = self.client.get(reverse('users:api_agent_truck_delete', args=[self.truck.pk]))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(Truck.objects.filter(pk=self.truck.pk).exists())

    def test_api_agent_trucks_scoped_to_own_transporters(self):
        self._login('agent.other@example.com')
        response = self.client.get(reverse('users:api_agent_trucks'))
        data = response.json()
        self.assertTrue(data['ok'])
        plates = [t['license_plate'] for t in data['trucks']]
        self.assertEqual(plates, ['T 100 SF'])
        self.assertEqual(data['trucks'][0]['tracking']['status'], 'Idle')


class LandingRedirectTest(TestCase):
    def _post(self, email, password='pass1234'):
        return self.client.post(
            reverse('users:api_login'),
            data='{"email": "%s", "password": "%s"}' % (email, password),
            content_type='application/json',
        ).json()

    def test_agent_login_redirects_to_awarded_orders(self):
        agent = CustomUser.objects.create_user(
            email='landag@example.com', password='pass1234', role=CustomUser.Role.AGENT,
        )
        data = self._post('landag@example.com')
        self.assertTrue(data['ok'])
        self.assertEqual(data['redirect'], reverse('users:agent_awarded'))

    def test_regular_user_login_redirects_to_dashboard(self):
        CustomUser.objects.create_user(email='landusr@example.com', password='pass1234')
        data = self._post('landusr@example.com')
        self.assertTrue(data['ok'])
        self.assertEqual(data['redirect'], reverse('tenders:dashboard'))

    def test_signup_redirects_to_dashboard(self):
        response = self.client.post(
            reverse('users:api_signup'),
            data=json.dumps({'email': 'landnew@example.com', 'password1': 'pass1234', 'password2': 'pass1234'}),
            content_type='application/json',
        )
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['redirect'], reverse('tenders:dashboard'))

    def test_signup_saves_names_phone_and_address(self):
        response = self.client.post(
            reverse('users:api_signup'),
            data=json.dumps({
                'email': 'sign.detail@example.com',
                'first_name': 'Alice',
                'last_name': 'Mangu',
                'city': 'Dar es Salaam',
                'country': 'Tanzania',
                'phone': '+255700000000',
                'street': 'Samora Ave 12',
                'password1': 'pass1234',
                'password2': 'pass1234',
            }),
            content_type='application/json',
        )
        self.assertTrue(response.json()['ok'])
        user = CustomUser.objects.get(email='sign.detail@example.com')
        self.assertEqual(user.first_name, 'Alice')
        self.assertEqual(user.last_name, 'Mangu')
        self.assertEqual(user.profile.phone, '+255700000000')
        address = user.addresses.first()
        self.assertEqual(address.city, 'Dar es Salaam')
        self.assertEqual(address.country, 'Tanzania')
        self.assertEqual(address.street, 'Samora Ave 12')
        self.assertTrue(address.is_primary)


class SocialAuthAndResetTest(TestCase):
    def test_google_login_redirects_when_not_configured(self):
        response = self.client.get(reverse('users:google_login'))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('users:login'))

    def test_google_callback_rejects_bad_state(self):
        response = self.client.get(reverse('users:google_callback'), {'state': 'bad', 'code': 'abc'})
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('users:login'))

    def test_password_reset_page_renders(self):
        response = self.client.get(reverse('users:password_reset'))
        self.assertEqual(response.status_code, 200)

    def test_password_reset_flow_updates_password(self):
        CustomUser.objects.create_user(email='reset.flow@example.com', password='oldpass1234')
        response = self.client.post(reverse('users:password_reset'), {'email': 'reset.flow@example.com'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        from urllib.parse import urlparse
        path = None
        for line in mail.outbox[0].body.splitlines():
            line = line.strip()
            if line.startswith('http://'):
                path = urlparse(line).path
                break
        self.assertIsNotNone(path)
        confirm = self.client.get(path)
        self.assertEqual(confirm.status_code, 302)
        self.assertIsNotNone(confirm.url)
        post = self.client.post(confirm.url, {
            'new_password1': 'newpass1234',
            'new_password2': 'newpass1234',
        })
        self.assertEqual(post.status_code, 302)
        login_resp = self.client.post(
            reverse('users:api_login'),
            data='{"email": "reset.flow@example.com", "password": "newpass1234"}',
            content_type='application/json',
        )
        self.assertTrue(login_resp.json()['ok'])


class LoginCsrfCookieTest(TestCase):
    def test_login_page_sets_csrftoken_cookie(self):
        response = self.client.get(reverse('users:login'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('csrftoken', response.cookies)

    def test_signup_page_sets_csrftoken_cookie(self):
        response = self.client.get(reverse('users:signup'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('csrftoken', response.cookies)

    def test_api_login_with_csrf_token(self):
        user = CustomUser.objects.create_user(email='csrf@example.com', password='pass1234')
        page = self.client.get(reverse('users:login'))
        token = page.cookies['csrftoken'].value
        response = self.client.post(
            reverse('users:api_login'),
            data='{"email": "csrf@example.com", "password": "pass1234"}',
            content_type='application/json',
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['redirect'], reverse('tenders:dashboard'))


class OnlinePresenceTest(TestCase):
    """Presence is driven by recent session activity, not unexpired sessions."""

    def setUp(self):
        self.user = CustomUser.objects.create_user(email='presence@example.com', password='pass1234')

    def _session(self, last_activity):
        session = SessionStore()
        session['_auth_user_id'] = str(self.user.pk)
        if last_activity is not None:
            session['last_activity'] = last_activity
        session.create()
        return session

    def test_recent_activity_is_online(self):
        self._session(timezone.now().timestamp())
        self.assertIn(self.user.pk, _online_user_ids())

    def test_idle_unexpired_session_is_offline(self):
        self._session(timezone.now().timestamp() - 3600)
        self.assertNotIn(self.user.pk, _online_user_ids())

    def test_session_without_activity_is_offline(self):
        self._session(None)
        self.assertNotIn(self.user.pk, _online_user_ids())

    def test_authenticated_request_records_activity(self):
        self.client.login(email='presence@example.com', password='pass1234')
        self.client.get(reverse('tenders:dashboard'))
        self.assertIn('last_activity', self.client.session)